# Graph RAG Demo (sem RAG de verdade)

Simulação de "Graph RAG" para apresentação: em vez de embeddings + busca vetorial,
o conteúdo extraído de sites vira notas `.md` organizadas como um vault do
Obsidian (com tags e `[[wikilinks]]` entre notas e entidades). Um servidor local
expõe esse vault para o **Open WebUI**, injetando todo o conteúdo das notas
como contexto a cada pergunta — sem buscar na internet.

## Componentes

| Arquivo | Papel |
|---|---|
| `scraper_agent.py` | Crawler: extrai páginas de um site (com profundidade configurável) e grava `.md` em `obsidian-vault/scraped/`. |
| `writer_agent.py` | Usa Claude para extrair tags/entidades de cada nota, cria notas "hub" de entidade em `obsidian-vault/entities/` e insere `[[wikilinks]]` entre notas relacionadas. |
| `context_server.py` | API local compatível com OpenAI (`/v1/chat/completions`), usada pelo Open WebUI. A cada pergunta, lê **todos** os `.md` do vault e responde via Claude restrito a esse contexto. |

## 1. Instalação

```bash
cd graph-rag-demo
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edite .env e preencha ANTHROPIC_API_KEY
```

## 2. Rodar o scraper agent

```bash
python scraper_agent.py https://exemplo.com --depth 2 --max-pages 20
```

- `--depth`: quantos níveis de links internos seguir (0 = só a página informada).
- `--max-pages`: limite total de páginas salvas.
- `--include-external`: também segue links para outros domínios (por padrão só segue o mesmo domínio).

Repita para quantos sites quiser — cada execução acrescenta notas em `obsidian-vault/scraped/`.

## 3. Rodar o writer agent

```bash
python writer_agent.py
```

Isso chama o Claude para cada nota nova, gera tags, identifica entidades e:
- cria notas de entidade em `obsidian-vault/entities/` (uma por entidade citada em 2+ notas);
- adiciona `## Tags`, `## Entidades relacionadas` e `## Notas relacionadas` em cada nota, com links `[[...]]`.

Rode de novo (`--force` para reprocessar tudo) sempre que adicionar mais notas — o grafo de relações é recalculado considerando o vault inteiro.

## 4. Abrir o vault no Obsidian

Abra o Obsidian → "Open folder as vault" → selecione a pasta `obsidian-vault/`.
Vá em **Graph view** para ver as notas conectadas entre si e às entidades (hub nodes).

## 5. Rodar o context server

```bash
python context_server.py
```

Por padrão sobe em `http://localhost:8001`. A cada request ele relê os `.md` do vault — não há cache nem índice, é leitura direta do disco.

## 6. Conectar o Open WebUI

Suba o Open WebUI (exemplo via Docker):

```bash
docker run -d -p 3000:8080 -e WEBUI_AUTH=False \
  --name open-webui ghcr.io/open-webui/open-webui:main
```

No Open WebUI: **Settings → Connections → OpenAI API → Add Connection**

- **Base URL**: `http://host.docker.internal:8001/v1` (se o Open WebUI estiver em Docker e o `context_server.py` rodando direto na máquina) ou `http://localhost:8001/v1` (se ambos rodarem fora de container).
- **API Key**: qualquer valor, ex. `demo` (não é validado pelo `context_server.py`).

Salve e selecione o modelo **`graph-rag-vault`** no chat. Pronto — as perguntas no Open WebUI agora são respondidas só com base no vault.

## 7. Roteiro da demo (provar a dependência do contexto)

1. Faça uma pergunta sobre algo presente nas notas. Mostre a resposta.
2. Abra o `.md` correspondente em `obsidian-vault/scraped/` e **edite a informação** (ex: mude um número, um nome, um fato).
3. Faça a **mesma pergunta de novo** no Open WebUI, sem reiniciar nada.
4. A resposta muda — porque o `context_server.py` releu o arquivo do disco e mandou o conteúdo atualizado para o Claude. Isso demonstra que a resposta do LLM está 100% amarrada ao conteúdo dos `.md`, e não a conhecimento geral do modelo nem à internet.

## Observações

- O scraper usa `requests` + `BeautifulSoup` (sem executar JavaScript). Funciona bem em sites com HTML renderizado no servidor (blogs, docs estáticos, wikis). Em sites SPA cujo conteúdo só aparece após JS rodar no navegador, a extração pode vir vazia/incompleta — escolha sites de conteúdo estático para a demo, ou valide o `.md` gerado antes de seguir para o writer agent.
- Isso **não é** um RAG real (não há embeddings nem busca por similaridade): o servidor manda o vault inteiro como contexto. Funciona bem para vaults pequenos/médios (cabe na janela de contexto do Claude); para vaults grandes seria necessário um índice/retrieval de verdade.
- `MAX_CONTEXT_CHARS` (no `.env`) trunca o contexto se o vault crescer demais.
- Os scripts são independentes entre si — cada um pode ser rodado e entendido isoladamente, refletindo a ideia de "agentes" separados (scraper, writer, servidor de Q&A).
