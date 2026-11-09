"""Inspeciona uma página do Pandapé usando o navegador que você já tem logado.

Conecta via CDP no Chromium aberto com --remote-debugging-port=9222, abre a URL
numa aba nova (aproveitando a sessão) e despeja o que existe de campo de texto,
seletor, botão e link. Serve para descobrir os seletores reais antes de escrever
a automação.

O HTML completo é salvo em data/playwright-output/ para análise offline.

Uso:
    .venv/bin/python scripts/inspecionar_pagina.py
    .venv/bin/python scripts/inspecionar_pagina.py --url <outra-url>
"""

import argparse
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "data" / "playwright-output"
URL_BUSCA = "https://pandape.infojobs.com.br/Company/Candidate/IndexInfojobs"

# Roda no browser: coleta os controles interativos com o que der para identificá-los.
COLETA_JS = """
() => {
  const desc = (el) => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type'),
      id: el.id || null,
      name: el.getAttribute('name'),
      placeholder: el.getAttribute('placeholder'),
      aria: el.getAttribute('aria-label'),
      dataTestId: el.getAttribute('data-testid') || el.getAttribute('data-test'),
      classe: (el.getAttribute('class') || '').slice(0, 120) || null,
      texto: (el.innerText || el.value || '').trim().slice(0, 80) || null,
      visivel: r.width > 0 && r.height > 0,
      opcoes: el.tagName === 'SELECT'
        ? [...el.options].slice(0, 25).map(o => ({ value: o.value, texto: o.text.trim() }))
        : undefined,
    };
  };
  const pega = (sel) => [...document.querySelectorAll(sel)].map(desc).filter(e => e.visivel);
  return {
    titulo: document.title,
    url: location.href,
    campos_texto: pega('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea'),
    selects: pega('select'),
    checkboxes: pega('input[type=checkbox], input[type=radio]').slice(0, 60),
    botoes: pega('button, input[type=submit], [role=button]').slice(0, 60),
    forms: [...document.querySelectorAll('form')].map(f => ({
      id: f.id || null, name: f.getAttribute('name'), action: f.getAttribute('action'),
      metodo: f.getAttribute('method'),
    })),
  };
}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Mapeia os seletores de uma página do Pandapé.")
    parser.add_argument("--url", default=URL_BUSCA, help=f"URL a inspecionar (padrão: {URL_BUSCA})")
    parser.add_argument(
        "--cdp",
        default="http://localhost:9222",
        help="Endpoint CDP do navegador aberto (padrão: http://localhost:9222)",
    )
    parser.add_argument(
        "--manter-aba",
        action="store_true",
        help="Não fecha a aba criada, para você conferir na tela",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", args.url.split("://", 1)[-1]).strip("-")[:80]

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(args.cdp)
        except PlaywrightError as exc:
            print(f"Não consegui conectar em {args.cdp}.\n", file=sys.stderr)
            print("Feche o Chromium por completo e reabra com:", file=sys.stderr)
            print("    chromium --remote-debugging-port=9222\n", file=sys.stderr)
            print(f"Detalhe: {exc}", file=sys.stderr)
            return 1

        # O navegador é seu: nunca chamamos browser.close().
        if not browser.contexts:
            print("Navegador conectado, mas sem nenhum contexto aberto.", file=sys.stderr)
            return 1

        context = browser.contexts[0]
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightError:
            pass  # páginas com polling nunca ficam idle; segue com o que carregou

        dados = page.evaluate(COLETA_JS)

        html_path = OUTPUT_DIR / f"{slug}.html"
        html_path.write_text(page.content(), encoding="utf-8")
        json_path = OUTPUT_DIR / f"{slug}.json"
        json_path.write_text(
            json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        page.screenshot(path=str(OUTPUT_DIR / f"{slug}.png"), full_page=True)

        if not args.manter_aba:
            page.close()

    print(json.dumps(dados, ensure_ascii=False, indent=2))
    print(f"\nHTML:        {html_path}")
    print(f"JSON:        {json_path}")
    print(f"Screenshot:  {OUTPUT_DIR / (slug + '.png')}")
    if dados["url"] != args.url:
        print(f"\nATENÇÃO: a página redirecionou para {dados['url']}")
        print("Se caiu na tela de login, a sessão desse navegador expirou.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
