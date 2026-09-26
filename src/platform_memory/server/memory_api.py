"""HTTP-поверхность Context Memory Engine: /api/memory/* (ADR-016 §9, §33).

Аддитивна к /api/brain/* (тот контракт не меняется). Auth — тот же Bearer
(``authenticate`` из app) и те же гранты по namespace (``_authorize``, ADR-017),
выдача — через тот же PII-барьер (``protect_pii_response``): новый маршрут не
может забыть ни маскирование, ни авторизацию.

Тестовый паттерн (как у app.py): движок вызывается через ИМЕНА ЭТОГО модуля
(``retain_observation``, ``build_context`` и т.д.) — юнит-тесты подменяют их
monkeypatch'ем без БД.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel

# Движок — модульные имена (late binding для тестов).
from platform_memory.context import build_context
from platform_memory.context.trace import ContextTraceStore
from platform_memory.context.typed import compile_typed_context
from platform_memory.core import db as dbmod
from platform_memory.core import timing
from platform_memory.core.kinds import PackError
from platform_memory.domain.reconcile import SnapshotError, StaleSnapshotError
from platform_memory.domain.reconcile import reconcile as reconcile_snapshot
from platform_memory.domain.registry import (
    PackConflictError,
    PackNotFoundError,
    open_registry,
)
from platform_memory.observations.consolidate import consolidate, delete_observation
from platform_memory.observations.ingest import retain_observation, retain_observations
from platform_memory.observations.store import ObservationStore


def _app_mod():
    """Модуль app (лениво — при импорте router'а app ещё инициализируется)."""
    from platform_memory.server import app as app_mod

    return app_mod


async def _authenticate(authorization: str | None = Header(default=None)):
    """Bearer-auth: та же логика и те же токены, что у /api/brain/*."""
    with timing.phase("auth"):
        return await _app_mod().authenticate(authorization)


async def _visibility(request: Request, principal=Depends(_authenticate)):
    """Разрешённые namespaces/scopes principal — единая точка app.visibility."""
    with timing.phase("visibility"):
        return await _app_mod().visibility(request, principal)


def _settings():
    return _app_mod().get_settings()


def _protect(payload: dict[str, Any], principal, route: str, trace_id: str | None, ns: str):
    """PII-барьер выдачи — единая точка app.protect_pii_response."""
    return _app_mod().protect_pii_response(payload, principal, route, trace_id, ns)


def _write_ns(scope: dict | None) -> str:
    return _app_mod()._scope_namespace(scope)


def _read_nss(scope: dict | None) -> list[str]:
    return _app_mod()._scope_namespaces(scope)


def _authorize(principal, namespaces, *, write: bool) -> None:
    """Гранты по namespace — единая точка app._authorize (403 при отказе)."""
    _app_mod()._authorize(principal, namespaces, write=write)


router = APIRouter(prefix="/api/memory", tags=["memory"])


class ObservationIn(BaseModel):
    """Наблюдение + scope записи. Схему содержимого валидирует Observation."""

    model_config = {"extra": "allow"}

    scope: dict | None = None  # {"namespace": "…"} — ровно одна KB записи


class ObservationBatchIn(BaseModel):
    observations: list[dict[str, Any]]
    scope: dict | None = None


class ContextIn(BaseModel):
    """ContextRequest + scope чтения (namespace(s) — как у /api/brain)."""

    model_config = {"extra": "allow"}

    scope: dict | None = None


@router.post("/observations", status_code=201)
def memory_observe(
    req: ObservationIn,
    principal=Depends(_authenticate),
) -> dict[str, Any]:
    """Принять одно наблюдение (идемпотентно по source identity)."""
    settings = _settings()
    ns = _write_ns(req.scope)
    _authorize(principal, [ns], write=True)
    payload = req.model_dump(exclude={"scope"})
    try:
        result = retain_observation(settings, payload, namespace=ns)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@router.post("/observations:batch", status_code=207)
def memory_observe_batch(
    req: ObservationBatchIn,
    principal=Depends(_authenticate),
) -> dict[str, Any]:
    """Batch-приём наблюдений: по-элементные результаты, partial errors (§34)."""
    settings = _settings()
    ns = _write_ns(req.scope)
    _authorize(principal, [ns], write=True)
    if len(req.observations) > settings.observations_max_batch:
        raise HTTPException(
            status_code=413,
            detail=f"Батч больше лимита {settings.observations_max_batch}",
        )
    try:
        return retain_observations(settings, req.observations, namespace=ns)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/observations/{observation_id}")
def memory_observation_get(
    observation_id: str,
    namespace: str = Query(default=""),
    principal=Depends(_authenticate),
    x_run_id: str | None = Header(default=None, alias="X-Run-Id"),
) -> dict[str, Any]:
    """Прочитать наблюдение по id (PII-барьер применяется к content)."""
    app_mod = _app_mod()
    settings = app_mod.get_settings()
    ns = app_mod._query_namespace(namespace)
    _authorize(principal, [ns], write=False)
    conn = dbmod.connect(settings)
    try:
        store = ObservationStore(conn, settings.observations_table, settings.default_namespace)
        record = (
            store.get(observation_id, namespaces=[ns] if ns else [])
            if store.table_exists()
            else None
        )
    finally:
        conn.close()
    if record is None:
        raise HTTPException(status_code=404, detail=f"Наблюдение не найдено: {observation_id}")
    return _protect(record.to_payload(), principal, "observation", x_run_id, ns)


@router.delete("/observations/{observation_id}")
def memory_observation_delete(
    observation_id: str,
    namespace: str = Query(default=""),
    mode: str = Query(default="redact", pattern="^(redact|purge)$"),
    actor: str = Query(default="api"),
    trace_id: str | None = Query(default=None),
    principal=Depends(_authenticate),
    x_run_id: str | None = Header(default=None, alias="X-Run-Id"),
) -> dict[str, Any]:
    """Удалить/зачистить наблюдение с каскадом на derived-память и аудитом (§40)."""
    app_mod = _app_mod()
    settings = app_mod.get_settings()
    ns = app_mod._query_namespace(namespace)
    _authorize(principal, [ns], write=True)
    result = delete_observation(
        settings,
        observation_id,
        namespace=ns,
        mode=mode,
        actor=actor,
        trace_id=trace_id or x_run_id or "",
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"Наблюдение не найдено: {observation_id}")
    return {"deleted": True, **result}


@router.post("/context")
def memory_context(
    req: ContextIn,
    principal=Depends(_authenticate),
    visible=Depends(_visibility),
    x_run_id: str | None = Header(default=None, alias="X-Run-Id"),
) -> dict[str, Any]:
    """Скомпилировать bounded ContextPack (без LLM-синтеза) — §18–25."""
    settings = _settings()
    nss = _read_nss(req.scope)
    _authorize(principal, nss, write=False)
    vis = _app_mod()._visible(visible)
    _app_mod().check_visible(vis, nss, settings.default_namespace)
    payload = req.model_dump(exclude={"scope", "allowedNamespaces", "allowedScopes"})
    payload.pop("allowed_namespaces", None)
    payload["namespaces"] = nss
    # Видимость задаёт сервер (MEM-ADR-019); клиентские allowedScopes уже учтены
    # зависимостью visibility как сужение — пересечение с серверными (MEM-ADR-021).
    payload["allowed_scopes"] = list(vis.scopes) if vis.scopes is not None else None
    try:
        pack = build_context(settings, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _protect(pack.to_payload(), principal, "context", x_run_id, nss[0] if nss else "")


@router.get("/context/trace/{trace_id}")
def memory_context_trace(
    trace_id: str,
    namespace: str = Query(default=""),
    principal=Depends(_authenticate),
) -> dict[str, Any]:
    """Трейс компиляции: каналы, счётчики, ranking- и budget-решения (§32)."""
    app_mod = _app_mod()
    settings = app_mod.get_settings()
    ns = app_mod._query_namespace(namespace)
    _authorize(principal, [ns], write=False)
    conn = dbmod.connect(settings)
    try:
        store = ContextTraceStore(conn, settings.context_traces_table, settings.default_namespace)
        trace = store.get(trace_id, namespaces=[ns] if ns else []) if store.table_exists() else None
    finally:
        conn.close()
    if trace is None:
        raise HTTPException(status_code=404, detail=f"Трейс не найден: {trace_id}")
    return trace


@router.post("/consolidate")
def memory_consolidate(
    scope: dict | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    principal=Depends(_authenticate),
) -> dict[str, Any]:
    """On-demand consolidation: redrive необработанных наблюдений (§26)."""
    settings = _settings()
    ns = _write_ns(scope)
    _authorize(principal, [ns], write=True)
    try:
        return consolidate(settings, namespace=ns, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/observations")
def memory_observations_list(
    namespace: str = Query(default=""),
    kind: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    principal=Depends(_authenticate),
    x_run_id: str | None = Header(default=None, alias="X-Run-Id"),
) -> dict[str, Any]:
    """Свежие наблюдения (аудит ingestion state; PII-барьер применяется)."""
    app_mod = _app_mod()
    settings = app_mod.get_settings()
    ns = app_mod._query_namespace(namespace)
    _authorize(principal, [ns], write=False)
    conn = dbmod.connect(settings)
    try:
        store = ObservationStore(conn, settings.observations_table, settings.default_namespace)
        if not store.table_exists():
            return {"count": 0, "observations": [], "statuses": {}}
        records = store.list_recent(
            [ns] if ns else [],
            kinds=[kind] if kind else (),
            status=status,
            limit=limit,
        )
        statuses = store.counts_by_status([ns] if ns else [])
    finally:
        conn.close()
    payload = {
        "count": len(records),
        "observations": [r.to_payload() for r in records],
        "statuses": statuses,
    }
    return _protect(payload, principal, "observations", x_run_id, ns)


# --- доменные пакеты видов, сверка снимков, типизированный обход (MEM-ADR-020) ---


def _authorize_core(principal) -> None:
    """Маршрут ядра (амендмент MEM-ADR-020) — единая точка app._authorize_core (403)."""
    _app_mod()._authorize_core(principal)


def _principal_label(principal) -> str:
    return str(getattr(principal, "label", "") or "")


def _with_registry(fn):
    """Выполнить fn(registry) на своём соединении; ошибки реестра -> HTTP-коды."""
    settings = _settings()
    conn = dbmod.connect(settings)
    try:
        return fn(open_registry(conn, settings))
    except PackNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PackConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


class PackIn(BaseModel):
    """Доменный пакет: {name, version, kinds[], relations[]} (валидирует core.kinds).

    ``version`` — строка или число (``version: 1`` из YAML пакета приводится к ``"1"``).
    """

    model_config = {"extra": "allow"}

    name: str
    version: str | int | float


@router.post("/packages", status_code=201)
def memory_package_register(
    req: PackIn,
    response: Response,
    principal=Depends(_authenticate),
) -> dict[str, Any]:
    """Зарегистрировать версию доменного пакета (service scope; версия иммутабельна).

    201 — версия создана, 200 — та же версия с тем же содержимым уже есть,
    409 — версия уже зарегистрирована с другим содержимым.
    """
    _authorize_core(principal)
    _app_mod()._authorize_service(principal)
    payload = req.model_dump()
    result = _with_registry(
        lambda reg: reg.register(payload, registered_by=_principal_label(principal))
    )
    if result["status"] == "unchanged":
        response.status_code = 200
    return result


@router.get("/packages")
def memory_packages_list(principal=Depends(_authenticate)) -> dict[str, Any]:
    """Список пакетов: имя, версии, последняя (встроенный ``default`` — первым)."""
    _authorize_core(principal)
    return {"packages": _with_registry(lambda reg: reg.list_packs())}


@router.get("/packages/{name}")
def memory_package_get(
    name: str,
    version: str = Query(default=""),
    principal=Depends(_authenticate),
) -> dict[str, Any]:
    """Версия пакета (по умолчанию — последняя)."""
    _authorize_core(principal)
    version = version if isinstance(version, str) else ""
    return _with_registry(lambda reg: reg.get(name, version).to_payload())


class NamespaceKindsIn(BaseModel):
    """Настройка видов namespace: строгий режим и набор пакетов (None — все)."""

    strict: bool = False
    packages: list[str] | None = None


@router.get("/namespaces/{namespace}/kinds")
def memory_namespace_kinds(
    namespace: str,
    principal=Depends(_authenticate),
) -> dict[str, Any]:
    """Действующий каталог видов namespace и его настройка."""
    _authorize_core(principal)
    ns = _app_mod()._query_namespace(namespace)
    _authorize(principal, [ns], write=False)

    def _read(reg):
        return {
            "settings": reg.get_settings(ns).to_payload(),
            "catalog": reg.catalog_for(ns).to_payload(),
        }

    return _with_registry(_read)


@router.put("/namespaces/{namespace}/kinds")
def memory_namespace_kinds_put(
    namespace: str,
    req: NamespaceKindsIn,
    principal=Depends(_authenticate),
) -> dict[str, Any]:
    """Задать строгий режим и набор пакетов namespace (право записи в namespace)."""
    _authorize_core(principal)
    ns = _app_mod()._query_namespace(namespace)
    _authorize(principal, [ns], write=True)

    def _write(reg):
        conf = reg.put_settings(
            ns,
            strict=req.strict,
            packages=req.packages,
            updated_by=_principal_label(principal),
        )
        return {"settings": conf.to_payload(), "catalog": reg.catalog_for(ns).to_payload()}

    return _with_registry(_write)


class ReconcileIn(BaseModel):
    """Документ снимка как есть (формат SNAPSHOT.md пакета) + namespace и scopes записи.

    Поля снимка (``pack, source, scope, snapshotId, observedAt, entities, relations``)
    валидирует ``domain.reconcile.parse_snapshot``. ``scope`` здесь — строка снимка
    (часть источника), а не scope namespace: namespace передаётся полем ``namespace``
    или query-параметром ``?namespace=``. ``scopes`` — scopes видимости (MEM-ADR-019),
    которые ядро выводит из задачи.
    """

    model_config = {"extra": "allow"}

    namespace: str | None = None
    scopes: list[str] | None = None


@router.post("/reconcile")
def memory_reconcile(
    req: ReconcileIn,
    namespace: str | None = Query(default=None),
    principal=Depends(_authenticate),
) -> dict[str, Any]:
    """Сверить полный снимок источника: открыть новое, закрыть изменившееся и пропавшее.

    Идемпотентно по (namespace, source, scope, snapshotId); ответ — счётчики
    opened/closed/unchanged/superseded, по связям — ещё pending/resolved.
    """
    _authorize_core(principal)
    settings = _settings()
    query_ns = namespace if isinstance(namespace, str) else ""
    if query_ns and req.namespace and query_ns != req.namespace:
        raise HTTPException(status_code=400, detail="namespace в query и в теле различаются")
    ns = _write_ns({"namespace": query_ns or req.namespace or ""})
    _authorize(principal, [ns], write=True)
    snapshot = req.model_dump(exclude={"namespace", "scopes"})
    try:
        return reconcile_snapshot(
            settings,
            snapshot=snapshot,
            namespace=ns,
            scopes=req.scopes or [],
            actor=_principal_label(principal),
        )
    except StaleSnapshotError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (SnapshotError, PackError, PackNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/context/typed")
def memory_context_typed(
    req: ContextIn,
    principal=Depends(_authenticate),
    visible=Depends(_visibility),
    x_run_id: str | None = Header(default=None, alias="X-Run-Id"),
) -> dict[str, Any]:
    """Типизированный обход: якоря -> связи с direction/depth/limit на as_of."""
    settings = _settings()
    nss = _read_nss(req.scope)
    _authorize(principal, nss, write=False)
    vis = _app_mod()._visible(visible)
    _app_mod().check_visible(vis, nss, settings.default_namespace)
    payload = req.model_dump(exclude={"scope", "allowedNamespaces", "allowedScopes"})
    payload.pop("allowed_namespaces", None)
    payload["namespaces"] = nss
    # Видимость задаёт сервер (MEM-ADR-019); клиентские allowedScopes уже учтены
    # зависимостью visibility как сужение — пересечение с серверными (MEM-ADR-021).
    payload["allowed_scopes"] = list(vis.scopes) if vis.scopes is not None else None
    try:
        with timing.phase("typed"):
            pack = compile_typed_context(settings, payload)
    except PackNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with timing.phase("protect"):
        return _protect(pack, principal, "context_typed", x_run_id, nss[0] if nss else "")
