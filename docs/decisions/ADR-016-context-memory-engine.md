# ADR-016: Эволюция в Context Memory Engine — Observations, temporal facts, scopes, Context Compiler

**Status:** Accepted (2026-09-12; реализовано — `core/{observations,scopes,identifiers}.py`,
`graph/facts.py`, `observations/*`, `context/*`, `server/memory_api.py`, MCP `build_context`/
`remember_observation`; потребитель — control-plane context-adapter)
**Date:** 2026-08-11

## Context

memory-service сегодня — graph/vector memory API: vault → типизированный граф (AGE) +
чанки (pgvector, гибридный вектор+FTS+RRF), retrieve/query с цитатами, write-back
агентских фактов, namespaces как единица изоляции (ADR-003/054). Продукт хочет стать
универсальным **Context Memory Engine**: принимать наблюдения из любых внешних систем
(issue tracker, CRM, почта, coding harness, agent runtime), хранить их в нескольких
формах памяти и по запросу собирать ограниченный по размеру **ContextPack** с полным
provenance. Требования (сводка ТЗ владельца, 52 пункта): first-class immutable
Observation с идемпотентным ingest; различие raw evidence ↔ derived memory (Episode);
temporal-факты (valid_from/valid_to, supersession без стирания истории); generic
scopes поверх namespaces; lexical retrieval для идентификаторов; hybrid retrieval;
`build_context()` без LLM-синтеза; token budgeting; трейс решений; consolidation;
корректная deletion semantics; сохранение всех текущих API и изоляции пакета (BIZ-023).

Хранилище остаётся PostgreSQL 16 + AGE + pgvector (ADR-014/015 закрыли вопрос движка).

## Decision

### 1. Observation — новая первичная форма памяти (реляционная таблица)

Наблюдения **не** кладём в граф: это append-only evidence-журнал с реляционной
семантикой (уникальность source identity, статусы обработки, батчи) — сильные стороны
Postgres, слабые — AGE. Таблица `observations` (имя из `CB_OBSERVATIONS_TABLE`):

```sql
CREATE TABLE observations (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  observation_id text NOT NULL,          -- стабильный публичный id: obs-<sha256[:24]>
  namespace     text NOT NULL,
  source_system text NOT NULL DEFAULT '',-- {system, stream, external_id} — source identity
  source_stream text NOT NULL DEFAULT '',
  external_id   text NOT NULL DEFAULT '',
  kind          text NOT NULL DEFAULT '',-- 'work.completed', 'meeting.happened', …
  occurred_at   text NOT NULL DEFAULT '',-- event time, ISO-8601 UTC (пусто = неизвестно)
  ingested_at   timestamptz NOT NULL DEFAULT now(),  -- ingestion time
  actor         jsonb,                   -- {type, id} — кто наблюдал/сообщил
  subject       jsonb,                   -- {type, id} — о ком/о чём
  scopes        jsonb NOT NULL DEFAULT '[]'::jsonb,  -- ["project:x", "task:y"]
  content       text NOT NULL DEFAULT '',-- human-readable
  data          jsonb NOT NULL DEFAULT '{}'::jsonb,  -- structured payload
  assertions    jsonb NOT NULL DEFAULT '[]'::jsonb,  -- structured assertions (см. §3)
  provenance    jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {uri, …}
  content_hash  text NOT NULL,           -- sha256 канонической формы — дедуп без external_id
  status        text NOT NULL DEFAULT 'received',
                -- received | processed | partially_processed | failed | redacted
  status_detail text NOT NULL DEFAULT '',
  processed_at  timestamptz
);
UNIQUE (namespace, observation_id)
UNIQUE (namespace, source_system, source_stream, external_id) WHERE external_id <> ''
-- дедуп содержимого при отсутствии external_id:
UNIQUE (namespace, content_hash) WHERE external_id = ''
-- retrieval:
GIN (to_tsvector('russian', content)); GIN (content gin_trgm_ops); GIN (scopes); (namespace, kind); (namespace, occurred_at)
```

Инварианты: observation **immutable** (меняются только status/status_detail/processed_at
и redaction по §8); повторный ingest того же external observation возвращает существующий
`observation_id` с `duplicate: true` (`ON CONFLICT DO NOTHING` + повторное чтение).
`observation_id = "obs-" + sha256(namespace|system|stream|external_id)[:24]`, а без
external_id — от `content_hash`: id детерминирован и воспроизводим на стороне клиента.

Времена разделены явно: `occurred_at` (event time, строка ISO-8601 UTC — сравнение
лексикографично), `ingested_at` (server time), validity time живёт на фактах (§4).

### 2. Episode — derived-представление, не отдельная таблица

Episode концептуально отличается от Observation (сырое свидетельство ↔ осмысленный
фрагмент опыта), но физически это **узел графа** `type='episode'` с provenance до
observation(ов). Создаётся: (а) детерминированно — structured assertion с
`entity.type='episode'`; (б) consolidation'ом из группы наблюдений. Отдельную таблицу
не заводим — раздутие схемы без нового качества; различие фиксируется онтологией и
документацией (docs/context-engine.md).

### 3. Structured assertions — детерминированная проекция без LLM

Observation может нести массив `assertions`, которые проецируются в граф/индекс
синхронно и без LLM:

```json
{"assert": "entity", "entity": {"type": "person", "id": "alice", "title": "Alice", "properties": {}}}
{"assert": "fact",   "fact": {"subject": "person:alice", "predicate": "WORKS_ON",
                               "object": "project:x", "valid_from": "2026-08-01T00:00:00Z",
                               "confidence": 0.9, "supersedes": "fact-…"}}
{"assert": "text",   "text": {"content": "…", "title": "…"}}  // чанк в индекс (embedding — если провайдер доступен)
```

Ingest-ACK не ждёт LLM: запись observation + детерминированная проекция — синхронно;
LLM-extraction unstructured-контента — только on-demand/явно (consolidation, §7).
Ошибка проекции не теряет observation: `status='partially_processed'|'failed'` +
`status_detail`; повторная обработка идемпотентна (redrive через `process_observations`).

### 4. Temporal facts — рёбра с fact_id, validity-интервалами и evidence class

Факт = ребро графа с параллельной историей. Существующий `upsert_edge` (MERGE по паре
узлов) не подходит для истории — вводим **отдельный слой** `graph/facts.py`
(`FactStore`), который создаёт рёбра `MERGE (a)-[r:REL {fact_id: $fid}]->(b)`:
несколько рёбер одного типа между одной парой сосуществуют, различаясь `fact_id`.

Свойства ребра-факта: `fact_id` (детерминированный: `fact-sha256(ns|subj|rel|obj|valid_from)[:24]`),
`observed_at`, `valid_from`, `valid_to` (NULL = действует), `evidence`
(`asserted | extracted | inferred | derived`), `confidence` (0..1, ranking/audit-сигнал,
не математическая истина), `observation_ids` (список), `supersedes` / `superseded_by`
(fact_id), `origin='agent'`, `namespace`, `scopes`.

Supersession: новый факт с `supersedes=X` в одной операции закрывает `X.valid_to =
new.valid_from` и ставит `X.superseded_by`. Историю не удаляем никогда. Автоматического
разрешения противоречий нет: перекрывающиеся по validity факты одного `(subject,
predicate)` оба остаются, retrieval помечает их `conflict: true` — решает потребитель,
не LLM. Запрос `as_of` фильтрует `valid_from <= t AND (valid_to IS NULL OR valid_to > t)`
(ISO-строки, лексикографическое сравнение). Полный bitemporal не строим; модель его
не блокирует (`observed_at`+`ingested_at` уже разделены).

### 5. Scope — фильтр видимости внутри namespace

- **namespace** — жёсткая граница хранения/изоляции KB (как было, ADR-003);
- **scope** — generic-фильтр retrieval ВНУТРИ namespace: пара `type:id`, каноническая
  строка `"{scope_type}:{scope_id}"` (валидация формата, без hardcoded-типов).

Хранение: observations — колонка `scopes jsonb`; чанки — `meta.scopes` (массив в уже
существующей `meta jsonb` с GIN); узлы/рёбра графа — свойство `scopes` (список строк).
Фильтрация: chunks/observations — `?|`-предикат по массиву; граф — сгенерированный OR
из `$s IN n.scopes` (лимит MAX_READ_SCOPES=10, как у namespaces). Семантика: запрос
без scopes видит всё в namespace; запрос со scopes видит элементы, у которых пересечение
scopes непусто, **плюс** элементы вовсе без scopes (общая память KB). Engine не решает
оргправа: caller передаёт разрешённые scopes (extension point — заголовок/поле
allowed_scopes, enforcement на границе HTTP).

### 6. Context Compiler — `build_context(ContextRequest) -> ContextPack`

Новый модуль `context/` (`models.py`, `retrieval.py`, `budget.py`, `compiler.py`).
Отдельный от LLM-синтеза: `query()` продолжает отвечать текстом, `build_context()`
возвращает evidence. Конвейер:

```
ContextRequest{query, subject?, scopes[], anchors[], ephemeral_context{}, budget{max_tokens}, strategy?, namespaces[]}
  → каналы-кандидаты: lexical (chunks FTS+trgm, observations FTS+trgm)
                      vector (chunks pgvector)
                      graph  (expansion от anchors + от топ-хитов, лимиты max_depth≤2/max_nodes/max_edges)
                      recent (свежие observations по scopes — read-after-ingest, §7)
                      facts  (temporal-факты вокруг anchors/хитов, as_of=now по умолчанию)
  → merge (RRF по каналам) → temporal filter → scope filter → weighted ranking → dedup
  → секции: current(ephemeral) | facts | episodes | documents | entities | observations
  → token budgeting (估 оценка chars/4, конфигурируемо; секционные минимумы; отчёт dropped)
  → ContextPack{query, sections[], sources[], token_estimate, dropped[], conflicts[], trace_id}
```

Ranking детерминирован и документирован (без ML): RRF-базис по каналам + взвешенные
бонусы: recency (occurred_at), temporal validity (действующий факт > закрытого),
evidence strength (asserted > extracted > inferred > derived), scope proximity (точное
совпадение scope > без scope), lexical exact-match. `strategy ∈ {semantic, exact,
graph, hybrid, context}` — advanced-указание; дефолт компилятор выбирает сам (hybrid).
`ephemeral_context` не сохраняется — только в текущую компиляцию, секция `current`,
бюджетируется первой. Trace каждой компиляции — строка в таблице `context_traces`
(trace_id, namespace, request-эхо без секретов, счётчики каналов, ranking-решения,
budget-решения, латентности) + `GET /api/memory/context/trace/{id}`.

Дополнение 2026-09-23: канал `resolve` — детерминированное разрешение идентификаторов
запроса и якорей в сущности журнала сверки, seeds graph expansion и бонус ранжирования
(MEM-ADR-020, «Амендмент: канал resolve компилятора контекста»; TAI-ADR-0042 п.4).

### 7. Consolidation и consistency

`consolidate(namespace)` — on-demand/CLI/HTTP, не демон: (а) redrive необработанных
observations (проекция assertions, опциональный embedding text-assertions); (б)
reinforcement — одинаковый факт из нового observation добавляет `observation_ids` и
поднимает confidence (cap); (в) закрытие интервалов по explicit supersedes; (г)
опциональный LLM-extraction unstructured-контента (флаг, отдельно). Уровни
консистентности документируются: observation виден lexical/recent-каналам сразу после
ACK (**read-after-ingest**); вектор — после embedding (синхронно при доступном
провайдере, иначе после consolidate); LLM-derived граф — только после явного extraction.

### 8. Deletion / redaction

`delete_observation(id, mode='redact'|'purge')`: redact — content/data/assertions
затираются, запись и id остаются (аудит); purge — строка удаляется. Оба варианта
каскадно помечают derived-факты: из `observation_ids` удаляется id; если список
опустел — факт получает `evidence_lost: true` и по умолчанию исключается из
retrieval (остаётся в графе для аудита; не «dangling truth»). Всё — через существующий
механизм аудита (ADR-001). GDPR-движок не строим.

### 9. API-поверхности

HTTP (новый префикс, `/api/brain/*` нетронут):

```
POST /api/memory/observations         → {observation_id, duplicate, status}
POST /api/memory/observations:batch   → per-item результаты (partial errors), max size
GET  /api/memory/observations/{id}
POST /api/memory/context              → ContextPack
GET  /api/memory/context/trace/{id}
POST /api/memory/consolidate
DELETE /api/memory/observations/{id}
```

Python: `retain_observation`, `retain_observations`, `build_context`,
`process_observations` + типы `Observation`, `ContextRequest`, `ContextPack`,
`ContextItem` в публичных экспортах. MCP: `remember_observation`, `build_context`
(+ существующие без изменений). CLI: `cb observe`, `cb observations`, `cb context`,
`cb trace`. Batch: жёсткий `max_batch` (дефолт 500), по-элементные ошибки, каждая
запись — своя транзакция (батч не одна гигантская транзакция).

### 10. Storage seam для будущего бенчмарка движков

Не абстрагируем AGE (ADR-015). Вместо этого: (а) Context Compiler ходит в граф только
через узкий набор методов GraphStore/FactStore; (б) `benchmarks/` — отдельный пакет
скриптов вне pytest: ingest throughput, batch, vector/lexical/graph latency, hybrid
context на бюджетах, датасеты 1k/10k/100k, и `GraphBenchOps`-протокол (1-hop, 2-hop,
temporal filter, entity lookup, neighborhood, shortest path) с реализацией для AGE —
seam для сравнения с альтернативным движком без переписывания Context API.

## Consequences

- Добавляются: расширение `pg_trgm` (Postgres contrib, лицензионно чисто), таблицы
  `observations`, `context_traces`; всё создаётся идемпотентным `ensure_schema` —
  существующие БД апгрейдятся на старте, fresh init работает как раньше.
- `/api/brain/*`, Python `retrieve/query/write_and_index_fact`, MCP, CLI, Console,
  demo — без изменений поведения. Новые возможности только аддитивны.
- Ядро корректно без LLM: ingest, structured projection, lexical/vector/graph/hybrid
  retrieval, build_context, provenance, trace — работают на fake/echo.
- Rаw observations не «раздувают» полезную память: Context Compiler отдаёт их только в
  recent-канале и секции observations; consolidation сворачивает повторы в факты.
- Hidden chain-of-thought не хранится: принимаются только явные
  observation/summary/tool result/document/structured fact.

## Alternatives

- **Observations в графе AGE** — отвергнуто: append-only журнал с уникальными
  индексами, статусами и батчами — реляционная задача; AGE-упсерты дороги и гоночны.
- **Отдельная таблица Episodes** — отвергнуто: derived-представление без собственной
  логики хранения; узел графа с provenance выражает то же дешевле.
- **Версионирование фактов в свойствах одного ребра (массив интервалов)** — отвергнуто:
  нечитаемые Cypher-фильтры, невозможность индексирования, суперсешн мутировал бы
  «текущее» значение.
- **Полноценная bitemporal-модель** — отложено: резкий рост scope; текущая модель
  (event/ingestion/validity) её не блокирует.
- **Elasticsearch/внешний поисковик** — отвергнуто: Postgres FTS+pg_trgm достаточны,
  внешняя зависимость ломает one-file/one-instance деплой.
- **User-facing query DSL** — отвергнуто: strategy-подсказка + sane defaults.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: "src/platform_memory/server/memory_api.py", pattern: 'router = APIRouter\(prefix="/api/memory"'}
  repo: memory-service
- route: "POST /observations"
  repo: memory-service
- route: "POST /context"
  repo: memory-service
- grep: {path: "src/platform_memory/observations/store.py", pattern: 'ON CONFLICT \(namespace, observation_id\) DO NOTHING'}
  repo: memory-service
- grep: {path: "src/platform_memory/graph/facts.py", pattern: 'MERGE \(a\)-\[r:\{etype\} \{\{fact_id: \$fid\}\}\]->\(b\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/core/config.py", pattern: 'context_traces_table: str = Field\(default="context_traces"\)'}
  repo: memory-service
```
