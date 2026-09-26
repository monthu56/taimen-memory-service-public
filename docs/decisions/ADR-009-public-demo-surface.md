# ADR-009: Публичная демо-витрина `/demo` — gated in-process поверхность поверх движка

## Status

Accepted (2026-09-12; реализовано — `server/demo.py`, `CB_DEMO_*`, `tests/test_demo.py`;
витрина работает на публичном домене владельца). Предложено 2026-07-24 агентом по
треку T-108 «демо-песочница памяти для ЮВА/Taimen». Включение на любом контуре с
реальными данными по-прежнему требует явного решения владельца.

## Context

Нужна **публичная интерактивная витрина** ценности «проверяемой корпоративной памяти»
для лидов ЮВА (B2B, EN): вопрос по демо-базе → top-k фрагментов со score и **открываемым
источником** (provenance). Это маркетинговая поверхность, не продукт: цель — показать
ценность и собрать заявки на пилот, поверх реального инференса на стенде.

Ограничения диктуют форму:
- **Ключ сервиса (`CB_SERVER_API_KEY`) не должен попадать в браузер.** Значит фронт не
  может ходить в `/api/brain/*` напрямую с токеном — нужен server-to-server слой.
- Витрина **read-only** и **изолирована по одной демо-KB**; нельзя дать публике запись,
  чужие namespace, PII или служебные поля.
- Нельзя ломать публичный контракт `/api/brain/*` и `/console` (инвариант обратной
  совместимости) и нельзя поднимать второй рантайм без необходимости.

Движок уже даёт всё нужное: `retrieve` (вектор+FTS+граф, hits со `score`/`source_path`),
`brain_source` (оригинал + provenance), namespace-фильтр (ADR-003/054),
`protect_pii_response` (ADR-002). Console (ADR-004) уже доказала паттерн «server-rendered
in-process поверх того же движка, ключ в браузер не уходит».

## Decision

1. **Витрина — выключенный по умолчанию in-process роутер `server/demo.py`**, включаемый
   в то же FastAPI-приложение, что и Memory API (как Console). Отдельного рантайма нет:
   страница и её API живут в одном процессе с движком, поэтому между витриной и движком
   **ключ вообще не нужен**. Гейт `CB_DEMO_PUBLIC_ENABLED=false` → 404 на всех маршрутах.
2. **Поверхность — ровно три маршрута, все read-only и публичные (без Bearer):**
   - `GET /demo` — HTML-витрина (hero + «Search Check» + how-it-works + CTA + mode-badge);
   - `POST /demo/api/search` — top-k фрагментов со score и цитатой (движок `retrieve`);
   - `GET /demo/api/source?key=…` — полный оригинал + provenance (движок `brain_source`);
   - плюс `GET /` → 302 на `/demo` (удобство поддомена), тоже под гейтом.
3. **Изоляция жёсткая:** namespace = `CB_DEMO_NAMESPACE` зашит на сервере, **клиент его
   задать не может** (в модели запроса нет поля scope/namespace; лишние поля игнорируются).
   Чужие базы недостижимы с витрины.
4. **Защита выдачи:** всегда маскированный принципал (`access="masked"`) через
   `protect_pii_response`; source-view отдаёт только display-поля (заголовок, текст,
   источник, confidence, дата), служебные поля provenance (actor_id/trace_id/metadata)
   наружу не идут; ошибки движка → обобщённое 503 без stack trace / DB URI; rate-limit
   по IP (`CB_DEMO_RATE_LIMIT`, скользящее окно, in-process).
5. **Бренд/домен — в конфиге** (`CB_DEMO_BRAND_*`, `CB_DEMO_CTA_URL`, `CB_DEMO_DOMAIN`),
   нейтрально по умолчанию (нейминг Taimen не развязан). В разметке бренд не зашит.
6. **Офлайн-фолбэк (canned):** тот же шаблон рендерится сборкой `demo/build_canned.py` в
   самодостаточный `dist/index.html` с вшитым синтетическим корпусом; поиск в браузере.
   Единый интерфейс `DataProvider` (`LiveProvider`/`CannedProvider`) в клиентском JS.

## Consequences

- Публичный контракт `/api/brain/*` и `/console` не меняются: демо аддитивно, выключено
  по умолчанию, не требует новых обязательных переменных. Регресс покрыт тестами.
- Витрина — **не часть контракта потребителя** (`docs/INTEGRATION.md`): это маркетинговая
  поверхность, документируется в `demo/README.md`. В INTEGRATION.md — только короткая
  ссылка, чтобы не расширять машинный контракт.
- Namespace-изоляция логическая (в одном инстансе). Для демо этого достаточно; демо-KB
  наполняется только синтетикой (`*.example`, без ПДн/реальных сущностей).
- Включение на контуре с реальными данными — риск: публичная read-only поверхность видит
  только `CB_DEMO_NAMESPACE`, но админ обязан не сажать демо на боевую KB. Явно помечено
  в конфиге, README и логе гейта.
- Вход с `taimen.ai` (поддомен vs `/demo`), реальный бренд, домен и публикация — решения
  владельца; вынесены в один конфиг/константу, кодовых блокеров нет.

## Alternatives

- **Отдельный тонкий backend в `teststand/memory-demo`** (свой рантайм, проксирует в
  memory-service с ключом) — отклонено: лишний рантайм и хранение ключа в ещё одном
  месте; in-process слой в существующем приложении проще и безопаснее (ключ не нужен).
- **Фронт напрямую в `/api/brain/*` с токеном из браузера** — отклонено: утечка ключа,
  прямое нарушение границы; несовместимо с публичной поверхностью.
- **Только статический canned на Pages** — оставлено как вторичный офлайн-фолбэк за
  флагом `mode`, но не основной: живое демо на реальном инференсе убедительнее.
- **Реюз маршрутов `/console`** — отклонено: Console админская (запись/удаление, allowlist
  KB, русский UI); публичной витрине нужна сознательно более узкая read-only поверхность.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- route: "GET /demo"
  repo: memory-service
- route: "POST /demo/api/search"
  repo: memory-service
- route: "GET /demo/api/source"
  repo: memory-service
- grep: {path: "src/platform_memory/core/config.py", pattern: 'demo_public_enabled: bool = Field\(default=False\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/server/demo.py", pattern: 'Principal\(access="masked", label="demo"\)'}
  repo: memory-service
- grep: {path: "tests/test_demo.py", pattern: 'def test_(search_ignores_client_supplied_namespace|demo_exposes_no_write_routes|api_key_never_in_demo_html)'}
  repo: memory-service
```
