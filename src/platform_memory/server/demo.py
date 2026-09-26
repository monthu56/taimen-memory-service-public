"""Публичная демо-витрина (/demo) — read-only, одна KB, ADR-009.

Витрина ценности «проверяемой памяти» для лида (аудитория — B2B ЮВА, EN): вопрос →
top-k фрагментов со score и цитатой-источником → открытие полного оригинала с
provenance. Работает in-process поверх ТОГО ЖЕ движка, что и Memory API (тот же
``retrieve``, тот же source-view ``brain_source``).

Отличия от Console (сознательно жёстче — поверхность публичная):
- **Публичная (без Bearer).** Браузер ходит ТОЛЬКО в ``/demo`` и ``/demo/api/*``;
  API-ключ сервиса (CB_SERVER_API_KEY) в браузер не передаётся ни в каком виде.
- **Ровно одна KB.** Namespace жёстко = ``CB_DEMO_NAMESPACE``; клиент его НЕ задаёт —
  изоляция абсолютная, чужие базы недостижимы с витрины.
- **Только чтение.** Search (``retrieve``) + source-view (``brain_source``). Никаких
  retain/delete/import/audit/facts и никаких других namespace.
- **Маскированный принципал.** Выдача всегда идёт через ``protect_pii_response`` с
  ``access="masked"`` — даже если в демо-KB случайно попадут ПДн, наружу они не утекут.
- **Rate-limit по IP** на публичном входе (``CB_DEMO_RATE_LIMIT`` запросов/мин).
- **Без утечки внутренностей.** Ошибки движка отдаются обобщённым сообщением (без
  stack trace / DB URI); source-view отдаёт только display-поля, не служебные.

Выключена по умолчанию (``CB_DEMO_PUBLIC_ENABLED=false``) → 404 на всех маршрутах.
НЕ включать на контуре с реальными данными.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from platform_memory.core.config import Settings

router = APIRouter(include_in_schema=False)
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Путь витрины на приложении. Все URL внутри страницы строятся от инъектируемого
# DEMO_BASE, поэтому страница одинаково работает и на поддомене (demo.taimen.ai),
# и на подпути (…/demo) — см. ADR-009.
_DEMO_BASE = "/demo"

# Максимальная длина вопроса с публичного входа (защита от абьюза; лишнее режется).
_MAX_QUERY_LEN = 500

# Примеры вопросов — разные сценарии использования памяти компании (кликом заполняют
# строку поиска): политики/процессы, продукт, безопасность, решения, люди, онбординг.
_SAMPLE_QUESTIONS = [
    "What is our data retention policy?",
    "How does zero-trust remote access work?",
    "What's the runbook when a security incident happens?",
    "What did we decide about the VPN and MFA rollout?",
    "How do I set up multi-factor authentication?",
    "What are the API rate limits?",
    "How does onboarding work for a new hire?",
    "How do I get a business trip expense reimbursed?",
]

# Сценарии использования памяти (сетка 3×3). Каждый — как сессия агента-харнеса:
# последовательность запросов, «взгляд вглубь» памяти в несколько шагов. Кликом карточка
# проигрывает шаги по очереди. Запросы подобраны под наполнение демо-KB.
_SCENARIOS = [
    {
        "kicker": "Tender assistant",
        "title": "Build a competitive bid",
        "steps": [
            {
                "label": "Our security posture",
                "query": "What are our security controls and data protection measures?",
            },
            {
                "label": "Relevant decisions",
                "query": "What did we decide about data retention and access control?",
            },
            {"label": "Experts to field", "query": "Who leads security and compliance?"},
        ],
    },
    {
        "kicker": "Personal assistant",
        "title": "Get a new hire productive",
        "steps": [
            {"label": "Onboarding process", "query": "How does onboarding work for a new hire?"},
            {
                "label": "Access they need",
                "query": "What accounts and access does a new hire get provisioned?",
            },
            {"label": "Who to ask", "query": "Who handles people operations and HR?"},
        ],
    },
    {
        "kicker": "Call-center support",
        "title": "Customer locked out after a device swap",
        "steps": [
            {"label": "How access works", "query": "How does zero-trust remote access work?"},
            {
                "label": "Device compliance",
                "query": "What is the device enrollment and compliance policy?",
            },
            {
                "label": "SLA & on-call",
                "query": "What are the SLA response targets and who is on-call?",
            },
        ],
    },
    {
        "kicker": "Document prep",
        "title": "Draft an incident post-mortem",
        "steps": [
            {"label": "The runbook", "query": "What is the incident response runbook?"},
            {
                "label": "Post-mortem policy",
                "query": "What did we decide about blameless post-mortems?",
            },
            {"label": "Who was involved", "query": "Who is responsible for incident response?"},
        ],
    },
    {
        "kicker": "Task-setting",
        "title": "Plan the zero-trust migration",
        "steps": [
            {
                "label": "The rollout decision",
                "query": "What did we decide about the VPN and MFA rollout?",
            },
            {
                "label": "Tasks in the project",
                "query": "What tasks are part of the zero-trust remote access project?",
            },
            {
                "label": "Owner & dependencies",
                "query": "Who owns the zero-trust project and what does it depend on?",
            },
        ],
    },
    {
        "kicker": "Security review",
        "title": "Prepare for an audit",
        "steps": [
            {
                "label": "Data security controls",
                "query": "What are our data security and encryption controls?",
            },
            {
                "label": "Access lifecycle",
                "query": "What is our access provisioning and deprovisioning process?",
            },
            {"label": "Audit logging", "query": "What audit logging do we keep and why?"},
        ],
    },
    {
        "kicker": "Procurement",
        "title": "Approve a vendor purchase",
        "steps": [
            {
                "label": "PO approval",
                "query": "What is the purchase order approval process and thresholds?",
            },
            {"label": "Vendor onboarding", "query": "How do we onboard a new vendor?"},
            {"label": "Who approves", "query": "Who approves procurement and budget?"},
        ],
    },
    {
        "kicker": "HR & expenses",
        "title": "Answer an expenses question",
        "steps": [
            {"label": "Reimbursement", "query": "How do I get a business trip expense reimbursed?"},
            {"label": "Travel policy", "query": "What is the travel and per-diem policy?"},
            {"label": "Who handles it", "query": "Who processes expenses and travel?"},
        ],
    },
    {
        "kicker": "Exec briefing",
        "title": "Catch up on the Atlas product",
        "steps": [
            {
                "label": "Key decisions",
                "query": "What decisions shaped the Atlas identity product?",
            },
            {"label": "Current MFA policy", "query": "What is the current MFA policy?"},
            {
                "label": "Owners & open questions",
                "query": "Who owns Atlas and what questions are still open?",
            },
        ],
    },
]

# Итоговый вопрос сценария — по нему после прогона шагов синтезируется финальный LLM-ответ.
_SCENARIO_GOALS = {
    "Tender assistant": "Summarize our security posture and the key decisions to cite in a competitive bid.",
    "Personal assistant": "Summarize how to get a new hire set up: onboarding, accounts and access, and who to ask.",
    "Call-center support": "How do I help a customer who is locked out after a device change?",
    "Document prep": "Outline an incident post-mortem based on our runbook and post-mortem policy.",
    "Task-setting": "Summarize the plan, tasks, owner and dependencies for the zero-trust migration.",
    "Security review": "Summarize our data-security, access-lifecycle and audit-logging controls for an audit.",
    "Procurement": "Summarize the vendor purchase approval process, thresholds and who approves.",
    "HR & expenses": "Summarize how business-trip expenses are reimbursed and the travel policy.",
    "Exec briefing": "Summarize the key decisions, current MFA policy and open questions for the Atlas product.",
}

# Системный промпт финального ответа витрины — ПО-АНГЛИЙСКИ, строго из контекста памяти.
_DEMO_ANSWER_SYSTEM = (
    "You are a company-memory assistant. Answer the user's request concisely and ONLY from the "
    "provided context (the company's memory). Do not invent facts; if the context lacks the answer, "
    "say so plainly. Prefer 3-6 sentences or short bullets. Write in English. The human reviews and "
    "decides — present findings, don't act."
)


# Rate-limit: скользящее окно 60с, in-process, по IP. Для одного демо-инстанса
# достаточно; распределённый лимит — задача reverse proxy, если понадобится.
_RATE_WINDOW_S = 60.0
# Жёсткий потолок числа отслеживаемых IP: защита памяти от всплеска уникальных клиентов.
_MAX_TRACKED_IPS = 8192
_rate_hits: dict[str, deque[float]] = {}


def _engine():
    """Модуль server.app — лениво (разрыв цикла импорта app ↔ demo).

    Доступ через модуль, а не прямые импорты символов: monkeypatch фейков движка
    (retrieve/brain_source/protect_pii_response/get_settings) в тестах действует и здесь.
    """
    from platform_memory.server import app as app_mod

    return app_mod


def _demo_settings() -> Settings:
    """Гейт витрины: выключена конфигом → 404 на всех маршрутах."""
    settings = _engine().get_settings()
    if not settings.demo_public_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    return settings


def _demo_principal():
    """Принципал витрины: ВСЕГДА маскированный допуск, метка ``demo`` в журнале."""
    return _engine().Principal(access="masked", label="demo")


def _client_ip(request: Request) -> str:
    """IP клиента за доверенным reverse proxy.

    Начало цепочки X-Forwarded-For клиент может подделать из браузера, поэтому левый
    хоп для rate-limit брать НЕЛЬЗЯ (иначе лимит обходится сменой заголовка). Доверенный
    прокси (Traefik) ДОПИСЫВАЕТ реальный peer-IP в КОНЕЦ цепочки — берём ПОСЛЕДНИЙ хоп.
    Допущение: ровно один доверенный прокси перед сервисом (так и развёрнут стенд). Без
    заголовка — адрес соединения.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in fwd.split(",") if p.strip()]
    if parts:
        return parts[-1]
    client = request.client
    return client.host if client else "unknown"


def _prune_rate_hits(cutoff: float) -> None:
    """Ограничить память rate-limit: снести протухшие корзины, при переполнении —
    вытеснить самые старые по последнему обращению."""
    for k in [k for k, b in _rate_hits.items() if not b or b[-1] < cutoff]:
        _rate_hits.pop(k, None)
    if len(_rate_hits) > _MAX_TRACKED_IPS:
        overflow = len(_rate_hits) - _MAX_TRACKED_IPS
        oldest = sorted(_rate_hits.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
        for k, _b in oldest[:overflow]:
            _rate_hits.pop(k, None)


def _rate_check(settings: Settings, request: Request) -> None:
    """Скользящее окно по IP: превышение CB_DEMO_RATE_LIMIT/мин → 429."""
    limit = settings.demo_rate_limit
    if limit <= 0:
        return
    now = time.monotonic()
    cutoff = now - _RATE_WINDOW_S
    ip = _client_ip(request)
    bucket = _rate_hits.get(ip)
    if bucket is None:
        bucket = _rate_hits[ip] = deque()
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded — please slow down.")
    bucket.append(now)
    # Держим словарь ограниченным: всплеск уникальных IP не должен исчерпать память.
    if len(_rate_hits) > _MAX_TRACKED_IPS:
        _prune_rate_hits(cutoff)


def demo_page_context(
    settings: Settings, *, mode: str = "live", base: str = _DEMO_BASE, corpus: Any = None
) -> dict[str, Any]:
    """Контекст страницы витрины. Используется и роутом (live), и сборкой canned.

    ``mode`` — 'live' (ходит в /demo/api/*) или 'canned' (ищет по вшитому корпусу).
    ``base`` — префикс URL страницы (для live). ``corpus`` — список статей для canned.
    """
    cta = settings.demo_cta_url or (
        f"mailto:hello@{settings.demo_domain}"
        if settings.demo_domain
        else "mailto:hello@example.com"
    )
    config = {
        "mode": mode,
        "base": base,
        "apiSearch": f"{base}/api/search",
        "apiSource": f"{base}/api/source",
        "apiAnswer": f"{base}/api/answer",
        "sampleQuestions": _SAMPLE_QUESTIONS,
        "scenarios": [
            {**sc, "goal": _SCENARIO_GOALS.get(sc["kicker"], sc["title"])} for sc in _SCENARIOS
        ],
        "brand": {"name": settings.demo_brand_name, "tagline": settings.demo_brand_tagline},
        "cta": cta,
        "searchK": settings.demo_search_k,
        "domain": settings.demo_domain,
    }
    return {
        "brand_name": settings.demo_brand_name,
        "brand_tagline": settings.demo_brand_tagline,
        "mode": mode,
        # Заранее сериализуем в строки: в шаблоне выводим внутри <script type=json> через
        # | safe. Экранируем "</" → нельзя закрыть <script> данными корпуса/конфига (XSS).
        "config_json": _json_for_html(config),
        "corpus_json": _json_for_html(corpus) if corpus is not None else "null",
    }


def _json_for_html(value: Any) -> str:
    """JSON для встраивания в <script>: "</" экранируется, чтобы не закрыть тег."""
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def _pipeline_stages(
    settings: Settings, r: Any, hits: list, subgraph: dict, reranked: bool
) -> list[dict[str, Any]]:
    """Этапы конвейера поиска с РЕАЛЬНЫМИ счётчиками — чтобы наглядно показать процесс.

    Воронка движка: гибридный поиск (вектор+FTS, RRF) → обход графа компании → LLM-реранк
    по настоящей релевантности → порог уверенности. Числа берём из ``r.stats`` (наполняется
    ``retrieve``); при их отсутствии (старый фейк в тестах) этапы деградируют мягко.
    """
    stats = getattr(r, "stats", {}) or {}
    r_hits = list(getattr(r, "hits", []) or [])
    vector_candidates = int(stats.get("vector_candidates") or len(r_hits))
    pool = int(stats.get("pool") or vector_candidates)
    graph_nodes = len(subgraph.get("nodes") or [])
    top_conf = None
    if reranked and hits and getattr(hits[0], "rerank_score", None) is not None:
        top_conf = round(float(hits[0].rerank_score), 4)

    stages: list[dict[str, Any]] = [
        {
            "key": "search",
            "label": "Hybrid search",
            "detail": "vector + full-text, RRF-fused",
            "count": vector_candidates,
            "unit": "candidate passages",
        },
        {
            "key": "graph",
            "label": "Graph expansion",
            "detail": "typed edges: people, decisions, projects",
            "count": graph_nodes,
            "unit": "related nodes",
        },
    ]
    if reranked:
        gate_pct = int(round(settings.demo_min_confidence * 100))
        stages.append(
            {
                "key": "rerank",
                "label": "LLM rerank",
                "detail": "each candidate scored by true relevance",
                "count": pool,
                "unit": "candidates scored",
                "confidence": top_conf,
            }
        )
        stages.append(
            {
                "key": "gate",
                "label": "Confidence gate",
                "detail": f"kept ≥ {gate_pct}% confidence",
                "count": len(hits),
                "unit": "passages shown",
                "dropped": max(0, len(r_hits) - len(hits)),
            }
        )
    else:
        stages.append(
            {
                "key": "rank",
                "label": "Ranked",
                "detail": "ordered by hybrid score",
                "count": len(hits),
                "unit": "passages shown",
            }
        )
    return stages


# ---------------- страница ----------------


@router.get("/", include_in_schema=False)
def demo_root_redirect() -> RedirectResponse:
    """Корень контура витрины → /demo (удобно для поддомена demo.*). Гейт: выкл → 404."""
    _demo_settings()
    return RedirectResponse(url=_DEMO_BASE, status_code=302)


@router.get("/demo", response_class=HTMLResponse)
@router.get("/demo/", response_class=HTMLResponse)
def demo_page(request: Request) -> HTMLResponse:
    """Витрина: hero + «Search Check» + how-it-works + CTA. Ключ в HTML не попадает."""
    settings = _demo_settings()
    ctx = demo_page_context(settings, mode="live", base=_DEMO_BASE)
    return _templates.TemplateResponse(request=request, name="demo.html", context=ctx)


# ---------------- read-only API витрины ----------------


class DemoSearchRequest(BaseModel):
    query: str


@router.post("/demo/api/search")
def demo_search(req: DemoSearchRequest, request: Request) -> dict[str, Any]:
    """Публичный read-only поиск по ОДНОЙ демо-KB: top-k фрагментов + score + источник.

    Namespace жёстко ``CB_DEMO_NAMESPACE`` (клиент не задаёт). Выдача — маскированная.
    Ошибка движка → обобщённое 503 (без внутренностей на публичной поверхности).
    """
    settings = _demo_settings()
    _rate_check(settings, request)
    eng = _engine()
    q = (req.query or "").strip()[:_MAX_QUERY_LEN]
    if not q:
        return {"query": "", "count": 0, "results": [], "sources": []}
    ns = settings.demo_namespace
    trace_id = uuid.uuid4().hex
    t0 = time.monotonic()
    try:
        # Витрина реранкует свой поиск (ADR-005): настоящая 0..1 уверенность и лучший
        # порядок. Флаг demo_rerank отдельный — realtime /api/brain не затрагивается.
        r = eng.retrieve(
            settings, q, k=settings.demo_search_k, namespaces=[ns], rerank=settings.demo_rerank
        )
    except Exception:  # noqa: BLE001 — публичной поверхности не показываем причину/стек
        raise HTTPException(status_code=503, detail="Search temporarily unavailable.") from None
    reranked = any(getattr(h, "rerank_score", None) is not None for h in r.hits)
    # Порог уверенности: не показываем нерелевантные карточки (и не тащим их в seeds графа).
    # Действует только когда реранк отработал — иначе нечем судить (fallback показывает всё).
    hits = list(r.hits)
    if reranked and settings.demo_min_confidence > 0:
        hits = [h for h in hits if (h.rerank_score or 0.0) >= settings.demo_min_confidence]
    # Подграф окрестности: найденное связано в графе компании с людьми, решениями,
    # проектами, концептами (типизированные рёбра) — память о людях и процессах, а не
    # плоский RAG. Ассистивно: показывает, кто знает/решал, чтобы помочь человеку.
    seed_keys = list(dict.fromkeys(h.node_key for h in hits))
    try:
        conn = eng.dbmod.connect(settings)
        try:
            graph = eng.GraphStore(conn, settings.graph_name, settings.default_namespace)
            subgraph = graph.neighborhood_subgraph(seed_keys, namespaces=[ns], hops=2, per_seed=6)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — граф-панель необязательна, поиск не роняем
        subgraph = {"nodes": [], "edges": []}
    took_ms = int((time.monotonic() - t0) * 1000)  # реальное время движка — «почти realtime»
    payload = {
        "query": q,
        "count": len(hits),
        "reranked": reranked,
        "took_ms": took_ms,
        "pipeline": _pipeline_stages(settings, r, hits, subgraph, reranked),
        "graph": subgraph,
        "results": [
            {
                "node_key": h.node_key,
                "title": h.title,
                "heading": h.heading,
                "source_path": h.source_path,
                "score": round(float(h.score), 4),
                # rerank_score — настоящая релевантность 0..1 (когда реранк включён), иначе null.
                "rerank_score": (
                    round(float(h.rerank_score), 4) if h.rerank_score is not None else None
                ),
                # Ключ "text" (а не "snippet") — чтобы фрагмент прошёл PII-маскирование.
                "text": h.text[:320] + ("…" if len(h.text) > 320 else ""),
            }
            for h in hits
        ],
        "sources": [
            {"source_path": h.source_path, "node_key": h.node_key, "title": h.title} for h in hits
        ],
    }
    return eng.protect_pii_response(payload, _demo_principal(), "demo_search", trace_id, ns)


class DemoAnswerRequest(BaseModel):
    query: str


@router.post("/demo/api/answer")
def demo_answer(req: DemoAnswerRequest, request: Request) -> dict[str, Any]:
    """Финальный LLM-ответ витрины: retrieve (с реранком) + синтез на английском с цитатами.

    Кульминация сценария — «что ассистент выяснил в памяти». Read-only, одна демо-KB,
    маскированная выдача. Ошибка/недоступность LLM → обобщённое 503.
    """
    settings = _demo_settings()
    _rate_check(settings, request)
    eng = _engine()
    q = (req.query or "").strip()[:_MAX_QUERY_LEN]
    if not q:
        return {"query": "", "answer": "", "sources": []}
    ns = settings.demo_namespace
    trace_id = uuid.uuid4().hex
    t0 = time.monotonic()
    try:
        ans = eng.run_query(
            settings,
            q,
            k=settings.demo_search_k,
            namespaces=[ns],
            rerank=False,  # шаги уже показали реранк; финал синтезируем из top-k+граф — быстрее
            system=_DEMO_ANSWER_SYSTEM,
            max_tokens=280,  # короткий ответ витрины → быстрая генерация
        )
    except Exception:  # noqa: BLE001 — публичной поверхности не показываем причину/стек
        raise HTTPException(status_code=503, detail="Answer temporarily unavailable.") from None
    took_ms = int((time.monotonic() - t0) * 1000)
    payload = {
        "query": q,
        "answer": ans.text,
        "took_ms": took_ms,
        "sources": [
            {"source_path": s.source_path, "node_key": s.node_key, "title": s.title}
            for s in ans.sources
        ],
    }
    return eng.protect_pii_response(payload, _demo_principal(), "demo_answer", trace_id, ns)


@router.get("/demo/api/source")
def demo_source(request: Request, key: str = Query(...)) -> dict[str, Any]:
    """Полный оригинал статьи демо-KB + provenance (source-view по клику на цитату).

    Только display-поля (заголовок, текст, источник, confidence, дата) — служебные
    поля provenance (actor_id/trace_id/metadata) наружу не отдаются. Namespace жёстко
    демо-KB; из другой базы статья недостижима (404).
    """
    settings = _demo_settings()
    _rate_check(settings, request)
    eng = _engine()
    ns = settings.demo_namespace
    trace_id = uuid.uuid4().hex
    try:
        src = eng.brain_source(key, namespace=ns, principal=_demo_principal(), x_run_id=trace_id)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="Source not found.") from None
        raise HTTPException(status_code=503, detail="Source temporarily unavailable.") from None
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="Source temporarily unavailable.") from None
    prov = src.get("provenance") if isinstance(src.get("provenance"), dict) else {}
    return {
        "node_key": src.get("natural_key"),
        "title": src.get("title"),
        "content": src.get("content"),
        "content_source": src.get("content_source"),
        "source": prov.get("source") or prov.get("source_path"),
        "source_path": prov.get("source_path"),
        "origin": prov.get("origin"),
        "confidence": prov.get("confidence"),
        "last_seen": prov.get("last_seen"),
    }
