"""Extrai a sessão já logada de um Chromium/Chrome que está rodando.

Em vez de fazer login de novo, este script se conecta ao navegador que você já
usa (via CDP, a porta de depuração) e copia os cookies e o localStorage para
data/pandape_storage_state.json. Nenhuma senha é digitada ou armazenada.

Pré-requisito: o navegador precisa estar aberto com a porta de depuração.
Feche o Chromium por completo e reabra assim (a sessão logada continua lá,
os cookies ficam salvos no disco do perfil):

    chromium --remote-debugging-port=9222

Depois abra o Pandapé numa aba e rode:

    .venv/bin/python scripts/pandape_sessao_cdp.py
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_STATE = PROJECT_ROOT / "data" / "pandape_storage_state.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copia a sessão logada de um navegador aberto com --remote-debugging-port."
    )
    parser.add_argument(
        "--cdp",
        default="http://localhost:9222",
        help="Endpoint CDP do navegador aberto (padrão: http://localhost:9222)",
    )
    parser.add_argument(
        "--dominio",
        default="pandape",
        help="Trecho da URL que identifica a aba do site (padrão: pandape)",
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
        try:
            browser = p.chromium.connect_over_cdp(args.cdp)
        except PlaywrightError as exc:
            print(f"Não consegui conectar em {args.cdp}.\n", file=sys.stderr)
            print("Feche o Chromium por completo e reabra com:", file=sys.stderr)
            print("    chromium --remote-debugging-port=9222\n", file=sys.stderr)
            print(f"Detalhe: {exc}", file=sys.stderr)
            return 1

        # Nunca chamamos browser.close() aqui: o navegador é seu, não foi aberto
        # por este script. Ao sair do bloco só a conexão CDP é encerrada.
        if not browser.contexts:
            print("Navegador conectado, mas sem nenhum contexto aberto.", file=sys.stderr)
            return 1

        context = browser.contexts[0]
        urls = [page.url for page in context.pages]
        print("Abas encontradas:")
        for url in urls:
            print(f"  - {url}")

        alvo = [u for u in urls if args.dominio.lower() in u.lower()]
        if not alvo:
            print(
                f"\nNenhuma aba com '{args.dominio}' na URL. Abra o site logado "
                "numa aba desse navegador e rode de novo.",
                file=sys.stderr,
            )
            return 1

        context.storage_state(path=str(args.storage_state))

    print(f"\nAba usada: {alvo[0]}")
    print(f"Sessão salva em: {args.storage_state}")
    print("Esse arquivo está no .gitignore — não vai para o repositório.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
