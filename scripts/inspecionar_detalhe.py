"""Abre a página de detalhe de UM candidato e salva o HTML para mapeamento.

Conecta na sua sessão (CDP) ou usa a sessão salva, abre o /DetailInfojobs/{id} e
grava HTML, texto e screenshot em data/playwright-output/. Serve para eu ver a
estrutura da página de detalhe antes de escrever a extração em lote.

Uso:
    .venv/bin/python scripts/inspecionar_detalhe.py --id 20323983 --cdp http://localhost:9222
    .venv/bin/python scripts/inspecionar_detalhe.py --url https://.../DetailInfojobs/20323983 --cdp http://localhost:9222
"""

import argparse
import re
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_STATE = PROJECT_ROOT / "data" / "pandape_storage_state.json"
DUMP_DIR = PROJECT_ROOT / "data" / "playwright-output"
BASE_DETALHE = "https://pandape.infojobs.com.br/Company/Candidate/DetailInfojobs/"


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspeciona a página de detalhe de um candidato.")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--id", help="ID do candidato, ex: 20323983")
    grupo.add_argument("--url", help="URL completa do detalhe")
    parser.add_argument("--cdp", help="Navegador já aberto, ex: http://localhost:9222")
    parser.add_argument("--storage-state", type=Path, default=STORAGE_STATE)
    args = parser.parse_args()

    url = args.url or (BASE_DETALHE + args.id)
    DUMP_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        if args.cdp:
            browser = p.chromium.connect_over_cdp(args.cdp)
            if not browser.contexts:
                print("navegador sem contexto aberto", file=sys.stderr)
                return 1
            context = browser.contexts[0]
            fechar = False
        else:
            if not args.storage_state.exists():
                print(f"sessão não encontrada em {args.storage_state}", file=sys.stderr)
                return 1
            browser = p.chromium.launch(channel="chrome", headless=False)
            context = browser.new_context(storage_state=str(args.storage_state),
                                          viewport={"width": 1440, "height": 900})
            fechar = True

        page = context.new_page()
        print(f"abrindo: {url}")
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeout:
            pass
        page.wait_for_timeout(2000)

        if "/Account/Login" in page.url or "login" in page.url.lower():
            print("caiu no login — sessão sem acesso a esse candidato", file=sys.stderr)
            if fechar:
                browser.close()
            return 1

        slug = re.sub(r"[^0-9]", "", url)[-8:] or "detalhe"
        (DUMP_DIR / f"detalhe-{slug}.html").write_text(page.content(), encoding="utf-8")
        texto = page.evaluate("() => (document.querySelector('main') || document.body).innerText")
        (DUMP_DIR / f"detalhe-{slug}.txt").write_text(texto, encoding="utf-8")
        try:
            page.screenshot(path=str(DUMP_DIR / f"detalhe-{slug}.png"), full_page=True)
        except PlaywrightError:
            pass

        # links de download / pdf, se houver
        pdfs = page.evaluate(r"""() => [...document.querySelectorAll('a[href]')]
            .map(a => a.getAttribute('href'))
            .filter(h => h && /pdf|download|curriculum|print|export/i.test(h))""")

        print(f"\nHTML:  {DUMP_DIR / ('detalhe-'+slug+'.html')}")
        print(f"TEXTO: {DUMP_DIR / ('detalhe-'+slug+'.txt')}")
        print(f"\n--- primeiros 1200 caracteres do texto da página ---\n")
        print(texto[:1200])
        print("\n--- links que parecem PDF/download ---")
        for h in pdfs[:20]:
            print("  ", h)
        if fechar:
            input("\nENTER para fechar > ")
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
