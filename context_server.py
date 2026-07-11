#!/usr/bin/env python3
"""
context_server.py
------------------
Servidor compatível com a API da OpenAI (/v1/models e /v1/chat/completions)
para ser usado como "Connection" no Open WebUI.

A cada pergunta, este servidor:
  1) Le TODOS os arquivos .md do vault do Obsidian (sem cache, sem busca
     vetorial / embeddings — e por isso NAO e um RAG de verdade).
  2) Monta um unico bloco de CONTEXTO com o conteudo de todas as notas.
  3) Envia esse contexto + a pergunta para o Claude, com instrucao explicita
     de responder SOMENTE com base no contexto (sem internet, sem
     conhecimento geral).

Isso prova, na demo, que a resposta do LLM depende 100% do conteudo atual
dos arquivos .md: edite uma nota, faça a mesma pergunta de novo (sem
reiniciar nada) e a resposta muda.

Uso:
    python context_server.py
    # depois, no Open WebUI: Settings > Connections > OpenAI API
    #   Base URL: http://localhost:8001/v1   (ou http://host.docker.internal:8001/v1 no Docker)
    #   API key: qualquer valor (ex: "demo") - nao e validado
"""
import json
import os
import time
import uuid
from glob import glob

import frontmatter
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

load_dotenv()

import anthropic  # noqa: E402  (precisa vir depois do load_dotenv)

VAULT_PATH = os.environ.get("VAULT_PATH", "obsidian-vault")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MODEL_ID = "graph-rag-vault"
MAX_CONTEXT_CHARS = int(os.environ.get("MAX_CONTEXT_CHARS", "150000"))

app = FastAPI(title="Graph RAG Demo - Context Server")

_client = None


def get_client():
    """Cria o cliente Anthropic na primeira chamada (nao no import), para o
    servidor subir mesmo se a chave/rede so ficarem disponiveis depois."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client

SYSTEM_PROMPT_TEMPLATE = """Você é um assistente de perguntas e respostas que usa EXCLUSIVAMENTE o CONTEXTO abaixo, extraído das notas de um vault do Obsidian.

Regras obrigatórias:
- Responda somente com base no CONTEXTO fornecido. Não utilize conhecimento geral, não busque na internet e não invente informações que não estejam no contexto.
- Se a resposta não estiver no contexto, diga claramente que a informação não foi encontrada nas notas do vault. Não tente adivinhar.
- Quando possível, cite o título da nota de onde veio a informação.
- Responda no mesmo idioma da pergunta do usuário.

=== CONTEXTO (notas do Obsidian) ===
{context}
=== FIM DO CONTEXTO ===
"""


def load_vault_context(vault_path: str, max_chars: int):
    paths = sorted(glob(os.path.join(vault_path, "**", "*.md"), recursive=True))
    parts = []
    for path in paths:
        try:
            post = frontmatter.load(path)
        except Exception:
            continue
        title = post.get("title", os.path.basename(path))
        tags = post.get("tags", []) or []
        source = post.get("source", "")
        header = f"## Nota: {title}"
        meta_bits = []
        if tags:
            meta_bits.append(f"tags: {', '.join(tags)}")
        if source:
            meta_bits.append(f"fonte: {source}")
        if meta_bits:
            header += f" ({'; '.join(meta_bits)})"
        parts.append(f"{header}\n{post.content.strip()}\n")

    full_context = "\n".join(parts)
    truncated = len(full_context) > max_chars
    if truncated:
        full_context = full_context[:max_chars] + "\n\n[...contexto truncado por tamanho...]"
    return full_context, len(paths)


def to_anthropic_messages(openai_messages):
    """Converte mensagens estilo OpenAI (excluindo 'system') para o formato do Anthropic."""
    converted = []
    for msg in openai_messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role not in ("user", "assistant"):
            continue
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
        converted.append({"role": role, "content": content or ""})
    if not converted:
        converted = [{"role": "user", "content": ""}]
    return converted


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "created": int(time.time()), "owned_by": "graph-rag-demo"}],
    }


@app.get("/")
def root():
    _, n_notes = load_vault_context(VAULT_PATH, MAX_CONTEXT_CHARS)
    return {"status": "ok", "service": "graph-rag-demo context_server", "vault_path": VAULT_PATH, "notas_no_vault": n_notes}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    openai_messages = body.get("messages", [])
    stream = bool(body.get("stream", False))

    context, _n_notes = load_vault_context(VAULT_PATH, MAX_CONTEXT_CHARS)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(context=context or "(vault vazio - nenhuma nota encontrada)")
    anthropic_messages = to_anthropic_messages(openai_messages)

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    client = get_client()

    if not stream:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=anthropic_messages,
        )
        answer = "".join(block.text for block in resp.content if hasattr(block, "text"))
        return JSONResponse({
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": MODEL_ID,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": getattr(resp.usage, "input_tokens", 0),
                "completion_tokens": getattr(resp.usage, "output_tokens", 0),
                "total_tokens": getattr(resp.usage, "input_tokens", 0) + getattr(resp.usage, "output_tokens", 0),
            },
        })

    def event_stream():
        first_chunk = {
            "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(first_chunk)}\n\n"
        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=1024,
                system=system_prompt,
                messages=anthropic_messages,
            ) as stream_resp:
                for text in stream_resp.text_stream:
                    chunk = {
                        "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
                        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
        except Exception as exc:
            error_chunk = {
                "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
                "choices": [{"index": 0, "delta": {"content": f"\n[erro: {exc}]"}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(error_chunk)}\n\n"

        final_chunk = {
            "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": MODEL_ID,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8001"))
    print(f"[context_server] vault={VAULT_PATH}  modelo={MODEL}  porta={port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
