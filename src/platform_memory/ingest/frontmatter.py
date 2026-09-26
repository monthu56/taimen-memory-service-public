"""Парсинг YAML-frontmatter Obsidian-заметок."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass(slots=True)
class ParsedDoc:
    """Результат разбора заметки: frontmatter (dict) и тело документа."""

    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""


def parse(text: str) -> ParsedDoc:
    """Разделить заметку на frontmatter (dict) и тело. Без frontmatter -> пустой dict."""
    # Убрать BOM, нормализовать перевод строк.
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = text.split("\n")
    # Frontmatter обязан открываться строкой ровно из '---' в самом начале файла.
    if not lines or lines[0].strip() != "---":
        return ParsedDoc(frontmatter={}, body=text)

    # Закрывающий разделитель — первая последующая строка, равная ровно '---'
    # (привязка к строке: '----' и '---' внутри значений не считаются концом).
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            raw_fm = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :]).lstrip("\n")
            try:
                data = yaml.safe_load(raw_fm)
            except yaml.YAMLError:
                data = None
            if not isinstance(data, dict):
                data = {}
            return ParsedDoc(frontmatter=data, body=body)

    # Открыли, но не закрыли — считаем, что frontmatter нет.
    return ParsedDoc(frontmatter={}, body=text)
