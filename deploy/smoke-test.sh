#!/usr/bin/env bash
# Сквозной smoke-test изолированного инстанса memory-service.
#
# Работает на fake/echo провайдерах (внешние ключи не нужны). Проверяет:
# живость, импорт в две KB, изоляцию поиска и source-view между KB,
# удаление с аудит-следом. После себя подчищает тестовые данные.
#
#   BASE=http://127.0.0.1:8077 TOKEN=<CB_SERVER_API_KEY> ./smoke-test.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8077}"
TOKEN="${TOKEN:?Задайте TOKEN=<CB_SERVER_API_KEY>}"
AUTH=(-H "Authorization: Bearer ${TOKEN}")
JSON=(-H "Content-Type: application/json")
NS_A="smoke-a"
NS_B="smoke-b"
KEY_A="https://smoke.test/article-a"
KEY_B="https://smoke.test/article-b"

step()  { printf '\n== %s\n' "$*"; }
fail()  { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
# has <needle> — проверить, что stdin содержит подстроку
has()   { grep -qF -- "$1" || fail "в ответе нет «$1»"; }

cleanup() {
  curl -fsS -X DELETE "${AUTH[@]}" "$BASE/api/brain/nodes/$KEY_A?namespace=$NS_A" >/dev/null 2>&1 || true
  curl -fsS -X DELETE "${AUTH[@]}" "$BASE/api/brain/nodes/$KEY_B?namespace=$NS_B" >/dev/null 2>&1 || true
}
trap cleanup EXIT

step "healthz"
curl -fsS "$BASE/healthz" | has '"ok":true'

step "авторизация: без токена — 401"
code=$(curl -s -o /dev/null -w '%{http_code}' "${JSON[@]}" -d '{"query":"x"}' "$BASE/api/brain/recall")
[ "$code" = "401" ] || fail "recall без токена вернул $code, ожидался 401"

step "импорт статьи A в KB «${NS_A}»"
curl -fsS "${AUTH[@]}" "${JSON[@]}" -d @- "$BASE/api/brain/retain" <<EOF | has '"retained":true'
{"content": "Смоук-статья А. Карта действует пять лет.", "type": "article",
 "title": "Статья А", "external_id": "$KEY_A",
 "provenance": {"source": "smoke.test", "actor": "smoke-test"},
 "scope": {"namespace": "$NS_A"}}
EOF

step "импорт статьи B в KB «${NS_B}»"
curl -fsS "${AUTH[@]}" "${JSON[@]}" -d @- "$BASE/api/brain/retain" <<EOF | has '"retained":true'
{"content": "Смоук-статья Б. Уведомление приходит по СМС.", "type": "article",
 "title": "Статья Б", "external_id": "$KEY_B",
 "provenance": {"source": "smoke.test", "actor": "smoke-test"},
 "scope": {"namespace": "$NS_B"}}
EOF

step "recall в «${NS_A}» видит A и не видит B"
resp=$(curl -fsS "${AUTH[@]}" "${JSON[@]}" \
  -d "{\"query\": \"статья\", \"budget\": \"high\", \"scope\": {\"namespace\": \"$NS_A\"}}" \
  "$BASE/api/brain/recall")
printf '%s' "$resp" | has "$KEY_A"
printf '%s' "$resp" | grep -qF -- "$KEY_B" && fail "изоляция KB нарушена: recall в $NS_A вернул $KEY_B"

step "recall в «${NS_B}» видит B и не видит A"
resp=$(curl -fsS "${AUTH[@]}" "${JSON[@]}" \
  -d "{\"query\": \"статья\", \"budget\": \"high\", \"scope\": {\"namespace\": \"$NS_B\"}}" \
  "$BASE/api/brain/recall")
printf '%s' "$resp" | has "$KEY_B"
printf '%s' "$resp" | grep -qF -- "$KEY_A" && fail "изоляция KB нарушена: recall в $NS_B вернул $KEY_A"

step "source-view: оригинал A целиком в «${NS_A}»"
resp=$(curl -fsS "${AUTH[@]}" "$BASE/api/brain/sources/$KEY_A?namespace=$NS_A")
printf '%s' "$resp" | has "Карта действует пять лет"
printf '%s' "$resp" | has '"source":"smoke.test"'

step "source-view: A не видна из «${NS_B}» (404)"
code=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$BASE/api/brain/sources/$KEY_A?namespace=$NS_B")
[ "$code" = "404" ] || fail "источник чужой KB отдался с кодом $code, ожидался 404"

step "удаление A с аудит-следом"
resp=$(curl -fsS -X DELETE "${AUTH[@]}" "$BASE/api/brain/nodes/$KEY_A?namespace=$NS_A&actor=smoke-test")
printf '%s' "$resp" | has '"deleted":true'
trace=$(printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["trace_id"])')
curl -fsS "${AUTH[@]}" "$BASE/api/brain/trace/$trace?namespace=$NS_A" | has '"delete"'

step "повторное удаление A — 404 (узла больше нет)"
code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "${AUTH[@]}" "$BASE/api/brain/nodes/$KEY_A?namespace=$NS_A")
[ "$code" = "404" ] || fail "повторный DELETE вернул $code, ожидался 404"

printf '\nSMOKE OK: инстанс жив, KB изолированы, источник и аудит работают.\n'
