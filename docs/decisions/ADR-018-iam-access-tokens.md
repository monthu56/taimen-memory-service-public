# ADR-018: Access token IAM на HTTP-границе рядом со статическими ключами

## Status

Accepted (2026-09-12; реализовано в этом же коммите). Исполняет решения суперпроекта
ADR-0013 (шаг миграции «Memory Service adapter с отдельным audience») и ADR-0030
(раздел 1, пункт 6): каждый resource service платформы проверяет access token IAM
общим `platform-auth-sdk`.

## Context

До этого решения движок памяти знал только **статические** credentials:
`CB_SERVER_API_KEY`, `CB_SERVER_API_KEYS_PII` (ADR-002) и реестр `CB_API_KEYS` с
префиксными грантами по namespace (ADR-017). Для одиночного заказчика этого
достаточно, но в составе платформы у сервиса появляется второй класс вызывающих —
principals IAM (люди, агенты, сервисы платформы), чьи полномочия выпускает IAM
короткоживущим audience-bound токеном, а не общий бессрочный ключ. Control Plane уже
именует память tenant'а namespace'ами `tenant:<tenant_id>`
(`CP_CONTEXT_NAMESPACE_PREFIX=tenant:`), а IAM держит реестр audiences и scope ceiling.

Инвариант изоляции пакета (BIZ-023, CLAUDE.md, инвариант 1) запрещает импорты
`platform_*`. Писать собственную проверку JWT (подпись по JWKS с ротацией и bounded
stale window, точный audience, запрет `none`/HS*, единый deny-контракт) — значит
разойтись с остальными сервисами ровно в том месте, где расхождение опаснее всего.

## Decision

1. **`platform-auth-sdk` — единственное осознанное исключение из инварианта
   изоляции.** Path-зависимость `../platform-auth-sdk` (как у entitlement-service и
   control-plane); импорт `platform_auth` допустим **только** в
   `server/iam.py`. Движок (`core/`, `ingest/`, `graph/`, `index/`, `retrieval/`,
   `context/`, `observations/`) других сервисов не видит. Carve-out в open-source —
   удалить модуль и зависимость; статические ключи остаются.
2. **Второй способ, не замена.** При `CB_IAM_ENABLED=true` Bearer, не совпавший ни с
   одним статическим ключом и похожий на JWT (три части через точку — решение по
   форме, не перебором способов), проверяется `TokenVerifier` SDK: подпись по JWKS
   (`CB_IAM_JWKS_URL`, `JwksCache`), точные `iss` (`CB_IAM_ISSUER`) и `aud`
   (`CB_IAM_AUDIENCE`, по умолчанию `memory-service`), временные claims с допуском
   `CB_IAM_LEEWAY_SECONDS`. Статические ключи работают как прежде; IAM выключен по
   умолчанию. Локальный режим без авторизации при включённом IAM **не** включается.
3. **Проекция в существующие `CallerGrants`** — `_authorize` и PII-барьер не меняются:
   - scopes `memory:read` / `memory:write` → флаги read/write всех грантов токена;
     `memory:pii` → полный допуск к ПДн (`CallerGrants.pii`). Scope действует, только
     если он есть и в токене, и в `scope_ceiling` (правило SDK);
   - namespaces: `tenant:<tenant_id>` **ровно** и поддерево `tenant:<tenant_id>:*`;
     плюс каждый namespace из необязательного claim `memory_namespaces` (список) —
     тоже ровно и его поддерево. Для «ровно» в `NamespaceGrant` добавлен флаг `exact`:
     префикс `tenant:<uuid>` не должен покрывать `tenant:<uuid>0`;
   - scope `memory:tenants` (амендмент 2026-09-12) → поддерево `tenant:*` целиком,
     независимо от `tenant_id` токена. Это полномочие платформенного сервиса: service
     account Control Plane один на инсталляцию, а память он ведёт для всех своих
     tenant'ов (`context-adapter`). Людям и агентам scope не выдаётся — это решает
     потолок service account в IAM, движок лишь проецирует scope в грант;
   - `GET /api/brain/stats` (глобальная выдача) IAM-токену недоступна — как любому
     гранту без пустого префикса;
   - валидный токен без scope памяти не покрывает ни одного namespace → 403 от
     `_authorize`, а не отдельный код на этапе аутентификации.
4. **Deny-контракт.** Любой дефект токена → `401` с тем же телом, что у неверного
   ключа (endpoint не оракул). Проверить нечем (JWKS недоступен дольше stale window,
   IAM не сконфигурирован) → `503`, не allow — fail closed; статические ключи при этом
   продолжают работать.
5. **Сборка образа — из корня суперпроекта** (`-f memory-service/Dockerfile`), как у
   других resource services: SDK лежит соседним каталогом. `Dockerfile.dockerignore`
   рядом с Dockerfile ограничивает контекст `memory-service/` и `platform-auth-sdk/`
   (корневой `.dockerignore` исключает `memory-service/` целиком и не трогается).
   Healthcheck — `127.0.0.1`, а не `localhost` (IPv6-гоча slim-образов).

## Consequences

- `authenticate` стала `async` (verify SDK асинхронный); `memory_api._authenticate`
  — тоже. Прямые вызовы хендлеров в тестах не затронуты.
- Новые переменные: `CB_IAM_ENABLED`, `CB_IAM_ISSUER`, `CB_IAM_JWKS_URL`,
  `CB_IAM_AUDIENCE`, `CB_IAM_LEEWAY_SECONDS`. На стороне IAM нужен audience
  `memory-service` с `allowedScopes` `memory:read`, `memory:write`, `memory:pii`,
  `memory:tenants` (последний — только в потолке service account Control Plane;
  `deploy/bootstrap.py` суперпроекта заводит и audience, и service account).
- `uv.lock` получил `platform-auth-sdk` (editable path) и его транзитивные
  `pyjwt[crypto]`; `THIRD_PARTY.md` фиксирует зависимость и условие carve-out.
- Не сделано сознательно: статический публичный ключ вместо JWKS
  (`StaticKeySet`), binding IAM-principal → локальные роли (у движка нет своих ролей —
  полномочия целиком выводятся из scope и tenant), revocation-каталог (токены
  короткоживущие; при необходимости — `CachingRevocationDirectory` SDK).

## Alternatives

- **Своя проверка JWT на `pyjwt`** без SDK — сохраняет букву инварианта, но плодит
  второй deny-контракт и вторую политику JWKS; отвергнуто.
- **Шлюз обменивает IAM-токен на статический ключ** перед вызовом памяти — токен
  теряет tenant и scope, audit неразличим; отвергнуто (ADR-0013 требует отдельный
  audience).
- **Гранты из binding-таблицы**, как в control-plane — у памяти нет доменных
  permissions, tenant и scope уже полностью определяют доступ; таблица была бы пустой
  надстройкой.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: "src/platform_memory/server/iam.py", pattern: '^from platform_auth(\.verify)? import'}
  repo: memory-service
- absent: {path: "src/platform_memory/[!s]*/**/*.py", pattern: '^\s*(from|import)\s+platform_auth'}
  repo: memory-service
- absent: {path: "src/platform_memory/server/[!i]*.py", pattern: '^\s*(from|import)\s+platform_auth'}
  repo: memory-service
- grep: {path: "src/platform_memory/core/config.py", pattern: 'iam_audience: str = Field\(default="memory-service"\)'}
  repo: memory-service
- grep: {path: "src/platform_memory/server/auth.py", pattern: '^\s+exact: bool = False'}
  repo: memory-service
- grep: {path: "tests/test_server_iam_auth.py", pattern: 'def test_(jwks_unreachable_is_503|foreign_namespace_is_403|static_keys_still_work_with_iam_enabled)'}
  repo: memory-service
```
