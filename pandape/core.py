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

Além da busca geral na base de currículos, também dá pra coletar quem se
candidatou a um processo seletivo (vaga) já publicado pela empresa:
    resultado = executar_coleta_vaga(palavra_chave_vaga="Vigilante", com_detalhes=True)
Usa os mesmos endpoints que a tela "Processos" do Pandapé usa por trás (achar
a vaga pelo título/referência, listar inscritos, abrir cada currículo) — o
currículo sai com a mesma estrutura de sempre (reaproveita EXTRAIR_DETALHE_JS).
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import date
from html import unescape
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_STATE = PROJECT_ROOT / "data" / "pandape_storage_state.json"

BASE = "https://pandape.infojobs.com.br/Company/Candidate/IndexInfojobs"
DETALHE_BASE = "https://pandape.infojobs.com.br/Company/Candidate/DetailInfojobs/"

# Candidatos inscritos num processo (vaga) já publicado — endpoints por trás
# da aba "Processos" > abrir um processo > lista de inscritos.
VACANCY_INDEX_URL = "https://pandape.infojobs.com.br/Company/Vacancy/Index"
MATCH_LIST_URL = "https://pandape.infojobs.com.br/Company/Match/ListMatches"
MATCH_DETAIL_BASE = "https://pandape.infojobs.com.br/Company/Match/Detail?id="

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


def montar_url_vagas(palavra_chave, pagina=1, por_pagina=10, ordem="1"):
    """Monta a URL da aba Processos filtrada por palavra-chave (título/referência)."""
    partes = []
    if palavra_chave:
        partes.append(f"Keywords[0]={quote(palavra_chave)}")
    partes += [
        f"Pagination[0]={pagina}",
        f"Pagination[1]={por_pagina}",
        f"Order={ordem}",
        "IdsFilter=0",
        "Geo=false",
    ]
    return f"{VACANCY_INDEX_URL}?" + "&".join(partes)


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
    experiencias.push(limpar(bloco));
  }

  const formacao = sec('DivStudiesContainer');

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

EXTRAIR_VAGAS_JS = r"""
() => {
  const limpar = t => (t || '').replace(/\s+/g, ' ').trim();
  const linhas = [...document.querySelectorAll('.js_rowvacancy')];
  const vagas = linhas.map(row => {
    const link = row.querySelector('a[href*="/Company/Match/Matches/"]');
    const href = link ? link.getAttribute('href') : null;
    const m = href ? href.match(/Matches\/(\d+)\?idvacancyfolder=(\d+)/i) : null;
    const checkbox = row.querySelector('input[data-idvacancy]');
    return {
      idVacancy: m ? m[1] : (checkbox ? checkbox.getAttribute('data-idvacancy') : null),
      idVacancyFolder: m ? m[2] : null,
      titulo: link ? limpar(link.innerText) : null,
    };
  }).filter(v => v.idVacancy && v.idVacancyFolder);

  const elTotal = document.querySelector('#textFiltered, #MatchSearchTotal');
  return { total_texto: elTotal ? limpar(elTotal.innerText) : null, vagas };
}
"""


# ------------------------------------------------------------------ período das experiências

MESES_PT = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
            "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}

_PERIODO_EXPERIENCIA = re.compile(
    r"^\s*([A-Za-zç]{3})\s+(\d{4})\s*-\s*"
    r"(?:([A-Za-zç]{3})\s+(\d{4})|(Atual|o momento|Momento atual))",
    re.IGNORECASE)


def _meses_entre(inicio: date, fim: date) -> int:
    return (fim.year - inicio.year) * 12 + (fim.month - inicio.month)


def analisar_experiencia(texto: str, hoje: date | None = None) -> dict:
    """Lê o período no começo do bloco de experiência e mede o que dá pra medir.

    O Pandapé escreve sempre no formato "Abr 2025 - Mar 2026" ou
    "Jan 2021 - Atual". Devolve início, fim, duração em meses e há quantos
    meses a pessoa saiu — None quando não dá pra saber.

    `periodo_suspeito` marca preenchimento impossível (ano 0001, fim antes do
    início, data no futuro). Acontece: o candidato digita errado no Pandapé e
    aparece coisa como "Jan 0001 - Atual (2025 anos e 10 meses)".
    """
    hoje = hoje or date.today()
    info = {"inicio": None, "fim": None, "em_andamento": False,
            "meses": None, "meses_desde_saida": None, "periodo_suspeito": False}

    m = _PERIODO_EXPERIENCIA.match(texto or "")
    if not m:
        return info

    mes_ini, ano_ini, mes_fim, ano_fim, em_curso = m.groups()
    num_ini = MESES_PT.get(mes_ini.lower())
    if not num_ini:
        return info

    ano_ini = int(ano_ini)
    if ano_ini < 1940 or ano_ini > hoje.year:
        info["periodo_suspeito"] = True
        return info

    inicio = date(ano_ini, num_ini, 1)
    info["inicio"] = inicio.isoformat()

    if em_curso:
        info["em_andamento"] = True
        info["meses"] = _meses_entre(inicio, hoje)
        return info

    num_fim = MESES_PT.get((mes_fim or "").lower())
    ano_fim = int(ano_fim)
    if not num_fim or ano_fim < 1940 or ano_fim > hoje.year + 1:
        info["periodo_suspeito"] = True
        return info

    fim = date(ano_fim, num_fim, 1)
    if fim < inicio:
        info["periodo_suspeito"] = True
        return info

    info["fim"] = fim.isoformat()
    info["meses"] = _meses_entre(inicio, fim)
    info["meses_desde_saida"] = max(0, _meses_entre(fim, hoje))
    return info


def experiencia_atende(texto: str, min_meses=None, max_meses_desde_saida=None,
                       hoje: date | None = None) -> bool:
    """A experiência passa nos critérios de tempo de casa e de saída recente?

    `max_meses_desde_saida` só faz sentido pra quem já saiu — quem continua na
    empresa (`em_andamento`) não atende, porque não há data de saída pra contar.
    Período ilegível também não atende: sem data confiável não dá pra afirmar
    que o critério foi cumprido.
    """
    info = analisar_experiencia(texto, hoje=hoje)
    if info["periodo_suspeito"] or info["inicio"] is None:
        return False
    if min_meses is not None and (info["meses"] or 0) < min_meses:
        return False
    if max_meses_desde_saida is not None:
        if info["em_andamento"] or info["meses_desde_saida"] is None:
            return False
        if info["meses_desde_saida"] > max_meses_desde_saida:
            return False
    return True


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

def buscar_vagas(context, palavra_chave, pagina=1, por_pagina=10):
    """Procura processos seletivos (vagas) pelo título/referência — a mesma
    busca da aba "Processos" do site. Devolve {total_texto, vagas:[...]},
    cada vaga com idVacancy/idVacancyFolder (precisa dos dois pra listar os
    inscritos com `coletar_candidaturas`)."""
    page = context.new_page()
    try:
        url = montar_url_vagas(palavra_chave, pagina, por_pagina)
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeout:
            pass
        if _caiu_no_login(page):
            raise SessaoExpirada("sessão do Pandapé expirou (redirecionou para login)")
        page.wait_for_timeout(600)
        return page.evaluate(EXTRAIR_VAGAS_JS)
    finally:
        page.close()


def _extrair_matches_do_fragmento(html_fragmento):
    """Extrai {idCandidato, nome, idCandidateResume, link_curriculo} de cada
    card de candidato num fragmento HTML devolvido por MATCH_LIST_URL — as
    mesmas chaves que `coletar_detalhes`/`_estruturar` já esperam, pra
    reaproveitar os dois sem mudar nada neles.

    Aqui a leitura é por regex em cima do HTML cru, não por innerText no
    navegador — então as entidades HTML chegam escapadas e precisam ser
    desfeitas na mão: sem isso, "Márcio" vira "M&#xE1;rcio" e segue assim
    pra todo lado (webhook, banco, parâmetro de template de WhatsApp).
    """
    candidatos = []
    for tag in re.finditer(r'<a\b[^>]*class="match-link[^"]*"[^>]*>', html_fragmento):
        trecho = tag.group(0)

        def attr(nome, _trecho=trecho):
            m = re.search(rf'{nome}="([^"]*)"', _trecho)
            return unescape(m.group(1)) if m else None

        id_match = attr("data-idmatch")
        if not id_match:
            continue
        candidatos.append({
            "idCandidato": id_match,
            "nome": attr("data-username"),
            "idCandidateResume": attr("data-idcandidateresume"),
            "link_curriculo": MATCH_DETAIL_BASE + id_match,
        })
    return candidatos


def coletar_candidaturas(context, id_vacancy, id_vacancy_folder, paginas=1, por_pagina=10,
                         todas=False, max_candidatos=1000, progresso=None):
    """Lista quem se candidatou a uma vaga (dado idVacancy + idVacancyFolder,
    de `buscar_vagas`) — direto via o mesmo endpoint que o site usa pra
    rolagem infinita, sem precisar abrir/rolar a tela. Devolve o mesmo
    formato de `coletar_busca`: {total_texto, candidatos:[...]}.
    """
    vistos, todos, total_texto = set(), [], None
    pagina = 0
    while True:
        pagina += 1
        resp = context.request.post(
            MATCH_LIST_URL,
            form={
                "matchesFilter.PageNumber": str(pagina),
                "matchesFilter.PageSize": str(por_pagina),
                "matchesFilter.Order": "0",
                "matchesFilter.IsLast": "False",
                "matchesFilter.IdVacancy": str(id_vacancy),
                "matchesFilter.IdVacancyFolder": str(id_vacancy_folder),
            },
        )
        if not resp.ok:
            raise SessaoExpirada(
                f"Pandapé recusou a listagem de candidatos (status {resp.status})")
        try:
            dados = resp.json()
        except Exception as exc:  # noqa: BLE001 — resposta não-JSON = sessão caiu
            raise SessaoExpirada("sessão do Pandapé expirou (ListMatches sem JSON)") from exc

        total_texto = dados.get("total", total_texto)
        candidatos_pagina = _extrair_matches_do_fragmento(dados.get("viewList", ""))
        if not candidatos_pagina:
            break

        novos = 0
        for cand in candidatos_pagina:
            if cand["idCandidato"] in vistos:
                continue
            vistos.add(cand["idCandidato"])
            cand["pagina"] = pagina
            todos.append(cand)
            novos += 1

        if progresso:
            progresso({"fase": "candidaturas", "pagina": pagina,
                       "coletados": len(todos), "total_texto": total_texto})

        if not todas and pagina >= paginas:
            break
        if len(todos) >= max_candidatos:
            break
        if novos == 0 or len(candidatos_pagina) < por_pagina:
            break  # última página
    return {"total_texto": total_texto, "candidatos": todos}


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


# DDDs que existem de fato no Brasil. O Pandapé aceita qualquer coisa no campo
# de telefone, então aparece DDD inventado (00, 10, 20...) em currículo real.
DDDS_VALIDOS = {
    11, 12, 13, 14, 15, 16, 17, 18, 19,
    21, 22, 24, 27, 28,
    31, 32, 33, 34, 35, 37, 38,
    41, 42, 43, 44, 45, 46, 47, 48, 49,
    51, 53, 54, 55,
    61, 62, 63, 64, 65, 66, 67, 68, 69,
    71, 73, 74, 75, 77, 79,
    81, 82, 83, 84, 85, 86, 87, 88, 89,
    91, 92, 93, 94, 95, 96, 97, 98, 99,
}


def motivo_telefone_invalido(telefone) -> str | None:
    """Por que esse telefone NÃO serve pra mandar WhatsApp — ou None se serve.

    O destino dos candidatos é um disparo de WhatsApp, então "válido" aqui quer
    dizer celular brasileiro no formato atual (DD + 9 dígitos começando em 9).
    Fixo e celular antigo (8 dígitos, de antes do 9º dígito) são número real de
    gente real, mas não recebem mensagem — mandar pra eles queima o lead no
    dedup sem ninguém ser contatado.
    """
    if not telefone:
        return "vazio"
    casa = re.fullmatch(r"(\d{2})-(\d{8,9})", telefone.strip())
    if not casa:
        return "fora do formato DD-NNNNNNNNN"
    ddd, numero = int(casa.group(1)), casa.group(2)
    if ddd not in DDDS_VALIDOS:
        return f"DDD {ddd:02d} não existe"
    if len(set(numero)) == 1:
        return "dígitos repetidos (preencheu o campo de qualquer jeito)"
    if len(numero) == 9:
        return None if numero[0] == "9" else "9 dígitos mas não começa com 9 (não é celular)"
    if numero[0] in "6789":
        return "celular antigo, sem o 9º dígito"
    return "telefone fixo (não tem WhatsApp)"


def normalizar_telefone(telefone):
    """Acrescenta o 9º dígito em celular antigo. Devolve (numero, corrigido?).

    Entre 2013 e 2016 a Anatel pôs um 9 na frente de todo celular do país. O
    currículo do Pandapé tem cadastro de antes disso que nunca foi atualizado:
    `11-91247848` é o mesmo aparelho que hoje atende em `11-991247848`.

    Só mexe em 8 dígitos começando em 6-9, que era a faixa de celular — fixo
    (2-5) continua fixo, e não vira celular acrescentando dígito.
    """
    if not telefone:
        return telefone, False
    casa = re.fullmatch(r"(\d{2})-(\d{8})", telefone.strip())
    if not casa or casa.group(2)[0] not in "6789":
        return telefone, False
    candidato = f"{casa.group(1)}-9{casa.group(2)}"
    # Só aceita a correção se o resultado realmente passar a valer — assim um
    # DDD inexistente continua sendo descartado em vez de virar número "bom".
    if motivo_telefone_invalido(candidato) is not None:
        return telefone, False
    return candidato, True


def _separar_telefones(telefones: list) -> tuple[list, list, list]:
    """Divide em (os que servem pra WhatsApp, os descartados, os corrigidos)."""
    bons, descartados, corrigidos = [], [], []
    for tel in telefones:
        numero, foi_corrigido = normalizar_telefone(tel)
        motivo = motivo_telefone_invalido(numero)
        if motivo is None:
            if numero in bons:
                continue  # a correção pode colidir com um número que já veio certo
            bons.append(numero)
            if foi_corrigido:
                corrigidos.append({"de": tel, "para": numero,
                                   "motivo": "celular antigo, acrescentado o 9º dígito"})
        else:
            descartados.append({"numero": tel, "motivo": motivo})
    return bons, descartados, corrigidos


def _tem_contato(cand: dict) -> bool:
    """True se o candidato tem telefone, e-mail ou WhatsApp (na lista ou no detalhe)."""
    if cand.get("email") or cand.get("whatsapp"):
        return True
    if motivo_telefone_invalido(normalizar_telefone(cand.get("telefone"))[0]) is None:
        return True
    det = cand.get("detalhe") or {}
    if det.get("emails"):
        return True
    bons, _, _ = _separar_telefones(det.get("telefones") or [])
    return bool(bons)


def _experiencia_relevante(registro: dict, min_meses=None, max_meses_desde_saida=None) -> bool:
    """O candidato tem ALGUMA experiência que atenda aos critérios de tempo?

    Basta uma — alguém com um bico de 2 meses e um emprego de 3 anos continua
    interessante por causa do emprego.
    """
    if min_meses is None and max_meses_desde_saida is None:
        return True
    exps = ((registro.get("curriculo") or {}).get("experiencias")) or []
    return any(experiencia_atende(e, min_meses, max_meses_desde_saida) for e in exps)


def _estruturar(cand: dict) -> dict:
    """Achata o candidato num objeto limpo: dados principais + contato + currículo."""
    det = cand.get("detalhe") or {}

    telefones = list(det.get("telefones") or [])
    if cand.get("telefone") and cand["telefone"] not in telefones:
        telefones.insert(0, cand["telefone"])
    # Telefone que não recebe WhatsApp (fixo, DDD inventado, 00000000) sai de
    # `telefones` e vai pra `telefones_descartados` com o motivo — assim o
    # consumidor do webhook nunca pega um número morto como se fosse bom, e
    # ninguém perde a informação de que o candidato tinha ESSE número.
    telefones_bons, telefones_descartados, telefones_corrigidos = _separar_telefones(telefones)

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
            "telefone": telefones_bons[0] if telefones_bons else None,
            "email": emails[0] if emails else None,
            "whatsapp": cand.get("whatsapp"),
            "telefones": telefones_bons,
            "telefones_descartados": telefones_descartados,
            "telefones_corrigidos": telefones_corrigidos,
            "emails": emails,
            "bloqueado": det.get("contato_bloqueado", not (telefones or emails)),
        },
    }
    if det:
        experiencias = det.get("experiencias") or []
        registro["curriculo"] = {
            "pretensao_salarial": det.get("pretensao_salarial"),
            "disponibilidade": det.get("disponibilidade"),
            "resumo": det.get("resumo"),
            "idiomas": det.get("idiomas") or [],
            "habilidades": det.get("habilidades"),
            "experiencias": experiencias,
            # Mesma lista, com o período já lido (duração, saída, se é atual).
            # Fica separada pra não mudar o formato de `experiencias`, que já
            # é consumido por fora.
            "experiencias_periodo": [
                {"texto": e, **analisar_experiencia(e)} for e in experiencias
            ],
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
                    somente_com_telefone=False,
                    storage_state=STORAGE_STATE, cdp=None, headless=True, progresso=None):
    """Coleta completa gerenciando o próprio browser. Retorna dict estruturado.

    somente_com_contato: descarta candidatos sem telefone/e-mail/WhatsApp.
    somente_com_telefone: mais estrito — exige celular que receba WhatsApp
    (ver `motivo_telefone_invalido`). Use quando o destino é disparo de
    mensagem: quem só tem e-mail ou telefone fixo não dá pra contatar, e
    mandar assim mesmo só serve pra queimar o lead no dedup.
    """
    with abrir_browser(storage_state=storage_state, cdp=cdp, headless=headless) as context:
        busca = coletar_busca(context, palavras, paginas=paginas, por_pagina=por_pagina,
                              todas=todas, max_candidatos=max_candidatos, match=match,
                              escopo=escopo, filtros=filtros, progresso=progresso)
        candidatos = busca["candidatos"]

        if com_detalhes:
            # Quando só queremos quem tem contato, filtra ANTES de abrir os
            # detalhes — senão `limite_detalhes` desperdiça abertura de perfil
            # em quem vai ser descartado no final, e quem sobrevive ao filtro
            # pode acabar sem currículo nenhum (só os dados da listagem).
            alvo_detalhes = candidatos
            if somente_com_contato:
                alvo_detalhes = [c for c in candidatos if c.get("telefone") or c.get("email") or c.get("whatsapp")]
            detalhes = coletar_detalhes(context, alvo_detalhes,
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
        antes_telefone = len(estruturados)
        if somente_com_telefone:
            estruturados = [c for c in estruturados if c["contato"]["telefone"]]

        return {
            "palavras": palavras,
            "sem_telefone_valido": antes_telefone - len(estruturados) if somente_com_telefone else None,
            "total_no_site": busca["total_texto"],
            "coletados": coletados,
            "com_contato": sum(1 for c in estruturados
                               if c["contato"]["telefone"] or c["contato"]["email"]
                               or c["contato"]["whatsapp"]),
            "retornados": len(estruturados),
            "filtrado_por_contato": somente_com_contato,
            "candidatos": estruturados,
        }


def executar_coleta_vaga(palavra_chave_vaga=None, id_vacancy=None, id_vacancy_folder=None,
                         paginas=1, por_pagina=10, todas=False, max_candidatos=1000,
                         com_detalhes=False, limite_detalhes=None, somente_com_contato=False,
                         somente_com_telefone=False,
                         min_meses_contrato=None, max_meses_desde_saida=None,
                         storage_state=STORAGE_STATE, cdp=None, headless=True, progresso=None):
    """Coleta quem se candidatou a um processo seletivo (vaga) já publicado.

    Informe id_vacancy + id_vacancy_folder diretamente se já souber (ex: veio
    de uma chamada anterior a `buscar_vagas`), ou só palavra_chave_vaga pra
    deixar escolher o primeiro processo que bater a busca por título/referência.

    Ao contrário da busca geral, a listagem de inscritos não traz telefone/
    e-mail — só dá pra saber quem tem contato depois de abrir o currículo
    (com_detalhes=True), então `somente_com_contato` aqui só filtra o
    resultado final, não evita abrir detalhe de quem não tem contato.
    """
    with abrir_browser(storage_state=storage_state, cdp=cdp, headless=headless) as context:
        vaga_usada = None
        if id_vacancy is None or id_vacancy_folder is None:
            if not palavra_chave_vaga:
                raise ValueError(
                    "informe id_vacancy + id_vacancy_folder, ou palavra_chave_vaga pra buscar")
            achadas = buscar_vagas(context, palavra_chave_vaga, por_pagina=10)
            if not achadas["vagas"]:
                return {
                    "erro": f"nenhum processo encontrado pra '{palavra_chave_vaga}'",
                    "vagas_encontradas": [],
                }
            vaga_usada = achadas["vagas"][0]
            id_vacancy = vaga_usada["idVacancy"]
            id_vacancy_folder = vaga_usada["idVacancyFolder"]

        lista = coletar_candidaturas(
            context, id_vacancy, id_vacancy_folder, paginas=paginas, por_pagina=por_pagina,
            todas=todas, max_candidatos=max_candidatos, progresso=progresso)
        candidatos = lista["candidatos"]

        if com_detalhes:
            detalhes = coletar_detalhes(context, candidatos, limite=limite_detalhes,
                                        progresso=progresso)
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
        antes_telefone = len(estruturados)
        if somente_com_telefone:
            estruturados = [c for c in estruturados if c["contato"]["telefone"]]

        antes_experiencia = len(estruturados)
        if min_meses_contrato is not None or max_meses_desde_saida is not None:
            estruturados = [c for c in estruturados
                            if _experiencia_relevante(c, min_meses_contrato,
                                                      max_meses_desde_saida)]

        return {
            "id_vacancy": id_vacancy,
            "sem_telefone_valido": antes_telefone - antes_experiencia if somente_com_telefone else None,
            "filtro_experiencia": {
                "min_meses_contrato": min_meses_contrato,
                "max_meses_desde_saida": max_meses_desde_saida,
                "descartados": antes_experiencia - len(estruturados),
            } if (min_meses_contrato is not None or max_meses_desde_saida is not None) else None,
            "id_vacancy_folder": id_vacancy_folder,
            "vaga_titulo": vaga_usada["titulo"] if vaga_usada else None,
            "total_no_site": lista["total_texto"],
            "coletados": coletados,
            "com_contato": sum(1 for c in estruturados
                               if c["contato"]["telefone"] or c["contato"]["email"]
                               or c["contato"]["whatsapp"]),
            "retornados": len(estruturados),
            "filtrado_por_contato": somente_com_contato,
            "candidatos": estruturados,
        }


def buscar_curriculo_ao_vivo(id_candidato, storage_state=STORAGE_STATE, cdp=None,
                             headless=True):
    """Abre o currículo de UM candidato direto no Pandapé, pelo id do match, e
    devolve no mesmo formato de sempre (contato + currículo estruturado).

    Serve pra quando o candidato não foi coletado com detalhe, ou quando o
    currículo mudou depois da coleta — aqui o dado vem do site na hora, não
    do banco.

    Importante: o Pandapé não permite PROCURAR currículo por telefone/e-mail.
    A página de detalhe só abre por id (`/Company/Match/Detail?id=...`), que
    é o mesmo `link_curriculo` que já vai em toda coleta. Pra achar alguém
    por telefone sem ter o id, use o banco (`db.buscar_por_contato`).
    """
    id_candidato = str(id_candidato).strip()
    if not id_candidato.isdigit():
        raise ValueError(f"id do candidato deve ser numérico: {id_candidato!r}")

    with abrir_browser(storage_state=storage_state, cdp=cdp, headless=headless) as context:
        cand = {
            "idCandidato": id_candidato,
            "link_curriculo": MATCH_DETAIL_BASE + id_candidato,
        }
        detalhes = coletar_detalhes(context, [cand])
        if not detalhes:
            return None
        detalhe = detalhes[0]
        if detalhe.get("erro"):
            raise RuntimeError(f"falha ao abrir o currículo: {detalhe['erro']}")

        cand["nome"] = detalhe.get("nome_detalhe")
        cand["detalhe"] = detalhe
        return _estruturar(cand)
