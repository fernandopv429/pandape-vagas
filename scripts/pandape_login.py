"""Login manual no Pandapé, uma única vez, para salvar a sessão em disco.

Abre uma janela do Chrome, você faz o login (inclusive 2FA/captcha se houver) e,
ao pressionar ENTER no terminal, os cookies e o localStorage são gravados em
data/pandape_storage_state.json. Os scripts de coleta reusam esse arquivo e
rodam sem pedir senha.

Abre direto na busca de candidatos; o site redireciona para o login e, depois de
entrar, você já cai na tela certa — o que confirma que a sessão serve.

Uso:
    .venv/bin/python scripts/pandape_login.py
    .venv/bin/python scripts/pandape_login.py --url https://pandape.infojobs.com.br/
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_STATE = PROJECT_ROOT / "data" / "pandape_storage_state.json"
DEFAULT_URL = "https://pandape.infojobs.com.br/Company/Candidate/IndexInfojobs"


def main() -> int:
    parser = argparse.ArgumentParser(description="Salva a sessão logada do Pandapé.")
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Página de login do Pandapé (padrão: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--storage-state",
        type=Path,
        default=STORAGE_STATE,
        help="Onde gravar a sessão (padrão: data/pandape_storage_state.json)",
    )
    args = parser.parse_args()

    args.storage_state.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded")

        print()
        print("=" * 70)
        print("Faça o login na janela do Chrome que acabou de abrir.")
        print("Depois de entrar (já dentro do painel do Pandapé), volte aqui")
        print("e pressione ENTER para salvar a sessão.")
        print("=" * 70)
        input("ENTER para salvar > ")

        context.storage_state(path=str(args.storage_state))
        url_final = page.url
        context.close()
        browser.close()

    print(f"\nSessão salva em: {args.storage_state}")
    print(f"URL em que você estava: {url_final}")
    print("Esse arquivo está no .gitignore — não vai para o repositório.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
