#!/bin/sh
set -e

: "${VNC_PASSWORD:?defina VNC_PASSWORD (senha pro acesso remoto ao Chrome via noVNC)}"
CHROME_PROFILE_DIR="${CHROME_PROFILE_DIR:-/data/chrome-profile}"
mkdir -p "$CHROME_PROFILE_DIR"

echo "[entrypoint] subindo Xvfb em $DISPLAY..."
# Mesmo motivo do lock do Chrome logo abaixo: se o processo anterior morreu
# sem liberar o display (ex: `docker restart`, que reaproveita o /tmp em vez
# de dar um container limpo), sobra esse lock e o Xvfb novo recusa subir.
rm -f "/tmp/.X${DISPLAY#:}-lock"
Xvfb "$DISPLAY" -screen 0 1440x900x24 &
sleep 1

echo "[entrypoint] subindo Chrome persistente (perfil em $CHROME_PROFILE_DIR, CDP só em localhost:9222)..."
# Só roda uma instância de Chrome por vez nesse perfil — qualquer lock que
# sobrou é de um container anterior que morreu sem fechar o Chrome direito
# (ex: redeploy no Coolify substituindo o container). Como o hostname muda a
# cada container, o Chrome vê esse lock e recusa subir achando que é outra
# máquina usando o perfil — por isso apaga antes de tentar.
rm -f "$CHROME_PROFILE_DIR/SingletonLock" "$CHROME_PROFILE_DIR/SingletonCookie" "$CHROME_PROFILE_DIR/SingletonSocket"

# --no-sandbox: o container roda como root, e o Chrome recusa iniciar como
# root sem essa flag (sandbox do Chrome exige um usuário não-root).
google-chrome-stable \
    --remote-debugging-port=9222 \
    --user-data-dir="$CHROME_PROFILE_DIR" \
    --no-first-run --no-default-browser-check \
    --disable-dev-shm-usage \
    --no-sandbox \
    --start-maximized \
    "https://pandape.infojobs.com.br/Company/Candidate/IndexInfojobs" &
sleep 2

echo "[entrypoint] subindo x11vnc (porta 5900, com senha)..."
x11vnc -display "$DISPLAY" -forever -shared -rfbport 5900 -passwd "$VNC_PASSWORD" &

echo "[entrypoint] subindo noVNC em :6080..."
# O pacote noVNC do Debian não traz index.html — só vnc.html. Sem isso, abrir a
# raiz do domínio devolve a listagem de arquivos do diretório em vez da tela do
# navegador remoto, e parece que o serviço está quebrado.
ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html
websockify --web=/usr/share/novnc 6080 localhost:5900 &

echo "[entrypoint] subindo API em :8090..."
exec python -m uvicorn api.main:app --host 0.0.0.0 --port 8090
