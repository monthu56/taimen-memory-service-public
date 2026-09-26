r"""Извлечение и нормализация связей: wiki-ссылки `[[...]]` и элементы frontmatter `links:`.

Поддерживаемые формы:
  [[имя]]                       — basename
  [[путь/файл]]                 — путь от корня vault (без .md)
  [[../отн/путь|алиас]]         — относительный путь + алиас
  [[открытые-вопросы#Q-024|Q-024]] — путь + якорь-заголовок + алиас (часто это id)
В таблицах Obsidian экранирует разделитель как `\|`.
В frontmatter `links:` элемент — либо голый id ("BIZ-004"), либо wiki-ссылка ("[[обзор]]").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from platform_memory.core.ontology import is_natural_id

_WIKILINK = re.compile(r"\[\[([^\[\]]+)\]\]")


@dataclass(slots=True)
class LinkRef:
    """Разобранная ссылка: путь, якорь, алиас и признак wiki-формы."""

    raw: str
    path_part: str  # часть до якоря/алиаса (путь или id)
    anchor: str | None = None  # якорь после '#'
    alias: str | None = None  # текст после '|'
    is_wikilink: bool = True

    def candidate_ids(self) -> list[str]:
        """Кандидаты, похожие на стабильный id (BIZ-/T-/Q-/M-NNN), по приоритету."""
        out: list[str] = []
        for token in (self.anchor, self.alias, self.path_part, self.raw):
            if token and is_natural_id(token) and token not in out:
                out.append(token.strip())
        return out


def _unescape(token: str) -> str:
    return token.replace("\\|", "|").replace("\\#", "#").strip()


def parse_target(inner: str, *, is_wikilink: bool = True) -> LinkRef:
    """Разобрать содержимое ссылки (без скобок) на путь/якорь/алиас."""
    raw = _unescape(inner)
    target, _, alias = raw.partition("|")
    target = target.strip()
    alias = alias.strip() or None
    path_part, hash_sep, anchor = target.partition("#")
    return LinkRef(
        raw=raw,
        path_part=path_part.strip(),
        anchor=anchor.strip() if hash_sep else None,
        alias=alias,
        is_wikilink=is_wikilink,
    )


def extract_wikilinks(text: str) -> list[LinkRef]:
    """Все `[[...]]` из текста (тело заметки или ячейка таблицы)."""
    return [parse_target(m.group(1)) for m in _WIKILINK.finditer(text)]


def parse_frontmatter_link(item: str) -> LinkRef | None:
    """Разобрать один элемент массива `links:` (голый id или wiki-ссылка)."""
    if not isinstance(item, str):
        return None
    item = item.strip()
    if not item:
        return None
    m = _WIKILINK.search(item)
    if m:
        return parse_target(m.group(1))
    # Голый токен: id или имя файла.
    return parse_target(item, is_wikilink=False)


def parse_frontmatter_links(items) -> list[LinkRef]:
    """Разобрать массив `links:` из frontmatter в список ссылок."""
    if not isinstance(items, list):
        return []
    out: list[LinkRef] = []
    for item in items:
        ref = parse_frontmatter_link(item)
        if ref is not None:
            out.append(ref)
    return out
