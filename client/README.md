# platform-memory-client

*English. Russian version: [README.ru.md](README.ru.md)*

The canonical HTTP client for memory-service (superproject ADR-0030): `MemoryClient`
(sync) and `AsyncMemoryClient` (asyncio) over the service's public contract —
`/api/brain/*` (recall, search, query, retain, audit, documents, nodes, sources)
and `/api/memory/*` (observations, context; ADR-016; domain packs, reconcile,
typed context; MEM-ADR-020). The only dependencies are
`httpx` and `pydantic`: a consumer does not pull in the engine (psycopg, openai, networkx).

It lives in the memory-service repository as a separate distribution (the same way
`control-plane/client` does for the core), so that consumers add it as the path
dependency `../memory-service/client` and the client version keeps pace with the
server contract. The service itself installs it in the dev group — the client
tests (`tests/client`) run together with the service tests.

The credential is a bearer: a static key grant (ADR-017) or an IAM access token
(ADR-018). It is passed as a string or as a callable that is asked before every
request; the async client also accepts an object with `async token()` — the
`CredentialProvider` contract from `control-plane-client`, so an `IamCredential`
with audience `memory-service` plugs in directly:

```python
from control_plane_client.iam import IamCredential
from platform_memory_client import AsyncMemoryClient

cred = IamCredential(iam_url, "", audience="memory-service",
                     scopes=("memory:read", "memory:write"),
                     platform_access_token=lambda: pat)
async with AsyncMemoryClient("http://memory-service:8077", token=cred) as mem:
    hits = await mem.query("how do I get a visitor pass?", namespaces=["demo"])
```

Errors: `MemoryServiceError` (`status_code`, `detail`, and the `unavailable` (5xx)
and `not_found` properties), `MemoryTransportError` — no response was received
(`status_code == 0`), `MemorySnapshotStaleError` — `reconcile(..., expected_state=…)`
found the `(source, scope)` state changed since the `dry_run` plan (`409
snapshot_stale`; `state_token` is the current state to rebuild the plan from).
