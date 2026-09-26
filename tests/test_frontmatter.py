import pytest

from platform_memory.ingest import frontmatter

pytestmark = pytest.mark.unit


def test_parse_with_frontmatter():
    text = '---\nentity_type: task\nid: "T-001"\n---\n\n# Заголовок\n\nтело'
    doc = frontmatter.parse(text)
    assert doc.frontmatter["entity_type"] == "task"
    assert doc.frontmatter["id"] == "T-001"
    assert doc.body.startswith("# Заголовок")


def test_parse_without_frontmatter():
    text = "# Просто заметка\n\nбез frontmatter"
    doc = frontmatter.parse(text)
    assert doc.frontmatter == {}
    assert "Просто заметка" in doc.body


def test_parse_malformed_yaml_returns_empty():
    text = "---\n: : : not valid : :\n  - broken\n---\nтело"
    doc = frontmatter.parse(text)
    assert isinstance(doc.frontmatter, dict)
    assert "тело" in doc.body


def test_parse_crlf_and_bom():
    text = "﻿---\r\nentity_type: product\r\n---\r\nтело"
    doc = frontmatter.parse(text)
    assert doc.frontmatter["entity_type"] == "product"


def test_parse_empty_frontmatter_block():
    # `---\n---\n` — валидный пустой frontmatter; маркеры НЕ должны утечь в тело.
    doc = frontmatter.parse("---\n---\nтело")
    assert doc.frontmatter == {}
    assert doc.body == "тело"


def test_closing_delimiter_is_line_anchored():
    # '---' в теле (после корректного закрытия) сохраняется; '----' не считается концом.
    doc = frontmatter.parse("---\nentity_type: task\n---\nтело\n---\nещё")
    assert doc.frontmatter["entity_type"] == "task"
    assert doc.body == "тело\n---\nещё"
