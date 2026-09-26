"""Обход Obsidian-vault (строго read-only) и чтение заметок."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platform_memory.ingest import frontmatter

# Каталоги, которые не индексируем.
_SKIP_DIRS = {".obsidian", ".trash", ".git", ".obsidian.vimrc"}


@dataclass(slots=True)
class VaultDoc:
    """Распарсенная заметка vault: путь, frontmatter и тело."""

    relpath: str  # относительный путь от корня vault (posix, с расширением)
    frontmatter: dict[str, Any]
    body: str


def iter_markdown_files(vault_root: Path):
    """Все *.md файлы vault, кроме служебных каталогов. Только чтение."""
    for path in sorted(vault_root.rglob("*.md")):
        if any(part in _SKIP_DIRS for part in path.relative_to(vault_root).parts):
            continue
        if path.name.startswith("."):
            continue
        yield path


def read_docs(vault_root: str | Path) -> list[VaultDoc]:
    """Прочитать и распарсить все заметки vault."""
    root = Path(vault_root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Vault не найден: {root}")
    docs: list[VaultDoc] = []
    for path in iter_markdown_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        parsed = frontmatter.parse(text)
        relpath = path.relative_to(root).as_posix()
        docs.append(VaultDoc(relpath=relpath, frontmatter=parsed.frontmatter, body=parsed.body))
    return docs
