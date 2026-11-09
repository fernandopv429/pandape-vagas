#!/usr/bin/env bash
# Sobe a API do Pandapé e a expõe na internet via ngrok (URL HTTPS pública).
# A sessão do Pandapé fica nesta máquina; o n8n na nuvem chama a URL do ngrok.
#
# Uso:
#   scripts/servir_publico.sh
#
# Lê variáveis do arquivo .env (copie de .env.example). PANDAPE_API_KEY é
# obrigatória — sem ela a API recusa as consultas.
set -euo pipefail

cd "$(dirname "$0")/.."
RAIZ="$(pwd)"

# carrega .env se existir
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

PORT="${PORT:-8090}"

if [[ -z "${PANDAPE_API_KEY:-}" ]]; then
  echo "ERRO: PANDAPE_API_KEY não definida."
  echo "  Copie .env.example para .env e defina a chave, ou:"
  echo "  export PANDAPE_API_KEY=\$(.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(32))')"
  exit 1
fi

if ! command -v ngrok >/dev/null 2>&1; then
  echo "ERRO: ngrok não encontrado no PATH."
  exit 1
fi

echo "==> subindo a API em http://127.0.0.1:${PORT}"
.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port "${PORT}" \
  > "${RAIZ}/data/api.log" 2>&1 &
API_PID=$!

encerrar() {
  echo; echo "==> encerrando..."
  kill "${API_PID}" 2>/dev/null || true
  kill "${NGROK_PID:-}" 2>/dev/null || true
}
trap encerrar EXIT INT TERM

# espera a API responder
for i in $(seq 1 30); do
  if curl -s --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then break; fi
  sleep 0.4
done
echo "==> API no ar (log: data/api.log)"

echo "==> abrindo túnel ngrok para a porta ${PORT}"
ngrok http "${PORT}" --log stdout > "${RAIZ}/data/ngrok.log" 2>&1 &
NGROK_PID=$!

# pega a URL pública da API local do ngrok
URL=""
for i in $(seq 1 30); do
  URL=$(curl -s --max-time 2 http://127.0.0.1:4040/api/tunnels \
        | grep -o '"public_url":"https:[^"]*"' | head -1 | cut -d'"' -f4 || true)
  [[ -n "${URL}" ]] && break
  sleep 0.5
done

echo
echo "======================================================================"
if [[ -n "${URL}" ]]; then
  echo " URL pública:   ${URL}"
  echo " Health:        ${URL}/health"
  echo " Consumo (n8n): POST ${URL}/coletar   (header X-API-Key)"
else
  echo " Não consegui ler a URL do ngrok. Veja data/ngrok.log"
  echo " ou o painel local:  http://127.0.0.1:4040"
fi
echo "======================================================================"
echo " Deixe este terminal aberto. Ctrl+C encerra API e túnel."
echo
wait
