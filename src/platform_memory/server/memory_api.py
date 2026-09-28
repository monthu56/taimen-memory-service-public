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
from platform_memory.context.entities import query_entities
from platform_memory.context.trace import ContextTraceStore
from platform_memory.context.typed import compile_typed_context
from platform_memory.core import db as dbmod
from platform_memory.core import timing
from platform_memory.core.kinds import TENANT_REF_PREFIX, PackError
from platform_memory.domain.reconcile import (
    SnapshotError,
    SnapshotStateMismatch,
    StaleSnapshotError,
)
from platform_memory.domain.reconcile import reconcile as reconcile_snapshot
from platform_memory.domain.registry import (
    PackConflictError,
    PackNotFoundError,
    PackScopeConflictError,
    open_registry,
    pack_payload,
)
from platform_memory.domain.searchable import reindex_namespace
from platform_memory.index import build_embedder
from platform_memory.observations.consolidate import consolidate, delete_observation
from platform_memory.observations.ingest import retain_observation, retain_observations
from platform_memory.observations.store import ObservationStore
from platform_memory.server.domain_schemas import (
    EntitiesQueryIn,
    EntitiesQueryResult,
    PackageList,
    PackIn,
    PackRegistered,
    PackScopeConflict,
    ReconcileIn,
    ReconcileResult,
    SnapshotStaleError,
    TypedContextIn,
    TypedContextResult,
)


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
    except PackScopeConflictError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    except PackConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PackError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post(
    "/packages",
    status_code=201,
    response_model=None,
    responses={
        201: {"model": PackRegistered},
        200: {"model": PackRegistered, "description": "Та же версия уже есть"},
        409: {
            "model": PackScopeConflict,
            "description": "Версия уже есть с другим содержимым (detail — строка) или "
            "имя пакета арендатора совпало с общим (detail.code)",
        },
    },
)
def memory_package_register(
    req: PackIn,
    response: Response,
    principal=Depends(_authenticate),
) -> dict[str, Any]:
    """Зарегистрировать версию доменного пакета (версия иммутабельна).

    Общий пакет — service scope. Пакет арендатора (``scope: tenant``) — право записи
    в namespace-владелец. 201 — версия создана, 200 — та же версия с тем же
    содержимым уже есть, 409 — версия уже зарегистрирована с другим содержимым или
    имя пакета арендатора совпало с общим (``detail.code``).
    """
    _authorize_core(principal)
    tenant = req.scope == "tenant"
    owner = ""
    if tenant:
        if not req.namespace:
            raise HTTPException(status_code=400, detail="scope: tenant требует namespace")
        owner = _app_mod()._query_namespace(req.namespace)
        _authorize(principal, [owner], write=True)
    else:
        if req.namespace:
            raise HTTPException(
                status_code=400, detail="namespace задаётся только у пакета scope: tenant"
            )
        _app_mod()._authorize_service(principal)
    # Только присланные поля: объявленные в схеме необязательные поля не дописываются
    # в пакет (каноническая форма и хэш версии не меняются).
    payload = req.model_dump(by_alias=True, exclude_unset=True)
    payload.pop("scope", None)
    payload.pop("namespace", None)
    label = _principal_label(principal)
    if tenant:
        result = _with_registry(
            lambda reg: reg.register_tenant(payload, owner=owner, registered_by=label)
        )
    else:
        result = _with_registry(lambda reg: reg.register(payload, registered_by=label))
    if result["status"] == "unchanged":
        response.status_code = 200
    return result


def _packages_namespace(principal, namespace: str | None) -> str:
    """Namespace чтения пакетов арендатора (право чтения); пусто — только общие."""
    if not isinstance(namespace, str) or not namespace:
        return ""
    ns = _app_mod()._query_namespace(namespace)
    _authorize(principal, [ns], write=False)
    return ns


@router.get("/packages", response_model=None, responses={200: {"model": PackageList}})
def memory_packages_list(
    namespace: str | None = Query(default=None),
    principal=Depends(_authenticate),
) -> dict[str, Any]:
    """Список пакетов: имя, версии, последняя (встроенный ``default`` — первым).

    С ``?namespace=N`` — ещё пакеты арендатора, видимые в ``N`` (нужно право чтения).
    """
    _authorize_core(principal)
    ns = _packages_namespace(principal, namespace)
    return {"packages": _with_registry(lambda reg: reg.list_packs(ns or None))}


@router.get("/packages/{name}")
def memory_package_get(
    name: str,
    version: str = Query(default=""),
    namespace: str | None = Query(default=None),
    principal=Depends(_authenticate),
) -> dict[str, Any]:
    """Версия пакета (по умолчанию — последняя).

    ``tenant:<имя>`` — пакет арендатора, видимый в ``?namespace=N`` (право чтения);
    невидимый или без ``namespace`` — 404.
    """
    _authorize_core(principal)
    version = version if isinstance(version, str) else ""
    ns = _packages_namespace(principal, namespace)
    if not name.startswith(TENANT_REF_PREFIX):
        return _with_registry(lambda reg: pack_payload(reg.get(name, version)))
    pack_name = name[len(TENANT_REF_PREFIX) :]
    if not ns:
        raise HTTPException(status_code=404, detail=f"Пакет {name} не найден")
    return _with_registry(
        lambda reg: pack_payload(reg.get_tenant(pack_name, version, namespace=ns))
    )


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
    """Задать строгий режим и набор пакетов namespace (право записи в namespace).

    Затем — переиндексация сущностей для поиска по смыслу по новому каталогу (виды с
    ``searchable``): ``reindex`` — счётчики или ``error``. Ошибка переиндексации
    настройку не откатывает: индекс остаётся прежним, повтор ``PUT`` доделает.
    """
    _authorize_core(principal)
    settings = _settings()
    ns = _app_mod()._query_namespace(namespace)
    _authorize(principal, [ns], write=True)

    def _write(reg):
        conf = reg.put_settings(
            ns,
            strict=req.strict,
            packages=req.packages,
            updated_by=_principal_label(principal),
        )
        catalog = reg.catalog_for(ns)
        try:
            reindex = reindex_namespace(
                settings, reg.conn, ns, catalog, lambda: build_embedder(settings)
            )
        except Exception as exc:  # noqa: BLE001 — настройка уже сохранена
            reindex = {"error": str(exc)}
        return {
            "settings": conf.to_payload(),
            "catalog": catalog.to_payload(),
            "reindex": reindex,
        }

    return _with_registry(_write)


@router.post(
    "/reconcile",
    response_model=None,
    responses={
        200: {"model": ReconcileResult},
        409: {"model": SnapshotStaleError, "description": "snapshot_stale или снимок старше"},
    },
)
def memory_reconcile(
    req: ReconcileIn,
    namespace: str | None = Query(default=None),
    principal=Depends(_authenticate),
) -> dict[str, Any]:
    """Сверить полный снимок источника: открыть новое, закрыть изменившееся и пропавшее.

    Идемпотентно по (namespace, source, scope, snapshotId); ответ — счётчики
    opened/closed/unchanged/superseded, по связям — ещё pending/resolved, и ``changes`` —
    ключи ``{kind, key}`` новых, изменённых и пропавших узлов (с лимитом и ``truncated``).

    ``dryRun: true`` — план без записи; ``expectedState`` — применить, только если
    состояние пары ``(source, scope)`` равно ``stateToken`` плана, иначе
    ``409 snapshot_stale`` (амендмент MEM-ADR-020 2026-09-28).
    """
    _authorize_core(principal)
    settings = _settings()
    query_ns = namespace if isinstance(namespace, str) else ""
    if query_ns and req.namespace and query_ns != req.namespace:
        raise HTTPException(status_code=400, detail="namespace в query и в теле различаются")
    ns = _write_ns({"namespace": query_ns or req.namespace or ""})
    _authorize(principal, [ns], write=True)
    snapshot = req.model_dump(exclude={"namespace", "scopes", "dry_run", "expected_state"})
    try:
        return reconcile_snapshot(
            settings,
            snapshot=snapshot,
            namespace=ns,
            scopes=req.scopes or [],
            actor=_principal_label(principal),
            dry_run=req.dry_run,
            expected_state=req.expected_state,
        )
    except SnapshotStateMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "snapshot_stale",
                "message": str(exc),
                "expectedState": exc.expected_state,
                "stateToken": exc.state_token,
            },
        ) from exc
    except StaleSnapshotError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (SnapshotError, PackError, PackNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/context/typed",
    response_model=None,
    responses={200: {"model": TypedContextResult}},
)
def memory_context_typed(
    req: TypedContextIn,
    principal=Depends(_authenticate),
    visible=Depends(_visibility),
    x_run_id: str | None = Header(default=None, alias="X-Run-Id"),
) -> dict[str, Any]:
    """Типизированный обход: якоря -> связи с direction/depth/limit на as_of.

    ``where`` (верхний и шагов) исполняет движок (``context/where.py``, K006): форму
    верхнего ``where`` проверяет схема (422), значения и условия шагов — движок (400).
    """
    settings = _settings()
    nss = _read_nss(req.scope)
    _authorize(principal, nss, write=False)
    vis = _app_mod()._visible(visible)
    _app_mod().check_visible(vis, nss, settings.default_namespace)
    # Только присланные поля: объявленные в схеме поля без значения не подменяют
    # camelCase-варианты (asOf, allowSemantic), которые движок читает сам.
    payload = req.model_dump(
        exclude_unset=True, exclude={"scope", "allowedNamespaces", "allowedScopes"}
    )
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


@router.post(
    "/entities:query",
    response_model=None,
    responses={200: {"model": EntitiesQueryResult}},
)
def memory_entities_query(
    req: EntitiesQueryIn,
    principal=Depends(_authenticate),
    visible=Depends(_visibility),
    x_run_id: str | None = Header(default=None, alias="X-Run-Id"),
) -> dict[str, Any]:
    """Перечень сущностей видов на as_of с фильтрами ``where`` и курсором.

    Версии — из журнала сверки снимков; порядок ``(kind, key, namespace)``; конец
    перечня — ``nextCursor: null`` (страница может быть короче ``limit`` и до конца).
    Namespaces — ``namespaces`` или ``scope`` (как у ``context/typed``), видимость scopes
    задаёт сервер (MEM-ADR-019/021).
    """
    settings = _settings()
    if req.namespaces is not None and req.scope is not None:
        raise HTTPException(status_code=400, detail="namespaces и scope — что-то одно")
    scope = {"namespaces": req.namespaces} if req.namespaces is not None else req.scope
    nss = _read_nss(scope)
    _authorize(principal, nss, write=False)
    vis = _app_mod()._visible(visible)
    _app_mod().check_visible(vis, nss, settings.default_namespace)
    payload = req.model_dump(
        exclude_unset=True, exclude={"scope", "allowedNamespaces", "allowedScopes"}
    )
    payload.pop("allowed_namespaces", None)
    payload["namespaces"] = nss
    payload["allowed_scopes"] = list(vis.scopes) if vis.scopes is not None else None
    try:
        with timing.phase("entities"):
            page = query_entities(settings, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with timing.phase("protect"):
        return _protect(page, principal, "entities_query", x_run_id, nss[0] if nss else "")
