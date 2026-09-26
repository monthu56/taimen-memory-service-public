"""Загрузить синтетический демо-корпус в демо-KB стенда через POST /api/brain/retain.

Читает ``demo/corpus/corpus.json`` и по ОДНОЙ статье (последовательно, без параллельных
батчей — как просит MEMORY-API-ДОСТУП.md) отправляет ``retain`` с ``external_id =
source_url`` в namespace демо-KB. Идемпотентно: повторный запуск обновит те же записи,
дублей не будет.

Секреты — ТОЛЬКО из окружения, никогда в коде:
  DEMO_BASE_URL    базовый URL memory-service (напр. https://<host>)   [обязательно]
  DEMO_TOKEN       Bearer-токен сервиса                                [если включён auth]
  DEMO_NAMESPACE   namespace демо-KB (по умолчанию 'demo')

Запуск:
  DEMO_BASE_URL=https://<host> DEMO_TOKEN=<token> uv run python demo/seed_stand.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CORPUS = Path(__file__).resolve().parent / "corpus" / "corpus.json"


def load_corpus() -> list[dict]:
    data = json.loads(CORPUS.read_text("utf-8"))
    return data.get("articles", []) if isinstance(data, dict) else data


def retain(base_url: str, token: str, namespace: str, article: dict) -> tuple[bool, str]:
    """Отправить одну статью в retain демо-KB. Возвращает (успех, сообщение)."""
    body = {
        "content": article["body"],
        "type": "article",
        "title": article.get("title") or None,
        "external_id": article["source_url"],  # URL как ключ → идемпотентность
        "provenance": {
            "source": article.get("source_label") or "demo-kb",
            "actor": "demo-seed",
        },
        "scope": {"namespace": namespace},
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/brain/retain",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return True, payload.get("natural_key", article["source_url"])
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def main() -> int:
    base_url = os.environ.get("DEMO_BASE_URL", "").strip()
    token = os.environ.get("DEMO_TOKEN", "").strip()
    namespace = os.environ.get("DEMO_NAMESPACE", "demo").strip() or "demo"
    if not base_url:
        print("! Задайте DEMO_BASE_URL (и DEMO_TOKEN, если включён auth).", file=sys.stderr)
        return 2
    if not CORPUS.exists():
        print(f"! Корпус не найден: {CORPUS}", file=sys.stderr)
        return 2

    articles = load_corpus()
    print(f"Загрузка {len(articles)} статей в namespace '{namespace}' на {base_url} …")
    ok = 0
    for i, art in enumerate(articles, 1):
        success, msg = retain(base_url, token, namespace, art)
        status = "OK " if success else "ERR"
        print(
            f"[{i:>3}/{len(articles)}] {status} {art.get('title', '')[:60]}  {msg if not success else ''}".rstrip()
        )
        ok += int(success)
        time.sleep(0.15)  # мягкий темп: rate-limit не включён, не заваливаем сервис
    print(f"Готово: {ok}/{len(articles)} успешно.")
    return 0 if ok == len(articles) else 1


if __name__ == "__main__":
    raise SystemExit(main())
