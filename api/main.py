"""API local para consultar candidatos no Pandapé.

Passa palavra-chave, recebe candidatos (busca + detalhe de cada currículo).
Como uma coleta grande leva minutos, ela roda como JOB: você dispara e depois
consulta o resultado — sem timeout de requisição.

Endpoints:
    GET  /                             formulário web (montar busca/coleta sem JSON na mão)
    GET  /health                       estado da API, sessão salva e banco de dedup
    POST /coletar                      inicia um job de coleta -> {job_id}
    GET  /job/{job_id}                 status + resultado do job
    GET  /jobs                         lista os jobs desta execução
    GET  /buscar?palavra=...           busca rápida e SÍNCRONA (só a lista, 1 página)
    POST /candidatos/marcar-usado      marca candidato(s) como usados com sucesso
    POST /candidatos/desmarcar         reverte a marca de usado
    GET  /candidatos                   lista candidatos já salvos no banco local

Subir (nesta máquina):
    .venv/bin/python -m uvicorn api.main:app --port 8090

A sessão vem de data/pandape_storage_state.json (gerada rodando os scripts uma
vez com --cdp, ou scripts/pandape_login.py). Defina PANDAPE_HEADLESS=0 para ver
o navegador enquanto a coleta roda.

Dedup: toda busca/coleta grava os candidatos no Postgres (DATABASE_URL no
.env) e, por padrão (excluir_usados=true), tira quem já foi marcado como
usado via POST /candidatos/marcar-usado — assim o mesmo candidato não volta
numa busca futura depois que você já fez algo com sucesso com ele. Sem
DATABASE_URL configurada, a API funciona normal, só sem esse dedup.

Webhook: toda coleta concluída via POST /coletar manda os candidatos (já
filtrados pelo dedup) pra WEBHOOK_URL (.env), por exemplo um Webhook Trigger
do n8n. O resultado do envio fica em job.webhook (GET /job/{job_id}). Sem
WEBHOOK_URL configurada, a coleta funciona normal, só não envia pra lugar
nenhum automaticamente.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import traceback
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from pandape import core, db

STATIC_DIR = Path(__file__).resolve().parent / "static"

STORAGE_STATE = Path(os.environ.get("PANDAPE_STORAGE_STATE", str(core.STORAGE_STATE)))
RESULTADOS_DIR = core.PROJECT_ROOT / "data" / "resultados"
HEADLESS = os.environ.get("PANDAPE_HEADLESS", "1") not in ("0", "false", "False")
CDP = os.environ.get("PANDAPE_CDP") or None
# Chave de acesso: obrigatória para os endpoints que consultam o Pandapé.
# Defina PANDAPE_API_KEY antes de expor a API. Sem ela, tudo é recusado.
API_KEY = os.environ.get("PANDAPE_API_KEY") or None
# Webhook (ex: n8n) que recebe os candidatos de cada coleta concluída.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or None

DB_DISPONIVEL = bool(db.DATABASE_URL)


def _enviar_webhook(job_id: str, resultado: dict) -> dict:
    """Manda os candidatos coletados (já filtrados pelo dedup) pro webhook
    configurado. Não derruba o job se o webhook falhar — só registra o erro.
    """
    if not WEBHOOK_URL:
        return {"enviado": False, "motivo": "WEBHOOK_URL não configurada"}
    payload = {
        "job_id": job_id,
        "enviado_em": agora(),
        "palavras": resultado.get("palavras"),
        "total_candidatos": len(resultado.get("candidatos", [])),
        "candidatos": resultado.get("candidatos", []),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return {"enviado": True, "status_http": resp.status}
    except urllib.error.HTTPError as exc:
        return {"enviado": False, "status_http": exc.code, "erro": exc.read().decode(errors="replace")[:500]}
    except Exception as exc:  # noqa: BLE001 — webhook fora do ar não pode derrubar o job
        return {"enviado": False, "erro": str(exc)}


def _persistir_e_filtrar(resultado: dict, excluir_usados: bool) -> dict:
    """Grava os candidatos coletados no banco e, se pedido, tira quem já foi
    usado com sucesso antes — evita devolver o mesmo contato duas vezes.

    Resiliente: se o banco estiver fora do ar, loga e devolve o resultado
    original sem dedup (não derruba a coleta por causa disso).
    """
    if not DB_DISPONIVEL:
        resultado["dedup_ativo"] = False
        return resultado
    try:
        db.upsert_candidatos(resultado.get("candidatos", []))
        if excluir_usados:
            ids = [c.get("id") for c in resultado.get("candidatos", []) if c.get("id")]
            nao_usados = db.filtrar_nao_usados(ids)
            originais = resultado.get("candidatos", [])
            resultado["candidatos"] = [c for c in originais if c.get("id") in nao_usados]
            resultado["excluidos_ja_usados"] = len(originais) - len(resultado["candidatos"])
            resultado["retornados"] = len(resultado["candidatos"])
        resultado["dedup_ativo"] = True
    except Exception as exc:  # noqa: BLE001 — banco fora do ar não pode derrubar a coleta
        resultado["dedup_ativo"] = False
        resultado["dedup_erro"] = str(exc)
    return resultado


def exigir_chave(x_api_key: str = Header(default="", alias="X-API-Key")):
    """Dependência: só passa com o header X-API-Key correto.

    Recusa também quando nenhuma chave foi configurada — para não expor a API
    aberta por engano. Comparação em tempo constante (evita timing attack).
    """
    if not API_KEY:
        raise HTTPException(
            503, "API sem chave configurada. Defina PANDAPE_API_KEY no servidor "
                 "antes de usar/expor esta API.")
    if not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(401, "X-API-Key ausente ou inválida.")

app = FastAPI(
    title="Pandapé — Consulta de Candidatos",
    description="Busca candidatos no Pandapé por palavra-chave e extrai os currículos.",
    version="1.0.0",
)


@app.on_event("startup")
def _startup():
    if DB_DISPONIVEL:
        try:
            db.inicializar()
        except Exception as exc:  # noqa: BLE001 — não impede a API de subir sem banco
            print(f"[aviso] não consegui inicializar o banco de dedup: {exc}")

# Um worker só: uma coleta de cada vez (um navegador por vez, evita sobrecarga).
_executor = ThreadPoolExecutor(max_workers=1)
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def agora() -> str:
    return datetime.now(timezone.utc).isoformat()


class ColetaPedido(BaseModel):
    palavra: list[str] = Field(..., min_length=1, description="Palavra(s)-chave da busca")
    paginas: int = Field(1, ge=1, le=100, description="Quantas páginas coletar")
    todas: bool = Field(False, description="Ignora 'paginas' e coleta até acabar / max_candidatos")
    por_pagina: int = Field(100, description="10, 25, 50 ou 100")
    max_candidatos: int = Field(1000, ge=1, le=10000, description="Teto para 'todas'")
    match: str = Field("AllWords", description="AllWords ou AnyWords")
    com_detalhes: bool = Field(True, description="Abrir a página de detalhe de cada candidato")
    limite_detalhes: int | None = Field(
        None, description="Quantos detalhes abrir (null = todos os coletados)")
    somente_com_contato: bool = Field(
        True, description="Retornar só candidatos com telefone/e-mail/WhatsApp")
    excluir_usados: bool = Field(
        True, description="Não devolver candidatos já marcados como usados com sucesso antes")
    # filtros (opcionais)
    estado: list[str] | None = Field(
        None, description="UF por sigla ou nome, ex.: ['GO'] ou ['Goiás','SP']")
    area: list[str] | None = Field(
        None, description="Área profissional por nome, ex.: ['Administração']")
    cidade: list[int] | None = Field(
        None, description="Código(s) de cidade do Pandapé (IdsLocation3)")
    cargo: list[int] | None = Field(
        None, description="Código(s) de cargo do Pandapé (IdsJob)")


def _atualizar(job_id: str, **campos):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(campos, atualizado=agora())


def _rodar_coleta(job_id: str, pedido: ColetaPedido):
    _atualizar(job_id, status="rodando")

    def progresso(info):
        _atualizar(job_id, progresso=info)

    try:
        filtros = core.resolver_filtros(
            estado=pedido.estado, area=pedido.area,
            cidade=pedido.cidade, cargo=pedido.cargo)
        resultado = core.executar_coleta(
            palavras=pedido.palavra,
            paginas=pedido.paginas,
            por_pagina=pedido.por_pagina,
            todas=pedido.todas,
            max_candidatos=pedido.max_candidatos,
            match=pedido.match,
            filtros=filtros,
            com_detalhes=pedido.com_detalhes,
            limite_detalhes=pedido.limite_detalhes,
            somente_com_contato=pedido.somente_com_contato,
            storage_state=STORAGE_STATE,
            cdp=CDP,
            headless=HEADLESS,
            progresso=progresso,
        )
        resultado = _persistir_e_filtrar(resultado, pedido.excluir_usados)
        webhook = _enviar_webhook(job_id, resultado)
        _atualizar(job_id, status="concluido", resultado=resultado, webhook=webhook)
        try:  # persiste em disco para inspeção/histórico
            RESULTADOS_DIR.mkdir(parents=True, exist_ok=True)
            (RESULTADOS_DIR / f"job-{job_id}.json").write_text(
                json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    except ValueError as exc:  # filtro com nome inválido
        _atualizar(job_id, status="erro", erro=str(exc), tipo_erro="filtro_invalido")
    except core.SessaoExpirada as exc:
        _atualizar(job_id, status="erro", erro=str(exc), tipo_erro="sessao_expirada")
    except Exception as exc:  # noqa: BLE001 — queremos registrar qualquer falha no job
        _atualizar(job_id, status="erro", erro=str(exc),
                   tipo_erro="falha", traceback=traceback.format_exc())


@app.get("/")
def pagina_inicial():
    """Formulário web pra montar buscas/coletas sem escrever JSON na mão."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    existe = STORAGE_STATE.exists()
    info = {
        "status": "ok",
        "sessao_salva": existe,
        "arquivo_sessao": str(STORAGE_STATE),
        "headless": HEADLESS,
        "cdp": CDP,
        "jobs_em_memoria": len(_jobs),
        "dedup_banco_configurado": DB_DISPONIVEL,
        "webhook_configurado": bool(WEBHOOK_URL),
    }
    if existe:
        idade_s = datetime.now().timestamp() - STORAGE_STATE.stat().st_mtime
        info["sessao_idade_horas"] = round(idade_s / 3600, 1)
    else:
        info["status"] = "sem_sessao"
        info["dica"] = ("gere a sessão: rode scripts/buscar.py uma vez com --cdp, "
                        "ou scripts/pandape_login.py")
    return info


@app.get("/filtros")
def filtros_disponiveis():
    """Valores aceitos nos filtros por nome (UF e Área). Cidade/cargo usam ID."""
    return {
        "estado": {"formato": "sigla ou nome", "opcoes": sorted(core.UF_CODIGOS)},
        "area": {"formato": "nome", "opcoes": sorted(
            n.title() for n in core.AREA_CODIGOS)},
        "cidade": {"formato": "código numérico (IdsLocation3)",
                   "obs": "descubra o código aplicando o filtro no site"},
        "cargo": {"formato": "código numérico (IdsJob)",
                  "exemplos": {"Assistente Administrativo": 999552,
                               "Assistente Financeiro": 1002361,
                               "Auxiliar Administrativo": 999595,
                               "Recepcionista": 999728, "Vendedor": 1001180}},
    }


@app.post("/coletar", dependencies=[Depends(exigir_chave)])
def coletar(pedido: ColetaPedido):
    if pedido.por_pagina not in (10, 25, 50, 100):
        raise HTTPException(400, "por_pagina deve ser 10, 25, 50 ou 100")
    if not STORAGE_STATE.exists() and not CDP:
        raise HTTPException(503, "sem sessão salva; veja /health para gerar")
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {"job_id": job_id, "status": "na_fila", "pedido": pedido.model_dump(),
                         "criado": agora(), "atualizado": agora(), "progresso": None,
                         "resultado": None, "erro": None}
    _executor.submit(_rodar_coleta, job_id, pedido)
    return {"job_id": job_id, "status": "na_fila",
            "consulte": f"/job/{job_id}"}


@app.get("/job/{job_id}", dependencies=[Depends(exigir_chave)])
def ver_job(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "job não encontrado")
        return dict(job)


@app.get("/jobs", dependencies=[Depends(exigir_chave)])
def listar_jobs():
    with _lock:
        return [{"job_id": j["job_id"], "status": j["status"], "criado": j["criado"],
                 "atualizado": j["atualizado"],
                 "progresso": j.get("progresso")} for j in _jobs.values()]


@app.get("/buscar", dependencies=[Depends(exigir_chave)])
def buscar_rapido(palavra: str, paginas: int = 1, por_pagina: int = 100,
                  match: str = "AllWords", excluir_usados: bool = True):
    """Busca síncrona: só a lista, sem abrir detalhes. Para poucas páginas."""
    if paginas > 5:
        raise HTTPException(400, "para muitas páginas ou detalhes, use POST /coletar (job)")
    if por_pagina not in (10, 25, 50, 100):
        raise HTTPException(400, "por_pagina deve ser 10, 25, 50 ou 100")
    if not STORAGE_STATE.exists() and not CDP:
        raise HTTPException(503, "sem sessão salva; veja /health")
    try:
        resultado = core.executar_coleta(
            palavras=[palavra], paginas=paginas, por_pagina=por_pagina, match=match,
            com_detalhes=False, storage_state=STORAGE_STATE, cdp=CDP, headless=HEADLESS)
        return _persistir_e_filtrar(resultado, excluir_usados)
    except core.SessaoExpirada as exc:
        raise HTTPException(401, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc))


class MarcarUsadoPedido(BaseModel):
    ids: list[str] = Field(..., min_length=1, description="IDs de candidato (campo 'id')")
    contexto: str | None = Field(None, description="Nota livre: onde/como foi usado")


@app.post("/candidatos/marcar-usado", dependencies=[Depends(exigir_chave)])
def marcar_usado(pedido: MarcarUsadoPedido):
    """Marca candidato(s) como usados com sucesso — não voltam mais em buscas
    futuras (com excluir_usados=true, que é o padrão)."""
    if not DB_DISPONIVEL:
        raise HTTPException(503, "DATABASE_URL não configurada — sem banco de dedup.")
    try:
        marcados = db.marcar_usados(pedido.ids, contexto=pedido.contexto)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"falha ao marcar no banco: {exc}")
    return {"marcados": marcados}


@app.post("/candidatos/desmarcar", dependencies=[Depends(exigir_chave)])
def desmarcar_usado(pedido: MarcarUsadoPedido):
    """Reverte a marca de usado (caso tenha marcado por engano)."""
    if not DB_DISPONIVEL:
        raise HTTPException(503, "DATABASE_URL não configurada — sem banco de dedup.")
    try:
        revertidos = db.desmarcar_usados(pedido.ids)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"falha ao desmarcar no banco: {exc}")
    return {"revertidos": revertidos}


@app.get("/candidatos", dependencies=[Depends(exigir_chave)])
def listar_candidatos(usado: bool | None = None, limite: int = 100, offset: int = 0):
    """Lista candidatos já persistidos no banco local — sem consultar o Pandapé."""
    if not DB_DISPONIVEL:
        raise HTTPException(503, "DATABASE_URL não configurada — sem banco de dedup.")
    try:
        return db.listar(usado=usado, limite=limite, offset=offset)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"falha ao consultar o banco: {exc}")
