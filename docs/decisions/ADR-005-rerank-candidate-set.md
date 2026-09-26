# ADR-005: Реранкинг объединённого кандидатного набора перед синтезом ответа

## Status

Accepted (2026-09-12; реализовано — `retrieval/rerank.py`, `CB_RERANK_*`,
`tests/test_rerank.py`; включено на staging). Предложено 2026-07-23 по разбору
CodeAlive Context Engine. Часть пакета улучшений retrieval ADR-005…008 —
принимаются независимо друг от друга.

## Context

Сейчас `VectorIndex.search()` (`index/store.py`) сливает вектор (pgvector HNSW)
и русский FTS через reciprocal rank fusion (`_rrf_merge`). Затем `retrieve()`
(`retrieval/query.py`) добавляет соседей графа (`GraphStore.expand_neighbors`)
**отдельным блоком** «Связанные сущности» в `_build_context`, который не
участвует в ранжировании — соседи просто дописываются.

RRF — это слияние двух ранжированных списков по позициям, а не переоценка
релевантности пары (запрос, кандидат). Семантического реранкера в конвейере нет.
Слабое место: релевантный источник может не попасть в top-k, если он не в топе
ни вектора, ни FTS по отдельности; а граф-сосед, который реально релевантнее
любого чанка, вообще не ранжируется. Качество цитат-источников — продуктовый
приоритет №2, поэтому именно ранжирование top-k, уходящего в `sources`, —
чувствительная точка.

## Decision

1. **Ввести этап rerank** между сбором кандидатов и подачей в LLM. Кандидатный
   пул = чанки (вектор ∪ FTS) вместе с чанками узлов-соседей графа
   (`expand_neighbors`), приведёнными к общему представлению. Реранкер оценивает
   релевантность (запрос, кандидат) и даёт единый порядок; top-k идёт в контекст
   и в `sources`. Так граф-соседи становятся полноценными кандидатами, а не
   довеском.
2. **Провайдер по умолчанию — LLM-rerank** через существующий `openai`-клиент
   (`retrieval/llm.py`), без новых инфраструктурных зависимостей (инвариант 1):
   батч кандидатов → оценки релевантности → пересортировка. Опциональный
   локальный cross-encoder — за `CB_RERANK_PROVIDER`, если латентность LLM
   окажется неприемлемой (тогда — новая зависимость + запись в `THIRD_PARTY.md`).
3. **За флагом `CB_RERANK_ENABLED` (дефолт off)** → при выключенном флаге
   поведение ровно текущее (RRF без изменений), обратная совместимость. Выход
   API не меняется: `sources`/`score` остаются; опционально добавляется поле
   `rerank_score` в hit — аддитивно (инвариант 2).
4. **Трейс** (`trace_id`, приоритет №5) дополняется этапом rerank: видно, как
   переоценка сдвинула порядок относительно RRF.

## Consequences

- Качество top-k и цитат растёт — прямое усиление приоритета №2.
- +латентность и +стоимость на LLM-rerank; митигируется малым пулом
  (`pool≈20`) и флагом; RRF остаётся дешёвым fallback при off.
- Открывает кэширование rerank по (query, chunk) как отдельный шаг оптимизации.
- Реранк — естественная точка приложения весов источника (ADR-008).

## Alternatives

- **Просто увеличить `k`** — отклонено: тащит шум в контекст LLM и не решает
  саму задачу ранжирования.
- **Ручные веса вектор vs FTS в RRF** — отклонено: хрупко и не переносит
  граф-кандидатов в общий скоринг.
- **Cross-encoder по умолчанию** — отклонено как дефолт: новая тяжёлая
  зависимость и модель в on-prem-контуре, тогда как LLM-клиент уже есть;
  оставлен как опциональный провайдер.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- file: src/platform_memory/retrieval/rerank.py
  repo: memory-service
- grep: {path: "src/platform_memory/core/config.py", pattern: 'rerank_enabled: bool = Field\(default=False\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/core/config.py", pattern: 'rerank_provider: str'}
  repo: memory-service
- grep: {path: "src/platform_memory/retrieval/query.py", pattern: 'h\.rerank_score = s'}
  repo: memory-service
- grep: {path: "tests/test_rerank.py", pattern: 'def test_(rerank_disabled_by_default_in_settings|apply_rerank_reorders_and_sets_score)'}
  repo: memory-service
```
