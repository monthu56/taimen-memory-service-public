# ADR-021: Клиентское сужение видимости — пересечение с серверной, не источник прав

## Status

Proposed (2026-09-23; реализовано в ветке задачи — `server/visibility.py`
(`narrow_visibility`, `check_visible` с namespace по умолчанию), зависимость
`visibility` в `server/app.py`, клиент `platform-memory-client`; ждёт утверждения
владельца). Суперпроект: TAI-ADR-0031 п.5 (амендмент «видимость памяти считает
ядро»), TAI-ADR-0042 (амендмент «память только через Control Plane»). Уточняет
ADR-019 п.3: фраза «клиентское `allowed_scopes` в `/api/memory/context`
перезаписывается серверным» заменяется этим решением.

## Context

Ядро платформы в local-режиме сужает чтение памяти задачи до фокусного
workspace и его предков и передаёт это множество в `allowedScopes` запроса
`POST /api/memory/context`. Сервис памяти по ADR-019 п.3 клиентское значение
молча перезаписывал серверной видимостью. Без policy-режима серверная видимость
— `UNRESTRICTED` (`allowed_scopes=None`), фильтра не было вовсе: задача из
`org/backend` получала память соседнего `org/finance`, лежащую в том же
namespace корня `org` (ревью TASK-000298 отклонено 2026-09-23).

Принцип суперпроекта: видимость памяти считает ядро, сервис памяти вызывается
только identity ядра. Но и сервис не должен доверять клиенту как источнику прав:
policy-режим (ADR-019) остаётся верхней границей того, что вызывающему можно.

## Decision

1. **`effective = server ∩ client`.** Поля тела `allowedNamespaces` и
   `allowedScopes` (и snake_case-синонимы) у любого вызывающего — **сужение**
   серверной видимости `VisibleSet` (ADR-019 п.3). Сервер берёт пересечение;
   серверное множество `None` (без ограничения: статический ключ или policy
   выключен) — берётся клиентское. Элемент, которого нет в серверном множестве,
   клиент добавить не может: сужение не источник прав.
2. **Отсутствие поля или `null`** — серверная видимость без изменений, как до
   этого решения.
3. **Пустой список — ничего, не «всё».** `allowedScopes: []` — ни один элемент
   со scope `workspace:`/`principal:` не виден (элементы уровня namespace —
   без таких scopes — по-прежнему решаются правом читать namespace, ADR-019 п.2);
   `allowedNamespaces: []` — ни один namespace не читается (`403`).
4. **Namespace по умолчанию проверяется.** Запрос без `scope` (дефолт движка)
   сверяется с видимостью как `CB_DEFAULT_NAMESPACE` — так же, как гранты
   (`_authorize`, ADR-017). Иначе пустой `scope` обходил бы ограничение
   namespaces — и серверное (policy), и клиентское.
5. **Одна точка применения.** Сужение применяет зависимость `visibility`
   (`server/app.py`) сразу после `resolve_visibility`, поэтому оно одинаково
   действует на всех read-маршрутах с телом: `/api/memory/context`,
   `/api/memory/context/typed`, `/api/brain/query|recall|search`. Хендлеры
   берут `allowed_scopes` только из итогового `VisibleSet`. Для service account
   с `memory:on-behalf` тело уже и есть серверная видимость; пересечение с ним
   же идемпотентно.
6. **Валидация.** Не список строк, некорректный namespace или scope — `400`
   (не `403`: это ошибка формы запроса). Scopes канонизируются `resolve_scopes`,
   лимит — `MAX_ALLOWED_SCOPES`.

## Consequences

- Утечка соседнего поддерева в local-режиме ядра закрыта: ядро сужает, сервис
  исполняет. В policy-режиме клиент по-прежнему не выходит за policy.
- Контракт расширен аддитивно: поля были в контракте только для on-behalf;
  теперь их может передать любой вызывающий. Потребитель, который их не
  передаёт, поведения не замечает. Изменение для запросов без `scope` в
  policy-режиме: namespace по умолчанию вне видимости — `403` (раньше
  проверка пропускалась; гранты IAM-токенов его и так обычно не покрывали).
- Клиент `platform-memory-client`: `allowed_namespaces`/`allowed_scopes` у
  `recall`, `search`, `query` (sync/async), у `context`/`typed_context` —
  обновлена семантика в docstring.
- Известные пределы (как в ADR-019): структурный режим `/api/brain/search`
  (`filters.type`) и `GET /api/brain/nodes` (тела нет), узловые маршруты
  `/nodes/{key}`, `/sources`, `/trace`, MCP-сервер и Console scope-сужения не
  применяют — отдельный шаг.
- Тесты: `tests/test_visibility.py` — пересечение, отсутствие поля, пустой
  список, `400`; `tests/integration/test_visibility_narrowing.py` — живая
  Postgres: два поддерева в одном namespace, сужение на одно, отказ в
  расширении в policy-режиме, пустой список.

## Alternatives

- **Доверять клиенту целиком** (клиентское значение заменяет серверное) —
  клиент с тем же токеном мог бы расширить видимость сверх policy; противоречит
  ADR-019 («видимость задаёт сервер, не клиент»).
- **Оставить перезапись серверным (как было)** — сужение ядра теряется, в
  local-режиме фильтра нет вовсе; это и есть отклонённое поведение.
- **Иерархия workspace на стороне сервиса** (сервис сам выводит предков
  фокусного workspace) — сервису пришлось бы знать дерево воркспейсов
  платформы; видимость считает ядро, сервис лишь применяет пересечение.
- **Пустой список = «без ограничения»** — делает ошибку вызывающего (потерял
  все scopes) утечкой; fail-closed надёжнее.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- file: src/platform_memory/server/visibility.py
  repo: memory-service
- grep: {path: "src/platform_memory/server/visibility.py", pattern: '^def narrow_visibility\(visible: VisibleSet, body'}
  repo: memory-service
- grep: {path: "src/platform_memory/server/app.py", pattern: 'return narrow_visibility\(visible, body\)'}
  repo: memory-service
- absent: {path: "src/platform_memory/server/memory_api.py", pattern: 'клиентское allowed_scopes не доверяется'}
  repo: memory-service
- route: "POST /api/memory/context"
  repo: memory-service
- file: tests/integration/test_visibility_narrowing.py
  repo: memory-service
- grep: {path: "tests/integration/test_visibility_narrowing.py", pattern: 'def test_(narrowing_hides_sibling_subtree|client_cannot_widen_policy_visibility|empty_list_sees_nothing)'}
  repo: memory-service
```
