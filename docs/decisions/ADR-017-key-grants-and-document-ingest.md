# ADR-017: Гранты ключей по namespace и батчевый ингест документов

## Status

Accepted (2026-09-12; реализовано в этом же коммите. Перенос из platform-core
ADR-054 «Platform Memory — мультитенантность через namespace-гибрид», разделы 2–4,
по решению суперпроекта ADR-0030: канон движка памяти — этот репозиторий, копия
`platform-core/packages/memory` удаляется, ADR-054 там помечен Superseded → сюда).

## Context

Движок памяти два месяца жил в двух копиях. Эта (memory-service) ушла вперёд:
PII-барьер ([ADR-002](ADR-002-pii-protection.md)), scope-контракт
([ADR-003](ADR-003-scope-namespace-contract.md)), консоль, витрина, реранк, Context
Engine ([ADR-016](ADR-016-context-memory-engine.md)). Копия в platform-core получила
надстройку ADR-054, которой здесь не было и которая нужна потребителям с несколькими
базами знаний в одном инстансе:

1. **Auth различал только уровень допуска к ПДн**, но не базы знаний: любой валидный
   Bearer-токен читал и писал в любой namespace. Изоляция по ADR-003 держалась на
   дисциплине вызывающего. Для «один инстанс — несколько потребителей» (tenant-ы за
   шлюзом, общая память `shared`) этого недостаточно.
2. **Не было батчевого ингеста готовых чанков.** `POST /api/brain/retain` принимает
   одну статью и режет её сам; мигрируемые Базы знаний уже имеют собственный парсинг
   PDF/DOCX и хотят присылать чанки батчем, один вызов на документ, с фильтруемыми
   тегами и идемпотентным re-ingest.
3. `filters.meta` в `/api/brain/search` индекс поддерживал (`index.search(meta_filter=)`),
   а HTTP-слой не пробрасывал.
4. Офлайн-анализ сообществ (`apply_communities`, `community_members`, MCP-инструменты
   `get_neighbors`/`shortest_path`) работал по всему графу без учёта namespace —
   разметка одного потребителя могла задеть узлы другого с тем же `natural_key`.

## Decision

### 1. Реестр ключей с префиксными грантами — `CB_API_KEYS` (data-plane, опционально)

```
CB_API_KEYS='[{"name":"tp-api","key":"…","grants":[
    {"prefix":"tp:","read":true,"write":true},
    {"prefix":"shared","read":true,"write":false}]},
  {"name":"ops","key":"…","pii":true,"grants":[{"prefix":"","read":true,"write":true}]}]'
```

- Ключ несёт список грантов `{prefix, read, write}`; запрос авторизуется, если **все**
  его namespaces покрыты подходящим грантом, иначе `403` с именем непокрытой базы.
  Пустой префикс покрывает всё; глобальная статистика (`GET /api/brain/stats`, без
  namespace) — только грантам с пустым префиксом.
- Реализация — `server/auth.py` (`NamespaceGrant`, `CallerGrants`, `parse_api_keys`,
  `match_key`, `check_grants`; сравнение ключей constant-time). В `server/app.py`
  `Principal` получает поле `grants`, а каждый data-эндпоинт `/api/brain/*` и
  `/api/memory/*` вызывает `_authorize(principal, namespaces, write=…)`. Console и
  Demo — in-process поверхности с собственным принципалом, гранты их не касаются.
- **Слияние с ADR-002, без изменения прежнего поведения.** Порядок совпадений в
  `authenticate`: `CB_SERVER_API_KEYS_PII` → полный допуск к ПДн, все базы;
  `CB_SERVER_API_KEY` → все базы, маскирование при `CB_PII_PROTECTION` (как раньше);
  ключ реестра → гранты записи, допуск к ПДн по необязательному флагу `"pii": true`
  (без него — как у базового ключа). Без единого настроенного ключа — локальный
  режим без авторизации. Гранты — **сужение для новых ключей**, legacy-ключи их не
  получают: существующие инсталляции и `.env` работают без изменений.
- Пустой namespace запроса (`scope` не задан) при проверке грантов считается
  `CB_DEFAULT_NAMESPACE` — ключ, которому дефолтная база не выдана, получает `403`, а
  не тихий доступ.
- Identity (пользователи, JWT, RBAC продукта) остаётся на стороне хоста — движок
  различает только вызывающие сервисы. Переход на audience-токены `platform-auth-sdk`
  (ADR-0030 суперпроекта, п. 6) не отменяет реестр: он заменит способ предъявления
  ключа, а модель «гранты по префиксу namespace» останется data-plane-политикой.

### 2. `POST /api/brain/documents` и `DELETE /api/brain/documents/{natural_key}`

```
POST /api/brain/documents
{ "natural_key": "doc:<uuid>", "title": "…", "type": "document",
  "namespace": "bank",            # либо "scope": {"namespace": "bank"}; пусто -> default
  "source_path": "s3://…", "properties": {}, "meta": {"collection": "licenses"},
  "chunks": [{"text": "…", "heading": "…", "order": 0}, …],   # ≤ 500 за вызов
  "replace": true }               # продолжение большого документа — replace=false
DELETE /api/brain/documents/{natural_key}?namespace=bank&actor=…&trace_id=…
```

- Хост парсит документ сам; движок владеет эмбеддингами (единая модель/размерность),
  пишет узел `document` + чанки в **один** namespace (`retrieval/documents.py`).
  Идемпотентный re-ingest по `natural_key`; `meta` копируется на каждый чанк и
  фильтруется через `filters.meta`.
- Имя базы принимается двумя способами — `namespace` (как у клиента
  `platform-memory-client`) и `scope.namespace` (как у остальных маршрутов записи);
  расхождение между ними — `400`.
- ПДн маркируются на узле по авто-детекции в чанках плюс явной метке
  `pii`/`pii_categories`, как у `retain` (ADR-002).
- Удаление — hard delete с обязательным аудит-событием `action="delete"` в контуре
  той же базы ([ADR-001](ADR-001-delete-with-audit.md)), узлы аудит-контура защищены
  (`400`). В отличие от `DELETE /api/brain/nodes/{key}`, повторный вызов по
  отсутствующему ключу идемпотентен — `200 {"deleted": false}`: батч-миграции
  повторяют удаления без разбора статусов.

### 3. `filters.meta` в `/api/brain/search`

Семантический режим пробрасывает `filters.meta` (объект) в `retrieve(meta_filter=)`
→ jsonb-containment по колонке `meta` чанков. Результаты `search` и `memories` в
`recall` аддитивно получают `namespace`, `search` — ещё `meta`.

### 4. Namespace-скоупинг офлайн-анализа

`GraphStore.set_node_communities(namespace=)` размечает узлы только одной базы
(default стора по умолчанию), `community_members(namespaces=)` отдаёт только её,
`apply_communities` и MCP-инструменты строят проекцию `to_networkx(store,
namespaces=[default])`. Чужие базы знаний не участвуют ни в кластеризации, ни в
разметке даже при коллизии `natural_key`.

## Consequences

- Публичный контракт расширен аддитивно: новые маршруты, новая переменная, новые
  поля выдачи; существующие маршруты, ключи и схемы ответов не меняются. Инвариант 2
  CLAUDE.md соблюдён, `docs/INTEGRATION.md` обновлён.
- Инвариант изоляции пакета (BIZ-023) не тронут: никаких JWT, identity и зависимостей
  платформы в движке; гранты — статический конфиг.
- `deploy/docker-compose.traefik.yml` теперь публикует и `/api/memory/*` (ADR-016 до
  этого наружу через этот файл не выходил).
- Тесты: 11 unit на `auth.py`, 10 на `core/namespaces.py`, 21 HTTP-тест слияния auth
  без БД; 35 интеграционных (documents + grants over HTTP, изоляция namespaces,
  plugin-API, shared-memory smoke) на реальной AGE+pgvector — все перенесены из
  platform-core под conftest этого репозитория (`CB_TEST_DATABASE_URL`).
- Ротация ключей `CB_API_KEYS` — редеплой конфига; секрет-хранилище вне скоупа.
- HNSW-поиск с namespace-фильтром — post-filter (over-fetch `pool`); при деградации
  recall на селективных базах — partial-индексы per-namespace, контракт не меняется.

## Alternatives

- **Per-tenant JWT / audience-проверка в движке.** Отклонено здесь: тянет identity
  через границу изоляции пакета. Планируется на уровне шлюза/SDK (ADR-0030), реестр
  грантов при этом остаётся.
- **Только legacy-ключи, изоляция на совести вызывающего.** Текущее состояние —
  недостаточно для нескольких потребителей в одном инстансе.
- **Парсинг PDF/DOCX внутри движка.** Отклонено (ADR-054 §4): зависимости на
  парсеры и форматы — забота хоста; движок — владелец эмбеддингов и индекса.
- **Один namespace-параметр в documents (`namespace` ИЛИ `scope`).** Принят
  двойной вход ради совместимости и с клиентом platform-core, и с остальными
  маршрутами записи; конфликт — ошибка, а не тихий приоритет.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: "src/platform_memory/core/config.py", pattern: '^\s+api_keys: str = Field'}
  repo: memory-service
- grep: {path: "src/platform_memory/server/auth.py", pattern: '^def (parse_api_keys|check_grants)\('}
  repo: memory-service
- grep: {path: "src/platform_memory/server/app.py", pattern: '^def _authorize\(principal: Any, namespaces: Iterable\[str\], \*, write: bool\)'}
  repo: memory-service
- route: "POST /api/brain/documents"
  repo: memory-service
- route: "DELETE /api/brain/documents/{natural_key:path}"
  repo: memory-service
- grep: {path: "src/platform_memory/server/app.py", pattern: 'meta_filter = filters\.get\("meta"\)'}
  repo: memory-service
```
