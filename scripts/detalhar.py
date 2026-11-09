"""Abre a página de detalhe de cada candidato de uma busca e extrai os dados.

Lê o CSV/JSON gerado por buscar.py, visita o /DetailInfojobs/{id} de cada
candidato (na sua sessão, via CDP) e coleta as informações completas do perfil.

Começe com --limite 5 para validar os campos; depois solte no volume que quiser.

Uso:
    .venv/bin/python scripts/detalhar.py --limite 5 --cdp http://localhost:9222
    .venv/bin/python scripts/detalhar.py --entrada data/resultados/busca-assistente.csv \
        --limite 5 --cdp http://localhost:9222 --dump
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_STATE = PROJECT_ROOT / "data" / "pandape_storage_state.json"
SAIDA_DIR = PROJECT_ROOT / "data" / "resultados"
DUMP_DIR = PROJECT_ROOT / "data" / "playwright-output"


# Extração no detalhe, seção por seção (cada bloco tem um id estável):
#   #ResumeContactData  dados pessoais + endereço + (contato, se liberado)
#   #Salary             pretensão salarial, jornada, tipo de contrato
#   #ResumeProfile      resumo/objetivo
#   #ResumeAdditionalInformation  disponibilidade
#   #ResumeLanguages / #ResumeSkills / #DivExperiencesContainer / #DivStudiesContainer
EXTRAIR_DETALHE_JS = r"""
() => {
  const limpar = t => (t || '').replace(/[ \t]+/g, ' ').replace(/\s*\n\s*/g, ' ').trim();
  const sec = (id) => { const e = document.getElementById(id); return e ? limpar(e.innerText) : ''; };
  const pega = (txt, re) => { const m = txt.match(re); return m ? limpar(m[1]) : null; };

  const contato = sec('ResumeContactData');
  const salario = sec('Salary');
  const adic = sec('ResumeAdditionalInformation');
  const corpoTexto = limpar((document.querySelector('main') || document.body).innerText);

  // --- dados pessoais (de #ResumeContactData) ---
  const genero = pega(contato, /\b(Mulher|Homem)\b/);
  const estado_civil = pega(contato, /\b(Solteir[ao]|Casad[ao]|Divorciad[ao]|Vi[úu]v[ao]|Uni[ãa]o est[áa]vel)\b/);
  const idade = pega(contato, /de\s+(\d{1,3})\s+anos/);
  const nascimento = pega(contato, /Nasceu\s+([^)]+?)\)/);
  const nacionalidade = pega(contato, /Nacionalidade\s+([^•\n]+?)(?:\s*•|\s*CPF|$)/);
  const cpf = pega(contato, /CPF\s+(\d{11})/);

  // endereço: "Rua ..., Bairro, Cidade - UF. CEP 00000-000"
  const mEnd = contato.match(/([^,]+?),\s*([^,]+?),\s*([^,-]+?)\s*-\s*([A-Z]{2})\.?\s*(?:CEP\s*([\d-]+))?/);
  const endereco = mEnd ? limpar(mEnd[1]) : null;
  const bairro   = mEnd ? limpar(mEnd[2]) : null;
  const cidade   = mEnd ? limpar(mEnd[3]) : null;
  const uf       = mEnd ? mEnd[4] : null;
  const cep      = mEnd ? (mEnd[5] || null) : null;

  // --- pretensão / jornada / contrato (de #Salary) ---
  const pretensao = pega(salario, /Pretens[ãa]o salarial\s+(.+?\))/) ||
                    pega(salario, /Pretens[ãa]o salarial\s+([^]+?)(?:Jornada|$)/);
  const jornada = pega(salario, /Jornada\s+(.+?)(?:\s+Contrato|$)/);
  const contrato = pega(salario, /Contrato\s+(.+?)(?:\s+Redes|$)/);

  // --- disponibilidade (de #ResumeAdditionalInformation) ---
  let disponibilidade = null;
  const mDisp = adic.match(/(Possui disponibilidade[^]*?)(?:Agradecemos|$)/i);
  if (mDisp) disponibilidade = limpar(mDisp[1]);

  // --- resumo/objetivo ---
  let resumo = sec('ResumeProfile');
  resumo = resumo.replace(/^Perfil e objetivos\s*/i, '').replace(/^Resumo\s*/i, '');

  // --- idiomas: só as linhas "Idioma (Nível)" ---
  const idiomas = [...new Set((sec('ResumeLanguagesContainer')
      .match(/[A-Za-zÀ-ÿ]+\s*\((?:Nativo|B[áa]sico|Intermedi[áa]rio|Avan[çc]ado|Fluente)\)/g) || []))];

  // --- habilidades ---
  let habilidades = sec('ResumeSkillsContainer');

  // --- experiências: cada entrada começa com um período "Mês AAAA - ..." ---
  const expTxt = sec('DivExperiencesContainer');
  const reper = /[A-Z][a-zç]{2}\s+\d{4}\s*-\s*(?:[A-Z][a-zç]{2}\s+\d{4}|o momento|Atual|Momento atual)/g;
  const cortes = [];
  let mm; while ((mm = reper.exec(expTxt)) !== null) cortes.push(mm.index);
  const experiencias = [];
  for (let i = 0; i < cortes.length; i++) {
    const bloco = expTxt.slice(cortes[i], cortes[i + 1] ?? expTxt.length);
    experiencias.push(limpar(bloco).slice(0, 400));
  }

  // --- formação ---
  const formacao = sec('DivStudiesContainer').slice(0, 800);

  // --- contato: liberado ou bloqueado? ---
  const bloqueado = /Visualizar dados de contato/i.test(corpoTexto);
  const emails = [...new Set((corpoTexto.match(/[\w.+-]+@[\w-]+\.[\w.-]+/g) || []))];
  // telefone no formato "41-985253119" (evita o CPF de 11 dígitos)
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
    texto_detalhe: corpoTexto.slice(0, 4000),
  };
}
"""


def carregar_candidatos(entrada: Path):
    """Lê a saída da busca (CSV ou JSON) e devolve lista de dicts com link_curriculo."""
    if entrada.suffix == ".json":
        dados = json.loads(entrada.read_text(encoding="utf-8"))
        return dados.get("candidatos", dados if isinstance(dados, list) else [])
    with entrada.open(encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def achar_entrada_padrao() -> Path | None:
    candidatos = sorted(SAIDA_DIR.glob("busca-*.csv")) + sorted(SAIDA_DIR.glob("busca-*.json"))
    return candidatos[-1] if candidatos else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrai dados da página de detalhe de cada candidato.")
    parser.add_argument("--entrada", type=Path, help="CSV/JSON da busca (padrão: o mais recente)")
    parser.add_argument("--limite", type=int, default=5, help="Quantos candidatos abrir (padrão 5)")
    parser.add_argument("--cdp", help="Navegador já aberto, ex: http://localhost:9222")
    parser.add_argument("--storage-state", type=Path, default=STORAGE_STATE)
    parser.add_argument("--saida", choices=["json", "csv"], default="json")
    parser.add_argument("--dump", action="store_true",
                        help="Salva o HTML de cada detalhe (recomendado nas primeiras vezes)")
    parser.add_argument("--pausa", type=int, default=800,
                        help="Pausa em ms entre candidatos (padrão 800)")
    args = parser.parse_args()

    entrada = args.entrada or achar_entrada_padrao()
    if not entrada or not entrada.exists():
        print("Não achei a saída da busca. Rode buscar.py primeiro, ou passe --entrada.",
              file=sys.stderr)
        return 1

    candidatos = carregar_candidatos(entrada)[: args.limite]
    if not candidatos:
        print(f"Nenhum candidato em {entrada}.", file=sys.stderr)
        return 1
    print(f"Entrada: {entrada}  |  abrindo {len(candidatos)} candidato(s)")

    resultados = []
    with sync_playwright() as p:
        if args.cdp:
            browser = p.chromium.connect_over_cdp(args.cdp)
            if not browser.contexts:
                print("navegador sem contexto aberto", file=sys.stderr)
                return 1
            context = browser.contexts[0]
            fechar = False
            # salva a sessão para as próximas execuções dispensarem a porta CDP
            try:
                args.storage_state.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(args.storage_state))
                print(f"(sessão salva em {args.storage_state.name} — "
                      "próximas vezes pode rodar sem --cdp)")
            except PlaywrightError:
                pass
        else:
            if not args.storage_state.exists():
                print(f"sessão não encontrada em {args.storage_state}", file=sys.stderr)
                return 1
            browser = p.chromium.launch(channel="chrome", headless=False)
            context = browser.new_context(storage_state=str(args.storage_state),
                                          viewport={"width": 1440, "height": 900})
            fechar = True

        page = context.new_page()
        try:
            for i, cand in enumerate(candidatos, 1):
                link = cand.get("link_curriculo") or cand.get("link")
                if not link:
                    continue
                print(f"\n[{i}/{len(candidatos)}] {cand.get('nome', '?')}  {link}")
                try:
                    page.goto(link, wait_until="domcontentloaded")
                    try:
                        page.wait_for_load_state("networkidle", timeout=20000)
                    except PlaywrightTimeout:
                        pass
                    page.wait_for_timeout(1500)

                    if "/Account/Login" in page.url or "login" in page.url.lower():
                        print("  caiu no login — sessão sem acesso a este candidato.",
                              file=sys.stderr)
                        continue

                    det = page.evaluate(EXTRAIR_DETALHE_JS)

                    if args.dump:
                        DUMP_DIR.mkdir(parents=True, exist_ok=True)
                        sid = cand.get("idCandidato") or re.sub(r"\D", "", link)[-8:]
                        (DUMP_DIR / f"detalhe-{sid}.html").write_text(
                            page.content(), encoding="utf-8")

                    registro = {
                        "idCandidato": cand.get("idCandidato"),
                        "nome_busca": cand.get("nome"),
                        "link_curriculo": link,
                        **det,
                    }
                    resultados.append(registro)
                    print(f"  nome: {det.get('nome_detalhe')}  | {det.get('idade')} anos  | "
                          f"{det.get('cidade')}-{det.get('uf')}")
                    print(f"  pretensão: {det.get('pretensao_salarial')}  | {det.get('contrato')}")
                    print(f"  e-mails: {det.get('emails')}  | tels: {det.get('telefones')}"
                          f"  | contato_bloqueado: {det.get('contato_bloqueado')}")
                    print(f"  experiências: {len(det.get('experiencias') or [])}  | "
                          f"idiomas: {det.get('idiomas')}")
                except PlaywrightError as exc:
                    print(f"  erro ao abrir: {exc}", file=sys.stderr)
                page.wait_for_timeout(args.pausa)
        finally:
            page.close()
            if fechar:
                browser.close()

    if not resultados:
        print("\nNada extraído.", file=sys.stderr)
        return 1

    SAIDA_DIR.mkdir(parents=True, exist_ok=True)
    base = entrada.stem.replace("busca-", "detalhe-")
    if args.saida == "json":
        caminho = SAIDA_DIR / f"{base}.json"
        caminho.write_text(json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        caminho = SAIDA_DIR / f"{base}.csv"
        campos = [k for k in resultados[0].keys() if k != "texto_detalhe"]
        with caminho.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=campos, extrasaction="ignore", delimiter=";")
            writer.writeheader()
            for r in resultados:
                linha = {k: v for k, v in r.items() if k != "texto_detalhe"}
                # listas viram texto separado por " | " para caber na célula
                for k, v in linha.items():
                    if isinstance(v, list):
                        linha[k] = " | ".join(str(x) for x in v)
                writer.writerow(linha)
    print(f"\n{len(resultados)} detalhe(s) salvos em: {caminho}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
