"""Busca candidatos no Pandapé por palavra-chave, montando a URL de pesquisa.

A busca do Pandapé é feita por query string — não é preciso preencher formulário
nem clicar em nada. Basta navegar autenticado para:

    /Company/Candidate/IndexInfojobs?Keywords[0]=assistente&KeywordMatchType=AllWords
        &KeywordSearchScopeType=AllFields&Pagination[0]=1&Pagination[1]=10&Order=0

Parâmetros observados na URL real:
    Keywords[0..n]            palavras-chave
    KeywordMatchType          AllWords | AnyWords (como combinar as palavras)
    KeywordSearchScopeType    AllFields (onde procurar)
    Pagination[0]             número da página
    Pagination[1]             registros por página
    Order                     ordenação (0 = padrão)
    ExperienceCtInProgress, HasEvaluationComments, CandidateAssignmentType,
    ReadCvStatus, StudyingStatus, PhotoFilterCt   filtros (None = sem filtro)
    NegotiableSalary, IsLast                      flags (False)

A sessão vem de data/pandape_storage_state.json (gerada por pandape_login.py ou
pandape_sessao_cdp.py) ou, com --cdp, do seu navegador já aberto.

Uso:
    .venv/bin/python scripts/buscar.py --palavra assistente
    .venv/bin/python scripts/buscar.py --palavra assistente juridico --paginas 3
    .venv/bin/python scripts/buscar.py --palavra advogado --por-pagina 50 --saida csv
    .venv/bin/python scripts/buscar.py --palavra assistente --dump   # salva o HTML
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_STATE = PROJECT_ROOT / "data" / "pandape_storage_state.json"
SAIDA_DIR = PROJECT_ROOT / "data" / "resultados"
DUMP_DIR = PROJECT_ROOT / "data" / "playwright-output"

BASE = "https://pandape.infojobs.com.br/Company/Candidate/IndexInfojobs"

# Filtros que a URL manda como "None" quando não estão em uso.
FILTROS_NEUTROS = {
    "ExperienceCtInProgress": "None",
    "HasEvaluationComments": "None",
    "CandidateAssignmentType": "None",
    "ReadCvStatus": "None",
    "StudyingStatus": "None",
    "PhotoFilterCt": "None",
}


def montar_url(palavras, pagina, por_pagina, match, escopo, ordem, extras=None):
    """Monta a URL de busca. Os colchetes ficam literais, como o site usa."""
    partes = [f"{chave}={valor}" for chave, valor in FILTROS_NEUTROS.items()]
    for i, palavra in enumerate(palavras):
        partes.append(f"Keywords[{i}]={quote(palavra)}")
    partes += [
        f"Order={ordem}",
        "IsLast=False",
        "NegotiableSalary=False",
        f"KeywordMatchType={match}",
        f"KeywordSearchScopeType={escopo}",
        f"Pagination[0]={pagina}",
        f"Pagination[1]={por_pagina}",
    ]
    for chave, valor in (extras or {}).items():
        partes.append(f"{chave}={quote(str(valor))}")
    return f"{BASE}?" + "&".join(partes)


# Extração no browser. Cada candidato é <div id="candidate-{id}" class="js_row
# js_userInfo" data-id="{id}"> dentro de #grid. Os campos ficam em classes
# estáveis: .candidate-name, .candidate-job (área), e blocos .c-md com cargo
# atual / empresa / tempo / formação / atualização; telefone vem ao lado do
# ícone .icon-whatsapp-outline e o e-mail num mailto/texto.
EXTRAIR_JS = r"""
() => {
  const limpar = t => (t || '').replace(/\s+/g, ' ').trim();
  const txt = (raiz, sel) => { const e = raiz.querySelector(sel); return e ? limpar(e.innerText) : null; };

  const linhas = [...document.querySelectorAll('div.js_userInfo[data-id], div.js_row[data-id]')];

  const candidatos = linhas.map(row => {
    const id = row.getAttribute('data-id');
    const alvo = limpar(row.innerText);

    // e-mail e telefone por padrão, onde quer que estejam no cartão
    const email = (alvo.match(/[\w.+-]+@[\w-]+\.[\w.-]+/) || [])[0] || null;
    const telefone = (alvo.match(/\b\d{2}-\d{8,9}\b/) || [])[0] || null;

    // link do WhatsApp (wa.me), quando o candidato tem celular
    const aWpp = row.querySelector('a[href*="wa.me"], a[href*="api.whatsapp"]');
    const whatsapp = aWpp ? aWpp.getAttribute('href') : null;

    // Experiência e formação ficam cada uma num .box-container. O container tem
    // o resumo visível ("Cargo em Empresa (tempo)") mais um popover com a lista
    // completa — clonamos e removemos o popover para ficar só com o resumo.
    // [0] = experiência, [1] = formação ("Nível em Instituição").
    const resumoBox = (el) => {
      const clone = el.cloneNode(true);
      clone.querySelectorAll('.box-popover, [class*="popover"]').forEach(e => e.remove());
      return limpar(clone.innerText);
    };
    const boxes = [...row.querySelectorAll('[class*="box-container"]')].map(resumoBox);
    const textoExp = boxes[0] || '';
    const textoForm = boxes[1] || '';
    const mExp = textoExp.match(/^(.*?)\s+em\s+(.*?)\s*\(([^)]*)\)\s*$/);
    const mForm = textoForm.match(/^(.*?)\s+em\s+(.*)$/);

    // "Última atualização: 27 Jul 26" fica num .c-md próprio
    let atualizacao = null;
    for (const c of row.querySelectorAll('.c-md')) {
      const m = (c.innerText || '').match(/[ÚU]ltima atualiza[çc][ãa]o:\s*(.+)$/);
      if (m) { atualizacao = limpar(m[1]); break; }
    }

    return {
      idCandidato: id,
      nome: txt(row, '.candidate-name'),
      area: txt(row, '.candidate-job'),
      cargo_atual: mExp ? limpar(mExp[1]) : (textoExp || null),
      empresa: mExp ? limpar(mExp[2]) : null,
      tempo_experiencia: mExp ? limpar(mExp[3]) : null,
      formacao: mForm ? limpar(mForm[1]) : (textoForm || null),
      instituicao: mForm ? limpar(mForm[2]) : null,
      ultima_atualizacao: atualizacao,
      telefone,
      email,
      whatsapp,
      link_curriculo: id ? location.origin + '/Company/Candidate/DetailInfojobs/' + id : null,
    };
  }).filter(c => c.idCandidato);

  // total de resultados da busca: #MatchSearchTotal ("2.457.402 candidatos encontrados")
  const elTotal = document.querySelector('#MatchSearchTotal');
  const total = elTotal ? limpar(elTotal.innerText) : null;

  // botão "próxima página": se estiver desabilitado, chegamos ao fim
  const btnNext = document.querySelector('.js_btnPaginationNext');
  const temProxima = !!btnNext && !/disabled/.test(btnNext.className);

  return {
    url: location.href,
    titulo: document.title,
    total_texto: total,
    tem_proxima: temProxima,
    quantidade_na_pagina: candidatos.length,
    candidatos,
    amostra_do_corpo: candidatos.length ? null : limpar(document.body.innerText).slice(0, 1500),
  };
}
"""


def abrir_contexto(p, args):
    """Devolve (context, fechar_ao_final). Usa CDP ou a sessão salva em arquivo."""
    if args.cdp:
        browser = p.chromium.connect_over_cdp(args.cdp)
        if not browser.contexts:
            raise RuntimeError("navegador conectado, mas sem contexto aberto")
        context = browser.contexts[0]
        # salva a sessão para as próximas execuções dispensarem a porta CDP
        try:
            args.storage_state.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(args.storage_state))
            print(f"(sessão salva em {args.storage_state.name} — "
                  "próximas vezes pode rodar sem --cdp)")
        except PlaywrightError:
            pass
        # O navegador é do usuário: não fechamos.
        return context, False

    if not args.storage_state.exists():
        raise RuntimeError(
            f"sessão não encontrada em {args.storage_state}.\n"
            "Gere com:  .venv/bin/python scripts/pandape_login.py\n"
            "ou use --cdp http://localhost:9222 com seu Chromium já logado."
        )
    browser = p.chromium.launch(channel="chrome", headless=args.headless)
    context = browser.new_context(
        storage_state=str(args.storage_state), viewport={"width": 1440, "height": 900}
    )
    return context, True


def esperar_grid(page, timeout_ms=15000):
    """Espera o nº de candidatos no grid parar de mudar (AJAX terminou de render)."""
    fim = timeout_ms / 1000.0
    passo = 0.6
    anterior, estavel, decorrido = -1, 0, 0.0
    while decorrido < fim:
        atual = page.evaluate(
            "() => document.querySelectorAll('.js_userInfo[data-id]').length")
        if atual > 0 and atual == anterior:
            estavel += 1
            if estavel >= 2:  # duas leituras iguais seguidas = assentou
                return atual
        else:
            estavel = 0
        anterior = atual
        page.wait_for_timeout(int(passo * 1000))
        decorrido += passo
    return anterior


def ajustar_tamanho_pagina(page, por_pagina):
    """Seleciona 10/25/50/100 por página pelo dropdown do site, se possível.

    O tamanho não é controlado pela URL, e sim por um dropdown que dispara AJAX.
    Se não der para acionar, seguimos no padrão (10) — só dá mais cliques.
    """
    if por_pagina == 10:
        return
    try:
        # dropdown de tamanho de página: toggle #changePagination ("Mostrar 10")
        toggle = page.locator("#changePagination")
        if toggle.count() == 0:
            print("  (não achei o seletor de itens por página; seguindo com 10)",
                  file=sys.stderr)
            return
        toggle.first.click()
        page.wait_for_timeout(400)
        # escolhe o item visível com o valor desejado (evita dropdowns de outra área)
        item = page.locator(f"#changePagination ~ * .js_customDDitem[data-value='{por_pagina}'], "
                            f".js_customDDitem[data-value='{por_pagina}']").locator("visible=true")
        if item.count() == 0:
            print(f"  (não achei a opção de {por_pagina} por página; seguindo com 10)",
                  file=sys.stderr)
            return
        antes = page.evaluate(
            "() => { const e = document.querySelector('.js_userInfo[data-id]');"
            " return e ? e.getAttribute('data-id') : null; }")
        item.first.click()
        # espera o grid recarregar
        try:
            page.wait_for_function(
                "(id) => { const e = document.querySelector('.js_userInfo[data-id]');"
                " return e && e.getAttribute('data-id') !== id; }",
                arg=antes, timeout=15000)
        except PlaywrightTimeout:
            page.wait_for_timeout(2000)
        print(f"  tamanho de página ajustado para {por_pagina}")
    except PlaywrightError as exc:
        print(f"  (não consegui mudar o tamanho da página: {exc}; seguindo com 10)",
              file=sys.stderr)


def ir_para_proxima(page, primeiro_id_atual) -> bool:
    """Clica no botão 'próxima' e espera o grid trocar. Retorna False se não deu."""
    btn = page.locator(".js_btnPaginationNext")
    if btn.count() == 0:
        return False
    classe = btn.first.get_attribute("class") or ""
    if "disabled" in classe:
        return False
    try:
        btn.first.scroll_into_view_if_needed(timeout=5000)
        btn.first.click()
        page.wait_for_function(
            "(id) => { const e = document.querySelector('.js_userInfo[data-id]');"
            " return e && e.getAttribute('data-id') !== id; }",
            arg=primeiro_id_atual, timeout=20000)
        esperar_grid(page)  # deixa o restante do grid assentar
        return True
    except PlaywrightTimeout:
        # pode ter trocado mesmo sem detectarmos; confere se o 1º id mudou
        agora = page.evaluate(
            "() => { const e = document.querySelector('.js_userInfo[data-id]');"
            " return e ? e.getAttribute('data-id') : null; }")
        return agora is not None and agora != primeiro_id_atual
    except PlaywrightError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Busca candidatos no Pandapé por palavra-chave.")
    parser.add_argument("--palavra", nargs="+", required=True,
                        help="Uma ou mais palavras-chave")
    parser.add_argument("--paginas", type=int, default=1,
                        help="Quantas páginas coletar (padrão 1). Ignorado com --todas.")
    parser.add_argument("--todas", action="store_true",
                        help="Coleta página após página até o site não ter mais resultados "
                             "(ou até --max-candidatos)")
    parser.add_argument("--max-candidatos", type=int, default=1000,
                        help="Teto de segurança para --todas (padrão 1000)")
    parser.add_argument("--por-pagina", type=int, default=100, choices=[10, 25, 50, 100],
                        help="Registros por página, Pagination[1] (o site aceita 10/25/50/100; "
                             "padrão 100)")
    parser.add_argument("--match", default="AllWords", choices=["AllWords", "AnyWords"],
                        help="Combinar as palavras: todas ou qualquer uma")
    parser.add_argument("--escopo", default="AllFields",
                        help="KeywordSearchScopeType (padrão AllFields)")
    parser.add_argument("--ordem", default="0", help="Order (padrão 0)")
    parser.add_argument("--cdp", help="Usar navegador já aberto, ex: http://localhost:9222")
    parser.add_argument("--storage-state", type=Path, default=STORAGE_STATE)
    parser.add_argument("--headless", action="store_true",
                        help="Sem janela (só com --storage-state)")
    parser.add_argument("--saida", choices=["json", "csv"], default="json")
    parser.add_argument("--dump", action="store_true",
                        help="Salva HTML e screenshot de cada página, para ajustar seletores")
    args = parser.parse_args()

    todos = []
    meta = {}

    with sync_playwright() as p:
        try:
            context, fechar = abrir_contexto(p, args)
        except (RuntimeError, PlaywrightError) as exc:
            print(f"{exc}", file=sys.stderr)
            return 1

        page = context.new_page()
        vistos = set()
        slug = re.sub(r"[^a-z0-9]+", "-", "-".join(args.palavra).lower()).strip("-")
        try:
            # Página 1: navega uma vez pela URL (só aqui a URL funciona). As
            # próximas vêm clicando em "próxima", porque o site pagina por AJAX.
            url = montar_url(args.palavra, 1, args.por_pagina,
                             args.match, args.escopo, args.ordem)
            print(f"abrindo busca: {url}")
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeout:
                pass

            if "/Account/Login" in page.url or "login" in page.url.lower():
                print("\nCaiu na tela de login — a sessão expirou.", file=sys.stderr)
                print("Rode de novo:  .venv/bin/python scripts/pandape_login.py", file=sys.stderr)
                return 1

            esperar_grid(page)
            ajustar_tamanho_pagina(page, args.por_pagina)
            esperar_grid(page)

            pagina = 0
            while True:
                pagina += 1
                dados = page.evaluate(EXTRAIR_JS)
                meta.setdefault("total_texto", dados["total_texto"])

                if args.dump or not dados["candidatos"]:
                    DUMP_DIR.mkdir(parents=True, exist_ok=True)
                    (DUMP_DIR / f"busca-{slug}-p{pagina}.html").write_text(
                        page.content(), encoding="utf-8")
                    page.screenshot(path=str(DUMP_DIR / f"busca-{slug}-p{pagina}.png"),
                                    full_page=True)

                if not dados["candidatos"]:
                    print(f"\npágina {pagina}: sem candidatos — encerrando.", file=sys.stderr)
                    if dados.get("amostra_do_corpo"):
                        print(f"  início do conteúdo: {dados['amostra_do_corpo'][:200]}",
                              file=sys.stderr)
                    break

                novos = 0
                primeiro_id = dados["candidatos"][0]["idCandidato"]
                for cand in dados["candidatos"]:
                    if cand["idCandidato"] in vistos:
                        continue
                    vistos.add(cand["idCandidato"])
                    cand["pagina"] = pagina
                    todos.append(cand)
                    novos += 1

                print(f"\npágina {pagina}: {dados['quantidade_na_pagina']} na tela, {novos} novos"
                      f"  |  acumulado: {len(todos)}"
                      + (f"  |  {dados['total_texto']} no site" if dados["total_texto"] else ""))

                # condições de parada
                if not args.todas and pagina >= args.paginas:
                    break
                if len(todos) >= args.max_candidatos:
                    print(f"  atingiu o teto de {args.max_candidatos} (--max-candidatos).")
                    break
                if not dados.get("tem_proxima", False):
                    print("  não há próxima página — fim dos resultados.")
                    break

                # clica em "próxima" e espera o grid trocar (o 1º id muda)
                if not ir_para_proxima(page, primeiro_id):
                    print("  não consegui avançar para a próxima página — encerrando.",
                          file=sys.stderr)
                    break
        finally:
            page.close()
            if fechar:
                context.close()

    print(f"\n{len(todos)} candidato(s) coletado(s).")
    if not todos:
        return 1

    SAIDA_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", "-".join(args.palavra).lower()).strip("-")
    if args.saida == "json":
        caminho = SAIDA_DIR / f"busca-{slug}.json"
        caminho.write_text(
            json.dumps({"palavras": args.palavra, **meta, "candidatos": todos},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    else:
        caminho = SAIDA_DIR / f"busca-{slug}.csv"
        campos = ["pagina", "nome", "idCandidato", "area", "cargo_atual", "empresa",
                  "tempo_experiencia", "formacao", "instituicao", "ultima_atualizacao",
                  "telefone", "email", "whatsapp", "link_curriculo"]
        with caminho.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=campos, extrasaction="ignore", delimiter=";")
            writer.writeheader()
            writer.writerows(todos)
    print(f"Arquivo: {caminho}")

    for cand in todos[:10]:
        linha = cand.get("nome") or "(sem nome)"
        if cand.get("cargo_atual"):
            linha += f"  —  {cand['cargo_atual']}"
        if cand.get("telefone"):
            linha += f"  ·  {cand['telefone']}"
        print(f"  - {linha}")
        print(f"      {cand.get('link_curriculo')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
