"""Собрать статический офлайн-срез витрины (canned) для GitHub Pages.

Рендерит ТОТ ЖЕ шаблон ``src/platform_memory/server/templates/demo.html`` в режиме
``mode='canned'``, вшивая синтетический корпус (``demo/corpus/corpus.json``) прямо в
страницу. Результат — самодостаточный ``demo/dist/index.html``: работает офлайн, без
бэкенда и без секретов. Поиск в canned идёт в браузере (лексический скоринг по вшитому
корпусу); режим-бейдж показывает «Offline sample».

Запуск:  uv run python demo/build_canned.py
Проверка «без секретов»:  grep -RniE 'bearer|api[_-]?key|password|token' demo/dist/  → пусто
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from platform_memory.core.config import get_settings
from platform_memory.server.demo import demo_page_context

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
TEMPLATES = REPO / "src" / "platform_memory" / "server" / "templates"
CORPUS = ROOT / "corpus" / "corpus.json"
DIST = ROOT / "dist"


def load_corpus() -> list[dict]:
    """Прочитать синтетический корпус; поддерживает и голый список, и {'articles': [...]}."""
    if not CORPUS.exists():
        print(f"! Корпус не найден: {CORPUS} — соберётся пустой срез.", file=sys.stderr)
        return []
    data = json.loads(CORPUS.read_text("utf-8"))
    return data.get("articles", []) if isinstance(data, dict) else data


def main() -> int:
    corpus = load_corpus()
    # Бренд/домен берём из конфигурации (CB_DEMO_*), по умолчанию нейтрально.
    ctx = demo_page_context(get_settings(), mode="canned", base="", corpus=corpus)
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )
    html = env.get_template("demo.html").render(**ctx)
    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / "index.html"
    out.write_text(html, "utf-8")
    print(f"OK: {out}  ({len(html):,} bytes, {len(corpus)} articles, mode=canned)")
    print("Проверьте отсутствие секретов:  grep -RniE 'bearer|api[_-]?key|token' demo/dist/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
