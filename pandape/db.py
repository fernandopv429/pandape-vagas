"""Persistência dos candidatos coletados — evita re-consultar o mesmo
candidato duas vezes depois que ele já foi usado com sucesso em algum lugar.

Postgres compartilhado na VPS (base dedicada `pandape_candidatos`). String de
conexão em DATABASE_URL (.env).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Iterable

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidatos (
    id TEXT PRIMARY KEY,
    dados JSONB NOT NULL,
    usado BOOLEAN NOT NULL DEFAULT false,
    usado_em TIMESTAMPTZ,
    contexto TEXT,
    primeira_vista_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultima_vista_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_candidatos_usado ON candidatos (usado);
"""


@contextmanager
def _conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurada (.env) — dedup de candidatos desativado.")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn


def inicializar():
    """Cria a tabela se não existir. Chamar uma vez na subida da API."""
    with _conn() as conn:
        conn.execute(_SCHEMA)


def upsert_candidatos(candidatos: Iterable[dict]) -> int:
    """Grava/atualiza candidatos coletados. Devolve quantos foram processados."""
    candidatos = list(candidatos)
    if not candidatos:
        return 0
    with _conn() as conn:
        with conn.cursor() as cur:
            for c in candidatos:
                cid = c.get("id") or c.get("idCandidato")
                if not cid:
                    continue
                cur.execute(
                    """
                    INSERT INTO candidatos (id, dados, ultima_vista_em)
                    VALUES (%s, %s, now())
                    ON CONFLICT (id) DO UPDATE
                        SET dados = EXCLUDED.dados, ultima_vista_em = now()
                    """,
                    (str(cid), json.dumps(c, ensure_ascii=False)),
                )
    return len(candidatos)


def filtrar_nao_usados(ids: Iterable[str]) -> set[str]:
    """Dado um conjunto de IDs, devolve só os que NUNCA foram marcados como usados."""
    ids = [str(i) for i in ids]
    if not ids:
        return set()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id FROM candidatos WHERE id = ANY(%s) AND usado = true", (ids,)
        ).fetchall()
    usados = {r["id"] for r in rows}
    return {i for i in ids if i not in usados}


def marcar_usados(ids: Iterable[str], contexto: str | None = None) -> int:
    """Marca candidatos como usados com sucesso — não voltam mais em buscas futuras."""
    ids = [str(i) for i in ids]
    if not ids:
        return 0
    with _conn() as conn:
        cur = conn.execute(
            """
            UPDATE candidatos
               SET usado = true, usado_em = now(), contexto = COALESCE(%s, contexto)
             WHERE id = ANY(%s)
            """,
            (contexto, ids),
        )
        return cur.rowcount


def desmarcar_usados(ids: Iterable[str]) -> int:
    """Reverte a marca de usado (caso tenha marcado por engano)."""
    ids = [str(i) for i in ids]
    if not ids:
        return 0
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE candidatos SET usado = false, usado_em = NULL WHERE id = ANY(%s)", (ids,)
        )
        return cur.rowcount


def listar(usado: bool | None = None, limite: int = 100, offset: int = 0) -> list[dict]:
    """Lista candidatos já persistidos no banco (sem consultar o Pandapé)."""
    with _conn() as conn:
        if usado is None:
            rows = conn.execute(
                "SELECT * FROM candidatos ORDER BY ultima_vista_em DESC LIMIT %s OFFSET %s",
                (limite, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM candidatos WHERE usado = %s ORDER BY ultima_vista_em DESC LIMIT %s OFFSET %s",
                (usado, limite, offset),
            ).fetchall()
    return rows
