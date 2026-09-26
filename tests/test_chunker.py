import pytest

from platform_memory.ingest.chunker import chunk_markdown

pytestmark = pytest.mark.unit


def test_headings_breadcrumbs_and_order():
    body = "# Решение\n\nвступление\n\n## Status\n\nProposed\n\n## Context\n\nдетали"
    chunks = chunk_markdown(body)
    assert len(chunks) >= 3
    assert chunks[0].order == 0
    headings = {c.heading for c in chunks}
    assert "Решение > Status" in headings
    assert "Решение > Context" in headings
    assert [c.order for c in chunks] == list(range(len(chunks)))


def test_long_section_splits():
    long = "\n\n".join(f"Абзац номер {i} с некоторым текстом." for i in range(80))
    body = f"# Большой раздел\n\n{long}"
    chunks = chunk_markdown(body, max_chars=300)
    assert len(chunks) > 1
    assert all(len(c.text) <= 600 for c in chunks)


def test_empty_body():
    assert chunk_markdown("") == []
