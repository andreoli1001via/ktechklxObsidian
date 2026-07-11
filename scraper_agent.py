#!/usr/bin/env python3
"""
scraper_agent.py
----------------
Agente "scraper": recebe uma URL inicial, faz crawling (com profundidade
configuravel) seguindo links internos do mesmo dominio, extrai o conteudo
principal de cada pagina e grava um arquivo .md (com frontmatter) por pagina
dentro do vault do Obsidian.

Uso:
    python scraper_agent.py https://exemplo.com --depth 2 --max-pages 30

Cada nota gerada fica em <vault>/scraped/<slug>.md com frontmatter:

    ---
    title: "Titulo da pagina"
    source: "https://exemplo.com/pagina"
    domain: "exemplo.com"
    scraped_at: "2026-06-30T12:00:00"
    depth: 1
    tags: [scraped]
    processed: false
    ---

O campo `processed: false` é usado pelo writer_agent.py para saber quais
notas ainda precisam de tags/links gerados.
"""
import argparse
import hashlib
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_markdown

USER_AGENT = "GraphRagDemoBot/1.0 (+scraper_agent.py; uso educacional/demo)"
REMOVE_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg"]
MAIN_CONTENT_SELECTORS = ["main", "article", "[role=main]", "#content", ".content", ".post", ".article"]


def slugify(text: str, max_len: int = 60) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9\-\s]", "", text)
    text = re.sub(r"\s+", "-", text).strip("-")
    if not text:
        text = "pagina"
    return text[:max_len]


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def normalize_url(url: str) -> str:
    url, _frag = urldefrag(url)
    return url


def same_domain(url: str, domain: str) -> bool:
    return urlparse(url).netloc.lower() == domain.lower()


def extract_main_html(soup: BeautifulSoup) -> BeautifulSoup:
    for tag_name in REMOVE_TAGS:
        for el in soup.select(tag_name):
            el.decompose()

    for selector in MAIN_CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if node and len(node.get_text(strip=True)) > 200:
            return node

    return soup.body or soup


def extract_title(soup: BeautifulSoup, fallback_url: str) -> str:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return urlparse(fallback_url).path.strip("/") or fallback_url


def clean_markdown(md: str) -> str:
    md = re.sub(r"\n{3,}", "\n\n", md)
    lines = [line.rstrip() for line in md.split("\n")]
    return "\n".join(lines).strip() + "\n"


def build_frontmatter(meta: dict) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, list):
            items = ", ".join(value)
            lines.append(f"{key}: [{items}]")
        elif isinstance(value, bool):
            lines.append(f"{key}: {str(value).lower()}")
        else:
            safe = str(value).replace('"', "'")
            lines.append(f'{key}: "{safe}"')
    lines.append("---\n")
    return "\n".join(lines)


def fetch(url: str, timeout: int = 15):
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if "html" not in content_type:
        return None
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def extract_links(soup: BeautifulSoup, base_url: str) -> list:
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = normalize_url(urljoin(base_url, href))
        if absolute.startswith(("http://", "https://")):
            links.append(absolute)
    return links


def url_path_slug(url: str, max_len: int = 35) -> str:
    """Slug derivado do caminho da URL (ex: /blog/post-1 -> blog-post-1), para
    deixar o nome do arquivo rastreavel até a página de origem mesmo quando
    varias páginas têm títulos parecidos."""
    path = urlparse(url).path.strip("/")
    if not path:
        return "home"
    return slugify(path.replace("/", "-"), max_len=max_len)


def save_page(out_dir: str, url: str, title: str, markdown_body: str, depth: int) -> str:
    os.makedirs(out_dir, exist_ok=True)
    domain = urlparse(url).netloc
    title_slug = slugify(title, max_len=45)
    path_slug = url_path_slug(url)
    slug = f"{title_slug}--{path_slug}-{short_hash(url)}"
    filepath = os.path.join(out_dir, f"{slug}.md")

    meta = {
        "title": title,
        "source": url,
        "domain": domain,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "depth": depth,
        "tags": ["scraped"],
        "processed": False,
    }

    content = build_frontmatter(meta) + f"\n# {title}\n\n" + markdown_body
    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write(content)
    return filepath


def crawl(start_url: str, depth: int, max_pages: int, out_dir: str, delay: float, same_domain_only: bool):
    start_url = normalize_url(start_url)
    start_domain = urlparse(start_url).netloc

    visited = set()
    queued = {start_url}
    queue = deque([(start_url, 0)])
    saved = 0
    pages_seen = 0

    while queue and saved < max_pages:
        url, current_depth = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        pages_seen += 1

        print(f"[scraper] ({current_depth}/{depth}) baixando: {url}")
        try:
            html = fetch(url)
        except Exception as exc:
            print(f"[scraper]   falhou: {exc}")
            continue

        if html is None:
            print("[scraper]   ignorado (nao e HTML)")
            continue

        # Importante: extrai os links ANTES de remover nav/header/footer/aside
        # do soup (extract_main_html() muta a arvore com decompose()). Se a
        # extracao de links rodasse depois, o crawler perderia justamente os
        # links de menu/rodape/sidebar - as principais "ramificacoes" do site.
        soup = BeautifulSoup(html, "html.parser")
        title = extract_title(soup, url)
        page_links = extract_links(soup, url)

        main_node = extract_main_html(soup)
        raw_md = html_to_markdown(str(main_node), heading_style="ATX")
        markdown_body = clean_markdown(raw_md)

        if len(markdown_body) < 80:
            print("[scraper]   ignorado (conteudo muito curto)")
        else:
            path = save_page(out_dir, url, title, markdown_body, current_depth)
            saved += 1
            print(f"[scraper]   salvo -> {path}  ({saved}/{max_pages})")

        new_links = 0
        if current_depth < depth:
            for link in page_links:
                if link in visited or link in queued:
                    continue
                if same_domain_only and not same_domain(link, start_domain):
                    continue
                queued.add(link)
                queue.append((link, current_depth + 1))
                new_links += 1
        print(f"[scraper]   {len(page_links)} link(s) na pagina, {new_links} novo(s) adicionado(s) a fila (fila atual: {len(queue)})")

        time.sleep(delay)

    print(f"[scraper] concluido. {saved} pagina(s) salvas / {pages_seen} pagina(s) visitadas em {out_dir}")
    if queue:
        print(f"[scraper] aviso: ainda havia {len(queue)} link(s) na fila quando --max-pages foi atingido. "
              f"Aumente --max-pages e/ou --depth para cobrir mais ramificacoes do site.")


def main():
    parser = argparse.ArgumentParser(description="Scraper agent: extrai paginas de um site e grava .md no vault do Obsidian.")
    parser.add_argument("url", help="URL inicial para o crawling")
    parser.add_argument("--depth", type=int, default=1, help="Profundidade de links internos a seguir (default: 1)")
    parser.add_argument("--max-pages", type=int, default=20, help="Numero maximo de paginas a salvar (default: 20)")
    parser.add_argument("--out", default=os.environ.get("VAULT_PATH", "obsidian-vault") + "/scraped",
                         help="Pasta de saida dentro do vault (default: obsidian-vault/scraped)")
    parser.add_argument("--delay", type=float, default=0.5, help="Pausa entre requisicoes em segundos (default: 0.5)")
    parser.add_argument("--include-external", action="store_true",
                         help="Tambem seguir links para outros dominios (default: somente mesmo dominio)")
    args = parser.parse_args()

    crawl(
        start_url=args.url,
        depth=args.depth,
        max_pages=args.max_pages,
        out_dir=args.out,
        delay=args.delay,
        same_domain_only=not args.include_external,
    )


if __name__ == "__main__":
    main()
