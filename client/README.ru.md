# platform-memory-client

*Русская версия. English: [README.md](README.md)*

Канонический HTTP-клиент memory-service (ADR-0030 суперпроекта): `MemoryClient`
(sync) и `AsyncMemoryClient` (asyncio) над публичным контрактом сервиса —
`/api/brain/*` (recall, search, query, retain, audit, documents, nodes, sources)
и `/api/memory/*` (observations, context; ADR-016; доменные пакеты, reconcile,
typed context; MEM-ADR-020). Зависимости — только
`httpx` и `pydantic`: потребитель не тянет движок (psycopg, openai, networkx).

Живёт в репозитории memory-service отдельным дистрибутивом (как
`control-plane/client` у ядра), чтобы потребители подключали его
path-зависимостью `../memory-service/client`, а версия клиента шла в ногу с
контрактом сервера. Сам сервис ставит его в dev-группу — тесты клиента
(`tests/client`) гоняются вместе с тестами сервиса.

Credential — bearer: статический key grant (ADR-017) или IAM access token
(ADR-018). Передаётся строкой либо callable, который спрашивается перед каждым
запросом; асинхронный клиент принимает и объект с `async token()` — контракт
`CredentialProvider` из `control-plane-client`, так что `IamCredential`
с audience `memory-service` подключается напрямую:

```python
from control_plane_client.iam import IamCredential
from platform_memory_client import AsyncMemoryClient

cred = IamCredential(iam_url, "", audience="memory-service",
                     scopes=("memory:read", "memory:write"),
                     platform_access_token=lambda: pat)
async with AsyncMemoryClient("http://memory-service:8077", token=cred) as mem:
    hits = await mem.query("как оформить пропуск?", namespaces=["demo"])
```

Ошибки: `MemoryServiceError` (`status_code`, `detail`, свойства `unavailable`
для 5xx и `not_found`), `MemoryTransportError` — ответа не было (`status_code == 0`),
`MemorySnapshotStaleError` — `reconcile(..., expected_state=…)` застал состояние
`(source, scope)` изменившимся с плана `dry_run` (`409 snapshot_stale`; `state_token` —
текущее состояние, по нему строится новый план).
