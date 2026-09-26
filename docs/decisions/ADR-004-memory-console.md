# ADR-004: Memory Console — server-rendered админ-интерфейс внутри сервиса

## Status

Accepted (2026-07-18; рамки заданы владельцем в постановке задачи «Memory
Console MVP», реализация зафиксирована этим ADR).

## Context

Первый B2B-прогон Operator Copilot требует интерфейса для инженера внедрения:
загрузить статьи, проверить поиск с показом источника, посмотреть аудит и
удалить запись — без cURL и, главное, без передачи кому-либо API-ключа
сервиса. Отдельный frontend (Node/Vite) — второй runtime в on-prem поставке,
что дорого для контура «один заказчик = один инстанс».

## Decision

1. **Console — часть FastAPI-сервиса**: `GET /console` и серверные
   form-эндпоинты (`server/console.py` + Jinja2-шаблоны). Никакого клиентского
   фреймворка; HTMX не вендорим (THIRD_PARTY.md фиксирует только вендоренный
   код и не расширяется ради UI) — обычные HTML-формы, весь CSS локальный.
2. **Движок вызывается in-process** теми же хендлерами, что публичный API
   (`brain_retain`, `brain_node_delete`, `brain_source`, `retrieve`):
   идемпотентность, аудит и PII-контур общие; `CB_SERVER_API_KEY` в браузер
   не попадает ни в каком виде (закреплено тестом).
3. **Безопасность MVP**: выключена по умолчанию (`CB_CONSOLE_ENABLED=false`
   → 404); allowlist баз знаний только из конфига `CB_CONSOLE_NAMESPACES`
   (CSV), произвольный namespace → 400; POST — same-origin проверка
   (Origin vs Host); собственной аутентификации нет — доступ ограничивает
   reverse proxy / network policy контура (deploy/RUNBOOK.md).
4. **Допуск к ПДн**: операции Console работают с full-допуском под меткой
   `console`; каждая выдача ПДн журналируется `pii_access` (ADR-002).

## Consequences

- Один runtime, поставка не усложняется; Console обновляется вместе с сервисом.
- Аутентификация администратора — ответственность контура (proxy). Встроенная
  авторизация/RBAC — только отдельным ADR, если появится требование.
- Таблицы ограничены 50 строками с серверным фильтром — корпус в браузер не
  грузится; для массового импорта Console не предназначена (только поштучный).

## Alternatives

- **SPA (React/Vite) поверх публичного API** — отклонено: второй runtime,
  и браузеру пришлось бы выдавать API-ключ (или строить сессии/BFF) — прямое
  нарушение требования «ключ остаётся server-to-server секретом».
- **Собственная аутентификация в Console (логин/пароль)** — отложено до ADR:
  в первом контуре доступ и так закрыт периметром клиента, а управление
  учётками — отдельная ответственность.
- **HTMX (вендоренный)** — отклонено: третьесторонний JS требует атрибуции в
  THIRD_PARTY.md, ценности для MVP с полностраничными формами не добавляет.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: "src/platform_memory/server/console.py", pattern: 'router = APIRouter\(prefix="/console"'}
  repo: memory-service
- grep: {path: "src/platform_memory/core/config.py", pattern: 'console_enabled: bool = Field\(default=False\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/core/config.py", pattern: 'console_namespaces: str'}
  repo: memory-service
- grep: {path: "src/platform_memory/server/console.py", pattern: '^def _check_origin\(request: Request\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/server/console.py", pattern: 'Principal\(access="full", label="console"\)'}
  repo: memory-service
- grep: {path: "tests/test_console.py", pattern: 'def test_(api_key_never_in_console_html|console_disabled_404_everywhere|console_rejects_namespace_outside_allowlist)'}
  repo: memory-service
```
