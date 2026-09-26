# ADR-001: Удаление записей из графа — hard delete с обязательным аудитом

## Status

Accepted (2026-07-06, владелец запросил механизм явно).

## Context

Память открыта внешним потребителям: они наполняют базу через `retain` и ошибаются
(дубли, устаревшие статьи, мусор при тестировании). До сих пор удалить запись через
API было нельзя — только перезаписать по `external_id` или вручную в БД. Нужен
штатный механизм удаления, при этом удаление — безвозвратная операция, а продуктовое
требование №5 (трейс) требует восстановимости истории: «что удалили, кто и когда».

## Decision

1. **Hard delete**: `DELETE /api/brain/nodes/{natural_key}` физически удаляет узел
   (`DETACH DELETE` — вместе с рёбрами) и все его чанки из векторного индекса.
   Реализация: `GraphStore.delete_node` + `VectorIndex.delete_for_node`.
2. **Аудит обязателен и неотключаем**: после удаления в тот же граф пишется
   `audit_event` c `action="delete"` и снапшотом удалённого узла (type, title,
   chunks_deleted, namespace) в payload; `trace_id` = query → `X-Run-Id` → авто-uuid
   и возвращается в ответе. След — через `GET /api/brain/trace/{trace_id}`.
3. **Аудит-контур защищён**: типы `audit_event` и `pc_trace` удалять нельзя
   (`PROTECTED_TYPES`, ответ `400`) — иначе механизмом удаления можно стереть сам
   след удаления.
4. **Vault не затрагивается** (инвариант №5): удаляется только узел графа и индекс,
   документы-источники остаются.
5. Ключ маршрута — `{natural_key:path}`: `retain` использует `external_id`=URL как
   `natural_key`, ключи содержат слэши.

## Consequences

- Контракт `/api/brain/*` расширен аддитивно; `docs/INTEGRATION.md` дополнен разделом
  «Удаление».
- `VectorIndex.delete_for_node` теперь возвращает число удалённых чанков (было
  `None`) — аддитивно, вызывающие значение игнорировали.
- Повторный DELETE того же ключа даёт `404` (не идемпотентный `200`) — симметрия с
  `GET /api/brain/nodes/{key}` и честный сигнал «удалять нечего».
- История удалений накапливается в графе как `audit_event`-узлы; чистка самого
  аудита возможна только вне API (осознанно, руками владельца).

## Alternatives

- **Soft delete (пометка `deleted=true` + фильтрация в поиске)** — отклонено:
  демо-KB нужна физическая чистота (owner-требование «чистая база»), а фильтрация
  по флагу расползлась бы по всем путям чтения (retrieve/recall/search/nodes).
- **Идемпотентный DELETE (всегда 200)** — отклонено: `404` симметричен GET и
  позволяет отличить «удалил я» от «уже не было» без чтения аудита.
- **Удаление без аудита (проще)** — отклонено: противоречит продуктовому
  требованию трейса и делает безвозвратную операцию бесследной.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- route: "DELETE /api/brain/nodes/{natural_key:path}"
  repo: memory-service
- grep: {path: "src/platform_memory/graph/store.py", pattern: 'PROTECTED_TYPES = frozenset\(\{"audit_event", "pc_trace"\}\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/graph/store.py", pattern: 'DETACH DELETE n \$\$'}
  repo: memory-service
- grep: {path: "src/platform_memory/index/store.py", pattern: 'def delete_for_node\(.*\) -> int:'}
  repo: memory-service
- grep: {path: "tests/test_server_delete.py", pattern: 'def test_delete_(happy_path|protected_type_400_keeps_node|missing_node_404_without_audit)'}
  repo: memory-service
```
