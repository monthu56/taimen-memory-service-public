"""Thin HTTP clients for memory-service.

Two mirror-image clients over the same wire contract:

* :class:`MemoryClient` — synchronous (workers, CLI, scripts).
* :class:`AsyncMemoryClient` — asyncio (FastAPI hosts).

Both target the service's public API — ``/api/brain/*`` (recall, search, retain,
audit, query, documents, nodes, sources) and ``/api/memory/*`` (observations,
context; ADR-016; domain packs, reconcile, typed context; MEM-ADR-020) — and return
typed, forward-compatible models.

    from platform_memory_client import MemoryClient

    mem = MemoryClient("http://memory:8077", token="…")
    ctx = mem.recall("who owns billing?", budget="high")
    for m in ctx.memories:
        print(m.title, m.source_path)

Credential is a bearer token: a static key grant (ADR-017) or an IAM access token
(ADR-018). It is given as a string or as a callable asked before every request, so a
token that is exchanged and refreshed elsewhere (``IamCredential`` of the Control
Plane SDK, a rotated secret file) is picked up without rebuilding the client. The
async client additionally accepts an ``async`` callable / an object with an async
``token()`` method — the ``CredentialProvider`` contract of ``control-plane-client``.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import TracebackType
from typing import Any

import httpx

from platform_memory_client.models import (
    AuditResult,
    ContextPack,
    DocumentChunk,
    DocumentDeleteResult,
    DocumentIngestResult,
    NamespaceKinds,
    NodeDeleteResult,
    NodeResult,
    NodesResult,
    ObservationBatchResult,
    ObservationResult,
    PackageResult,
    QueryResult,
    RecallResult,
    ReconcileResult,
    RetainResult,
    SearchResult,
    SourceResult,
    TypedContextPack,
    EntitiesPage,
)

DEFAULT_TIMEOUT = 10.0

SyncToken = str | Callable[[], str] | None
AsyncToken = str | Callable[[], str] | Callable[[], Awaitable[str]] | Any | None


class MemoryServiceError(RuntimeError):
    """Non-2xx response from the memory service (carries status code + detail)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"memory service error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail

    @property
    def unavailable(self) -> bool:
        """The service did not answer or answered 5xx: an outage, not a client error."""
        return self.status_code == 0 or self.status_code >= 500

    @property
    def not_found(self) -> bool:
        return self.status_code == 404


class MemorySnapshotStaleError(MemoryServiceError):
    """409 ``snapshot_stale`` of ``reconcile``: the ``(source, scope)`` state changed since
    the plan (``expected_state``). Rebuild the plan from ``state_token`` (current state)."""

    def __init__(self, detail: Mapping[str, Any]) -> None:
        super().__init__(409, str(dict(detail)))
        self.expected_state = str(detail.get("expectedState") or "")
        self.state_token = str(detail.get("stateToken") or "")


class MemoryTransportError(MemoryServiceError):
    """The request never got an HTTP answer (connection, timeout, protocol)."""

    def __init__(self, detail: str) -> None:
        super().__init__(0, detail)


def _compact(data: Mapping[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values so optional fields fall back to service defaults."""
    return {k: v for k, v in data.items() if v is not None}


# --- request specs (shared by sync + async clients) ------------------------


def _pack_body(pack: Mapping[str, Any], tenant_namespace: str | None) -> dict[str, Any]:
    """Pack registration body; a tenant pack adds ``scope: tenant`` and its owner."""
    body = dict(pack)
    if tenant_namespace is not None:
        body.update(scope="tenant", namespace=tenant_namespace)
    return body


def _read_scope(
    namespaces: Sequence[str] | None, scope: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Read scope: explicit ``namespaces`` win over a raw ``scope`` dict."""
    if namespaces is not None:
        return {"namespaces": list(namespaces)}
    return scope


def _reconcile_body(
    snapshot: Mapping[str, Any],
    namespace: str | None,
    scopes: Sequence[str] | None,
    dry_run: bool = False,
    expected_state: str | None = None,
) -> dict[str, Any]:
    """Flat snapshot document + write namespace, visibility scopes (MEM-ADR-020) and
    the plan/apply-by-state fields (``dryRun``, ``expectedState``; sent only when set)."""
    body = dict(snapshot)
    if namespace:
        body["namespace"] = namespace
    if scopes is not None:
        body["scopes"] = list(scopes)
    if dry_run:
        body["dryRun"] = True
    if expected_state is not None:
        body["expectedState"] = expected_state
    return body


def _write_scope(namespace: str | None) -> dict[str, Any] | None:
    """Write scope: exactly one namespace (None -> service default)."""
    return {"namespace": namespace} if namespace else None


def _ns_params(namespace: str | None, **extra: Any) -> dict[str, Any]:
    return _compact({"namespace": namespace or None, **extra})


def _recall_spec(
    query: str, budget: str, hops: int, scope: dict[str, Any] | None
) -> dict[str, Any]:
    return _compact({"query": query, "budget": budget, "hops": hops, "scope": scope})


def _search_spec(
    query: str,
    type: str | None,
    status: str | None,
    limit: int,
    meta: dict[str, Any] | None,
    scope: dict[str, Any] | None,
) -> dict[str, Any]:
    filters = _compact({"type": type, "status": status, "limit": limit, "meta": meta})
    return _compact({"query": query, "filters": filters or None, "scope": scope})


def _retain_spec(
    content: str,
    type: str,
    external_id: str | None,
    title: str | None,
    links: list[str] | None,
    confidence: float,
    provenance: dict[str, Any] | None,
    scope: dict[str, Any] | None,
) -> dict[str, Any]:
    return _compact(
        {
            "content": content,
            "type": type,
            "external_id": external_id,
            "title": title,
            "links": links,
            "confidence": confidence,
            "provenance": provenance,
            "scope": scope,
        }
    )


def _audit_spec(
    trace_id: str,
    action: str,
    actor: str,
    payload: dict[str, Any] | None,
    ts: str | None,
    run_id: str | None,
    actor_id: str | None,
    actor_kind: str,
    scope: dict[str, Any] | None,
) -> dict[str, Any]:
    return _compact(
        {
            "trace_id": trace_id,
            "action": action,
            "actor": actor,
            "payload": payload,
            "ts": ts,
            "run_id": run_id,
            "actor_id": actor_id,
            "actor_kind": actor_kind,
            "scope": scope,
        }
    )


def _query_spec(
    question: str, k: int, hops: int, synthesize: bool | None, scope: dict[str, Any] | None
) -> dict[str, Any]:
    return _compact(
        {"question": question, "k": k, "hops": hops, "synthesize": synthesize, "scope": scope}
    )


def _document_spec(
    natural_key: str,
    title: str,
    chunks: Sequence[DocumentChunk | dict[str, Any]],
    namespace: str | None,
    type: str,
    source_path: str,
    properties: dict[str, Any] | None,
    meta: dict[str, Any] | None,
    links: list[str] | None,
    replace: bool,
) -> dict[str, Any]:
    chunk_dicts = [
        c.model_dump(exclude_none=True) if isinstance(c, DocumentChunk) else dict(c) for c in chunks
    ]
    return _compact(
        {
            "namespace": namespace,
            "natural_key": natural_key,
            "title": title,
            "type": type,
            "source_path": source_path or None,
            "properties": properties,
            "meta": meta,
            "links": links,
            "chunks": chunk_dicts,
            "replace": replace,
        }
    )


def _context_spec(request: Mapping[str, Any], scope: dict[str, Any] | None) -> dict[str, Any]:
    body = dict(request)
    if scope is not None:
        body["scope"] = scope
    return body


def _run_headers(run_id: str | None) -> dict[str, str]:
    return {"X-Run-Id": run_id} if run_id else {}


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        detail = response.json().get("detail", response.text)
    except Exception:  # noqa: BLE001
        detail = response.text
    if (
        response.status_code == 409
        and isinstance(detail, dict)
        and detail.get("code") == "snapshot_stale"
    ):
        raise MemorySnapshotStaleError(detail)
    raise MemoryServiceError(response.status_code, str(detail))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


class MemoryClient:
    """Synchronous client. Use as a context manager or call :meth:`close`."""

    def __init__(
        self,
        base_url: str,
        *,
        token: SyncToken = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        # ``api_key`` is the historical name of a static bearer; ``token`` also takes
        # a callable so a credential refreshed elsewhere is read on every request.
        self._token: SyncToken = token if token is not None else api_key
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def __enter__(self) -> MemoryClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        token = self._token() if callable(self._token) else self._token
        return {**_bearer(token or ""), **(extra or {})}

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        try:
            response = self._client.request(
                method, path, json=json, params=params, headers=self._headers(headers)
            )
        except httpx.HTTPError as exc:
            raise MemoryTransportError(f"{type(exc).__name__}: {exc}") from exc
        _raise_for_status(response)
        return response.json() if response.content else {}

    def _post(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        return self._send("POST", path, json=body, headers=headers)

    def healthz(self) -> dict[str, Any]:
        """Liveness + basic graph stats (unauthenticated ``/healthz``)."""
        data: dict[str, Any] = self._send("GET", "/healthz")
        return data

    # -- /api/brain: retrieval -------------------------------------------------

    def recall(
        self,
        query: str,
        *,
        budget: str = "mid",
        hops: int = 1,
        namespaces: Sequence[str] | None = None,
        scope: dict[str, Any] | None = None,
        allowed_namespaces: Sequence[str] | None = None,
        allowed_scopes: Sequence[str] | None = None,
    ) -> RecallResult:
        """Relevant context with citations. ``budget`` is one of low | mid | high.

        ``namespaces`` scopes the read (own namespaces + ``shared``); omitted ->
        the service default namespace. ``allowed_namespaces`` / ``allowed_scopes``
        narrow visibility (MEM-ADR-021): intersected with the server's, never wider.
        """
        return RecallResult.model_validate(
            self._post(
                "/recall",
                _with_visibility(
                    _recall_spec(query, budget, hops, _read_scope(namespaces, scope)),
                    allowed_namespaces,
                    allowed_scopes,
                ),
            )
        )

    def search(
        self,
        query: str,
        *,
        type: str | None = None,
        status: str | None = None,
        limit: int = 20,
        meta: dict[str, Any] | None = None,
        namespaces: Sequence[str] | None = None,
        allowed_namespaces: Sequence[str] | None = None,
        allowed_scopes: Sequence[str] | None = None,
    ) -> SearchResult:
        """Structural search (with ``type``) or semantic top-k otherwise.

        ``meta`` is a jsonb-containment filter over chunk tags (semantic mode only),
        e.g. ``{"collection": "licenses"}``. ``allowed_*`` — narrowing, as in ``recall``.
        """
        return SearchResult.model_validate(
            self._post(
                "/search",
                _with_visibility(
                    _search_spec(query, type, status, limit, meta, _read_scope(namespaces)),
                    allowed_namespaces,
                    allowed_scopes,
                ),
            )
        )

    def query(
        self,
        question: str,
        *,
        k: int = 8,
        hops: int = 1,
        synthesize: bool | None = None,
        namespaces: Sequence[str] | None = None,
        allowed_namespaces: Sequence[str] | None = None,
        allowed_scopes: Sequence[str] | None = None,
    ) -> QueryResult:
        """Ask a question; get hits, neighbors and (optionally) an LLM answer.

        ``allowed_*`` — narrowing, as in ``recall``."""
        return QueryResult.model_validate(
            self._post(
                "/api/brain/query",
                _with_visibility(
                    _query_spec(question, k, hops, synthesize, _read_scope(namespaces)),
                    allowed_namespaces,
                    allowed_scopes,
                ),
            )
        )

    # -- /api/brain: write-back ------------------------------------------------

    def retain(
        self,
        content: str,
        *,
        type: str = "note",
        external_id: str | None = None,
        title: str | None = None,
        links: list[str] | None = None,
        confidence: float = 0.8,
        provenance: dict[str, Any] | None = None,
        run_id: str | None = None,
        namespace: str | None = None,
    ) -> RetainResult:
        """Idempotently write a fact/decision back to the graph + index.

        A key held by an object outside the caller's visibility is not overwritten:
        :class:`MemoryServiceError` 403 (MEM-ADR-019); existing scopes are kept.
        """
        body = _retain_spec(
            content,
            type,
            external_id,
            title,
            links,
            confidence,
            provenance,
            _write_scope(namespace),
        )
        return RetainResult.model_validate(self._post("/retain", body, _run_headers(run_id)))

    def audit(
        self,
        trace_id: str,
        action: str,
        *,
        actor: str = "",
        payload: dict[str, Any] | None = None,
        ts: str | None = None,
        run_id: str | None = None,
        actor_id: str | None = None,
        actor_kind: str = "agent",
        namespace: str | None = None,
    ) -> AuditResult:
        """Record a task-trace audit event (``trace_id`` = issueId / runId)."""
        body = _audit_spec(
            trace_id,
            action,
            actor,
            payload,
            ts,
            run_id,
            actor_id,
            actor_kind,
            _write_scope(namespace),
        )
        return AuditResult.model_validate(self._post("/audit", body, _run_headers(run_id)))

    def retain_document(
        self,
        natural_key: str,
        title: str,
        chunks: Sequence[DocumentChunk | dict[str, Any]],
        *,
        namespace: str | None = None,
        type: str = "document",
        source_path: str = "",
        properties: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        links: list[str] | None = None,
        replace: bool = True,
        run_id: str | None = None,
    ) -> DocumentIngestResult:
        """Batch-ingest a document: one graph node + pre-chunked text (embeds server-side).

        Idempotent by ``natural_key``; ``replace=True`` swaps the node's chunks,
        continuation calls for large documents pass ``replace=False``. A key held by a
        node or chunks outside the caller's visibility: :class:`MemoryServiceError` 403,
        nothing written (MEM-ADR-019).
        """
        body = _document_spec(
            natural_key,
            title,
            chunks,
            namespace,
            type,
            source_path,
            properties,
            meta,
            links,
            replace,
        )
        return DocumentIngestResult.model_validate(
            self._post("/api/brain/documents", body, _run_headers(run_id))
        )

    def delete_document(
        self, natural_key: str, *, namespace: str | None = None
    ) -> DocumentDeleteResult:
        """Delete a document node (with edges) and all its chunks within a namespace."""
        return DocumentDeleteResult.model_validate(
            self._send(
                "DELETE", f"/api/brain/documents/{natural_key}", params=_ns_params(namespace)
            )
        )

    # -- /api/brain: nodes and sources ----------------------------------------

    def nodes(
        self,
        *,
        type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        namespace: str | None = None,
    ) -> NodesResult:
        """List graph nodes by type / status (structural query, no embeddings)."""
        return NodesResult.model_validate(
            self._send(
                "GET",
                "/api/brain/nodes",
                params=_ns_params(namespace, type=type, status=status, limit=limit),
            )
        )

    def node(
        self,
        natural_key: str,
        *,
        hops: int = 1,
        namespace: str | None = None,
        run_id: str | None = None,
    ) -> NodeResult:
        """One node by ``natural_key`` with its graph neighbours."""
        return NodeResult.model_validate(
            self._send(
                "GET",
                f"/api/brain/nodes/{natural_key}",
                params=_ns_params(namespace, hops=hops),
                headers=_run_headers(run_id),
            )
        )

    def source(
        self, natural_key: str, *, namespace: str | None = None, run_id: str | None = None
    ) -> SourceResult:
        """The original document behind a citation: full text, provenance, metadata."""
        return SourceResult.model_validate(
            self._send(
                "GET",
                f"/api/brain/sources/{natural_key}",
                params=_ns_params(namespace),
                headers=_run_headers(run_id),
            )
        )

    def delete_node(
        self,
        natural_key: str,
        *,
        namespace: str | None = None,
        actor: str = "api",
        trace_id: str | None = None,
        run_id: str | None = None,
    ) -> NodeDeleteResult:
        """Delete a node (with edges) and its chunks; the service records an audit event."""
        return NodeDeleteResult.model_validate(
            self._send(
                "DELETE",
                f"/api/brain/nodes/{natural_key}",
                params=_ns_params(namespace, actor=actor, trace_id=trace_id),
                headers=_run_headers(run_id),
            )
        )

    # -- /api/memory: Context Memory Engine (ADR-016) --------------------------

    def observe(
        self, observation: Mapping[str, Any], *, namespace: str | None = None
    ) -> ObservationResult:
        """Retain one observation (idempotent by its source identity).

        A visibility scope (``workspace:``/``principal:``) outside the caller's own
        visibility: :class:`MemoryServiceError` 403, nothing stored (MEM-ADR-019).
        """
        body = _compact({**observation, "scope": _write_scope(namespace)})
        return ObservationResult.model_validate(self._post("/api/memory/observations", body))

    def observe_batch(
        self, observations: Sequence[Mapping[str, Any]], *, namespace: str | None = None
    ) -> ObservationBatchResult:
        """Retain a batch; the 207 answer carries a per-item result."""
        body = _compact(
            {"observations": [dict(o) for o in observations], "scope": _write_scope(namespace)}
        )
        return ObservationBatchResult.model_validate(
            self._post("/api/memory/observations:batch", body)
        )

    def context(
        self,
        request: Mapping[str, Any],
        *,
        namespaces: Sequence[str] | None = None,
        run_id: str | None = None,
        allowed_namespaces: Sequence[str] | None = None,
        allowed_scopes: Sequence[str] | None = None,
    ) -> ContextPack:
        """Compile a bounded ContextPack (no LLM synthesis) for a ContextRequest.

        ``allowed_namespaces`` / ``allowed_scopes`` — visibility computed by a
        service reading on behalf of a principal (``memory:on-behalf``,
        MEM-ADR-019); for any other caller a narrowing intersected with the
        server's visibility, never wider; an empty list sees nothing (MEM-ADR-021)."""
        return ContextPack.model_validate(
            self._post(
                "/api/memory/context",
                _context_spec(
                    _with_visibility(request, allowed_namespaces, allowed_scopes),
                    _read_scope(namespaces),
                ),
                _run_headers(run_id),
            )
        )

    # -- /api/memory: domain packs, reconcile, typed context (MEM-ADR-020) ------
    # Packages, namespace kinds and reconcile may be restricted to the core identity
    # (scope memory:service / CB_CORE_IDENTITIES): other callers get 403 even with a grant.

    def register_package(
        self, pack: Mapping[str, Any], *, tenant_namespace: str | None = None
    ) -> PackageResult:
        """Register a domain pack version. Versions are immutable: the same content
        again is ``unchanged``, different content is a 409.

        A common pack needs the service scope. With ``tenant_namespace`` the pack is a
        tenant pack (``scope: tenant``) owned by that namespace: it needs a write grant
        there, is visible only in it and below it and is referenced as
        ``tenant:<name>[@<version>]``. A name clash with a common pack is a 409 with
        ``detail.code`` (``pack_name_conflict``, ``kind_conflict``, ``relation_conflict``).
        """
        return PackageResult.model_validate(
            self._post("/api/memory/packages", _pack_body(pack, tenant_namespace))
        )

    def packages(self, *, namespace: str | None = None) -> list[dict[str, Any]]:
        """Common domain packs: name, versions, latest (built-in ``default`` first);
        with ``namespace`` also the tenant packs visible there."""
        return list(
            self._send(
                "GET", "/api/memory/packages", params=_compact({"namespace": namespace})
            ).get("packages")
            or []
        )

    def package(
        self, name: str, *, version: str | None = None, namespace: str | None = None
    ) -> dict[str, Any]:
        """One pack version (latest when ``version`` is omitted). A tenant pack is
        ``tenant:<name>`` and needs the ``namespace`` it is visible in."""
        return self._send(
            "GET",
            f"/api/memory/packages/{name}",
            params=_compact({"version": version, "namespace": namespace}),
        )

    def namespace_kinds(self, namespace: str) -> NamespaceKinds:
        """Kind settings (strict, packages) and the effective catalog of a namespace."""
        return NamespaceKinds.model_validate(
            self._send("GET", f"/api/memory/namespaces/{namespace}/kinds")
        )

    def set_namespace_kinds(
        self, namespace: str, *, strict: bool, packages: Sequence[str] | None = None
    ) -> NamespaceKinds:
        """Turn strict mode on/off and pin the packages of a namespace.

        Entities of kinds with ``searchable`` are reindexed for semantic search
        (``reindex`` in the result); entity nodes are re-merged from their snapshot
        sources by the new ``sourcePriority`` (``merge``)."""
        body = {"strict": strict, "packages": list(packages) if packages is not None else None}
        return NamespaceKinds.model_validate(
            self._send("PUT", f"/api/memory/namespaces/{namespace}/kinds", json=body)
        )

    def reconcile(
        self,
        snapshot: Mapping[str, Any],
        *,
        namespace: str | None = None,
        scopes: Sequence[str] | None = None,
        dry_run: bool = False,
        expected_state: str | None = None,
    ) -> ReconcileResult:
        """Reconcile a full source snapshot document as is (the pack's SNAPSHOT.md format:
        ``pack, source, scope, snapshotId, observedAt, entities, relations``).

        A repeat of the last accepted ``snapshotId`` of ``(namespace, source, scope)``
        is a ``duplicate``; an earlier ``snapshotId`` coming back after another
        (A → B → A) is applied as a new snapshot. ``scopes`` are
        visibility scopes (MEM-ADR-019) written onto the snapshot's nodes and edges.
        ``dry_run`` builds the plan without writing; ``expected_state`` (the plan's
        ``state_token``) applies only if the ``(source, scope)`` state did not change
        since, else :class:`MemorySnapshotStaleError`."""
        body = _reconcile_body(snapshot, namespace, scopes, dry_run, expected_state)
        return ReconcileResult.model_validate(self._post("/api/memory/reconcile", body))

    def typed_context(
        self,
        request: Mapping[str, Any],
        *,
        namespaces: Sequence[str] | None = None,
        run_id: str | None = None,
        allowed_namespaces: Sequence[str] | None = None,
        allowed_scopes: Sequence[str] | None = None,
    ) -> TypedContextPack:
        """Typed traversal: anchors -> relations (direction/depth/limit) at ``as_of``.

        With ``allow_semantic`` an anchor may resolve by meaning: via the entity index
        (kinds with ``searchable``) or a document chunk — ``method: semantic``,
        ``matchedOn: entity | chunk`` and ``score`` (cosine, 0..1).
        ``request["where"]`` filters anchor candidates (semantic ones too),
        ``traverse[i]["where"]`` — the entities a step reaches
        (``eq``/``in``/``prefix``/``lte``/``gte``/``exists``); both are sent as is.
        Filtered anchors carry ``filtered``."""
        return TypedContextPack.model_validate(
            self._post(
                "/api/memory/context/typed",
                _context_spec(
                    _with_visibility(request, allowed_namespaces, allowed_scopes),
                    _read_scope(namespaces),
                ),
                _run_headers(run_id),
            )
        )

    def query_entities(
        self,
        kinds: Sequence[str],
        *,
        namespaces: Sequence[str] | None = None,
        where: Sequence[Mapping[str, Any]] | None = None,
        as_of: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        allowed_namespaces: Sequence[str] | None = None,
        allowed_scopes: Sequence[str] | None = None,
    ) -> EntitiesPage:
        """Entities of ``kinds`` at ``as_of`` (empty — open now) filtered by ``where``
        (``eq``/``in``/``prefix``/``lte``/``gte``/``exists``), ordered by
        ``(kind, key, namespace)``. Pass ``next_cursor`` back as ``cursor`` for the next
        page; the listing ends at ``next_cursor is None``. An unknown kind is empty."""
        return EntitiesPage.model_validate(
            self._post(
                "/api/memory/entities:query",
                _entities_body(
                    kinds,
                    namespaces,
                    where,
                    as_of,
                    limit,
                    cursor,
                    allowed_namespaces,
                    allowed_scopes,
                ),
            )
        )


class AsyncMemoryClient:
    """Asyncio client. Use ``async with`` or await :meth:`aclose`."""

    def __init__(
        self,
        base_url: str,
        *,
        token: AsyncToken = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token: AsyncToken = token if token is not None else api_key
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def __aenter__(self) -> AsyncMemoryClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _resolve_token(self) -> str:
        source = self._token
        if source is None:
            return ""
        if isinstance(source, str):
            return source
        # A provider object (``CredentialProvider`` of control-plane-client) or a
        # plain / async callable.
        provider = getattr(source, "token", None)
        value = provider() if provider is not None else source()
        if inspect.isawaitable(value):
            value = await value
        return str(value or "")

    async def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {**_bearer(await self._resolve_token()), **(extra or {})}

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method, path, json=json, params=params, headers=await self._headers(headers)
            )
        except httpx.HTTPError as exc:
            raise MemoryTransportError(f"{type(exc).__name__}: {exc}") from exc
        _raise_for_status(response)
        return response.json() if response.content else {}

    async def _post(
        self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> Any:
        return await self._send("POST", path, json=body, headers=headers)

    async def healthz(self) -> dict[str, Any]:
        data: dict[str, Any] = await self._send("GET", "/healthz")
        return data

    async def recall(
        self,
        query: str,
        *,
        budget: str = "mid",
        hops: int = 1,
        namespaces: Sequence[str] | None = None,
        scope: dict[str, Any] | None = None,
        allowed_namespaces: Sequence[str] | None = None,
        allowed_scopes: Sequence[str] | None = None,
    ) -> RecallResult:
        return RecallResult.model_validate(
            await self._post(
                "/recall",
                _with_visibility(
                    _recall_spec(query, budget, hops, _read_scope(namespaces, scope)),
                    allowed_namespaces,
                    allowed_scopes,
                ),
            )
        )

    async def search(
        self,
        query: str,
        *,
        type: str | None = None,
        status: str | None = None,
        limit: int = 20,
        meta: dict[str, Any] | None = None,
        namespaces: Sequence[str] | None = None,
        allowed_namespaces: Sequence[str] | None = None,
        allowed_scopes: Sequence[str] | None = None,
    ) -> SearchResult:
        return SearchResult.model_validate(
            await self._post(
                "/search",
                _with_visibility(
                    _search_spec(query, type, status, limit, meta, _read_scope(namespaces)),
                    allowed_namespaces,
                    allowed_scopes,
                ),
            )
        )

    async def query(
        self,
        question: str,
        *,
        k: int = 8,
        hops: int = 1,
        synthesize: bool | None = None,
        namespaces: Sequence[str] | None = None,
        allowed_namespaces: Sequence[str] | None = None,
        allowed_scopes: Sequence[str] | None = None,
    ) -> QueryResult:
        return QueryResult.model_validate(
            await self._post(
                "/api/brain/query",
                _with_visibility(
                    _query_spec(question, k, hops, synthesize, _read_scope(namespaces)),
                    allowed_namespaces,
                    allowed_scopes,
                ),
            )
        )

    async def retain(
        self,
        content: str,
        *,
        type: str = "note",
        external_id: str | None = None,
        title: str | None = None,
        links: list[str] | None = None,
        confidence: float = 0.8,
        provenance: dict[str, Any] | None = None,
        run_id: str | None = None,
        namespace: str | None = None,
    ) -> RetainResult:
        body = _retain_spec(
            content,
            type,
            external_id,
            title,
            links,
            confidence,
            provenance,
            _write_scope(namespace),
        )
        return RetainResult.model_validate(await self._post("/retain", body, _run_headers(run_id)))

    async def audit(
        self,
        trace_id: str,
        action: str,
        *,
        actor: str = "",
        payload: dict[str, Any] | None = None,
        ts: str | None = None,
        run_id: str | None = None,
        actor_id: str | None = None,
        actor_kind: str = "agent",
        namespace: str | None = None,
    ) -> AuditResult:
        body = _audit_spec(
            trace_id,
            action,
            actor,
            payload,
            ts,
            run_id,
            actor_id,
            actor_kind,
            _write_scope(namespace),
        )
        return AuditResult.model_validate(await self._post("/audit", body, _run_headers(run_id)))

    async def retain_document(
        self,
        natural_key: str,
        title: str,
        chunks: Sequence[DocumentChunk | dict[str, Any]],
        *,
        namespace: str | None = None,
        type: str = "document",
        source_path: str = "",
        properties: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        links: list[str] | None = None,
        replace: bool = True,
        run_id: str | None = None,
    ) -> DocumentIngestResult:
        body = _document_spec(
            natural_key,
            title,
            chunks,
            namespace,
            type,
            source_path,
            properties,
            meta,
            links,
            replace,
        )
        return DocumentIngestResult.model_validate(
            await self._post("/api/brain/documents", body, _run_headers(run_id))
        )

    async def delete_document(
        self, natural_key: str, *, namespace: str | None = None
    ) -> DocumentDeleteResult:
        return DocumentDeleteResult.model_validate(
            await self._send(
                "DELETE", f"/api/brain/documents/{natural_key}", params=_ns_params(namespace)
            )
        )

    async def nodes(
        self,
        *,
        type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        namespace: str | None = None,
    ) -> NodesResult:
        return NodesResult.model_validate(
            await self._send(
                "GET",
                "/api/brain/nodes",
                params=_ns_params(namespace, type=type, status=status, limit=limit),
            )
        )

    async def node(
        self,
        natural_key: str,
        *,
        hops: int = 1,
        namespace: str | None = None,
        run_id: str | None = None,
    ) -> NodeResult:
        return NodeResult.model_validate(
            await self._send(
                "GET",
                f"/api/brain/nodes/{natural_key}",
                params=_ns_params(namespace, hops=hops),
                headers=_run_headers(run_id),
            )
        )

    async def source(
        self, natural_key: str, *, namespace: str | None = None, run_id: str | None = None
    ) -> SourceResult:
        return SourceResult.model_validate(
            await self._send(
                "GET",
                f"/api/brain/sources/{natural_key}",
                params=_ns_params(namespace),
                headers=_run_headers(run_id),
            )
        )

    async def delete_node(
        self,
        natural_key: str,
        *,
        namespace: str | None = None,
        actor: str = "api",
        trace_id: str | None = None,
        run_id: str | None = None,
    ) -> NodeDeleteResult:
        return NodeDeleteResult.model_validate(
            await self._send(
                "DELETE",
                f"/api/brain/nodes/{natural_key}",
                params=_ns_params(namespace, actor=actor, trace_id=trace_id),
                headers=_run_headers(run_id),
            )
        )

    async def observe(
        self, observation: Mapping[str, Any], *, namespace: str | None = None
    ) -> ObservationResult:
        body = _compact({**observation, "scope": _write_scope(namespace)})
        return ObservationResult.model_validate(await self._post("/api/memory/observations", body))

    async def observe_batch(
        self, observations: Sequence[Mapping[str, Any]], *, namespace: str | None = None
    ) -> ObservationBatchResult:
        body = _compact(
            {"observations": [dict(o) for o in observations], "scope": _write_scope(namespace)}
        )
        return ObservationBatchResult.model_validate(
            await self._post("/api/memory/observations:batch", body)
        )

    async def context(
        self,
        request: Mapping[str, Any],
        *,
        namespaces: Sequence[str] | None = None,
        run_id: str | None = None,
        allowed_namespaces: Sequence[str] | None = None,
        allowed_scopes: Sequence[str] | None = None,
    ) -> ContextPack:
        return ContextPack.model_validate(
            await self._post(
                "/api/memory/context",
                _context_spec(
                    _with_visibility(request, allowed_namespaces, allowed_scopes),
                    _read_scope(namespaces),
                ),
                _run_headers(run_id),
            )
        )

    async def register_package(
        self, pack: Mapping[str, Any], *, tenant_namespace: str | None = None
    ) -> PackageResult:
        return PackageResult.model_validate(
            await self._post("/api/memory/packages", _pack_body(pack, tenant_namespace))
        )

    async def packages(self, *, namespace: str | None = None) -> list[dict[str, Any]]:
        return list(
            (
                await self._send(
                    "GET", "/api/memory/packages", params=_compact({"namespace": namespace})
                )
            ).get("packages")
            or []
        )

    async def package(
        self, name: str, *, version: str | None = None, namespace: str | None = None
    ) -> dict[str, Any]:
        return await self._send(
            "GET",
            f"/api/memory/packages/{name}",
            params=_compact({"version": version, "namespace": namespace}),
        )

    async def namespace_kinds(self, namespace: str) -> NamespaceKinds:
        return NamespaceKinds.model_validate(
            await self._send("GET", f"/api/memory/namespaces/{namespace}/kinds")
        )

    async def set_namespace_kinds(
        self, namespace: str, *, strict: bool, packages: Sequence[str] | None = None
    ) -> NamespaceKinds:
        body = {"strict": strict, "packages": list(packages) if packages is not None else None}
        return NamespaceKinds.model_validate(
            await self._send("PUT", f"/api/memory/namespaces/{namespace}/kinds", json=body)
        )

    async def reconcile(
        self,
        snapshot: Mapping[str, Any],
        *,
        namespace: str | None = None,
        scopes: Sequence[str] | None = None,
        dry_run: bool = False,
        expected_state: str | None = None,
    ) -> ReconcileResult:
        body = _reconcile_body(snapshot, namespace, scopes, dry_run, expected_state)
        return ReconcileResult.model_validate(await self._post("/api/memory/reconcile", body))

    async def typed_context(
        self,
        request: Mapping[str, Any],
        *,
        namespaces: Sequence[str] | None = None,
        run_id: str | None = None,
        allowed_namespaces: Sequence[str] | None = None,
        allowed_scopes: Sequence[str] | None = None,
    ) -> TypedContextPack:
        return TypedContextPack.model_validate(
            await self._post(
                "/api/memory/context/typed",
                _context_spec(
                    _with_visibility(request, allowed_namespaces, allowed_scopes),
                    _read_scope(namespaces),
                ),
                _run_headers(run_id),
            )
        )

    async def query_entities(
        self,
        kinds: Sequence[str],
        *,
        namespaces: Sequence[str] | None = None,
        where: Sequence[Mapping[str, Any]] | None = None,
        as_of: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        allowed_namespaces: Sequence[str] | None = None,
        allowed_scopes: Sequence[str] | None = None,
    ) -> EntitiesPage:
        return EntitiesPage.model_validate(
            await self._post(
                "/api/memory/entities:query",
                _entities_body(
                    kinds,
                    namespaces,
                    where,
                    as_of,
                    limit,
                    cursor,
                    allowed_namespaces,
                    allowed_scopes,
                ),
            )
        )


def _entities_body(
    kinds: Sequence[str],
    namespaces: Sequence[str] | None,
    where: Sequence[Mapping[str, Any]] | None,
    as_of: str | None,
    limit: int | None,
    cursor: str | None,
    allowed_namespaces: Sequence[str] | None,
    allowed_scopes: Sequence[str] | None,
) -> dict[str, Any]:
    body = _compact(
        {
            "kinds": list(kinds),
            "where": [dict(c) for c in where] if where is not None else None,
            "asOf": as_of,
            "limit": limit,
            "cursor": cursor,
        }
    )
    return _context_spec(
        _with_visibility(body, allowed_namespaces, allowed_scopes), _read_scope(namespaces)
    )


def _with_visibility(
    request: Mapping[str, Any],
    allowed_namespaces: Sequence[str] | None,
    allowed_scopes: Sequence[str] | None,
) -> dict[str, Any]:
    if allowed_namespaces is None and allowed_scopes is None:
        return dict(request)
    merged = dict(request)
    if allowed_namespaces is not None:
        merged["allowedNamespaces"] = list(allowed_namespaces)
    if allowed_scopes is not None:
        merged["allowedScopes"] = list(allowed_scopes)
    return merged
