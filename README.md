# pandape-vagas

Automação de coleta de informações no Pandapé com Playwright.

A ideia: você informa o termo de busca e os filtros, o script faz a pesquisa no
Pandapé já autenticado e devolve os resultados.

Tela alvo (busca de candidatos / banco de currículos, lado empresa):
<https://pandape.infojobs.com.br/Company/Candidate/IndexInfojobs>

## Instalação

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Os navegadores do Playwright já estão em cache nesta máquina. Se algum dia
faltar:

```bash
.venv/bin/python -m playwright install chromium
```

## 1. Sessão (uma vez)

Os scripts de coleta leem a sessão de `data/pandape_storage_state.json`. Há dois
jeitos de gerar esse arquivo — nenhum dos dois guarda senha.

### Opção A — aproveitar o Chromium que já está logado (mais rápido)

Feche o Chromium **por completo** (se ele já estiver rodando, a flag abaixo é
ignorada) e reabra com a porta de depuração. A sessão logada continua lá, porque
os cookies ficam no perfil em disco:

```bash
chromium --remote-debugging-port=9222
```

Abra o Pandapé numa aba e rode:

```bash
.venv/bin/python scripts/pandape_sessao_cdp.py
```

### Opção B — login manual numa janela nova

Abre o Chrome, você digita usuário e senha (funciona com 2FA e captcha) e a
sessão é salva ao apertar ENTER:

```bash
.venv/bin/python scripts/pandape_login.py
```

Quando a sessão expirar, repita a opção que preferir.

## 2. Coleta pela linha de comando

Busca por palavra-chave (lista de candidatos):

```bash
.venv/bin/python scripts/buscar.py --palavra assistente --paginas 3 --por-pagina 100 \
    --cdp http://localhost:9222 --saida csv
```

Detalhe de cada candidato (abre o currículo e extrai tudo):

```bash
.venv/bin/python scripts/detalhar.py --entrada data/resultados/busca-assistente.csv \
    --limite 5 --cdp http://localhost:9222
```

Depois de rodar uma vez com `--cdp`, a sessão fica salva e você pode omitir o
`--cdp` (o script sobe o próprio navegador).

## 3. API (para o n8n / integrações)

A API vive só neste projeto e faz uma coisa: consultar o Pandapé. Sobe local:

```bash
.venv/bin/python -m uvicorn api.main:app --port 8090
```

Como uma coleta grande leva minutos, ela roda como **job**: dispara e consulta.

| Método | Rota | O que faz |
|---|---|---|
| GET | `/health` | estado da API, sessão salva e banco de dedup |
| GET | `/buscar?palavra=assistente&paginas=1` | busca rápida e síncrona (só a lista) |
| POST | `/coletar` | inicia a coleta (busca + detalhes) → `{job_id}` |
| GET | `/job/{job_id}` | status e resultado do job |
| GET | `/jobs` | lista os jobs desta execução |
| POST | `/candidatos/marcar-usado` | marca candidato(s) como usados — não voltam mais numa busca |
| POST | `/candidatos/desmarcar` | reverte a marca de usado |
| GET | `/candidatos?usado=false` | lista candidatos já salvos no banco local |

### Dedup — não repetir o mesmo candidato

Toda busca/coleta grava os candidatos num Postgres (`DATABASE_URL` no `.env`).
Por padrão (`excluir_usados: true`), a busca já tira quem foi marcado como
usado antes — então depois que você usou um candidato com sucesso em algum
lugar, chama:

```bash
curl -X POST http://localhost:8090/candidatos/marcar-usado \
  -H "X-API-Key: SUA_CHAVE" -H 'Content-Type: application/json' \
  -d '{"ids": ["9380819"], "contexto": "enviado pro fluxo X"}'
```

E esse candidato nunca mais volta numa busca futura (a não ser que você
mande `excluir_usados: false` explicitamente, ou desmarque ele de novo em
`/candidatos/desmarcar`). Sem `DATABASE_URL` configurada a API funciona
normal, só sem esse dedup.

### Webhook — enviar os candidatos pra outro sistema

Toda coleta concluída (`POST /coletar`) manda os candidatos coletados (já
sem os que já foram usados) pra `WEBHOOK_URL` do `.env`, por exemplo um nó
Webhook do n8n — **em lotes**, não tudo de uma vez: `WEBHOOK_BATCH_SIZE`
(`.env`, padrão 20) define quantos candidatos vão por chamada, evitando um
JSON gigante numa coleta grande. Dá pra sobrescrever por pedido com o campo
`webhook_lote`. Cada lote é uma chamada POST separada:

```json
{
  "job_id": "ab12cd34ef56",
  "enviado_em": "2026-11-09T16:40:00+00:00",
  "palavras": ["assistente"],
  "lote": 1,
  "lotes_total": 3,
  "candidatos_no_lote": 20,
  "total_candidatos": 47,
  "candidatos": [ /* até 20 candidatos, mesma estrutura de /buscar */ ]
}
```

O resultado do envio fica em `job.webhook` — consulte em `GET /job/{job_id}`:
`lotes_total`, `lotes_enviados` e, se algum lote falhar, `erros` com o
detalhe de qual lote e por quê. Se um lote falhar, os outros continuam sendo
enviados normalmente — a coleta nunca trava por causa disso. Sem
`WEBHOOK_URL` configurada, não envia pra lugar nenhum.

Exemplo de coleta:

```bash
curl -X POST http://localhost:8090/coletar -H 'Content-Type: application/json' -d '{
  "palavra": ["assistente"], "paginas": 3, "por_pagina": 100,
  "com_detalhes": true, "limite_detalhes": 20
}'
# -> {"job_id":"ab12cd34ef56", ...}

curl http://localhost:8090/job/ab12cd34ef56
```

Variáveis de ambiente:

- `PANDAPE_HEADLESS=0` — mostra o navegador durante a coleta (padrão: escondido)
- `PANDAPE_CDP=http://localhost:9222` — usa seu navegador aberto em vez do arquivo
- `PANDAPE_STORAGE_STATE=/caminho` — outro arquivo de sessão

Documentação interativa: <http://localhost:8090/docs>

### Autenticação (obrigatória)

Os endpoints de consulta exigem o header `X-API-Key`. A chave vem de
`PANDAPE_API_KEY` (veja `.env.example`). **Sem chave configurada, a API recusa
tudo** — para não ficar aberta por engano. Só `/health` fica público.

```bash
curl -H "X-API-Key: SUA_CHAVE" "http://localhost:8090/buscar?palavra=assistente"
```

## 4. Deploy permanente na VPS (recomendado)

Em vez de rodar nesta máquina na mão, o `Dockerfile` sobe tudo num container só,
sempre no ar:

- **Chrome persistente** rodando numa tela virtual (Xvfb) — a sessão do
  Pandapé fica só no perfil desse Chrome, não precisa mais de
  `pandape_storage_state.json`;
- **noVNC**: acesso remoto a esse Chrome pelo navegador normal, protegido por
  senha — é assim que você loga (ou renova o login) no Pandapé sem precisar
  dessa máquina local;
- a **API** (porta 8090), já configurada pra sempre usar esse Chrome via CDP
  (`PANDAPE_CDP=http://localhost:9222`, interno ao container — nunca exposto
  pra fora).

No Coolify, crie um recurso apontando pra este repositório (build via
Dockerfile) e configure:

- **Domínios**: porta `8090` → `pandape.nexusdevhub.com` (API), porta `6080`
  → `pandape-login.nexusdevhub.com` (noVNC)
- **Variáveis de ambiente**: `PANDAPE_API_KEY`, `DATABASE_URL`, `WEBHOOK_URL`
  (os mesmos valores do `.env` local) e `VNC_PASSWORD` (senha pro acesso
  remoto — defina uma forte)
- **Volume persistente**: monte um volume em `/data` — é onde fica o perfil
  do Chrome (login) entre deploys/restarts. **Sem isso, todo redeploy perde a
  sessão e pede login de novo.**

Depois do primeiro deploy, abra `https://pandape-login.nexusdevhub.com/vnc.html`,
entre com a senha do VNC, e faça o login do Pandapé nesse Chrome (uma vez só —
fica salvo no volume). A partir daí a API em `pandape.nexusdevhub.com` já
funciona normalmente.

### Alternativa local: expor via ngrok (sem VPS)

Para o n8n alcançar a API rodando **nesta máquina** (sem fazer o deploy
acima), um túnel dá uma URL HTTPS pública apontando pra cá — útil só pra
testar, já que depende desta máquina estar ligada.

1. Configure o ngrok uma vez (se ainda não tiver token):
   ```bash
   ngrok config add-authtoken SEU_TOKEN
   ```
2. Ajuste o `.env` (copie de `.env.example`, defina `PANDAPE_API_KEY`).
3. Suba API + túnel:
   ```bash
   scripts/servir_publico.sh
   ```
   Ele imprime a URL pública (algo como `https://xxxx.ngrok-free.app`).

Deixe o terminal aberto; `Ctrl+C` derruba a API e o túnel. Com o plano grátis do
ngrok a URL muda a cada execução — para uma URL fixa, use um domínio do ngrok ou
um túnel nomeado.

### No n8n (nó HTTP Request)

- **Method**: POST
- **URL**: `https://xxxx.ngrok-free.app/coletar`
- **Headers**: `X-API-Key: SUA_CHAVE`
- **Body (JSON)**: `{"palavra":["assistente"],"paginas":3,"com_detalhes":true,"limite_detalhes":20}`

A resposta traz `job_id`. Faça um segundo nó GET
`https://xxxx.ngrok-free.app/job/{{job_id}}` (com o mesmo header) e repita até
`status: "concluido"`.

### Cuidados (LGPD / segurança)

Esta API devolve **dados pessoais de candidatos** (nome, CPF, telefone, e-mail,
endereço). Ao expô-la:

- **nunca** rode sem `PANDAPE_API_KEY`, e use uma chave forte;
- trate esses dados conforme a LGPD (finalidade legítima, acesso restrito);
- a URL do ngrok é pública — a proteção é a chave, então não a exponha em logs,
  prints ou repositórios;
- lembre que a coleta usa a **sua conta** do Pandapé; respeite os termos de uso.

## Playwright MCP

`.mcp.json` configura o servidor Playwright MCP para o Claude Code, reusando a
mesma sessão salva no passo 1. Serve para navegar e inspecionar o site durante o
desenvolvimento. O MCP só carrega quando o Claude Code inicia — e o arquivo de
sessão precisa existir antes.

## Estrutura

```
pandape/core.py            lógica de scraping (fonte única: busca + detalhe)
api/main.py                API FastAPI com coleta em job
scripts/buscar.py          CLI: busca por palavra-chave
scripts/detalhar.py        CLI: abre o detalhe de cada candidato
scripts/pandape_login.py   login manual, salva a sessão
scripts/pandape_sessao_cdp.py   copia a sessão do navegador aberto
data/                      sessão e saídas da coleta (fora do git)
.mcp.json                  Playwright MCP para o Claude Code
```

Observação: `pandape/core.py` é a fonte da lógica usada pela API. Os scripts
`buscar.py`/`detalhar.py` ainda têm a própria cópia dessa lógica (foi assim que
foram validados); a unificação deles para importar de `core.py` é o próximo
passo de manutenção.
