# Публичная демо-витрина памяти

Публичная интерактивная витрина ценности **«проверяемой корпоративной памяти»** для
B2B-аудитории (EN). Лид задаёт вопрос по демо-базе знаний и получает top-k фрагментов
со **score** и **открываемым источником** (provenance) — главный экран «Search Check».

Витрина живёт **внутри `memory-service`** как выключенный по умолчанию in-process
роутер (`src/platform_memory/server/demo.py`, ADR-009) — тот же паттерн, что `/console`.
Отдельный рантайм не нужен: страница и её read-only API отдаются тем же процессом, что
и движок, поэтому **API-ключ сервиса в браузер не попадает**.

## Два режима (один интерфейс `DataProvider`)

| Режим | Что это | Где | Данные |
|-------|---------|-----|--------|
| **Live** (по умолчанию) | Страница + `/demo/api/{search,source}` на стенде | демо-контур стенда | реальный инференс `retrieve`/source-view по демо-KB |
| **Canned** (офлайн-фолбэк) | Самодостаточный `dist/index.html` | GitHub Pages | синтетический корпус вшит в страницу, поиск в браузере |

Фронтенд один и тот же (`src/platform_memory/server/templates/demo.html`); режим
переключается полем `mode` в конфиге страницы. Бейдж честно показывает `Live` /
`Offline sample`.

## Жёсткие границы (проверяются тестами `tests/test_demo.py`)

- **Ключ не в браузере.** Браузер ходит только в `/demo` и `/demo/api/*`; ключ сервиса
  в HTML/JS/сети фронта не появляется. В canned ключей нет вовсе.
- **Ровно одна KB.** Namespace жёстко = `CB_DEMO_NAMESPACE`; клиент его задать не может.
- **Только чтение.** `search` + `source`. Никаких `retain`/`delete`/import/других KB/PII.
- **Rate-limit** по IP (`CB_DEMO_RATE_LIMIT`, по умолчанию 30/мин).
- **Выключено по умолчанию** (`CB_DEMO_PUBLIC_ENABLED=false`). **Не включать на контуре
  с реальными данными.**

## Локальный запуск (live)

```bash
uv sync
# Включаем витрину и указываем демо-KB (оффлайн-провайдеры — чтобы не ходить в внешний LLM):
CB_DEMO_PUBLIC_ENABLED=true CB_DEMO_NAMESPACE=demo \
CB_EMBEDDING_PROVIDER=fake CB_LLM_PROVIDER=echo \
  uv run platform-memory-serve
# → http://127.0.0.1:8077/demo   (и http://127.0.0.1:8077/ редиректит на /demo)
```

Наполнить демо-KB синтетикой (идемпотентно, external_id = URL статьи):

```bash
DEMO_BASE_URL=http://127.0.0.1:8077 DEMO_NAMESPACE=demo \
  uv run python demo/seed_stand.py
```

Построить **граф концептов** над статьями (узлы concept + рёбра `COVERS`) — чтобы
витрина показывала графовый отбор, а не плоский список. Запускается там, где есть
доступ к БД движка (внутри контейнера сервиса):

```bash
docker cp demo/build_graph.py <serve>:/tmp/ && \
  docker exec -e DEMO_NAMESPACE=demo <serve> python /tmp/build_graph.py
```

## Поднятие на стенде

На демо-контуре (`teststand/memory-demo/`) в `.env` добавить:

```env
CB_DEMO_PUBLIC_ENABLED=true
CB_DEMO_NAMESPACE=demo
CB_DEMO_RATE_LIMIT=30
CB_DEMO_BRAND_NAME=Verifiable Memory
CB_DEMO_BRAND_TAGLINE=Answers your team can trace back to the source.
CB_DEMO_CTA_URL=mailto:hello@<домен>
```

`docker compose up -d --build`, затем прогнать `seed_stand.py` против стенда
(`DEMO_BASE_URL`/`DEMO_TOKEN` из окружения). Traefik-роут для витрины — в
`teststand/memory-demo/docker-compose.yml` (см. блок `# demo-showcase`).

## Вход с taimen.ai — выбран поддомен `demo.taimen.ai`

Владелец выбрал **вариант А**: поддомен `demo.taimen.ai` → стенд (DNS уже направлен на
сервер). Traefik ловит `Host(demo.taimen.ai)` (роутер `memory-demo-show`, `DEMO_HOST`),
корень `/` редиректит на `/demo`. Витрина включается одним флагом `CB_DEMO_PUBLIC_ENABLED=true`
после наполнения демо-KB (см. `teststand/memory-demo/DEMO-SHOWCASE.md`).

Альтернатива (не выбрана): `taimen.ai/demo` как статический лончер на Pages. Все URL
внутри страницы относительны инъектируемого `base`, поэтому и поддомен, и подпуть
работают без пересборки — при необходимости переключение бесплатно.

## Сборка офлайн-среза (canned) для Pages

```bash
uv run python demo/build_canned.py          # → demo/dist/index.html (самодостаточный)
grep -RniE 'bearer|api[_-]?key|password|token' demo/dist/   # должно быть пусто
```

`demo/dist/` в `.gitignore` (артефакт сборки). Публикацию на Pages выполняют сопровождающие проекта.
Синтетический корпус — `demo/corpus/corpus.json` (только `*.example`-домены, без ПДн и
реальных сущностей); он же наполняет стенд через `seed_stand.py`.

## Файлы

```
demo/
  build_canned.py     сборка dist/index.html (canned) из corpus.json
  seed_stand.py       загрузка corpus.json в демо-KB стенда (retain, последовательно)
  build_graph.py      граф концептов над статьями (узлы concept + рёбра COVERS) для витрины
  corpus/corpus.json  синтетический EN-корпус (для canned и для стенда)
  .env.example        переменные (без секретов)
  dist/               артефакт сборки canned (gitignored)
src/platform_memory/server/
  demo.py             gated public router (ADR-009)
  templates/demo.html витрина (live + canned, один DataProvider)
```
