# ADR-019: Видимость памяти по principal через policy-service

## Status

Accepted (2026-09-12; дополнено TASK-001045…001064; реализовано — `server/visibility.py`, `core/scopes.py`
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

- `/api/brain/query|recall|search|nodes`, `/api/brain/nodes/{key}` и
  `/api/memory/context` применяют видимость (структурный `search` и
  `/nodes/{key}` — с TASK-001045; невидимый узел по ключу — `404`, неотличимо от
  отсутствующего).
- С TASK-001047 видимость применяют и остальные маршруты чтения HTTP:
  `/api/brain/sources/{key}` и `/api/memory/observations/{id}` — `404` для
  невидимого; `/api/memory/observations` — фильтр в SQL до `limit`, свод
  `statuses` тоже по видимым; `/api/brain/trace/{id}` отбрасывает невидимые факты
  и события аудита — scope события — его `props.scopes` плюс `payload.scopes`,
  куда удаление узла кладёт scopes снимка; `/api/memory/context/trace/{id}` —
  трейс хранит видимость автора (`request.allowed_scopes`) и виден читателю,
  чьи разрешённые scopes её покрывают (автор без ограничения и трейсы до
  TASK-001047 — только читателю без ограничения). События удаления, записанные
  до TASK-001047, scopes снимка не несут и остаются уровнем namespace.
- С TASK-001050 видимость применяют удаления: `DELETE /api/brain/nodes/{key}`,
  `DELETE /api/brain/documents/{key}` и `DELETE /api/memory/observations/{id}`.
  Невидимый объект не удаляется (ни узел, ни чанки, ни каскад наблюдения), ответ
  тот же, что для отсутствующего (`404`; документ — `200 deleted: false`), без
  полей объекта: право записи в namespace не даёт удалить чужую приватную память.
  Проверка — в движке (`GraphStore.delete_node`, `delete_document`,
  `delete_observation` принимают `allowed_scopes`). `payload.scopes` несут и
  события `observation_delete` (scopes наблюдения) и `pii_access` (scopes
  выданного узла, источника или наблюдения; у списочных выдач — нет, событие
  остаётся уровнем namespace). События `POST /api/brain/audit` скрываются по
  `payload.scopes`, если вызывающий его передал; маршрут принимает только
  список корректных scopes (иначе `400`) и хранит его канонизированным.
  `trace_subgraph` фильтрует видимость до лимита (200), чужие узлы не вытесняют
  видимые. Фильтр — в Python (`trace_node_scopes` + `is_visible`), не в Cypher:
  payload события приходит от клиента, и предикат по произвольному agtype
  (объект или строка вместо списка в событиях, записанных до валидации) ронял
  бы весь трейс; такой `payload.scopes` считается отсутствующим, как в
  `node_scopes`. Чанки удаляемого узла или документа фильтруются по
  `meta.scopes` (`VectorIndex.delete_for_node(allowed_scopes=…)`): чужой чанк без
  узла не удаляется и не попадает в `chunks_deleted`. Typed-трейс
  (`/api/memory/context/typed`) хранит `request.allowed_scopes`, как
  нетипизированный: автор с ограниченной видимостью читает свой трейс.
- С TASK-001052 видимость применяют записи по ключу существующего объекта:
  `POST /api/brain/retain`, `/api/brain/documents`, `/api/brain/facts`,
  `/api/memory/observations` и `:batch`. Без этого защиту чтения и удаления
  обходили в два шага: перезаписать чужой узел (`props` заменялись целиком
  вместе со `scopes`), затем прочитать или удалить.
  - **Отказ.** Узел ключа (любого вида — чтение и удаление вид не учитывают) или
    чанк ключа вне видимости пишущего не перезаписывается: `ForeignObjectError`
    до любой записи, HTTP — `403` с `detail` «Нет прав на запись: <ключ>», тем же
    кодом, что отсутствие права записи в namespace, без полей и scopes объекта.
    Выбран `403`, а не `404` (запись по отсутствующему ключу создаёт объект — ответ
    «не найдено» был бы ложью) и не `409` (конфликт обещает, что повтор с другими
    данными пройдёт). То, что ключ занят, неустранимо следует из самой
    возможности записи; скрываются содержимое, вид и scopes. `replace: true`
    документа поэтому не удаляет ничего чужого; удаление чанков дополнительно
    фильтруется `allowed_scopes`. У наблюдений отказ — ошибка assertion'а в
    статусе наблюдения, как любая ошибка проекции.
  - **Слияние scopes общей сущности** (прежде «побеждает последний писатель»):
    объединение — `merge_scopes` в `core/scopes.py`. Scopes видимости прежнего
    узла и записи объединяются: перезапись их не стирает, а сущность из
    наблюдений двух воркспейсов видна обоим. Объект уровня namespace (без scope
    видимости) им и остаётся: иначе запись со scope своего воркспейса скрыла бы
    общий объект от остальных. Scopes релевантности — последней записи, если она
    их передала, иначе прежние (не копятся от задачи к задаче). Сузить видимость
    объекта перезаписью нельзя — только удалить (с проверкой видимости) и
    записать заново. Отказ при несовпадении scopes отвергнут: сущности домена
    (люди, проекты) законно встречаются в наблюдениях разных воркспейсов.
  - Проверка и слияние — в движке, под advisory-локом upsert
    (`GraphStore.upsert_node(guard_scopes=True, allowed_scopes=…)`, через
    `write_fact` и `FactStore.ensure_entity`); чанки — `VectorIndex.guard_chunk_write`.
    Ингест vault и сверка снимков (`reconcile`) по-прежнему заменяют узел целиком:
    их источник — канон. Чанки, записанные этими маршрутами, несут в
    `meta.scopes` scopes узла и прежних чанков ключа — по ним фильтрует индекс.
  - Видимость пишущего — `write_visibility`: как у чтения, но service account с
    `memory:on-behalf` без `allowedNamespaces`/`allowedScopes` пишет от себя, без
    ограничения (ингест документов и наблюдений Control Plane); с ними —
    ограничен ими. Namespace записи решают гранты, не `VisibleSet.namespaces`.
  - **Scopes записи — только свои** (`check_write_scopes`). Scope видимости,
    который пишущий передаёт (`properties.scopes`, `meta.scopes` чанков, scopes
    наблюдения и факта), должен входить в его видимость; иначе `403` с
    `detail` «Нет прав на запись: <scope>» — scope передал сам пишущий, объектов
    ответ не раскрывает. Наблюдение с чужим scope не сохраняется вовсе (в батче —
    ошибка элемента). Без этого пишущий подсовывал бы объекты чужому воркспейсу,
    а redrive переписывал бы от его имени чужие узлы.
  - Redrive наблюдений (`process_observations`, `/api/memory/consolidate`) в
    TASK-001052 пишущего не знал и перезаписывал объекты, видимые scopes самого
    наблюдения. Этого не хватало для записей, принятых до `check_write_scopes`:
    их scopes пишущий объявлял сам. С TASK-001056 видимость пишущего хранится в
    записи (см. ниже).
  - **Факты** (`FactStore.assert_fact(allowed_scopes=…)`, проекция наблюдений):
    под advisory-локом факта проверяются оба конца (все узлы ключа) и
    существующий факт того же `fact_id` (id детерминирован, повтор того же
    утверждения попадает в то же ребро). Невидимый конец или факт —
    `ForeignObjectError`, ошибка assertion'а; концы больше не используются
    молча. Scopes повторённого видимого факта сливаются `merge_scopes`, а не
    заменяются последним. `supersedes` на невидимый факт его не закрывает и
    ведёт себя как ссылка на отсутствующий: новый факт записывается,
    `superseded: false`.
  - `links` в `write_fact` (`/api/brain/retain`, `/facts`, `/documents`) к
    невидимому узлу не создаются и не считаются в `edges` — как к отсутствующему,
    так что счётчик не раскрывает занятость чужого ключа.
  - Приватное наблюдение (со scope видимости), записавшее содержимое в общий
    узел namespace (`entity`/`text` по ключу узла без scope видимости), получает
    в ответе `warnings`: по правилу слияния узел остаётся общим, и пишущий должен
    знать, что содержимое видно всем, кто читает namespace.
  - `guard_chunk_write` в TASK-001052 читал чанки ключа вне лока (TOCTOU) —
    закрыто в TASK-001056 (ниже). Console `/import` и MCP — там же.
- С TASK-001056 — остальные поверхности и хвосты записи:
  - **Лок ключа записи** (`VectorIndex.key_write_lock`): проверка чанков и узлов
    ключа, запись узла и чанков (`write_and_index_fact`, `retain_document`,
    text-assertion наблюдения) — под одним сессионным advisory-локом
    `(таблица чанков, namespace, ключ)`. Прежде проверка чанков шла вне лока, а лок
    upsert'а узла зависит от вида: чанк без узла или узел другого вида,
    появившийся между проверкой и записью, перезаписывался. Лок сессионный, а не
    транзакционный: соединения движка autocommit, запись узла и чанков — несколько
    транзакций, длинную транзакцию ради лока не держим. Эмбеддинги считаются до
    лока (провайдер бывает сетевым). Ингест vault и `reconcile` лок не берут: их
    источник — канон и видимость не проверяется.
  - **Видимость пишущего в записи наблюдения** — колонка `writer_visibility jsonb`
    (`{"scopes": [...]}`; `{"scopes": null}` — без ограничения; `NULL` — запись до
    TASK-001056), аддитивная миграция в `ObservationStore.ensure_schema`. В ответы
    API не выдаётся: это членство пишущего, а не свойство наблюдения. Redrive
    пишет с видимостью `redrive_scopes`: видимость пишущего из записи, суженная
    видимостью запустившего redrive (`consolidate` вызывающего; CLI — без
    ограничения). Запись без сохранённой видимости — уровень namespace (`[]`):
    scopes таких записей объявлял сам пишущий, доверять им нельзя; их
    приватные assertions получают ошибку, общие — проецируются. Прежние
    возражения (до 500 scopes на запись, устаревание вместе с членством) приняты:
    объём ограничен `MAX_ALLOWED_SCOPES`, а устаревание гасит сужение видимостью
    запустившего — redrive не видит больше ни пишущего на момент приёма, ни
    вызывающего сейчас.
  - **`POST /api/memory/consolidate`** — `write_visibility` и `check_visible`:
    namespace вне видимости — `403` (отчёт раскрывает счётчики, как чтение);
    вызывающий с ограничением переобрабатывает и считает только видимые ему
    наблюдения (`list_unprocessed(allowed_scopes=…)`, фильтр в SQL до `limit`).
  - **Идемпотентность наблюдений.** Повтор source identity наблюдения, невидимого
    пишущему, — `ForeignObjectError` с source identity из запроса
    (`<system>/<stream>/<external_id>`, без `external_id` — `observation`): HTTP
    `403`, в батче — ошибка элемента; ни `observation_id`, ни статус чужой записи
    не выдаются. Ответ «как на отсутствие» невозможен: запись с тем же id не
    создать, а ответ без записи был бы ложью. Сам факт «identity занята»
    неустраним, как у ключей узлов; для наблюдений без `external_id` identity —
    хэш содержимого, и отказ подтверждает, что такое содержимое уже есть в
    невидимой записи. Это принято: содержимое вызывающий передал сам.
  - **Console `/import`** пишет с видимостью уровня namespace
    (`VisibleSet(scopes=())`): Console не знает principal (доступ ограничивает
    reverse proxy) и не может заявить воркспейс; объект со scope воркспейса или
    principal импорт не перезаписывает — `403` на странице результата. Чтение и
    удаление в Console — по-прежнему полный допуск администратора.
  - **MCP остаётся служебной поверхностью.** Кто вызывает: stdio — локальный
    процесс оператора, у которого и так есть `CB_DATABASE_URL`; Streamable HTTP —
    только держатель `CB_SERVER_API_KEY` (IAM-токены и ключи реестра MCP не
    принимает). Это тот же service-режим, что у статического ключа HTTP API, —
    MCP не даёт ничего сверх того, что у вызывающего уже есть; поэтому
    видимость по умолчанию не ограничена. Агент, действующий за конечного
    principal, передаёт в аргументах любого tool'а `allowed_scopes` — сужение
    (MEM-ADR-021): `get_node`, `get_neighbors`, `shortest_path`, `get_community`,
    `query_graph` (seed'ы и проекция `to_networkx(allowed_scopes=…)` без
    невидимых узлов и рёбер к ним), `build_context` и `remember_observation`
    (`check_write_scopes`, отказ по чужому дубликату) — та же проверка, что у
    HTTP. Поиск узла по заголовку ограничен default namespace (прежде шёл по
    всему графу). Выставлять MCP конечным пользователям нельзя — им HTTP API с
    IAM-токеном.
- С TASK-001059 — эксплуатационные хвосты TASK-001056:
  - **Миграция `writer_visibility` с `lock_timeout`.** `ALTER TABLE … ADD COLUMN`
    ждёт ACCESS EXCLUSIVE, пока открыт любой читатель таблицы, а все новые запросы
    встают в очередь за ALTER: долгий читатель останавливал приём наблюдений
    целиком. Теперь ALTER идёт в своей транзакции с `lock_timeout` 5 с
    (`set_config(…, true)` — только на эту транзакцию), до 3 попыток с паузой;
    затем `SchemaMigrationBusy` с указанием таблицы — HTTP `503` с
    `Retry-After: 5` на `POST /api/memory/observations` и `/consolidate`, в батче —
    ошибка элемента. Миграция выполняется один раз: колонку проверяет каталог.
  - **MCP по HTTP без ключа — только loopback.** Служебность MCP держится на
    `CB_SERVER_API_KEY`; без ключа Streamable HTTP на не-loopback адресе
    (`0.0.0.0`, `::`, адрес интерфейса, имя хоста) не стартует
    (`InsecureBindError`, выход с кодом 2), а не только предупреждает. Loopback
    (`127.0.0.0/8`, `::1`, `localhost`) без ключа допустим: его видит только
    локальный оператор, как stdio.
  - **Переобработка наблюдений без видимости пишущего** (`writer_visibility`
    NULL — приняты до TASK-001056). Обычный redrive проецирует их на уровне
    namespace, и приватные assertions остаются `failed` навсегда: видимость
    пишущего на момент приёма неизвестна, а угадывать её по scopes записи нельзя
    (их объявлял сам пишущий). Путь — явное решение оператора:
    `cb redrive-legacy` (`redrive_legacy_observations`) с ровно одним из
    `--writer-scope type:id …`, `--unrestricted` (service-режим),
    `--namespace-level`; опционально `--observation-id …`, `--namespace`.
    Заявленная видимость записывается в `writer_visibility` записи (след решения;
    дальнейший redrive пишет с ней же) — только там, где она NULL: сохранённую
    видимость команда не переписывает. Запись со scope видимости вне заявленной
    не трогается и выводится как пропущенная (как `check_write_scopes` при
    приёме): оператор повторяет команду с верной видимостью. Только CLI: у
    оператора и так полный доступ к БД; HTTP-маршрута нет — заявить чужую
    видимость пишущего через API значило бы расширить собственную.
    Альтернативы отвергнуты: брать scopes записи как видимость пишущего —
    ровно та дыра, которую закрывала TASK-001056; массово выставить
    `{"scopes": null}` миграцией — молча дать старым записям service-режим.
- С TASK-001064 — по ревью TASK-001059:
  - **Короткое ожидание лока миграции.** `lock_timeout` 5 с × 3 попытки держал
    очередь новых запросов за ожидающим ALTER до ~15 с, а попытки разных запросов
    шли внахлёст, пока жива долгая транзакция (`pg_dump`). Теперь попытка ждёт
    лок не дольше 500 мс, попыток 10 с паузой 1 с (очередь за ALTER
    рассасывается), и ALTER пробует только сеанс, взявший
    `pg_try_advisory_xact_lock` миграции таблицы: в очереди к таблице за ALTER
    одновременно стоит не больше одного сеанса. Остальные после паузы проверяют
    каталог — колонку мог добавить держатель лока. Исчерпав попытки —
    `SchemaMigrationBusy` (HTTP `503`), как прежде.
  - **Аудит `cb redrive-legacy`.** Заявленная видимость отличима от записанной
    при приёме: `writer_visibility` получает метку `declared_by: "operator"`,
    `declared_at` (UTC) и `actor` (`--actor`, по умолчанию `operator`); redrive
    читает из неё только `scopes`. На каждую заявленную запись — событие аудита
    `observation_writer_visibility_declared` (трейс — `observation_id`,
    `actor_kind: "operator"`, payload — заявленные `writer_scopes`, `source`
    записи и её `scopes` для фильтра видимости трейса); пишется до проекции, чтобы
    её сбой не оставил заявление без следа.
  - **Проверка источника до заявления.** Заявление видимости переигрывает запись
    от имени заявленного пишущего: запись, подложенная до TASK-001052/001056 (scope
    воркспейса объявлял сам пишущий), после заявления `--writer-scope` этого
    воркспейса перезапишет его объекты — отложенная перезапись. Поэтому сначала
    `cb redrive-legacy … --dry-run`: команда выводит `source.system/stream/
    external_id` и `scopes` каждой подходящей записи, ничего не записывая;
    записи из неожиданного источника исключаются (`--observation-id` только
    проверенных) или удаляются (`DELETE /api/memory/observations/{id}`).
- Маршруты без тела (`GET`, `DELETE`) не несут `allowedNamespaces`/`allowedScopes`,
  поэтому service account с `memory:on-behalf` получает на них `403`: удалить
  память от имени конечного principal сейчас нельзя.
- Console: чтение и удаление — полный допуск администратора за reverse proxy;
  запись — уровень namespace (TASK-001056). MCP — служебная поверхность
  статического ключа с сужением `allowed_scopes` (TASK-001056).
- Control Plane обязан снабжать наблюдения namespace и scope воркспейса,
  выведенными из задачи; наблюдение только с `task:`/`run:` остаётся уровнем
  namespace (переходный режим, дизайн v0 §11).
- Тесты: `tests/test_visibility.py` — предикаты, `VisibleSet`, HTTP-контракт с
  подменённым policy; `tests/integration/test_visibility_narrowing.py` и
  `tests/integration/test_visibility_reads.py`,
  `tests/integration/test_visibility_writes.py` и
  `tests/integration/test_visibility_surfaces.py` (Console, MCP, consolidate,
  дубликаты наблюдений, видимость пишущего, лок ключа),
  `tests/integration/test_legacy_observations.py` (миграция с `lock_timeout` и
  advisory-локом, `redrive-legacy` с меткой, аудитом и `--dry-run`) — на Postgres (W1/W2/без воркспейсов); `tests/test_mcp_server.py`
  (отказ старта MCP без ключа), `tests/test_cli_redrive_legacy.py`,
  `tests/test_observation_migration.py` (границы ожидания миграции).

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: "src/platform_memory/core/scopes.py", pattern: '^def (is_visible|observation_visibility_sql|chunk_visibility_sql)\('}
  repo: memory-service
- grep: {path: "src/platform_memory/server/app.py", pattern: 'Depends\(visibility\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/server/memory_api.py", pattern: 'payload\["allowed_scopes"\] = list\(vis\.scopes\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/core/scopes.py", pattern: '^def merge_scopes\('}
  repo: memory-service
- grep: {path: "src/platform_memory/core/scopes.py", pattern: '^def check_write_scopes\('}
  repo: memory-service
- grep: {path: "src/platform_memory/server/app.py", pattern: 'Depends\(write_visibility\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/index/store.py", pattern: 'def key_write_lock\('}
  repo: memory-service
- grep: {path: "src/platform_memory/observations/ingest.py", pattern: '^def redrive_scopes\('}
  repo: memory-service
- grep: {path: "src/platform_memory/observations/ingest.py", pattern: '^def redrive_legacy_observations\('}
  repo: memory-service
- grep: {path: "src/platform_memory/mcp/server.py", pattern: 'raise InsecureBindError\('}
  repo: memory-service
- grep: {path: "src/platform_memory/core/config.py", pattern: 'policy_enabled: bool = Field\(default=False\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/context/compiler.py", pattern: '"briefing": \{("resolve", )?"graph", "facts", "recent"\}'}
  repo: memory-service
- grep: {path: "tests/test_visibility.py", pattern: 'def test_(is_visible_private_by_default|visible_set_policy_unavailable_is_503|http_query_and_context_apply_visibility)'}
  repo: memory-service
```
