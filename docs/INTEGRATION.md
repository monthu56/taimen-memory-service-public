# Memory API — руководство по интеграции

Сервис памяти: типизированный граф знаний + векторный поиск по базе статей. Отвечает на вопросы **с цитатами на источники** — в каждом ответе есть ссылки на исходные документы, из которых взят контекст.

Сервис развёрнут как изолированный HTTP-сервис на отдельном сервере (РФ). Интеграция — только по API; разворачивать у себя ничего не нужно.

- **Base URL:** выдаётся отдельно вместе с токеном (далее `$BASE`).
- **Авторизация:** заголовок `Authorization: Bearer <token>` на всех маршрутах, кроме `GET /healthz`. Без токена — `401`. Токен может быть выдан на **конкретные базы знаний** (см. «Несколько баз знаний»): запрос к чужой базе — `403`. Токен бывает двух видов — статический ключ сервиса или access token IAM платформы (см. «Два способа аутентификации»).
- **Формат:** JSON (UTF-8). Ошибки — `{"detail": "<описание>"}` с соответствующим HTTP-кодом (`400` — некорректные данные, `401` — нет токена, `403` — нет прав на базу знаний, `404` — не найдено, `5xx` — внутренняя ошибка).
- **Автодокументация:** `GET $BASE/docs` (Swagger UI), `GET $BASE/openapi.json` (OpenAPI-спека).

## Быстрая проверка

```bash
curl $BASE/healthz
# {"ok": true, "graph": "…", "nodes": 123, "chunks": 456}

curl -H "Authorization: Bearer $TOKEN" $BASE/api/brain/health
# то же, но с проверкой токена — удобно для валидации конфига
```

---

## Чтение: получить контекст и ответ

### POST /api/brain/query — вопрос → ответ с источниками

Основной маршрут. Векторный поиск + полнотекстовый + соседи по графу; опционально — синтез готового ответа LLM.

Запрос:

```json
{
  "question": "Какой срок действия карты москвича?",
  "k": 8,            // сколько фрагментов искать (по умолчанию 8)
  "hops": 1,         // глубина обхода соседей графа (1–2)
  "synthesize": false // true — вернуть готовый ответ LLM; false — только контекст
}
```

Ответ при `synthesize: false` (рекомендуется, если ответ генерирует ваша LLM):

```json
{
  "question": "…",
  "synthesized": false,
  "hits": [ { "node_key": "…", "title": "…", "heading": "…", "source_path": "…", "text": "…" } ],
  "neighbors": { "…": ["…"] },
  "sources": [ { "path": "…", "title": "…" } ],
  "context": "готовый к вставке в промпт контекст"
}
```

Ответ при `synthesize: true` дополнительно содержит `answer` (текст) — источники и хиты те же.

`source_path` / `sources` — это ссылка на оригинальный документ: по ним показывается статья-источник подсказки.

### POST /api/brain/recall — контекст под бюджет

То же чтение, но объём задаётся бюджетом: `low` (4 фрагмента), `mid` (8), `high` (16). Удобно для realtime-пути, где важна латентность.

```json
{ "query": "не приходит уведомление о готовности карты", "budget": "low", "hops": 1 }
```

Ответ: `memories[]` (node_key, title, heading, source_path, text), `sources[]`, `neighbors`, `context`, `count`.

### POST /api/brain/search — точечный поиск

Без `filters.type` — семантический top-k; с `filters.type` — структурный список узлов графа.

```json
{ "query": "карта москвича", "filters": { "limit": 10 } }
{ "query": "", "filters": { "type": "service", "status": "active", "limit": 50 } }
{ "query": "лицензия", "filters": { "meta": { "collection": "licenses" } } }
```

`filters.meta` — фильтр по тегам фрагментов (containment: фрагмент подходит, если его
`meta` содержит все указанные пары). Теги задаются при загрузке документов через
`POST /api/brain/documents`; действует только в семантическом режиме. Каждый результат
семантического поиска несёт `node_key`, `title`, `source_path`, `text`, `namespace`, `meta`.

### GET /api/brain/sources/{natural_key} — оригинал статьи целиком

Source-view для карточки «откуда взято»: по `node_key`/`source` из результатов
`query`/`recall`/`search` возвращает полный текст статьи и её происхождение.

```bash
# natural_key — тот же ключ, что вернул retain (обычно external_id = URL статьи);
# слэши в ключе допустимы, экранировать не нужно.
curl "$BASE/api/brain/sources/https://www.mos.ru/city-card?namespace=bank" \
  -H "Authorization: Bearer $TOKEN"
```

Ответ `200`:

```json
{
  "natural_key": "…", "namespace": "bank", "type": "article", "title": "…",
  "content": "полный текст статьи, как был загружен",
  "content_source": "original",        // "chunks" — если оригинал восстановлен из индекса
  "chunks": 3,
  "provenance": { "source": "mos.ru", "source_path": "…", "origin": "agent",
                  "actor": "kb-import", "trace_id": "…", "confidence": 0.9,
                  "last_seen": "…", "metadata": {} },
  "pii": false, "pii_categories": []
}
```

- `404` — узла нет **в этой базе знаний** (статья другого namespace не видна).
- Выдача проходит ту же защиту ПДн, что и остальные маршруты: маскирование для
  токена без допуска, `pii_access`-журнал для токена с допуском.

### Структурные запросы к графу

- `GET /api/brain/nodes?type=…&status=…&limit=…` — список узлов.
- `GET /api/brain/nodes/{natural_key}?hops=1` — узел + его соседи (`404`, если нет).
- `GET /api/brain/stats` — узлы/рёбра по типам, число чанков.

---

## Запись: положить знания в память

### POST /api/brain/retain — записать статью/факт (идемпотентно)

Основной путь загрузки контента. Сырой текст статьи (как скопировали со страницы) кладётся как есть; движок сам строит связи и индекс, оригинал сохраняется и возвращается в источниках.

```json
{
  "content": "полный текст статьи…",
  "type": "article",
  "title": "Оформление карты москвича",
  "external_id": "https://www.mos.ru/…",   // идемпотентность: повторная отправка не создаст дубль
  "provenance": { "source": "mos.ru", "actor": "kb-import" },
  "links": [],
  "confidence": 0.9
}
```

Ответ `201`: `{"retained": true, "type": "article", "title": "…", …}`.

Рекомендация: как `external_id` использовать URL статьи — обновление статьи перезапишет её же, а не создаст копию.

Поле `provenance` сохраняется целиком и возвращается в source-view как
`provenance.metadata`. Для Git-индексации передавайте `repository`, `branch`,
`commit_sha`, `path`, `content_hash`, `author`, `committed_at` и `indexed_at`.

### POST /api/brain/facts — структурированный факт

Для записи с собственным `natural_key`, типом и свойствами (используется агентами):

```json
{ "natural_key": "…", "type": "note", "title": "…", "properties": {}, "links": [], "confidence": 0.8 }
```

### POST /api/brain/documents — документ готовыми фрагментами (батч)

Для миграции существующих баз знаний: вы парсите документ (PDF/DOCX/HTML) у себя и
присылаете готовые текстовые фрагменты одним вызовом — движок считает эмбеддинги,
создаёт узел `document` и кладёт фрагменты в индекс. Повторная отправка того же
`natural_key` заменяет фрагменты (`replace: true`); большой документ можно досылать
последующими вызовами с `replace: false`. До 500 фрагментов за вызов.

```json
{
  "natural_key": "doc:licenses-2026",
  "title": "Реестр лицензий",
  "type": "document",
  "scope": { "namespace": "bank" },        // или "namespace": "bank"; пусто — база по умолчанию
  "source_path": "s3://kb/licenses.pdf",    // цель цитат в выдаче
  "meta": { "collection": "licenses" },    // теги для filters.meta в search
  "chunks": [
    { "text": "Лицензия на строительство…", "heading": "Раздел 1", "order": 0 },
    { "text": "Лицензия на проектирование…", "heading": "Раздел 2", "order": 1 }
  ],
  "replace": true
}
```

Ответ `201`: `{"natural_key": "…", "namespace": "bank", "type": "document", "chunks": 2, "replaced": true}`
(плюс `pii_categories`, если при включённой защите ПДн во фрагментах найдены персональные
данные — как у `retain`). `400` — слишком много фрагментов или `namespace` и
`scope.namespace` указывают разные базы.

`DELETE /api/brain/documents/{natural_key}?namespace=bank` — удалить документ (узел,
связи и все фрагменты в этой базе) с аудит-событием, как у `DELETE /api/brain/nodes/{key}`;
ответ `200 {"deleted": true|false, "chunks_deleted": N, "trace_id": "…"}` — повторное
удаление отсутствующего документа не ошибка (`deleted: false`).

---

## Удаление: убрать запись из памяти

### DELETE /api/brain/nodes/{natural_key} — удалить узел с аудитом

Удаляет узел из графа (вместе со связями) и его фрагменты из поискового индекса.
Удаление **безвозвратное**, поэтому каждый акт автоматически фиксируется
аудит-событием `action="delete"` со снапшотом удалённого (тип, заголовок, число
фрагментов) — след восстанавливается по `GET /api/brain/trace/{trace_id}`.

```bash
# natural_key — тот же ключ, что вернул retain (обычно external_id = URL статьи);
# слэши в ключе допустимы, экранировать не нужно.
curl -X DELETE "$BASE/api/brain/nodes/https://www.mos.ru/city-card?actor=kb-admin" \
  -H "Authorization: Bearer $TOKEN"
```

Параметры (query): `actor` — кто удаляет (попадает в аудит; по умолчанию `api`),
`trace_id` — свой сквозной идентификатор (иначе берётся заголовок `X-Run-Id`,
иначе генерируется), `namespace` — если работаете не в дефолтном контуре.

Ответ `200`:

```json
{ "deleted": true, "natural_key": "…", "type": "article", "title": "…",
  "chunks_deleted": 3, "trace_id": "…" }
```

- `404` — узла нет (повторный DELETE того же ключа тоже даст `404`).
- `400` — узел принадлежит аудит-контуру (`audit_event`, `pc_trace`): журнал
  удалений защищён от удаления этим же механизмом.
- Оригиналы документов-источников (vault) не затрагиваются — удаляется только
  запись из графа и индекса.

---

## Трейс и аудит

Каждое взаимодействие можно связать сквозным идентификатором и потом показать «что происходило под капотом».

- `POST /api/brain/audit` — записать событие: `{ "trace_id": "…", "action": "…", "actor": "…", "payload": {} }`. Заголовок `X-Run-Id` подхватывается как сквозной идентификатор.
- `GET /api/brain/trace/{trace_id}` — подграф трейса: события по времени + привязанные факты.

---

## Персональные данные (152-ФЗ)

Сервис умеет защищать ПДн в контенте (включается на стороне сервиса; статус скажем
при выдаче доступа). Когда защита включена:

- **Допуски токенов.** Токены бывают двух уровней: с полным допуском к ПДн и
  маскированные. Уровень вашего токена сообщается при выдаче.
- **Маскированная выдача.** Если у токена нет допуска, во всех текстовых полях
  ответов (`context`, `text`, `content`, `title`, `answer`) обнаруженные ПДн
  заменяются масками `[ПДн:phone]`, `[ПДн:email]`, `[ПДн:passport_rf]`,
  `[ПДн:snils]`, `[ПДн:card]`, `[ПДн:inn]`; ответ помечается `"pii_masked": true`.
- **Маркировка при записи.** `retain` автоматически распознаёт ПДн в `content` и
  помечает запись; дополнительно можно передать явную метку:

```json
{ "content": "…", "type": "article", "pii": true, "pii_categories": ["fio"] }
```

  Ответ retain вернёт итоговый список `pii_categories`. ФИО и адреса автоматика
  не распознаёт — помечайте их явной меткой.
- **Журнал доступа.** Каждая выдача немаскированных ПДн токену с допуском
  фиксируется событием `pii_access` и видна в `GET /api/brain/trace/{trace_id}`
  (передавайте `X-Run-Id` для сквозной привязки).
- **Уничтожение по требованию субъекта ПДн** — штатный
  `DELETE /api/brain/nodes/{natural_key}` (см. «Удаление»): безвозвратно и с
  аудит-следом.

## Несколько баз знаний

Сервис ведёт несколько независимых баз знаний в одном инстансе. База выбирается
полем `scope` прямо в запросе; **новая база создаётся автоматически первой записью**
— ничего заводить заранее не нужно.

- **Запись** (`retain`, `facts`, `audit`) — ровно одна база:

```json
{ "content": "…", "type": "article", "scope": {"namespace": "bank"} }
```

- **Чтение** (`query`, `recall`, `search`) — одна база или несколько сразу (до 10):

```json
{ "query": "…", "scope": {"namespace": "bank"} }
{ "question": "…", "scope": {"namespaces": ["bank", "shared"]} }
```

- **Узлы и трейс** — query-параметр: `GET /api/brain/nodes/{key}?namespace=bank`,
  `DELETE /api/brain/nodes/{key}?namespace=bank`, `GET /api/brain/trace/{id}?namespace=bank`.
- Без `scope` запрос работает в **базе по умолчанию** (настраивается на стороне
  сервиса) — существующие интеграции продолжают работать без изменений.
- Имя базы: строчные латинские буквы/цифры и `._:-`, до 200 символов
  (`[a-z0-9][a-z0-9._:-]*`); иное имя или больше 10 баз в чтении → `400`.
- Аудит и трейс ведутся **в контуре своей базы**: удаления и доступы к ПДн в базе
  `bank` видны по `GET /api/brain/trace/{id}?namespace=bank`.

Изоляция логическая (общий инстанс). Если контуру нужна физическая изоляция данных
(отдельный сервер/БД) — договоримся при выдаче доступа.

### Права токена на базы знаний

Токен может быть выдан на все базы инстанса или только на некоторые — по префиксу
имени базы, отдельно на чтение и на запись (например: `bank*` — чтение и запись,
`shared` — только чтение). Правила:

- запрос авторизуется, если **все** базы из `scope` разрешены токену для этой
  операции; иначе `403` с именем базы в `detail` — данные другой базы не читаются
  и не пишутся даже частично;
- запрос **без `scope`** работает в базе по умолчанию сервиса — если токену она не
  выдана, будет `403`: всегда передавайте `scope` явно;
- `GET /api/brain/stats` (статистика всего инстанса) доступна только токенам без
  ограничений по базам;
- уровень допуска к ПДн (см. выше) и права на базы — независимые свойства токена,
  оба сообщаются при выдаче.

Это ограничение на стороне сервиса; вашему коду ничего дополнительно делать не
нужно — просто указывать `scope` в каждом запросе.

### Два способа аутентификации

Заголовок один и тот же — `Authorization: Bearer <token>`; отличается происхождение
токена. Оба способа могут работать на одном инстансе одновременно.

1. **Статический ключ сервиса.** Выдаётся вместе с доступом; бессрочный, права на базы
   знаний и допуск к ПДн закреплены за ключом при выдаче (см. выше). Ничего
   дополнительно делать не нужно.
2. **Access token IAM платформы** (если инстанс подключён к IAM — скажем при выдаче
   доступа). Короткоживущий JWT, который вы получаете у IAM, обменяв свой Platform
   Access Token на токен с **audience `memory-service`**. Сервис проверяет подпись,
   точные `iss`/`aud` и срок действия; права выводятся из токена:
   - scopes: `memory:read` — чтение, `memory:write` — запись (`retain`, `facts`,
     `audit`, `documents`, наблюдения), `memory:pii` — полный допуск к ПДн (без него
     при включённой защите — маскированная выдача, см. «Персональные данные»);
   - базы знаний: `tenant:<tenant_id>` и её поддерево `tenant:<tenant_id>:*` — по
     `tenant_id` из токена; дополнительно — базы из claim `memory_namespaces`, если IAM
     его выдал (каждая — ровно она и её поддерево `<name>:*`);
   - `GET /api/brain/stats` IAM-токену недоступна (`403`) — статистика всего инстанса
     только у ключей без ограничений по базам;
   - `memory:service` — служебный доступ: регистрация доменных пакетов и, если на
     инстансе включено ограничение маршрутов пакетов, видов и сверки снимков, —
     единственный scope, с которым они доступны (см. «Доменные виды, сверка снимков
     и типизированный обход»).

   Токен с любым дефектом (истёк, чужой `aud`/`iss`, неизвестный ключ подписи) даёт тот
   же `401`, что и неверный ключ; если сервису временно нечем проверить подпись —
   `503` (повторите позже, это не отказ в правах). Валидный токен без scope памяти
   получит `403` на любой запрос к данным.

```bash
# запрос с IAM-токеном ничем не отличается от запроса со статическим ключом
curl -H "Authorization: Bearer $IAM_ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"…","scope":{"namespace":"tenant:<tenant_id>:kb"}}' $BASE/api/brain/recall
```

## Context Memory Engine: /api/memory/* (аддитивно)

Новая поверхность для событийных интеграций (issue tracker, CRM, почта, agent
runtime): приём **наблюдений** (observations) и сборка **готового контекста**
(ContextPack) с бюджетом и provenance. Маршруты `/api/brain/*` не меняются;
авторизация, права токена на базы знаний и правила `scope`/ПДн — те же.

### POST /api/memory/observations — принять наблюдение (201)

Наблюдение — immutable-запись того, что источник сообщил. Повторная доставка того
же события (`source.system` + `source.stream` + `source.external_id`) не создаёт
дубликат — вернётся тот же `observation_id` с `"duplicate": true`.

```json
{
  "source": {"system": "issue-tracker", "stream": "events", "external_id": "event-1842"},
  "kind": "work.completed",
  "occurred_at": "2026-08-11T10:00:00Z",
  "actor": {"type": "principal", "id": "alice"},
  "scopes": ["project:alpha"],
  "content": "Regression was fixed",
  "data": {"issue": "T-1842"},
  "provenance": {"uri": "https://tracker.local/event/1842"},
  "assertions": [
    {"assert": "fact", "fact": {"subject": "person:alice", "predicate": "WORKS_ON",
      "object": "project:alpha", "valid_from": "2026-08-01T00:00:00Z"}}
  ],
  "scope": {"namespace": "bank"}
}
```

Ответ: `{"observation_id": "obs-…", "duplicate": false, "status": "processed"}`.
`status`: `processed` | `partially_processed` | `failed` (сырая запись сохраняется
всегда; `GET /api/memory/observations?status=failed` покажет, что не спроецировалось,
`POST /api/memory/consolidate` повторит обработку — это безопасно и идемпотентно).

`assertions` — необязательные структурированные утверждения (не требуют LLM):
`entity` (создать сущность), `fact` (temporal-факт с `valid_from`/`valid_to`/
`supersedes`/`confidence`), `text` (индексируемый фрагмент с цитатой на
`provenance.uri`). `scopes` — метки видимости `"type:id"` внутри базы знаний.

### POST /api/memory/observations:batch — пакетный приём (207)

`{"observations": [...], "scope": {"namespace": "bank"}}` → по-элементные результаты
(`results[]` с `observation_id` либо `error`), счётчики `accepted`/`duplicates`/
`failed`. Ошибка одного элемента не откатывает остальные. Максимум 500 за запрос
(больше — `413`).

### POST /api/memory/context — собрать готовый контекст

Главный маршрут чтения для агентов/harness'ов: возвращает **структурированный
ContextPack** под токен-бюджет, без LLM-синтеза.

```json
{
  "query": "Continue resolving the deployment issue",
  "scopes": ["project:alpha"],
  "anchors": ["person:alice"],
  "ephemeral_context": {"current_state": "deploy failed on step 3"},
  "budget": {"tokens": 12000},
  "scope": {"namespaces": ["bank", "shared"]}
}
```

Ответ:

```json
{
  "query": "…",
  "sections": [
    {"kind": "current", "items": [...]},
    {"kind": "relevant_facts", "items": [...]},
    {"kind": "episodes", "items": [...]},
    {"kind": "documents", "items": [...]},
    {"kind": "related_entities", "items": [...]},
    {"kind": "recent_observations", "items": [...]}
  ],
  "sources": [{"source_path": "…", "node_key": "…", "title": "…"}],
  "token_estimate": 9341,
  "budget": {"max_tokens": 12000, "dropped": [...], "truncated": false},
  "conflicts": ["fact-…"],
  "trace_id": "ctx-…"
}
```

Каждый элемент секции несёт `text`, `source_path`, `score`, `signals` (почему
включён: каналы поиска, совпадение идентификатора, свежесть, сила свидетельства)
и `provenance` (цепочка до наблюдения/документа-источника). Конфликтующие версии
фактов помечаются `"conflict": true` и обе остаются в выдаче — разрешение за вами.

Идентификаторы из `query` и `anchors` (ключи сущностей и их формы по доменному
пакету базы — номер решения, `METHOD /path/{id}`, имя события, метод клиента)
детерминированно, без LLM, разрешаются в сущности, загруженные сверкой снимков
(`POST /api/memory/reconcile`). Такие сущности приходят в `related_entities` с
`signals.evidence: "resolved"` и `signals.resolve: {input, method, exact}`
(`method` — `natural_key` | `alias` | `normalized` | `suffix`), цитатой
`source_path` вида `repo@sha:path:line` и `provenance.snapshot`; разрешённые по
точному ключу ранжируются выше свежих наблюдений, а их соседи по графу
добавляются как `related_entities`. Документ, найденный только семантическим
поиском, помечен `signals.evidence: "inferred"`. Разрешённые ключи — в
`stats.resolved[]` (`{namespace, kind, key, method}`), счётчик — в
`stats.channels.resolve`; разбор кандидатов — в трейсе. Нет совпадений — выдача
прежняя. Точные разрешения (ключ, псевдоним, нормализованная форма) занимают лимит
(20 сущностей) раньше суффиксных. Видимость проверяется по scopes, с которыми
сущность пришла в сверку: при нескольких источниках одного ключа текст и цитата
берутся из видимой вам версии. Регистр и ведущие нули номеров не нормализуются —
допустимые формы задаёт доменный пакет базы.

`ephemeral_context` — ваше текущее состояние: попадает в секцию `current` этой
компиляции и **не сохраняется** сервисом.

Опционально: `"strategy"` — `semantic` | `exact` | `graph` | `hybrid` | `briefing`
(по умолчанию hybrid; `briefing` — стоячий контекст без `query`: факты, соседи
якорей и свежие наблюдения); `"as_of"` — исторический срез фактов на момент
времени (ISO-8601).

#### Видимость по principal

Если у сервиса включена видимость по principal (MEM-ADR-019), внутри базы знаний
элемент со scope `workspace:<id>` или `principal:<id>` виден только тому, чьи
разрешённые scopes его пересекают; элементы без таких scopes видны всем, кто
читает базу. Разрешённые scopes задаёт сервер:

- токен IAM человека или агента — из policy-service платформы; запрос к базе
  знаний вне видимости отвечает `403`, недоступность policy-service — `503`;
- service account со scope `memory:on-behalf` (сервис читает память от имени
  конечного principal) обязан передать вычисленные им множества:

```json
{
  "query": "…",
  "scope": {"namespaces": ["tenant:<t>:ws:<root-workspace>"]},
  "allowedNamespaces": ["tenant:<t>:ws:<root-workspace>", "tenant:<t>"],
  "allowedScopes": ["workspace:<id>", "principal:<id>"]
}
```

Без них такой запрос отвечает `403`. Статические ключи сервиса видимостью не
ограничены.

#### Сужение видимости в запросе

Любой вызывающий может сузить свою видимость на один запрос, передав в теле
`allowedNamespaces` и/или `allowedScopes` (MEM-ADR-021). Поля принимают
`/api/memory/context`, `/api/memory/context/typed`, `/api/brain/query`,
`/api/brain/recall` и `/api/brain/search`:

- действует **пересечение** со своей видимостью: сужение не даёт прав и не
  расширяет видимость — scope или база знаний, которых нет в вашей видимости,
  из запроса молча отбрасываются;
- поле отсутствует или `null` — видимость без изменений (как раньше);
- пустой список — **ничего**: `"allowedScopes": []` скрывает все элементы со
  scope `workspace:`/`principal:` (видны только элементы уровня базы знаний),
  `"allowedNamespaces": []` запрещает чтение любой базы (`403`);
- база знаний запроса (`scope`, а без него — база по умолчанию) вне
  суженного `allowedNamespaces` — `403`;
- не список строк или некорректное имя scope/базы — `400`;
- структурный режим `/api/brain/search` (`filters.type`) и `GET /api/brain/nodes`
  проверяют только базу знаний: scope-фильтра видимости там пока нет.

Пример: из поддерева `backend` читать память `backend` и его предка `org`, но не
соседнего `finance`, лежащего в той же базе знаний:

```json
{
  "strategy": "briefing",
  "scope": {"namespace": "tenant:<t>:ws:<org>"},
  "allowedScopes": ["workspace:<backend>", "workspace:<org>"]
}
```

### Прочие маршруты

- `GET /api/memory/observations/{id}?namespace=…` — наблюдение по id (404 если нет).
- `GET /api/memory/observations?namespace=…&kind=…&status=…` — свежие наблюдения
  и свод статусов обработки.
- `DELETE /api/memory/observations/{id}?mode=redact|purge` — удаление: `redact`
  затирает содержимое (запись и id остаются в аудите), `purge` удаляет строку.
  Производные факты теряют это свидетельство; факты без оставшихся свидетельств
  выпадают из выдачи. Событие удаления — в аудит-контуре базы.
- `GET /api/memory/context/trace/{trace_id}` — трейс компиляции: каналы, счётчики,
  ranking- и budget-решения. По нему восстанавливается, почему собран именно такой
  контекст.
- `POST /api/memory/consolidate` — повторная обработка необработанных наблюдений
  (идемпотентно).

## Доменные виды, сверка снимков и типизированный обход (аддитивно)

Сервис не знает ни одного домена в коде: виды сущностей, связи и шаблоны
идентификаторов вашего домена (задачи трекера, договоры, сервисы…) описываются
**доменным пакетом** и регистрируются данными. Маршруты `/api/brain/*` и прежние
`/api/memory/*` не меняются; без пакетов и без строгого режима всё работает как раньше.

**Кто вызывает.** Инстанс может закрепить управление схемой и снимками за
доверенным оркестратором: тогда `POST/GET /api/memory/packages`,
`GET /api/memory/packages/{name}`, `GET/PUT /api/memory/namespaces/{ns}/kinds` и
`POST /api/memory/reconcile` отвечают `403` всем, кроме вызывающих со **service
scope** (IAM-токен со scope `memory:service`, ключ с `"service": true`) и явно
перечисленных ключей, даже если у токена есть права на базу (`memory:write`,
`memory:tenants`). Под ограничением и чтение каталога пакетов (`GET`): список пакетов
в этом случае доступен через оркестратор, а не напрямую. Доверенному вызывающему
права на базу по-прежнему нужны. Остальные маршруты это не затрагивает. Включено ли
ограничение — сообщаем при выдаче доступа.

### POST /api/memory/packages — зарегистрировать доменный пакет

Нужен **service scope**: ключ реестра с `"service": true`, IAM-токен со scope
`memory:service` или ключ с правом записи во все базы. Остальным — `403`.

```json
{
  "name": "code",
  "version": 1,
  "kinds": [
    {"kind": "component", "naturalKey": "<repo>"},
    {"kind": "source_file", "naturalKey": "<repo>:<path>"},
    {"kind": "endpoint",
     "naturalKey": "<METHOD> <path с параметрами как {}>",
     "aliases": ["<path с исходными именами параметров>"],
     "kindAliases": ["route"],
     "idPatterns": ["\\b(?:GET|POST|PUT|PATCH|DELETE)\\s+/[\\w{}./:-]+"],
     "attributes": {"type": "object", "properties": {"method": {"type": "string"}}}},
    {"kind": "ui_call", "naturalKey": "<repo>:<path>:<line>"},
    {"kind": "team"}
  ],
  "relations": [
    {"relation": "defined_in", "fromKinds": ["endpoint", "ui_call"], "toKinds": ["source_file"]},
    {"relation": "part_of", "fromKinds": ["source_file"], "toKinds": ["component"]},
    {"relation": "calls", "fromKinds": ["ui_call"], "toKinds": ["endpoint"]},
    {"relation": "owned_by", "fromKinds": ["component"], "toKinds": ["team"], "cardinality": "one"}
  ]
}
```

- `version` — строка или число (`1` хранится как `"1"`);
- `naturalKey` — JSON Schema ключа сущности (поддержаны `type`, `pattern`,
  `minLength`, `maxLength`, `enum`) или **шаблон ключа** с плейсхолдерами `<name …>`
  / `{name}` (имя — первое слово в скобках, остальное — пояснение). Шаблон собирает
  ключ из атрибутов, если снимок его не дал, а в строгом режиме проверяет форму ключа;
- `aliases` — **формы ключа** сущности, шаблоны с теми же плейсхолдерами. Значение
  плейсхолдера берётся из атрибутов сущности, затем из частей её ключа: у эндпоинта
  `GET /runs/{}` с атрибутом `path: "/runs/{run_id}"` псевдоним — `/runs/{run_id}`, у
  ключа `"<SERIES>-<number>"` = `DOC-0042` форма `"ADR-<number>"` даёт `ADR-0042`.
  По псевдонимам разрешается якорь обхода. Форма без плейсхолдера — ошибка `400`;
- `kindAliases` — синонимы **имени вида** (`route` → `endpoint`);
- `idPatterns` — регулярные выражения для извлечения идентификаторов вида из текста
  (группа `id` или всё совпадение);
- `attributes` — JSON Schema атрибутов (то же подмножество + `object`/`properties`/
  `required`/`additionalProperties`/`items`/`minimum`/`maximum`);
- `temporal` связи (по умолчанию `true`) — у фактов связи есть интервал валидности;
- `cardinality` связи — `many` (по умолчанию: у субъекта набор объектов, сверка
  закрывает только пропавшие пары) или `one` (один объект: при сверке смена объекта
  закрывает старый факт и ставит `supersedes` у нового).

Ответ `201` — версия создана, `200` — та же версия с тем же содержимым уже есть,
`409` — версия уже зарегистрирована с **другим** содержимым (версия иммутабельна —
выпускайте новую), `400` — пакет некорректен. Базовые виды `document`, `entity`,
`fact` есть всегда и не переопределяются; встроенный пакет `default` (бизнес-виды
vault) зарезервирован. Регистрация сама по себе ни на одну базу не влияет — пакет
действует только там, где включён (см. ниже).

Чтение (любой аутентифицированный вызывающий): `GET /api/memory/packages` —
список с версиями; `GET /api/memory/packages/{name}?version=…` — версия пакета
(по умолчанию последняя).

### PUT /api/memory/namespaces/{namespace}/kinds — пакеты и строгий режим базы знаний

```json
{"strict": true, "packages": ["code@1"]}
```

Нужно право записи в базу. `packages` — какие пакеты действуют в базе (`name` —
последняя версия, `name@version` — закреплённая). Пакет действует **только в базе,
где явно включён**: `null` и база без настройки — только встроенный `default`.
`strict: true` — запись сущности неизвестного вида (или с ключом/атрибутами вне схемы
вида) отвергается ответом `422` на `/api/brain/facts`, `/api/brain/retain`,
`/api/brain/documents`, `/api/memory/reconcile`; для сверки снимка строгий режим
проверяет и **связи**: связь должна быть объявлена пакетом, а виды её концов —
входить в `fromKinds`/`toKinds` (иначе `422`). В наблюдениях такой assertion
получает статус ошибки (само наблюдение сохраняется). `GET` того же пути —
настройка и действующий каталог видов.

### POST /api/memory/reconcile — сверить полный снимок источника

Тело — **документ снимка как есть** (общий формат для любого пакета) плюс база
знаний: полем `namespace` или параметром `?namespace=` (оба сразу — только
одинаковые, иначе `400`). Необязательное `scopes` — scopes видимости (например,
`["workspace:<id>"]`), которые пишутся на все узлы и связи снимка.

```json
{
  "pack": "code@1",
  "source": "git:web-app",
  "scope": "web-app",
  "snapshotId": "web-app@9ab1c2d",
  "observedAt": "2026-09-23T08:47:12+00:00",
  "entities": [
    {"kind": "ui_call", "key": "web-app:src/runs/page.tsx:38",
     "title": "/runs/${id}/checkpoints (src/runs/page.tsx:38)",
     "attributes": {"queryParams": ["limit"]},
     "provenance": {"repo": "web-app", "sha": "9ab1c2d", "path": "src/runs/page.tsx", "line": 38}}
  ],
  "relations": [
    {"relation": "calls",
     "from": {"kind": "ui_call", "key": "web-app:src/runs/page.tsx:38"},
     "to": {"kind": "endpoint", "key": "GET /api/v1/runs/{}/checkpoints"},
     "attributes": {"queryParams": ["limit"]}}
  ],
  "namespace": "tenant:<t>:ws:<w>",
  "scopes": ["workspace:<w>"]
}
```

- `source` + `scope` (строка) — чей это снимок. Снимок — **полное** состояние этого
  `(source, scope)`: сверка закрывает только то, что пришло от него же; снимок одного
  репозитория не закрывает сущности другого.
- `observedAt` (обязательно) — время снимка: от него `valid_from`/`valid_to` версий.
- `pack` (необязательно) — пакет снимка; он должен быть включён в базу (иначе `400`).
- Сущность — пара `(kind, key)`; `title`, `attributes` и `provenance` — её
  содержимое: изменилось любое из них (в том числе только `provenance` — код
  сдвинулся) — прежняя версия закрывается и заменяется новой. `provenance`
  `{repo, sha, path, line}` — цитата сущности (`source_path` = `repo@sha:path:line`).
- Связь — тройка `(relation, from, to)`. Связь может вести на сущность **из снимка
  другого источника**: ключи глобальны в пределах базы. Если цели ещё нет, связь
  хранится отложенной и становится ребром, как только цель появится (снимок цели
  может прийти позже — связи разрешатся при сверке любого снимка, где цель есть,
  или при следующем снимке источника связи).

Новое открывается, изменившееся закрывается и открывается новой версией
(`supersedes`), пропавшее закрывается (`valid_to = observedAt`); **ничего не
удаляется** — прошлое состояние доступно через `as_of`. Ответ:

```json
{"source": "git:web-app", "scope": "web-app", "namespace": "…", "snapshot_id": "…",
 "observed_at": "2026-09-23T08:47:12Z", "pack": "code@1",
 "opened": 9, "closed": 0, "unchanged": 0, "superseded": 0,
 "entities": {"opened": 3, "closed": 0, "unchanged": 0, "superseded": 0},
 "relations": {"opened": 6, "closed": 0, "unchanged": 0, "superseded": 0,
               "pending": 1, "resolved": 0, "retried": 0},
 "duplicate": false}
```

Изменившийся элемент считается и закрытым, и открытым. `relations.pending` —
связи снимка, чья цель ещё не появилась; `relations.resolved` — ранее отложенные
связи (этого и других источников), ставшие рёбрами при этой сверке;
`relations.retried` — сколько ранее отложенных связей сверка перепроверила (из них
`resolved` стали рёбрами). Повтор того же
`(namespace, source, scope, snapshotId)` ничего не меняет и возвращает те же
счётчики с `"duplicate": true`; тот же контент под новым `snapshotId` даёт только
`unchanged`. Снимок старше последнего принятого для `(source, scope)` — `409`.
Ключ сущности обязателен, либо собирается шаблоном `naturalKey` вида. Нужно право
записи в базу. Максимум 20000 элементов в снимке.

### POST /api/memory/context/typed — типизированный обход

```json
{
  "anchors": [{"kind": "issue", "value": "PROJ-1"}, {"value": "payment-bug"}],
  "traverse": [
    {"relation": "blocks", "direction": "out", "depth": 2, "limit": 20},
    {"relation": "assigned_to", "direction": "out", "from": "previous"}
  ],
  "as_of": "2026-09-05T00:00:00Z",
  "allow_semantic": false,
  "scope": {"namespaces": ["tenant:<t>:ws:<w>"]}
}
```

Якорь разрешается до первого успешного шага, так же, как канал `resolve` у
`POST /api/memory/context`: точный ключ сущности (`natural_key`) → псевдоним ключа
(`alias`: формы ключа по шаблонам пакета, например путь эндпоинта с исходными
именами параметров) → идентификатор, извлечённый из `value` шаблонами `idPatterns`
(`id_pattern`) → нормализованная форма (`normalized`: параметры `{name}` приводятся
к `{}`, так `POST /goals/{id}` находит `POST /goals/{}`) → суффикс ключа (`suffix`:
ключ оканчивается значением по границе пробела, `.` или `:` — путь без метода
находит эндпоинты всех методов, файл без префикса репозитория — `<repo>:<path>`,
имя таблицы — `<repo>:<table>`; значение короче 4 символов суффиксом не ищется, не
больше 10 сущностей в каждом namespace). Если шагу, разрешившему якорь, подошло
больше сущностей, чем отдано, — у якоря `"truncated": true`, уточните якорь: суффиксу —
больше 10 сущностей запрошенного вида в namespace (отданы первые 10 по ключу), точному
шагу журнала (ключ, псевдоним, нормализованная форма) — больше 20. Шаги `normalized` и `suffix` работают по сущностям, пришедшим
сверкой (`/api/memory/reconcile`). Точный ключ по-прежнему первый — прежние запросы
разрешаются как раньше. Способ разрешения якоря — `matchedBy`; если неточная форма
(`normalized`/`suffix`) подошла к нескольким сущностям, якорь разрешается во все,
а в ответе — `"ambiguous": true` и `candidates` (выбор за вами, сервис не гадает).
Неоднозначность считается только по неточным совпадениям: если в одном namespace
запроса якорь разрешён точно, а в другом — одной сущностью по суффиксу, это не
`ambiguous`; в `candidates` — только сущности неточных шагов.
Свидетельство якоря: `natural_key`/`alias`/`normalized` — `asserted`,
`id_pattern`/`suffix` — `extracted`. Векторный поиск — только при
`"allow_semantic": true`, такие якоря помечены `"evidence": "inferred"`. Обход идёт только по фактам, валидным на `as_of`
(по умолчанию — действующим сейчас): закрытые связи не проходятся. `direction` —
`in` | `out` | `both`, `depth` — 1..5 шагов по связи, `limit` — 1..200 новых
сущностей на шаг, `from` — `anchors` (по умолчанию) или `previous` (от сущностей
предыдущего шага).

Ответ:

```json
{
  "as_of": "…",
  "anchors": [{"input": {"kind": "issue", "value": "PROJ-1"}, "matchedBy": "natural_key",
               "resolved": [{"natural_key": "PROJ-1", "kind": "issue",
                             "method": "natural_key", "evidence": "asserted"}]},
              {"input": {"kind": "endpoint", "value": "/api/v1/runs/{id}"},
               "matchedBy": "suffix", "ambiguous": true,
               "resolved": [{"natural_key": "GET /api/v1/runs/{}", "kind": "endpoint",
                             "method": "suffix", "evidence": "extracted"}, "…"],
               "candidates": [{"namespace": "…", "kind": "endpoint",
                               "natural_key": "GET /api/v1/runs/{}"}, "…"]}],
  "unresolved": [],
  "sections": [{"kind": "issue", "items": [
    {"natural_key": "PROJ-2", "kind": "issue", "title": "…", "attributes": {"status": "open"},
     "aliases": [], "provenance": null, "source_path": "https://tracker.local/PROJ-2",
     "valid_from": "…", "valid_to": null, "anchor": false, "evidence": "asserted",
     "snapshot": {"source": "tracker:main", "scope": "", "snapshot_id": "…"},
     "reached_via": [{"step": 0, "relation": "blocks", "direction": "out", "depth": 1,
                      "from": "PROJ-1"}]}]}],
  "facts": [{"fact_id": "fact-…", "relation": "blocks", "subject": "PROJ-1",
             "object": "PROJ-2", "valid_from": "…", "valid_to": null,
             "evidence": "asserted",
             "snapshot": {"source": "…", "scope": "…", "snapshot_id": "…"}}],
  "used": {"entities": [{"namespace": "…", "natural_key": "PROJ-1"}],
           "facts": ["fact-…"],
           "snapshots": [{"source": "tracker:main", "scope": "", "snapshot_id": "…"}]},
  "sources": [{"source_path": "…", "node_key": "PROJ-2", "title": "…"}],
  "trace_id": "ctx-…"
}
```

Разделы — по видам сущностей; атрибуты и `provenance` сущности — той версии, что
действовала на `as_of`. Пример «кто вызывает эндпоинт»: якорь
`{"kind": "endpoint", "value": "GET /api/v1/runs/{}/checkpoints"}` и шаг
`{"relation": "calls", "direction": "in"}`. `used` — полный список сущностей, фактов и снимков, из которых собран
ответ (для evidence в вашей системе); трейс — `GET /api/memory/context/trace/{trace_id}`.
Права, видимость по principal и маскирование ПДн — как у `POST /api/memory/context`.

`stats` — служебная сводка: `entities`, `facts`, `latency_ms`, `steps` (на шаг —
`relation`, `direction`, `depth`, `limit`, `from`, `reached`; `"truncated": true` —
на каком-то уровне шага действующих рёбер оказалось больше 20 000 и часть не
рассмотрена) и `phases_ms` — длительности фаз компиляции (`connect`, `setup`,
`anchors`, `traverse`, `versions` — внутри двух предыдущих, `assemble`, `trace`); состав
фаз не часть контракта, для диагностики. При `CB_SERVER_TIMING=true` на стороне
сервиса ответ несёт заголовок `Server-Timing` с фазами всего запроса (`auth`,
`visibility`, `typed`, `typed.*`, `protect`, `total`).

## Рецепт для суфлёра (минимальный)

1. Реплика клиента → `POST /api/brain/recall` с `budget: "low"`.
2. `context` — в промпт вашей LLM; `sources[]` / `source_path` — в карточку подсказки («откуда взято»).
3. Клик по источнику → `GET /api/brain/sources/{node_key}` — оригинал статьи целиком с provenance.
4. Импорт новой статьи → `POST /api/brain/retain` с `external_id` = URL.

## Ограничения текущей версии

- Синтез ответа (`synthesize: true`) требует настроенного LLM-провайдера на стороне сервиса; без него используйте `synthesize: false` и свою модель.
- Rate limiting на уровне API не включён — не заваливайте сервис параллельными batch-импортами; для массовой загрузки согласуйте окно.

> **Вне этого контракта.** Кроме машинного API `/api/brain/*` сервис может отдавать
> вспомогательные поверхности — админ-консоль `/console` и публичную демо-витрину
> `/demo` (обе выключены по умолчанию, read-only для витрины). Это **не часть контракта
> потребителя**: их наличие/отсутствие и разметку на интеграцию не закладывайтесь.

Вопросы и запросы на доработку контракта — в issues репозитория сервиса.
