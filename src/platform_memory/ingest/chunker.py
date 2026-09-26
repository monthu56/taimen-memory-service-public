"""Чанкинг тела заметки: по заголовкам, затем по абзацам с ограничением размера."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Версия логики чанкинга. Кэш чанков версионируется по ней (аналог «AST-кэша» в graphify):
# при изменении правил разбиения версию надо поднять, чтобы старые чанки инвалидировались.
# В отличие от кэша эмбеддингов (он версии чанкера НЕ касается — иначе перебиллим Eden AI).
CHUNKER_VERSION = "1"

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(slots=True)
class TextChunk:
    """Один чанк текста с привязкой к заголовкам и порядковым номером."""

    text: str
    heading: str  # хлебные крошки заголовков (напр. "Decision > Status")
    order: int


def _split_sections(body: str) -> list[tuple[str, str]]:
    """Разбить тело на (хлебные_крошки_заголовка, текст_секции)."""
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []  # (уровень, заголовок)
    buf: list[str] = []

    def breadcrumb() -> str:
        """Собрать хлебные крошки из текущего стека заголовков."""
        return " > ".join(h for _, h in stack)

    def flush():
        """Сбросить накопленный буфер в секцию и очистить его."""
        text = "\n".join(buf).strip()
        if text:
            sections.append((breadcrumb(), text))
        buf.clear()

    for line in body.split("\n"):
        m = _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            heading = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
        else:
            buf.append(line)
    flush()
    return sections


def _split_text(text: str, max_chars: int, min_chars: int) -> list[str]:
    """Разбить длинный текст на окна по абзацам/строкам с ограничением max_chars."""
    if len(text) <= max_chars:
        return [text]
    paragraphs = re.split(r"\n\s*\n", text)
    out: list[str] = []
    cur = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            # Слишком длинный абзац (напр. строка таблицы) — режем по строкам.
            for line in para.split("\n"):
                if len(cur) + len(line) + 1 > max_chars and cur:
                    out.append(cur.strip())
                    cur = ""
                cur += line + "\n"
            continue
        if len(cur) + len(para) + 2 > max_chars and cur:
            out.append(cur.strip())
            cur = ""
        cur += para + "\n\n"
    if cur.strip():
        out.append(cur.strip())
    # Подклеить хвост, если он слишком мал.
    if len(out) >= 2 and len(out[-1]) < min_chars:
        tail = out.pop()
        out[-1] = (out[-1] + "\n\n" + tail).strip()
    return out


def chunk_markdown(body: str, max_chars: int = 1200, min_chars: int = 200) -> list[TextChunk]:
    """Разбить тело заметки на упорядоченные чанки с привязкой к заголовкам."""
    chunks: list[TextChunk] = []
    order = 0
    for heading, text in _split_sections(body):
        for piece in _split_text(text, max_chars, min_chars):
            piece = piece.strip()
            if not piece:
                continue
            chunks.append(TextChunk(text=piece, heading=heading, order=order))
            order += 1
    return chunks
