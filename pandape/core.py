"""Núcleo de scraping do Pandapé — usado pela API e pelos scripts de CLI.

Fonte única da lógica de busca e de extração de detalhe. Não tem argparse nem
print de CLI: expõe funções que recebem um contexto Playwright e devolvem dados.

Fluxo de alto nível:
    with abrir_browser(...) as (contexto, fechar):
        busca = coletar_busca(contexto, ["assistente"], paginas=3, por_pagina=100)
        enriquecidos = coletar_detalhes(contexto, busca["candidatos"][:5])

Ou tudo de uma vez, gerenciando o browser sozinho:
    resultado = executar_coleta(["assistente"], paginas=3, com_detalhes=True,
                                limite_detalhes=5)
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_STATE = PROJECT_ROOT / "data" / "pandape_storage_state.json"

BASE = "https://pandape.infojobs.com.br/Company/Candidate/IndexInfojobs"
DETALHE_BASE = "https://pandape.infojobs.com.br/Company/Candidate/DetailInfojobs/"

# Filtros que a URL manda como "None" quando não estão em uso.
FILTROS_NEUTROS = {
    "ExperienceCtInProgress": "None",
    "HasEvaluationComments": "None",
    "CandidateAssignmentType": "None",
    "ReadCvStatus": "None",
    "StudyingStatus": "None",
    "PhotoFilterCt": "None",
}

# Estado (UF) -> código interno do Pandapé (param IdsLocation2). Lista completa.
UF_CODIGOS = {
    "AC": 165, "AL": 166, "AP": 167, "AM": 168, "BA": 169, "CE": 170, "DF": 171,
    "ES": 172, "GO": 190, "MA": 173, "MT": 174, "MS": 175, "MG": 176, "PA": 177,
    "PB": 178, "PR": 179, "PE": 180, "PI": 181, "RJ": 182, "RN": 183, "RS": 184,
    "RO": 185, "RR": 186, "SC": 187, "SE": 188, "SP": 64, "TO": 189,
}

# Área Profissional -> código (param ExperienceIdsCategory1). Nomes em minúsculo.
# Parcial (o que apareceu na busca observada); ampliável.
AREA_CODIGOS = {
    "administração": 51, "agricultura, pecuária, veterinária": 52,
    "alimentação / gastronomia": 92, "arquitetura, decoração, design": 55,
    "artes": 56, "auditoria": 57, "ciências, pesquisa": 58,
    "comercial, vendas": 59, "comércio exterior, importação, exportação": 60,
    "compras": 62, "comunicação, tv, cinema": 63,
    "construção, manutenção": 64, "contábil, finanças, economia": 66,
    "cultura, lazer, entretenimento": 67, "educação, ensino, idiomas": 69,
    "engenharia": 70, "estética": 71, "hotelaria, turismo": 72,
    "industrial, produção, fábrica": 73, "informática, ti, telecomunicações": 74,
}


# Filtros de lista: nome amigável -> parâmetro na URL (valores são IDs numéricos)
FILTROS_LISTA = {
    "estado": "IdsLocation2",      # UF
    "cidade": "IdsLocation3",      # município
    "area": "ExperienceIdsCategory1",  # área profissional
    "cargo": "IdsJob",             # cargo desejado
}


def resolver_filtros(estado=None, cidade=None, area=None, cargo=None) -> dict:
    """Converte nomes/siglas em IDs. Aceita já-IDs também. Retorna {chave: [ids]}.

    - estado: sigla (ex. "GO") ou nome; resolvido por UF_CODIGOS
    - area:   nome (ex. "Administração"); resolvido por AREA_CODIGOS
    - cidade, cargo: só IDs numéricos (as listas são grandes e dinâmicas)
    Levanta ValueError com dica quando um nome não é reconhecido.
    """
    nome_para_uf = {v.lower(): k for k, v in {
        "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia",
        "CE": "Ceará", "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás",
        "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul",
        "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná",
        "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro",
        "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rondônia",
        "RR": "Roraima", "SC": "Santa Catarina", "SE": "Sergipe", "SP": "São Paulo",
        "TO": "Tocantins"}.items()}

    def como_ids(valor, tabela=None, rotulo=""):
        if valor is None:
            return []
        itens = valor if isinstance(valor, (list, tuple)) else [valor]
        ids = []
        for it in itens:
            s = str(it).strip()
            if s.isdigit():
                ids.append(int(s))
            elif tabela is not None:
                chave = s.upper() if tabela is UF_CODIGOS else s.lower()
                if tabela is UF_CODIGOS and chave not in tabela and s.lower() in nome_para_uf:
                    chave = nome_para_uf[s.lower()]
                if chave in tabela:
                    ids.append(tabela[chave])
                else:
                    validos = ", ".join(sorted(tabela)[:8]) + "…"
                    raise ValueError(f"{rotulo} '{it}' não reconhecido. Ex.: {validos}")
            else:
                raise ValueError(f"{rotulo} deve ser ID numérico: '{it}'")
        return ids

    filtros = {}
    if estado is not None:
        filtros["estado"] = como_ids(estado, UF_CODIGOS, "estado")
    if area is not None:
        filtros["area"] = como_ids(area, AREA_CODIGOS, "área")
    if cidade is not None:
        filtros["cidade"] = como_ids(cidade, None, "cidade (ID)")
    if cargo is not None:
        filtros["cargo"] = como_ids(cargo, None, "cargo (ID)")
    return filtros


def montar_url(palavras, pagina, por_pagina, match="AllWords",
               escopo="AllFields", ordem="0", filtros=None, extras=None):
    """Monta a URL de busca. Os colchetes ficam literais, como o site usa.

    filtros: {'estado': [190], 'area': [51], 'cidade': [...], 'cargo': [...]}
    """
    partes = [f"{chave}={valor}" for chave, valor in FILTROS_NEUTROS.items()]
    for i, palavra in enumerate(palavras):
        partes.append(f"Keywords[{i}]={quote(palavra)}")
    # filtros de lista viram Param[0]=id&Param[1]=id...
    for chave, ids in (filtros or {}).items():
        param = FILTROS_LISTA.get(chave, chave)
        for i, valor in enumerate(ids):
            partes.append(f"{param}[{i}]={valor}")
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


# ------------------------------------------------------------------ JS de extração

EXTRAIR_JS = r"""
() => {
  const limpar = t => (t || '').replace(/\s+/g, ' ').trim();
  const txt = (raiz, sel) => { const e = raiz.querySelector(sel); return e ? limpar(e.innerText) : null; };

  const linhas = [...document.querySelectorAll('div.js_userInfo[data-id], div.js_row[data-id]')];

  const candidatos = linhas.map(row => {
    const id = row.getAttribute('data-id');
    const alvo = limpar(row.innerText);

    const email = (alvo.match(/[\w.+-]+@[\w-]+\.[\w.-]+/) || [])[0] || null;
    const telefone = (alvo.match(/\b\d{2}-\d{8,9}\b/) || [])[0] || null;

    const aWpp = row.querySelector('a[href*="wa.me"], a[href*="api.whatsapp"]');
    const whatsapp = aWpp ? aWpp.getAttribute('href') : null;

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

  const elTotal = document.querySelector('#MatchSearchTotal');
  const total = elTotal ? limpar(elTotal.innerText) : null;

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

EXTRAIR_DETALHE_JS = r"""
() => {
  const limpar = t => (t || '').replace(/[ \t]+/g, ' ').replace(/\s*\n\s*/g, ' ').trim();
  const sec = (id) => { const e = document.getElementById(id); return e ? limpar(e.innerText) : ''; };
  const pega = (txt, re) => { const m = txt.match(re); return m ? limpar(m[1]) : null; };

  const contato = sec('ResumeContactData');
  const salario = sec('Salary');
  const adic = sec('ResumeAdditionalInformation');
  const corpoTexto = limpar((document.querySelector('main') || document.body).innerText);

  const genero = pega(contato, /\b(Mulher|Homem)\b/);
  const estado_civil = pega(contato, /\b(Solteir[ao]|Casad[ao]|Divorciad[ao]|Vi[úu]v[ao]|Uni[ãa]o est[áa]vel)\b/);
  const idade = pega(contato, /de\s+(\d{1,3})\s+anos/);
  const nascimento = pega(contato, /Nasceu\s+([^)]+?)\)/);
  const nacionalidade = pega(contato, /Nacionalidade\s+([^•\n]+?)(?:\s*•|\s*CPF|$)/);
  const cpf = pega(contato, /CPF\s+(\d{11})/);

  const mEnd = contato.match(/([^,]+?),\s*([^,]+?),\s*([^,-]+?)\s*-\s*([A-Z]{2})\.?\s*(?:CEP\s*([\d-]+))?/);
  const endereco = mEnd ? limpar(mEnd[1]) : null;
  const bairro   = mEnd ? limpar(mEnd[2]) : null;
  const cidade   = mEnd ? limpar(mEnd[3]) : null;
  const uf       = mEnd ? mEnd[4] : null;
  const cep      = mEnd ? (mEnd[5] || null) : null;

  const pretensao = pega(salario, /Pretens[ãa]o salarial\s+(.+?\))/) ||
                    pega(salario, /Pretens[ãa]o salarial\s+([^]+?)(?:Jornada|$)/);
  const jornada = pega(salario, /Jornada\s+(.+?)(?:\s+Contrato|$)/);
  const contrato = pega(salario, /Contrato\s+(.+?)(?:\s+Redes|$)/);

  let disponibilidade = null;
  const mDisp = adic.match(/(Possui disponibilidade[^]*?)(?:Agradecemos|$)/i);
  if (mDisp) disponibilidade = limpar(mDisp[1]);

  let resumo = sec('ResumeProfile');
  resumo = resumo.replace(/^Perfil e objetivos\s*/i, '').replace(/^Resumo\s*/i, '');

  const idiomas = [...new Set((sec('ResumeLanguagesContainer')
      .match(/[A-Za-zÀ-ÿ]+\s*\((?:Nativo|B[áa]sico|Intermedi[áa]rio|Avan[çc]ado|Fluente)\)/g) || []))];

  let habilidades = sec('ResumeSkillsContainer');

  const expTxt = sec('DivExperiencesContainer');
  const reper = /[A-Z][a-zç]{2}\s+\d{4}\s*-\s*(?:[A-Z][a-zç]{2}\s+\d{4}|o momento|Atual|Momento atual)/g;
  const cortes = [];
  let mm; while ((mm = reper.exec(expTxt)) !== null) cortes.push(mm.index);
  const experiencias = [];
  for (let i = 0; i < cortes.length; i++) {
    const bloco = expTxt.slice(cortes[i], cortes[i + 1] ?? expTxt.length);
    experiencias.push(limpar(bloco).slice(0, 400));
  }

  const formacao = sec('DivStudiesContainer').slice(0, 800);

  const bloqueado = /Visualizar dados de contato/i.test(corpoTexto);
  const emails = [...new Set((corpoTexto.match(/[\w.+-]+@[\w-]+\.[\w.-]+/g) || []))];
  const telefones = [...new Set((corpoTexto.match(/\b\d{2}-\d{8,9}\b/g) || []))];

  const nome = pega(contato, /Dados pessoais e de contato\s+(.+?)\s+(?:Mulher|Homem)/) ||
               (document.querySelector('h1, h2') ? limpar(document.querySelector('h1, h2').innerText) : null);

  return {
    nome_detalhe: nome,
    genero, estado_civil, idade, nascimento, nacionalidade, cpf,
    endereco, bairro, cidade, uf, cep,
    pretensao_salarial: pretensao, jornada, contrato,
    disponibilidade, resumo, idiomas, habilidades,
    experiencias, formacao,
    emails, telefones, contato_bloqueado: bloqueado,
  };
}
"""


# ------------------------------------------------------------------ helpers de página

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
            if estavel >= 2:
                return atual
        else:
            estavel = 0
        anterior = atual
        page.wait_for_timeout(int(passo * 1000))
        decorrido += passo
    return anterior


def ajustar_tamanho_pagina(page, por_pagina, log=lambda *_: None):
    """Seleciona 10/25/50/100 por página pelo dropdown do site, se possível."""
    if por_pagina == 10:
        return
    try:
        toggle = page.locator("#changePagination")
        if toggle.count() == 0:
            log("nao_achei_dropdown_tamanho")
            return
        toggle.first.click()
        page.wait_for_timeout(400)
        item = page.locator(
            f"#changePagination ~ * .js_customDDitem[data-value='{por_pagina}'], "
            f".js_customDDitem[data-value='{por_pagina}']").locator("visible=true")
        if item.count() == 0:
            log(f"nao_achei_opcao_{por_pagina}")
            return
        antes = page.evaluate(
            "() => { const e = document.querySelector('.js_userInfo[data-id]');"
            " return e ? e.getAttribute('data-id') : null; }")
        item.first.click()
        try:
            page.wait_for_function(
                "(id) => { const e = document.querySelector('.js_userInfo[data-id]');"
                " return e && e.getAttribute('data-id') !== id; }",
                arg=antes, timeout=15000)
        except PlaywrightTimeout:
            page.wait_for_timeout(2000)
        log(f"tamanho_pagina={por_pagina}")
    except PlaywrightError as exc:
        log(f"erro_tamanho_pagina:{exc}")


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
        esperar_grid(page)
        return True
    except PlaywrightTimeout:
        agora = page.evaluate(
            "() => { const e = document.querySelector('.js_userInfo[data-id]');"
            " return e ? e.getAttribute('data-id') : null; }")
        return agora is not None and agora != primeiro_id_atual
    except PlaywrightError:
        return False


class SessaoExpirada(RuntimeError):
    """Levantada quando o Pandapé redireciona para o login."""


def _caiu_no_login(page) -> bool:
    return "/Account/Login" in page.url or "login" in page.url.lower()


# ------------------------------------------------------------------ coleta de alto nível

def coletar_busca(context, palavras, paginas=1, por_pagina=100, todas=False,
                  max_candidatos=1000, match="AllWords", escopo="AllFields",
                  ordem="0", filtros=None, progresso=None):
    """Percorre as páginas de resultado e devolve {total_texto, candidatos:[...]}.

    progresso(dict) é chamado a cada página, se fornecido.
    """
    page = context.new_page()
    vistos, todos, meta = set(), [], {}
    try:
        url = montar_url(palavras, 1, por_pagina, match, escopo, ordem, filtros=filtros)
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeout:
            pass
        if _caiu_no_login(page):
            raise SessaoExpirada("sessão do Pandapé expirou (redirecionou para login)")

        esperar_grid(page)
        ajustar_tamanho_pagina(page, por_pagina)
        esperar_grid(page)

        pagina = 0
        while True:
            pagina += 1
            dados = page.evaluate(EXTRAIR_JS)
            meta.setdefault("total_texto", dados["total_texto"])
            if not dados["candidatos"]:
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

            if progresso:
                progresso({"fase": "busca", "pagina": pagina,
                           "coletados": len(todos), "total_texto": meta.get("total_texto")})

            if not todas and pagina >= paginas:
                break
            if len(todos) >= max_candidatos:
                break
            if not dados.get("tem_proxima", False):
                break
            if not ir_para_proxima(page, primeiro_id):
                break
        return {"total_texto": meta.get("total_texto"), "candidatos": todos}
    finally:
        page.close()


def coletar_detalhes(context, candidatos, limite=None, pausa_ms=800, progresso=None):
    """Abre o detalhe de cada candidato e devolve a lista enriquecida."""
    alvo = candidatos[:limite] if limite else candidatos
    page = context.new_page()
    resultados = []
    try:
        for i, cand in enumerate(alvo, 1):
            link = cand.get("link_curriculo") or cand.get("link")
            if not link:
                continue
            try:
                page.goto(link, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except PlaywrightTimeout:
                    pass
                page.wait_for_timeout(1200)
                if _caiu_no_login(page):
                    raise SessaoExpirada("sessão do Pandapé expirou ao abrir um detalhe")
                det = page.evaluate(EXTRAIR_DETALHE_JS)
                resultados.append({
                    "idCandidato": cand.get("idCandidato"),
                    "nome_busca": cand.get("nome"),
                    "link_curriculo": link,
                    **det,
                })
            except PlaywrightError as exc:
                resultados.append({
                    "idCandidato": cand.get("idCandidato"),
                    "link_curriculo": link,
                    "erro": str(exc),
                })
            if progresso:
                progresso({"fase": "detalhe", "processados": i, "total": len(alvo)})
            page.wait_for_timeout(pausa_ms)
        return resultados
    finally:
        page.close()


@contextmanager
def abrir_browser(storage_state=STORAGE_STATE, cdp=None, headless=True):
    """Context manager que entrega um contexto Playwright pronto e logado.

    - cdp: conecta num navegador já aberto (http://localhost:9222) e reusa a sessão.
    - senão: sobe o próprio Chrome usando o storage_state salvo.
    Ao conectar via CDP, atualiza o storage_state em disco para uso posterior.
    """
    storage_state = Path(storage_state)
    with sync_playwright() as p:
        if cdp:
            browser = p.chromium.connect_over_cdp(cdp)
            if not browser.contexts:
                raise RuntimeError("navegador conectado, mas sem contexto aberto")
            context = browser.contexts[0]
            try:
                storage_state.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(storage_state))
            except PlaywrightError:
                pass
            try:
                yield context
            finally:
                pass  # navegador é do usuário; não fechamos
        else:
            if not storage_state.exists():
                raise RuntimeError(
                    f"sessão não encontrada em {storage_state}. Gere com "
                    "scripts/pandape_login.py ou rode uma vez com cdp=...")
            browser = p.chromium.launch(channel="chrome", headless=headless)
            context = browser.new_context(
                storage_state=str(storage_state), viewport={"width": 1440, "height": 900})
            try:
                yield context
            finally:
                context.close()
                browser.close()


def _tem_contato(cand: dict) -> bool:
    """True se o candidato tem telefone, e-mail ou WhatsApp (na lista ou no detalhe)."""
    if cand.get("telefone") or cand.get("email") or cand.get("whatsapp"):
        return True
    det = cand.get("detalhe") or {}
    return bool(det.get("telefones") or det.get("emails"))


def _estruturar(cand: dict) -> dict:
    """Achata o candidato num objeto limpo: dados principais + contato + currículo."""
    det = cand.get("detalhe") or {}

    telefones = list(det.get("telefones") or [])
    if cand.get("telefone") and cand["telefone"] not in telefones:
        telefones.insert(0, cand["telefone"])
    emails = list(det.get("emails") or [])
    if cand.get("email") and cand["email"] not in emails:
        emails.insert(0, cand["email"])

    # cidade/UF: usa o do detalhe; se vazio (perfil anônimo), tenta a 1ª experiência
    cidade, uf = det.get("cidade"), det.get("uf")
    if not uf:
        for exp in det.get("experiencias") or []:
            m = re.search(r"([A-Za-zÀ-ÿ .'-]+?)\s*-\s*([A-Z]{2})\b", exp)
            if m:
                cidade = cidade or m.group(1).strip()
                uf = m.group(2)
                break

    registro = {
        "id": cand.get("idCandidato"),
        "perfil": cand.get("nome"),
        "cargo_atual": cand.get("cargo_atual"),
        "area": cand.get("area"),
        "empresa": cand.get("empresa"),
        "tempo_experiencia": cand.get("tempo_experiencia"),
        "formacao": cand.get("formacao"),
        "instituicao": cand.get("instituicao"),
        "cidade": cidade,
        "uf": uf,
        "atualizado_em": cand.get("ultima_atualizacao"),
        "link_curriculo": cand.get("link_curriculo"),
        "contato": {
            "telefone": telefones[0] if telefones else None,
            "email": emails[0] if emails else None,
            "whatsapp": cand.get("whatsapp"),
            "telefones": telefones,
            "emails": emails,
            "bloqueado": det.get("contato_bloqueado", not (telefones or emails)),
        },
    }
    if det:
        registro["curriculo"] = {
            "pretensao_salarial": det.get("pretensao_salarial"),
            "disponibilidade": det.get("disponibilidade"),
            "resumo": det.get("resumo"),
            "idiomas": det.get("idiomas") or [],
            "habilidades": det.get("habilidades"),
            "experiencias": det.get("experiencias") or [],
            "formacao_completa": det.get("formacao"),
            "dados_pessoais": {
                "genero": det.get("genero"),
                "estado_civil": det.get("estado_civil"),
                "idade": det.get("idade"),
                "nascimento": det.get("nascimento"),
            },
        }
    return registro


def executar_coleta(palavras, paginas=1, por_pagina=100, todas=False, max_candidatos=1000,
                    match="AllWords", escopo="AllFields", filtros=None, com_detalhes=False,
                    limite_detalhes=None, somente_com_contato=False,
                    storage_state=STORAGE_STATE, cdp=None, headless=True, progresso=None):
    """Coleta completa gerenciando o próprio browser. Retorna dict estruturado.

    somente_com_contato: descarta candidatos sem telefone/e-mail/WhatsApp.
    """
    with abrir_browser(storage_state=storage_state, cdp=cdp, headless=headless) as context:
        busca = coletar_busca(context, palavras, paginas=paginas, por_pagina=por_pagina,
                              todas=todas, max_candidatos=max_candidatos, match=match,
                              escopo=escopo, filtros=filtros, progresso=progresso)
        candidatos = busca["candidatos"]

        if com_detalhes:
            detalhes = coletar_detalhes(context, candidatos,
                                        limite=limite_detalhes, progresso=progresso)
            por_id = {d.get("idCandidato"): d for d in detalhes}
            for cand in candidatos:
                det = por_id.get(cand["idCandidato"])
                if det:
                    cand["detalhe"] = det

        estruturados = [_estruturar(c) for c in candidatos]
        coletados = len(estruturados)
        if somente_com_contato:
            estruturados = [c for c in estruturados
                            if c["contato"]["telefone"] or c["contato"]["email"]
                            or c["contato"]["whatsapp"]]

        return {
            "palavras": palavras,
            "total_no_site": busca["total_texto"],
            "coletados": coletados,
            "com_contato": sum(1 for c in estruturados
                               if c["contato"]["telefone"] or c["contato"]["email"]
                               or c["contato"]["whatsapp"]),
            "retornados": len(estruturados),
            "filtrado_por_contato": somente_com_contato,
            "candidatos": estruturados,
        }
