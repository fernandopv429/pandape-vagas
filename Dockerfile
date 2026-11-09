FROM python:3.12-slim

# Xvfb (tela virtual), x11vnc + noVNC (acesso remoto ao navegador pelo browser),
# Google Chrome real (a mesma sessão logada fica persistida no perfil) e as
# bibliotecas do sistema que o Chrome/Playwright precisam.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates \
        xvfb x11vnc novnc websockify \
        fonts-liberation libnss3 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
        libasound2 libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libu2f-udev \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x docker-entrypoint.sh

ENV DISPLAY=:99
# A API usa esse Chrome persistente (via CDP) em vez de reabrir um navegador
# novo a cada busca — a sessão logada fica só nesse perfil, sempre no ar.
ENV PANDAPE_CDP=http://localhost:9222

# 8090 = API (consultas/coleta). 6080 = noVNC (acesso remoto ao Chrome pra
# fazer/renovar o login do Pandapé). 9222 (CDP) NUNCA deve ser publicado —
# não tem autenticação nenhuma, só é usado internamente pelo próprio container.
EXPOSE 8090 6080

CMD ["./docker-entrypoint.sh"]
