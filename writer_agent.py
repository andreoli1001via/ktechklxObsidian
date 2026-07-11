#!/usr/bin/env python3
"""
writer_agent.py
----------------
Agente "writer": le as notas .md geradas pelo scraper_agent.py, usa a API da
Anthropic (Claude) para extrair tags e entidades de cada nota, e entao:

  1) Cria "notas de entidade" (hubs) em <vault>/entities/<entidade>.md para
     cada entidade citada em 2+ notas.
  2) Insere em cada nota original uma secao "## Entidades relacionadas" e
     "## Notas relacionadas" com links no formato [[wikilink]] do Obsidian,
     alem de tags (#tag) e tags no frontmatter.

O resultado e um vault onde o Graph View do Obsidian mostra clusters ricos:
notas conectadas entre si e a "hub nodes" de entidades, sem precisar de um
banco vetorial / RAG de verdade — a "organizacao do conhecimento" e literal,
em arquivos .md com links explicitos.

Uso:
    python writer_agent.py                  # processa notas novas
    python writer_agent.py --force          # reprocessa tudo
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict

import frontmatter
from dotenv import load_dotenv

load_dotenv()

import anthropic  # noqa: E402  (precisa vir depois do load_dotenv)

MARKER = "<!-- graphrag:auto-links -->"

EXTRACTION_PROMPT = """Você é um analista que organiza uma base de conhecimento no Obsidian.
Leia a nota abaixo e responda APENAS com um JSON válido (sem markdown, sem comentários, sem texto extra) no formato:
{{
  "tags": ["tag-1", "tag-2"],
  "entities": ["Entidade 1", "Entidade 2"],
  "summary": "resumo de 1 a 2 frases"
}}

Regras:
- "tags": 3 a 6 tags curtas, em kebab-case, sem acentos, representando os TEMAS da nota (ex: inteligencia-artificial, marketing-digital).
- "entities": 3 a 10 substantivos próprios ou conceitos centrais e específicos citados na nota (pessoas, empresas, produtos, tecnologias, lugares, normas). Use a grafia original (com acentos se houver). Evite termos genéricos.
- "summary": resumo objetivo do conteúdo da nota.

NOTA (titulo: "{title}"):
\"\"\"
{body}
\"\"\"
"""


def slugify(text: str, max_len: int = 60) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9\-\s]", "", text)
    text = re.sub(r"\s+", "-", text).strip("-")
    return (text or "nota")[:max_len]


def find_markdown_files(vault_dir: str, skip_dirs=("entities",)):
    files = []
    for root, dirs, filenames in os.walk(vault_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".md"):
                files.append(os.path.join(root, fn))
    return files


def note_id(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def extract_tags_entities(client, model, title, body, max_chars=6000):
    text = body[:max_chars]
    prompt = EXTRACTION_PROMPT.format(title=title, body=text)
    resp = client.messages.create(
        model=model,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(match.group(0)) if match else {}
    data.setdefault("tags", [])
    data.setdefault("entities", [])
    data.setdefault("summary", "")
    return data


def write_entity_note(entity_dir, entity_name, nids, notes):
    slug = slugify(entity_name)
    path = os.path.join(entity_dir, f"{slug}.md")
    lines = [
        "---",
        f'title: "{entity_name}"',
        "tags: [entity]",
        "---",
        "",
        f"# {entity_name}",
        "",
        "Mencionada nas seguintes notas:",
        "",
    ]
    for nid in sorted(nids):
        title = notes[nid]["post"].get("title", nid)
        lines.append(f"- [[{nid}|{title}]]")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return slug


def update_note_file(info, related_counts, shared_entities, max_related, notes):
    post = info["post"]
    path = info["path"]
    analysis = info["analysis"]

    existing_tags = set(post.get("tags", []) or [])
    new_tags = set(analysis["tags"])
    post["tags"] = sorted(existing_tags.union(new_tags))
    post["entities"] = analysis["entities"]
    if analysis.get("summary"):
        post["summary"] = analysis["summary"]
    post["processed"] = True

    body = post.content
    if MARKER in body:
        body = body.split(MARKER)[0].rstrip()

    sections = [body.rstrip(), "", MARKER]

    if new_tags:
        inline_tags = " ".join(f"#{t}" for t in sorted(new_tags))
        sections += ["", "## Tags", inline_tags]

    entity_links = []
    for ent in analysis["entities"]:
        if ent in shared_entities:
            slug = slugify(ent)
            entity_links.append(f"- [[{slug}|{ent}]]")
    if entity_links:
        sections += ["", "## Entidades relacionadas", *entity_links]

    top_related = sorted(related_counts.items(), key=lambda kv: kv[1], reverse=True)[:max_related]
    if top_related:
        sections += ["", "## Notas relacionadas"]
        for other_nid, count in top_related:
            other_title = notes[other_nid]["post"].get("title", other_nid)
            sections.append(f"- [[{other_nid}|{other_title}]] (entidades em comum: {count})")

    post.content = "\n".join(sections).rstrip() + "\n"

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(frontmatter.dumps(post))


def process_vault(vault_dir, model, force, max_related, create_entity_notes):
    client = anthropic.Anthropic()
    paths = find_markdown_files(vault_dir)
    if not paths:
        print(f"[writer] nenhuma nota .md encontrada em {vault_dir} (rode o scraper_agent.py primeiro).")
        return

    notes = {}
    for path in paths:
        post = frontmatter.load(path)
        nid = note_id(path)
        already = bool(post.get("processed", False))
        if already and not force:
            print(f"[writer] reaproveitando analise existente: {nid}")
            notes[nid] = {
                "post": post,
                "path": path,
                "analysis": {
                    "tags": list(post.get("tags", []) or []),
                    "entities": list(post.get("entities", []) or []),
                    "summary": post.get("summary", ""),
                },
            }
            continue

        title = post.get("title", nid)
        print(f"[writer] analisando com Claude: {nid}")
        analysis = extract_tags_entities(client, model, title, post.content)
        notes[nid] = {"post": post, "path": path, "analysis": analysis}

    entity_map = defaultdict(set)
    for nid, info in notes.items():
        for ent in info["analysis"]["entities"]:
            ent = ent.strip()
            if ent:
                entity_map[ent].add(nid)

    shared_entities = {ent: nids for ent, nids in entity_map.items() if len(nids) >= 2}

    related = defaultdict(lambda: defaultdict(int))
    for ent, nids in shared_entities.items():
        for a in nids:
            for b in nids:
                if a != b:
                    related[a][b] += 1

    if create_entity_notes and shared_entities:
        entity_dir = os.path.join(vault_dir, "entities")
        os.makedirs(entity_dir, exist_ok=True)
        for ent, nids in shared_entities.items():
            write_entity_note(entity_dir, ent, nids, notes)
        print(f"[writer] {len(shared_entities)} nota(s) de entidade criadas/atualizadas em {entity_dir}")

    for nid, info in notes.items():
        update_note_file(info, related.get(nid, {}), shared_entities, max_related, notes)

    print(f"[writer] concluido. {len(notes)} nota(s) no vault, {len(shared_entities)} entidade(s) compartilhada(s) entre notas.")


def main():
    parser = argparse.ArgumentParser(
        description="Writer agent: gera tags e wikilinks entre notas do vault para enriquecer o Graph View do Obsidian."
    )
    parser.add_argument("--vault", default=os.environ.get("VAULT_PATH", "obsidian-vault"),
                         help="Pasta do vault do Obsidian (default: obsidian-vault)")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"))
    parser.add_argument("--force", action="store_true",
                         help="Reprocessar (chamar Claude de novo) mesmo em notas ja marcadas como processed:true")
    parser.add_argument("--max-related", type=int, default=8,
                         help="Numero maximo de notas relacionadas listadas por nota (default: 8)")
    parser.add_argument("--no-entity-notes", action="store_true",
                         help="Nao criar notas de entidade (hubs) em vault/entities/")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[writer] ERRO: defina ANTHROPIC_API_KEY (copie .env.example para .env e preencha).", file=sys.stderr)
        sys.exit(1)

    process_vault(args.vault, args.model, args.force, args.max_related, not args.no_entity_notes)


if __name__ == "__main__":
    main()
