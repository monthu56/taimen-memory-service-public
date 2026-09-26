# ADR-019: Видимость памяти по principal через policy-service

## Status

Accepted (2026-09-12; реализовано — `server/visibility.py`, `core/scopes.py`
(`is_visible`, SQL-предикаты видимости), `allowed_scopes` в индексе, наблюдениях,
`retrieve` и Context Compiler; стратегия `briefing`; `MAX_READ_NAMESPACES = 50`).
Суперпроект: TAI-ADR-0025 (policy-service), TAI-ADR-0031 (память как канон знаний
компании, namespace на воркспейс верхнего уровня).

## Context

До этого решения движок знал только гранты на namespace: статический ключ или
IAM-токен давал доступ ко всему `tenant:<tenant_id>:*`, а `scopes` запроса
(ADR-016 §5) клиент объявлял сам — они повышали релевантность, но не ограничивали
доступ; элемент без scopes был виден всем в namespace. Владелец платформы
решил (2026-09-12), что канон знаний о компании живёт в графе, и видимость
должна настраиваться по ролям и воркспейсам principal, а не только по tenant.
Источник прав — внешний PDP policy-service (TAI-ADR-0025); движок ролей и
bindings не хранит.

## Decision

1. **Namespace на воркспейс верхнего уровня.** `tenant:<t>:ws:<root>` для каждого
   воркспейса без родителя, `tenant:<t>:principal:<p>` — приватная память
   principal, `tenant:<t>` — общая память tenant (`core/namespaces.py`). Имена
   детерминированы от UUID и укладываются в шаблон `tenant:<t>:*` грантов ADR-018.
   Объекту `memory_namespace` policy-service соответствует id с префиксом вида:
   `ws-<uuid>`, `principal-<uuid>`, `tenant-<uuid>`.
2. **Приватно по умолчанию внутри namespace.** Элемент (чанк, наблюдение, узел,
   факт), несущий scope `workspace:<id>` или `principal:<id>`, виден только тому,
   чьи разрешённые scopes его пересекают. Элемент без scopes и элемент только со
   scopes релевантности (`task:`, `run:`, `project:`) считается уровнем namespace:
   его видимость решена правом читать namespace. Предикат один и тот же в SQL
   (`observation_visibility_sql`, `chunk_visibility_sql`) и в Python
   (`is_visible`).
3. **Разрешённые scopes задаёт сервер, не клиент.** `VisibleSet{namespaces, scopes}`
   вычисляется зависимостью `visibility` на каждом read-маршруте:
   - статический ключ — service-режим, без ограничения (как раньше);
   - IAM-токен человека/агента при `CB_POLICY_ENABLED` — два `list_objects`
     policy-service (`memory.read` на `memory_namespace` и на `workspace`) от
     service identity движка (`CB_IAM_CLIENT_ID/SECRET`, audience `policy-service`,
     scope `policy:check-on-behalf`), кэш `CB_POLICY_CACHE_TTL_SECONDS`;
   - service account со scope `memory:on-behalf` (Control Plane за конечного
     principal) — `allowedNamespaces` и `allowedScopes` в теле, без них 403.
   Запрошенный namespace вне `VisibleSet.namespaces` — 403; policy-service
   недоступен — 503, не allow. Клиентские `allowedNamespaces`/`allowedScopes`
   любого вызывающего — сужение: пересечение с серверным `VisibleSet`, не
   расширение (уточнено ADR-021; прежде клиентское значение перезаписывалось).
4. **Стратегия `briefing`** Context Compiler: без запроса, каналы `graph`, `facts`,
   `recent` от anchors principal и его воркспейсов — стоячий контекст вместо
   `обзор.md` (TAI-ADR-0031 §6).
5. `MAX_READ_NAMESPACES` поднят до 50: principal с доступом к нескольким
   воркспейсам верхнего уровня читает объединение namespaces.

## Consequences

- `/api/brain/query|recall|search|nodes` и `/api/memory/context` применяют
  видимость; узловые маршруты `/nodes/{key}`, `/sources`, `/trace`, MCP-сервер и
  Console — пока только гранты namespace (следующий шаг).
- Control Plane обязан снабжать наблюдения namespace и scope воркспейса,
  выведенными из задачи; наблюдение только с `task:`/`run:` остаётся уровнем
  namespace (переходный режим, дизайн v0 §11).
- Тесты: `tests/test_visibility.py` — предикаты, `VisibleSet`, HTTP-контракт с
  подменённым policy; интеграционные тесты на Postgres — следующий шаг.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: "src/platform_memory/core/scopes.py", pattern: '^def (is_visible|observation_visibility_sql|chunk_visibility_sql)\('}
  repo: memory-service
- grep: {path: "src/platform_memory/server/app.py", pattern: 'Depends\(visibility\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/server/memory_api.py", pattern: 'payload\["allowed_scopes"\] = list\(vis\.scopes\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/core/config.py", pattern: 'policy_enabled: bool = Field\(default=False\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/context/compiler.py", pattern: '"briefing": \{("resolve", )?"graph", "facts", "recent"\}'}
  repo: memory-service
- grep: {path: "tests/test_visibility.py", pattern: 'def test_(is_visible_private_by_default|visible_set_policy_unavailable_is_503|http_query_and_context_apply_visibility)'}
  repo: memory-service
```
