"""Парсинг реестра открытых вопросов (entity_type: open_questions).

Q-NNN живут строками markdown-таблицы в теле файла (а не отдельными заметками).
Превращаем каждую строку в узел типа ``question`` с natural_key=Q-NNN, чтобы
многочисленные ссылки T-NNN/BIZ-NNN -> Q-NNN разрешались в рёбра.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from platform_memory.ingest.links import LinkRef, extract_wikilinks

_QID = re.compile(r"Q-\d{3,}")
_STATUS_WORDS = {"open", "blocked", "answered", "closed"}


@dataclass(slots=True)
class QuestionRow:
    """Распарсенная строка реестра: один открытый вопрос Q-NNN."""

    qid: str
    text: str
    status: str
    links: list[LinkRef] = field(default_factory=list)


def _strip_md(cell: str) -> str:
    return cell.replace("~~", "").strip()


def parse_questions(body: str) -> list[QuestionRow]:
    """Извлечь строки Q-NNN из markdown-таблиц тела файла в список QuestionRow."""
    out: list[QuestionRow] = []
    seen: set[str] = set()
    section_closed = False

    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            section_closed = "закрыт" in heading
            continue
        if not stripped.startswith("|"):
            continue

        # Делим по НЕэкранированному '|' (в wiki-ссылках Obsidian разделитель экранируется '\|').
        raw_cells = re.split(r"(?<!\\)\|", stripped.strip("|"))
        cells = [c.replace("\\|", "|").strip() for c in raw_cells]
        if not cells:
            continue
        first = _strip_md(cells[0])
        m = _QID.fullmatch(first)
        if not m:
            continue
        qid = m.group(0)
        if qid in seen:
            continue
        seen.add(qid)

        struck = "~~" in cells[0] or (len(cells) > 1 and "~~" in cells[1])
        text = _strip_md(cells[1]) if len(cells) > 1 else qid

        status = "closed" if (section_closed or struck) else "open"
        if status != "closed":
            for cell in cells[2:]:
                val = cell.strip().lower()
                if val in _STATUS_WORDS:
                    status = val
                    break

        links = extract_wikilinks(line)
        out.append(QuestionRow(qid=qid, text=text or qid, status=status, links=links))
    return out
