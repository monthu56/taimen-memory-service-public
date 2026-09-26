"""Трейс компиляций контекста: почему собран именно такой ContextPack (ADR-016 §32)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from platform_memory.core.namespaces import resolve_namespace, resolve_namespaces


class ContextTraceStore:
    """Таблица трейсов компиляций: request-эхо (без секретов) + retrieval-решения."""

    def __init__(self, conn: psycopg.Connection, table: str, default_namespace: str = ""):
        """Инициализировать стор на соединении и таблице трейсов."""
        self.conn = conn
        self.table = table
        self.default_namespace = resolve_namespace(default_namespace)

    @property
    def _tbl(self) -> sql.Identifier:
        return sql.Identifier("public", self.table)

    def ensure_schema(self) -> None:
        """Создать таблицу трейсов (идемпотентно, аддитивно)."""
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {tbl} (
                        id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        trace_id   text NOT NULL,
                        namespace  text NOT NULL,
                        request    jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                        decisions  jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                ).format(tbl=self._tbl)
            )
            cur.execute(
                sql.SQL(
                    "CREATE UNIQUE INDEX IF NOT EXISTS {ix} ON {tbl} (namespace, trace_id)"
                ).format(ix=sql.Identifier(f"{self.table}_ns_trace_key"), tbl=self._tbl)
            )

    def record(
        self,
        trace_id: str,
        namespace: str,
        request: dict[str, Any],
        decisions: dict[str, Any],
    ) -> None:
        """Сохранить трейс компиляции (идемпотентно по trace_id)."""
        ns = resolve_namespace(namespace, self.default_namespace)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {tbl} (trace_id, namespace, request, decisions) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (namespace, trace_id) DO NOTHING"
                ).format(tbl=self._tbl),
                (trace_id, ns, Jsonb(request), Jsonb(decisions)),
            )

    def get(self, trace_id: str, namespaces: Sequence[str] = ()) -> dict[str, Any] | None:
        """Вернуть трейс по id в области видимости namespaces, либо None."""
        nss = resolve_namespaces(namespaces, self.default_namespace)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT trace_id, namespace, request, decisions, created_at::text "
                    "FROM {tbl} WHERE trace_id = %s AND namespace = ANY(%s)"
                ).format(tbl=self._tbl),
                (trace_id, nss),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "trace_id": row[0],
            "namespace": row[1],
            "request": row[2],
            "decisions": row[3],
            "created_at": row[4],
        }

    def table_exists(self) -> bool:
        """Проверить, существует ли таблица трейсов."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"public.{self.table}",))
            return cur.fetchone()[0] is not None
