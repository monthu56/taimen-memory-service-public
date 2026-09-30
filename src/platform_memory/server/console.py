"""Memory Console — тонкий административный веб-интерфейс (/console).

Server-rendered (Jinja2): браузер получает готовый HTML и отправляет обычные
формы; все обращения к движку выполняются на сервере теми же функциями и
хендлерами, что обслуживают публичный Memory API (тот же идемпотентный retain,
тот же delete-с-аудитом, тот же PII-контур). API-ключ сервиса (CB_SERVER_API_KEY)
в браузер не передаётся ни в каком виде — Console работает in-process.

Границы безопасности (MVP):
- Выключена по умолчанию (``CB_CONSOLE_ENABLED=false``) — все маршруты дают 404.
- Собственной аутентификации нет: доступ к /console обязан ограничивать
  reverse proxy / network policy контура клиента (см. deploy/RUNBOOK.md).
- Базы знаний — только из allowlist ``CB_CONSOLE_NAMESPACES`` (CSV);
  namespace вне списка отклоняется с 400.
- POST-запросы проходят same-origin проверку (Origin vs Host) против CSRF.
- Операции Console выполняются с полным допуском к ПДн под меткой ``console`` —
  каждая выдача ПДн журналируется событием ``pii_access`` (ADR-002).
- Импорт пишет с видимостью уровня namespace (MEM-ADR-019): у Console нет
  principal, поэтому узел или чанк со scope воркспейса или principal импорт не
  перезаписывает — отказ, как у пишущего без воркспейсов.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from platform_memory.core.config import Settings

router = APIRouter(prefix="/console", include_in_schema=False)
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Служебные типы аудит-контура — не статьи, в таблице контента не показываются.
_NON_ARTICLE_TYPES = frozenset({"audit_event", "pc_trace", "pc_agent"})
# Сколько строк максимум рендерится в таблицах (весь корпус в браузер не грузим).
_MAX_ROWS = 50


def _engine():
    """Модуль server.app — лениво (разрыв цикла импорта app <-> console).

    Доступ через модуль, а не прямые импорты символов: monkeypatch фейков
    (GraphStore/VectorIndex/retrieve/get_settings) в тестах действует и здесь.
    """
    from platform_memory.server import app as app_mod

    return app_mod


def _console_settings() -> Settings:
    """Гейт Console: выключена конфигом -> 404 на всех маршрутах."""
    settings = _engine().get_settings()
    if not settings.console_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    return settings


def _resolve_ns(settings: Settings, ns: str | None) -> tuple[str, list[str]]:
    """Выбранная KB строго из allowlist; пусто -> первая из списка."""
    allowed = settings.console_namespace_list()
    if not ns:
        return allowed[0], allowed
    if ns not in allowed:
        raise HTTPException(status_code=400, detail=f"База знаний недоступна в Console: {ns}")
    return ns, allowed


def _check_origin(request: Request) -> None:
    """Same-origin проверка мутирующих запросов: браузерный кросс-сайт POST -> 403."""
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        if origin == "null":
            raise HTTPException(status_code=403, detail="Cross-origin запрос отклонён")
        return
    if urlsplit(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(status_code=403, detail="Cross-origin запрос отклонён")


def _console_principal():
    """Принципал операций Console: полный допуск, метка console в журнале ПДн."""
    return _engine().Principal(access="full", label="console")


def _console_write_visibility():
    """Видимость пишущего Console: только уровень namespace (MEM-ADR-019).

    Console не знает, кто за ней стоит (доступ ограничивает reverse proxy), и не
    может заявить ни воркспейс, ни principal: пишет как principal без воркспейсов.
    """
    return _engine().VisibleSet(scopes=())


def _page(request: Request, name: str, ctx: dict[str, Any], status_code: int = 200):
    """Отрендерить страницу с общим контекстом (ns, allowlist, активный раздел)."""
    return _templates.TemplateResponse(
        request=request, name=name, context=ctx, status_code=status_code
    )


def _node_row(node: dict[str, Any]) -> dict[str, Any]:
    """Свести узел графа к строке таблицы статей."""
    props = node.get("props") if isinstance(node.get("props"), dict) else {}
    return {
        "natural_key": node.get("natural_key", ""),
        "title": node.get("title", ""),
        "type": node.get("type", ""),
        "source": props.get("source") or node.get("source_path") or "",
        "updated": node.get("last_seen") or "",
        "pii": bool(props.get("pii", False)),
        "pii_categories": props.get("pii_categories") or [],
    }


def _audit_rows(graph, ns: str, limit: int = _MAX_ROWS) -> list[dict[str, Any]]:
    """Последние события аудита KB (новые сверху)."""
    events = graph.list_nodes(type="audit_event", limit=500, namespaces=[ns])
    rows = []
    for event in events:
        props = event.get("props") if isinstance(event.get("props"), dict) else {}
        rows.append(
            {
                "ts": props.get("ts", ""),
                "action": props.get("action", event.get("title", "")),
                "actor": props.get("actor", ""),
                "trace_id": props.get("trace_id", ""),
                "payload": props.get("payload") or {},
            }
        )
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows[:limit]


# ---------------- Overview ----------------


@router.get("", response_class=HTMLResponse)
def overview(request: Request, ns: str | None = Query(default=None)) -> HTMLResponse:
    """Обзор: живость БД, выбранная KB, счётчики узлов/чанков, последние операции."""
    eng = _engine()
    settings = _console_settings()
    ns, allowed = _resolve_ns(settings, ns)
    stats: dict[str, Any] = {"ok": False, "nodes": 0, "chunks": 0}
    recent: list[dict[str, Any]] = []
    error = ""
    try:
        conn = eng.dbmod.connect(settings)
        try:
            graph = eng.GraphStore(conn, settings.graph_name, settings.default_namespace)
            index = eng.VectorIndex(conn, settings.chunks_table, settings.embedding_dim)
            stats = {
                "ok": True,
                "graph": settings.graph_name,
                "nodes_by_type": {
                    t: n
                    for t, n in sorted(graph.count_nodes_by_type(namespaces=[ns]).items())
                    if t not in _NON_ARTICLE_TYPES
                },
                "chunks": index.count_chunks([ns]) if index.table_exists() else 0,
            }
            stats["nodes"] = sum(stats["nodes_by_type"].values())
            recent = _audit_rows(graph, ns, limit=5)
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — обзор должен открываться и при лежащей БД
        error = str(exc)
    return _page(
        request,
        "overview.html",
        {
            "section": "overview",
            "ns": ns,
            "allowed": allowed,
            "stats": stats,
            "recent": recent,
            "error": error,
        },
    )


# ---------------- Статьи ----------------


@router.get("/articles", response_class=HTMLResponse)
def articles(
    request: Request,
    ns: str | None = Query(default=None),
    q: str = Query(default=""),
    type: str = Query(default=""),
) -> HTMLResponse:
    """Таблица статей текущей KB: title, source, type, updated, chunks, ПДн-маркер."""
    eng = _engine()
    settings = _console_settings()
    ns, allowed = _resolve_ns(settings, ns)
    conn = eng.dbmod.connect(settings)
    try:
        graph = eng.GraphStore(conn, settings.graph_name, settings.default_namespace)
        index = eng.VectorIndex(conn, settings.chunks_table, settings.embedding_dim)
        nodes = graph.list_nodes(type=type or None, limit=500, namespaces=[ns])
        rows = [_node_row(n) for n in nodes if n.get("type") not in _NON_ARTICLE_TYPES]
        needle = q.strip().lower()
        if needle:
            rows = [
                r
                for r in rows
                if needle in r["title"].lower() or needle in r["natural_key"].lower()
            ]
        rows.sort(key=lambda r: r["updated"], reverse=True)
        total = len(rows)
        rows = rows[:_MAX_ROWS]
        if index.table_exists():
            for row in rows:
                row["chunks"] = index.count_for_node(row["natural_key"], namespace=ns)
    finally:
        conn.close()
    return _page(
        request,
        "articles.html",
        {
            "section": "articles",
            "ns": ns,
            "allowed": allowed,
            "rows": rows,
            "total": total,
            "shown": len(rows),
            "q": q,
            "type": type,
        },
    )


@router.get("/articles/view", response_class=HTMLResponse)
def article_view(
    request: Request,
    key: str,
    ns: str | None = Query(default=None),
) -> HTMLResponse:
    """Полный оригинал статьи + provenance (тот же движок, что /api/brain/sources)."""
    eng = _engine()
    settings = _console_settings()
    ns, allowed = _resolve_ns(settings, ns)
    trace_id = uuid.uuid4().hex
    try:
        source = eng.brain_source(
            key, namespace=ns, principal=_console_principal(), x_run_id=trace_id
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            return _page(
                request,
                "article_view.html",
                {
                    "section": "articles",
                    "ns": ns,
                    "allowed": allowed,
                    "source": None,
                    "key": key,
                    "trace_id": trace_id,
                },
                status_code=404,
            )
        raise
    return _page(
        request,
        "article_view.html",
        {
            "section": "articles",
            "ns": ns,
            "allowed": allowed,
            "source": source,
            "key": key,
            "trace_id": trace_id,
        },
    )


@router.post("/articles/delete", response_class=HTMLResponse)
def article_delete(
    request: Request,
    key: str = Form(...),
    ns: str = Form(...),
    confirm: str = Form(default=""),
) -> HTMLResponse:
    """Удалить статью (безвозвратно, с аудитом) — только с явным подтверждением."""
    eng = _engine()
    settings = _console_settings()
    _check_origin(request)
    ns, allowed = _resolve_ns(settings, ns)
    if confirm != "yes":
        return _page(
            request,
            "result.html",
            {
                "section": "articles",
                "ns": ns,
                "allowed": allowed,
                "ok": False,
                "message": "Удаление не подтверждено: отметьте чекбокс подтверждения.",
                "details": {"natural_key": key},
                "trace_id": "",
            },
            status_code=400,
        )
    trace_id = uuid.uuid4().hex
    try:
        result = eng.brain_node_delete(
            key, namespace=ns, actor="console", trace_id=trace_id, x_run_id=None
        )
    except HTTPException as exc:
        return _page(
            request,
            "result.html",
            {
                "section": "articles",
                "ns": ns,
                "allowed": allowed,
                "ok": False,
                "message": f"Удаление не выполнено: {exc.detail}",
                "details": {"natural_key": key},
                "trace_id": trace_id,
            },
            status_code=exc.status_code,
        )
    return _page(
        request,
        "result.html",
        {
            "section": "articles",
            "ns": ns,
            "allowed": allowed,
            "ok": True,
            "message": "Статья удалена (безвозвратно). Акт зафиксирован в аудите.",
            "details": {
                "natural_key": result["natural_key"],
                "title": result.get("title"),
                "chunks_deleted": result.get("chunks_deleted"),
            },
            "trace_id": result.get("trace_id", trace_id),
        },
    )


# ---------------- Импорт ----------------


@router.get("/import", response_class=HTMLResponse)
def import_form(request: Request, ns: str | None = Query(default=None)) -> HTMLResponse:
    """Форма поштучной загрузки статьи (сырой текст, Ctrl-A/Ctrl-C со страницы)."""
    settings = _console_settings()
    ns, allowed = _resolve_ns(settings, ns)
    return _page(
        request,
        "import.html",
        {"section": "import", "ns": ns, "allowed": allowed, "error": "", "form": {}},
    )


@router.post("/import", response_class=HTMLResponse)
def import_submit(
    request: Request,
    ns: str = Form(...),
    content: str = Form(default=""),
    title: str = Form(default=""),
    external_id: str = Form(default=""),
    source: str = Form(default=""),
) -> HTMLResponse:
    """Идемпотентный retain выбранной KB + аудит-событие import с trace_id."""
    eng = _engine()
    settings = _console_settings()
    _check_origin(request)
    ns, allowed = _resolve_ns(settings, ns)
    form = {"content": content, "title": title, "external_id": external_id, "source": source}
    if not content.strip():
        return _page(
            request,
            "import.html",
            {
                "section": "import",
                "ns": ns,
                "allowed": allowed,
                "error": "Текст статьи пуст — вставьте содержимое.",
                "form": form,
            },
            status_code=400,
        )
    trace_id = uuid.uuid4().hex
    req = eng.RetainRequest(
        content=content,
        type="article",
        title=title.strip() or None,
        external_id=external_id.strip() or None,
        provenance={
            "source": source.strip() or "console",
            "actor": "console",
            "trace_id": trace_id,
        },
        scope={"namespace": ns},
    )
    try:
        result = eng.brain_retain(req, x_run_id=trace_id, visible=_console_write_visibility())
    except HTTPException as exc:
        return _page(
            request,
            "import.html",
            {
                "section": "import",
                "ns": ns,
                "allowed": allowed,
                "error": f"Импорт не выполнен: {exc.detail}",
                "form": form,
            },
            status_code=exc.status_code,
        )
    # Аудит-событие import — след операции виден на вкладке «Аудит» этой KB.
    try:
        conn = eng.dbmod.connect(settings)
        try:
            eng.GraphStore(conn, settings.graph_name, settings.default_namespace).record_audit(
                trace_id,
                "console",
                "import",
                payload={"natural_key": result.get("natural_key"), "title": result.get("title")},
                namespace=ns,
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — журнал не должен ронять результат импорта
        pass
    details = {
        "natural_key": result.get("natural_key"),
        "title": result.get("title"),
        "namespace": ns,
    }
    if result.get("pii_categories"):
        details["pii_categories"] = ", ".join(result["pii_categories"])
    return _page(
        request,
        "result.html",
        {
            "section": "import",
            "ns": ns,
            "allowed": allowed,
            "ok": True,
            "message": "Статья записана (повторная отправка с тем же external_id обновит её же).",
            "details": details,
            "trace_id": trace_id,
            "view_key": result.get("natural_key"),
        },
    )


# ---------------- Спросить (демо Q&A) ----------------


@router.get("/ask", response_class=HTMLResponse)
def ask_form(request: Request, ns: str | None = Query(default=None)) -> HTMLResponse:
    """Форма вопроса. Сам вопрос принимается только POST'ом: в вопросе оператора
    регулярно встречаются ПДн клиента, и в query string (access-логи proxy/uvicorn,
    история браузера) он попадать не должен."""
    settings = _console_settings()
    ns, allowed = _resolve_ns(settings, ns)
    return _page(
        request,
        "ask.html",
        {
            "section": "ask",
            "ns": ns,
            "allowed": allowed,
            "q": "",
            "answer": None,
            "error": "",
            "trace_id": "",
        },
    )


@router.post("/ask", response_class=HTMLResponse)
def ask_submit(
    request: Request,
    ns: str = Form(...),
    q: str = Form(default=""),
) -> HTMLResponse:
    """Демо продукта: вопрос -> ответ LLM с цитатами (тот же движок, что /api/brain/query)."""
    eng = _engine()
    settings = _console_settings()
    _check_origin(request)
    ns, allowed = _resolve_ns(settings, ns)
    answer: dict[str, Any] | None = None
    error = ""
    trace_id = uuid.uuid4().hex
    if q.strip():
        try:
            ans = eng.run_query(settings, q, k=8, hops=1, namespaces=[ns])
            payload = {
                "text": ans.text,
                "sources": [
                    {"source_path": s.source_path, "node_key": s.node_key, "title": s.title}
                    for s in ans.sources
                ],
                "hits": ans.hits,
                "neighbors": ans.neighbors,
            }
            # Тот же PII-контур, что у API: full-допуск console журналируется.
            answer = eng.protect_pii_response(
                payload, _console_principal(), "console_ask", trace_id, ns
            )
        except Exception as exc:  # noqa: BLE001 — оператору нужна причина, а не 500
            error = str(exc)
    return _page(
        request,
        "ask.html",
        {
            "section": "ask",
            "ns": ns,
            "allowed": allowed,
            "q": q,
            "answer": answer,
            "error": error,
            "trace_id": trace_id,
        },
    )


# ---------------- Проверка поиска ----------------


@router.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    ns: str | None = Query(default=None),
    q: str = Query(default=""),
) -> HTMLResponse:
    """Вопрос -> recall по выбранной KB: top-результаты, источник, score, оригинал в один клик."""
    eng = _engine()
    settings = _console_settings()
    ns, allowed = _resolve_ns(settings, ns)
    results: list[dict[str, Any]] = []
    error = ""
    trace_id = uuid.uuid4().hex
    if q.strip():
        try:
            r = eng.retrieve(settings, q, k=8, namespaces=[ns])
            payload = {
                "results": [
                    {
                        "node_key": h.node_key,
                        "title": h.title,
                        "heading": h.heading,
                        "source_path": h.source_path,
                        "score": round(float(h.score), 4),
                        "text": h.text[:400] + ("…" if len(h.text) > 400 else ""),
                    }
                    for h in r.hits
                ]
            }
            # Тот же PII-контур, что у API: full-допуск console журналируется.
            payload = eng.protect_pii_response(
                payload, _console_principal(), "console_search", trace_id, ns
            )
            results = payload["results"]
        except Exception as exc:  # noqa: BLE001 — оператору нужна причина, а не 500
            error = str(exc)
    return _page(
        request,
        "search.html",
        {
            "section": "search",
            "ns": ns,
            "allowed": allowed,
            "q": q,
            "results": results,
            "error": error,
            "trace_id": trace_id,
        },
    )


# ---------------- Аудит ----------------


@router.get("/audit", response_class=HTMLResponse)
def audit(request: Request, ns: str | None = Query(default=None)) -> HTMLResponse:
    """Последние события аудита выбранной KB: import/delete/pii_access и прочие."""
    eng = _engine()
    settings = _console_settings()
    ns, allowed = _resolve_ns(settings, ns)
    conn = eng.dbmod.connect(settings)
    try:
        graph = eng.GraphStore(conn, settings.graph_name, settings.default_namespace)
        rows = _audit_rows(graph, ns)
    finally:
        conn.close()
    return _page(
        request,
        "audit.html",
        {"section": "audit", "ns": ns, "allowed": allowed, "rows": rows},
    )
