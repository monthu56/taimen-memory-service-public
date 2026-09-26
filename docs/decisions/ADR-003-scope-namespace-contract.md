# ADR-003: Несколько баз знаний по имени в запросе — scope.namespace в HTTP-контракте

## Status

Accepted (2026-07-08; владелец выбрал namespaces как механизм и `scope.namespace`
как имя параметра).

## Context

Потребителям нужны несколько независимых баз знаний с выбором базы в каждом запросе
и автосозданием новой базы по имени («новый граф по имени в запросе»). Приоритет №3
продукта. Движок изоляции уже существовал (ADR-054: namespaces в graph-узлах, чанках
индекса и всех API движка), но HTTP-слой поле `scope` принимал и игнорировал —
INTEGRATION.md честно называл его «в работе».

## Decision

1. **Механизм — namespaces (ADR-054), не физические AGE-графы.** Namespace — сквозной
   фильтр и в графе, и в векторном индексе; «новая база» создаётся автоматически
   первой записью (namespace — это значение, инициализация не нужна).
2. **Контракт**:
   - запись (`retain`, `facts`, `audit`) — ровно один: `scope: {"namespace": "bank"}`;
   - чтение (`query`, `recall`, `search`) — один или список:
     `{"namespaces": ["bank", "shared"]}`, лимит 10 (MAX_READ_NAMESPACES);
   - узловые/трейс-маршруты (`GET/DELETE /nodes/{key}`, `GET /nodes`,
     `GET /trace/{id}`) — query-параметр `namespace`;
   - пусто/не передан → `CB_DEFAULT_NAMESPACE` (обратная совместимость, ноль
     изменений для существующих потребителей);
   - невалидное имя (`[a-z0-9][a-z0-9._:-]{0,199}`) или >10 в списке → `400`.
3. **Аудит per-KB**: события delete/pii_access/audit пишутся в namespace запроса —
   у каждой базы свой аудит-контур, трейс читается с тем же `namespace`.

## Consequences

- Изоляция баз — логическая, в одном инстансе/графе. Физическая изоляция
  (требования заказчиков по данным) остаётся уровнем развёртывания: отдельный
  инстанс сервиса+БД на контур.
- Ответы query дополнены эхом `scope` (recall/search уже возвращали) — аддитивно.
- Держатель токена имеет доступ ко всем namespaces инстанса; разграничение
  допусков по KB на уровне токенов — возможное расширение (модель Principal
  из ADR-002 расширяема).

## Alternatives

- **Физический AGE-граф на каждое имя** — отклонено: таблица чанков общая
  (векторный поиск пришлось бы шардировать отдельно), противоречит ADR-054,
  заметно больше кода/инициализации; физическую изоляцию даёт отдельный инстанс.
- **Отдельное плоское поле `kb`** — отклонено: `scope` уже зарезервирован
  в контракте и INTEGRATION.md, два имени для одного понятия хуже одного.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: "src/platform_memory/core/namespaces.py", pattern: 'NAMESPACE_RE = re\.compile\(r"\^\[a-z0-9\]\[a-z0-9\._:-\]\{0,199\}\$"\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/core/namespaces.py", pattern: '^MAX_READ_NAMESPACES = \d+'}
  repo: memory-service
- grep: {path: "src/platform_memory/server/app.py", pattern: '^def _scope_namespaces\(scope: dict \| None\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/server/app.py", pattern: '^def _scope_namespace\(scope: dict \| None\)'}
  repo: memory-service
- grep: {path: "tests/test_server_scope.py", pattern: 'def test_(invalid_namespace_400|too_many_namespaces_400|no_scope_means_engine_default)'}
  repo: memory-service
```
