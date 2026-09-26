"""KindRegistry — хранение доменных пакетов и настроек видов namespace (MEM-ADR-020).

Реляционные таблицы (не граф), как у наблюдений: пакет — строка ``(name, version)``
с JSON-спецификацией. Версия иммутабельна: повторная регистрация той же версии с тем
же содержимым идемпотентна, с другим — ``PackConflictError`` (409). Пакет по
умолчанию (``core/packs/default.json``) встроен и в таблицу не пишется; его имя
зарезервировано.

Настройка namespace — ``{strict, packages}``: пакет действует только в namespace, где
он явно включён. ``packages=None`` (и namespace без настройки) — только пакет по
умолчанию; список — ровно перечисленные (``name`` — последняя версия,
``name@version`` — закреплённая). Регистрация пакета в реестре сама по себе ни на
один namespace не влияет.
``strict=True`` — сущность неизвестного вида отвергается (``UnknownKindError``);
иначе прежнее поведение (любой вид допустим).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from platform_memory.core.kinds import (
    DEFAULT_PACK_NAME,
    DomainPack,
    KindCatalog,
    PackError,
    default_pack,
    parse_pack,
    version_key,
)
from platform_memory.core.namespaces import resolve_namespace, validate_namespace


class PackConflictError(ValueError):
    """Версия пакета уже зарегистрирована с другим содержимым (иммутабельность)."""


class PackNotFoundError(LookupError):
    """Пакет/версия не зарегистрированы."""


@dataclass(slots=True)
class NamespaceKindSettings:
    """Настройка видов namespace: строгий режим и набор пакетов."""

    namespace: str
    strict: bool = False
    packages: list[str] | None = None  # None — только пакет по умолчанию
    updated_at: str = ""
    updated_by: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "strict": self.strict,
            "packages": self.packages,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


def _spec_hash(pack: DomainPack) -> str:
    return hashlib.sha256(pack.canonical_json().encode("utf-8")).hexdigest()


class KindRegistry:
    """Реестр пакетов видов и настроек namespace поверх двух таблиц Postgres."""

    def __init__(
        self,
        conn: psycopg.Connection,
        packs_table: str,
        settings_table: str,
        default_namespace: str = "",
    ):
        self.conn = conn
        self.packs_table = packs_table
        self.settings_table = settings_table
        self.default_namespace = resolve_namespace(default_namespace)
        self._exists: bool | None = None
        self._catalogs: dict[str, KindCatalog] = {}

    def _tbl(self, name: str) -> sql.Identifier:
        return sql.Identifier("public", name)

    # --- схема ---

    def ensure_schema(self) -> None:
        """Создать таблицы пакетов и настроек namespace (идемпотентно, аддитивно)."""
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {packs} (
                        name text NOT NULL,
                        version text NOT NULL,
                        spec jsonb NOT NULL,
                        spec_hash text NOT NULL,
                        registered_by text NOT NULL DEFAULT '',
                        registered_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (name, version)
                    )
                    """
                ).format(packs=self._tbl(self.packs_table))
            )
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {settings} (
                        namespace text PRIMARY KEY,
                        strict boolean NOT NULL DEFAULT false,
                        packages jsonb,
                        updated_by text NOT NULL DEFAULT '',
                        updated_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                ).format(settings=self._tbl(self.settings_table))
            )
        self._exists = True
        self._catalogs.clear()

    def table_exists(self) -> bool:
        if self._exists is None:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass(%s) IS NOT NULL AND to_regclass(%s) IS NOT NULL",
                    (f"public.{self.packs_table}", f"public.{self.settings_table}"),
                )
                row = cur.fetchone()
            self._exists = bool(row and row[0])
        return self._exists

    # --- пакеты ---

    def register(self, payload: dict[str, Any] | DomainPack, *, registered_by: str = "") -> dict:
        """Зарегистрировать версию пакета; вернуть ``{status: created|unchanged, pack}``."""
        pack = payload if isinstance(payload, DomainPack) else parse_pack(payload)
        if pack.name == DEFAULT_PACK_NAME:
            raise PackError(f"Имя пакета {DEFAULT_PACK_NAME!r} зарезервировано за встроенным")
        self.ensure_schema()
        digest = _spec_hash(pack)
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {t} (name, version, spec, spec_hash, registered_by) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (name, version) DO NOTHING "
                        "RETURNING name"
                    ).format(t=self._tbl(self.packs_table)),
                    (pack.name, pack.version, Jsonb(pack.to_payload()), digest, registered_by),
                )
                created = cur.fetchone() is not None
                if not created:
                    cur.execute(
                        sql.SQL(
                            "SELECT spec_hash FROM {t} WHERE name = %s AND version = %s"
                        ).format(t=self._tbl(self.packs_table)),
                        (pack.name, pack.version),
                    )
                    row = cur.fetchone()
                    if row and row[0] != digest:
                        raise PackConflictError(
                            f"Версия {pack.ref} уже зарегистрирована с другим содержимым; "
                            "версия пакета иммутабельна — выпустите новую"
                        )
        self._catalogs.clear()
        return {"status": "created" if created else "unchanged", "pack": pack.to_payload()}

    def _versions(self, name: str) -> list[tuple[str, dict[str, Any], str, str]]:
        if not self.table_exists():
            return []
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT version, spec, registered_by, registered_at::text FROM {t} "
                    "WHERE name = %s"
                ).format(t=self._tbl(self.packs_table)),
                (name,),
            )
            rows = cur.fetchall()
        return sorted(rows, key=lambda r: version_key(r[0]))

    def get(self, name: str, version: str = "") -> DomainPack:
        """Версия пакета (пусто — последняя); встроенный default — всегда."""
        if name == DEFAULT_PACK_NAME:
            pack = default_pack()
            if version and version != pack.version:
                raise PackNotFoundError(f"Пакет {name}@{version} не найден")
            return pack
        rows = self._versions(name)
        if version:
            rows = [r for r in rows if r[0] == version]
        if not rows:
            ref = f"{name}@{version}" if version else name
            raise PackNotFoundError(f"Пакет {ref} не найден")
        return parse_pack(rows[-1][1])

    def list_packs(self) -> list[dict[str, Any]]:
        """Все пакеты: имя, версии по возрастанию, последняя; встроенный default первым."""
        builtin = default_pack()
        out: list[dict[str, Any]] = [
            {
                "name": builtin.name,
                "versions": [builtin.version],
                "latest": builtin.version,
                "builtin": True,
            }
        ]
        if not self.table_exists():
            return out
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT name, version FROM {t}").format(t=self._tbl(self.packs_table))
            )
            rows = cur.fetchall()
        by_name: dict[str, list[str]] = {}
        for name, version in rows:
            by_name.setdefault(name, []).append(version)
        for name in sorted(by_name):
            versions = sorted(by_name[name], key=version_key)
            out.append(
                {"name": name, "versions": versions, "latest": versions[-1], "builtin": False}
            )
        return out

    def _resolve_ref(self, ref: str) -> DomainPack:
        name, _, version = ref.partition("@")
        return self.get(name.strip(), version.strip())

    # --- настройки namespace ---

    def get_settings(self, namespace: str) -> NamespaceKindSettings:
        ns = resolve_namespace(namespace, self.default_namespace)
        if not self.table_exists():
            return NamespaceKindSettings(namespace=ns)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT strict, packages, updated_by, updated_at::text FROM {t} "
                    "WHERE namespace = %s"
                ).format(t=self._tbl(self.settings_table)),
                (ns,),
            )
            row = cur.fetchone()
        if row is None:
            return NamespaceKindSettings(namespace=ns)
        packages = row[1] if isinstance(row[1], list) else None
        return NamespaceKindSettings(
            namespace=ns,
            strict=bool(row[0]),
            packages=[str(p) for p in packages] if packages is not None else None,
            updated_by=row[2] or "",
            updated_at=row[3] or "",
        )

    def put_settings(
        self,
        namespace: str,
        *,
        strict: bool,
        packages: Sequence[str] | None = None,
        updated_by: str = "",
    ) -> NamespaceKindSettings:
        """Задать настройку видов namespace; ссылки на пакеты проверяются сразу."""
        ns = validate_namespace(resolve_namespace(namespace, self.default_namespace))
        refs = None
        if packages is not None:
            refs = list(dict.fromkeys(str(p).strip() for p in packages if str(p).strip()))
            for ref in refs:
                self._resolve_ref(ref)  # PackNotFoundError, если ссылки нет
        self.ensure_schema()
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {t} (namespace, strict, packages, updated_by, updated_at) "
                    "VALUES (%s, %s, %s, %s, now()) ON CONFLICT (namespace) DO UPDATE SET "
                    "strict = EXCLUDED.strict, packages = EXCLUDED.packages, "
                    "updated_by = EXCLUDED.updated_by, updated_at = now()"
                ).format(t=self._tbl(self.settings_table)),
                (ns, bool(strict), Jsonb(refs) if refs is not None else None, updated_by),
            )
        self._catalogs.clear()
        return self.get_settings(ns)

    # --- каталог и проверка видов ---

    def catalog_for(self, namespace: str) -> KindCatalog:
        """Действующий каталог видов namespace (кэш на время жизни реестра)."""
        ns = resolve_namespace(namespace, self.default_namespace)
        cached = self._catalogs.get(ns)
        if cached is not None:
            return cached
        conf = self.get_settings(ns)
        if conf.packages is None:
            # Пакет действует только там, где явно включён (MEM-ADR-020, амендмент).
            packs = [default_pack()]
        else:
            packs = [self._resolve_ref(ref) for ref in conf.packages]
        catalog = KindCatalog.build(packs, strict=conf.strict)
        self._catalogs[ns] = catalog
        return catalog

    def check_entity(
        self,
        namespace: str,
        kind: str,
        natural_key: str = "",
        attributes: dict[str, Any] | None = None,
    ) -> str:
        """Kind-guard для GraphStore/FactStore: каноническое имя вида или отказ."""
        return self.catalog_for(namespace).check_entity(
            kind, natural_key=natural_key, attributes=attributes
        )


def open_registry(conn: psycopg.Connection, settings) -> KindRegistry:
    """Реестр на соединении по именам таблиц из настроек."""
    return KindRegistry(
        conn,
        settings.domain_packs_table,
        settings.namespace_settings_table,
        settings.default_namespace,
    )


def attach_kind_guard(graph, settings) -> KindRegistry:
    """Подключить проверку видов к GraphStore (пути записи агента: facts, retain,
    documents, observations, reconcile). Без таблиц реестра — нестрогий режим."""
    registry = open_registry(graph.conn, settings)
    graph.kind_guard = registry.check_entity
    return registry
