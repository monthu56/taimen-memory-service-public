"""Доступ к Postgres и подготовка сессии Apache AGE.

Особенность AGE: третий аргумент функции ``cypher()`` обязан быть **bind-параметром**
именно типа ``agtype`` (строковый литерал и выражение с ``::agtype`` отвергаются с
ошибкой "third argument of cypher function must be a parameter"). Поэтому параметры
передаются через обёртку :class:`Agtype` с кастомным dumper'ом psycopg, который
помечает значение OID'ом agtype.
"""

from __future__ import annotations

import json
import re
from typing import Any

import psycopg
from psycopg.adapt import Dumper
from psycopg.conninfo import conninfo_to_dict

from platform_memory.core.config import Settings

# Хвост-аннотация типа в выводе agtype: 12345::vertex / {...}::edge и т.п.
_AGTYPE_SUFFIX = re.compile(r"::(vertex|edge|path|numeric|integer|float)\s*$")


class Agtype:
    """Обёртка значения-параметра типа agtype для безопасной передачи в cypher()."""

    __slots__ = ("text",)

    def __init__(self, value: Any):
        """Сохранить значение как строку: str — как есть, иначе сериализовать в JSON."""
        # value сериализуется в JSON; ensure_ascii=False — кириллица как есть.
        self.text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def agtype(obj: Any) -> Agtype:
    """Сахар: завернуть Python-объект в параметр agtype."""
    return Agtype(obj)


def _lookup_agtype_oid(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT oid FROM pg_type WHERE typname = 'agtype'")
        row = cur.fetchone()
    if not row:
        raise RuntimeError("Тип agtype не найден — расширение AGE не установлено?")
    return int(row[0])


def register_agtype(conn: psycopg.Connection) -> None:
    """Зарегистрировать dumper для :class:`Agtype` с OID типа agtype в этом соединении."""
    oid = _lookup_agtype_oid(conn)

    class _AgtypeDumper(Dumper):
        def dump(self, obj: Agtype) -> bytes:
            """Сериализовать :class:`Agtype` в байты UTF-8 для передачи в Postgres."""
            return obj.text.encode("utf-8")

    _AgtypeDumper.oid = oid
    conn.adapters.register_dumper(Agtype, _AgtypeDumper)


def session_options(settings: Settings) -> dict[str, str]:
    """Параметры libpq для соединения сервиса: выключение JIT у графовых запросов (ADR-010).

    AGE отдаёт планировщику мусорную оценку кардинальности (на графе в 1037 вершин — 1.69 млрд
    строк), стоимость плана перешагивает ``jit_above_cost``, и LLVM компилирует каждый графовый
    запрос заново: замер ``expand_neighbors`` — 483 мс JIT из 578 мс общего времени при 39 мс
    полезной работы. Соединение живёт ровно один HTTP-запрос, поэтому компиляция оплачивается
    каждым запросом; ``jit=off`` даёт 8–12x на обходе графа.

    Явно заданный ``options`` в ``CB_DATABASE_URL`` уважается и не перетирается — так остаётся
    возможность настроить сессию вручную (в т.ч. для пулеров, не поддерживающих ``options``).
    """
    if settings.db_jit or "options" in conninfo_to_dict(settings.database_url):
        return {}
    return {"options": "-c jit=off"}


def connect(settings: Settings, *, autocommit: bool = True) -> psycopg.Connection:
    """Открыть подключение и подготовить сессию AGE (LOAD 'age' + search_path + dumper)."""
    conn = psycopg.connect(
        settings.database_url, autocommit=autocommit, **session_options(settings)
    )
    prepare_age(conn)
    return conn


def prepare_age(conn: psycopg.Connection) -> None:
    """Загрузить AGE в сессию и зарегистрировать dumper agtype. Безопасно повторно."""
    with conn.cursor() as cur:
        cur.execute("LOAD 'age';")
        cur.execute('SET search_path = ag_catalog, "$user", public;')
    register_agtype(conn)


def parse_agtype(value: Any) -> Any:
    """Распарсить значение agtype (приходит из psycopg строкой) в Python-объект."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    cleaned = _AGTYPE_SUFFIX.sub("", value.strip())
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return value


def graph_exists(conn: psycopg.Connection, graph_name: str) -> bool:
    """Проверить, существует ли граф AGE с указанным именем."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s;", (graph_name,))
        return cur.fetchone() is not None


def ensure_graph(conn: psycopg.Connection, graph_name: str) -> None:
    """Создать граф AGE, если его ещё нет (идемпотентно)."""
    if not graph_exists(conn, graph_name):
        with conn.cursor() as cur:
            cur.execute("SELECT ag_catalog.create_graph(%s);", (graph_name,))


def drop_graph(conn: psycopg.Connection, graph_name: str) -> None:
    """Удалить граф AGE вместе с данными (для --reset и тестов)."""
    if graph_exists(conn, graph_name):
        with conn.cursor() as cur:
            cur.execute("SELECT ag_catalog.drop_graph(%s, true);", (graph_name,))
