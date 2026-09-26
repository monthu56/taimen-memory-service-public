import pytest

from platform_memory.ingest.links import (
    extract_wikilinks,
    parse_frontmatter_links,
    parse_target,
)

pytestmark = pytest.mark.unit


def test_simple_name():
    ref = parse_target("обзор")
    assert ref.path_part == "обзор"
    assert ref.anchor is None
    assert ref.alias is None


def test_path_with_alias():
    ref = parse_target("задачи/T-055-аналитика|T-055")
    assert ref.path_part == "задачи/T-055-аналитика"
    assert ref.alias == "T-055"
    assert "T-055" in ref.candidate_ids()


def test_anchor_id():
    ref = parse_target("открытые-вопросы#Q-024|Q-024")
    assert ref.path_part == "открытые-вопросы"
    assert ref.anchor == "Q-024"
    assert ref.candidate_ids()[0] == "Q-024"


def test_escaped_pipe_in_table():
    refs = extract_wikilinks(r"строка [[открытые-вопросы#Q-024\|Q-024]] таблицы")
    assert len(refs) == 1
    assert refs[0].anchor == "Q-024"
    assert "Q-024" in refs[0].candidate_ids()


def test_frontmatter_links_mixed():
    refs = parse_frontmatter_links(["BIZ-004", "[[обзор]]", "T-002"])
    assert len(refs) == 3
    assert refs[0].candidate_ids() == ["BIZ-004"]
    assert refs[1].path_part == "обзор"
    assert refs[2].candidate_ids() == ["T-002"]


def test_extract_multiple_body_links():
    body = "см. [[a]] и [[b/c|алиас]] плюс [[d#e|BIZ-009]]"
    refs = extract_wikilinks(body)
    assert [r.path_part for r in refs] == ["a", "b/c", "d"]
