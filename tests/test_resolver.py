import pytest

from platform_memory.ingest.links import parse_frontmatter_link, parse_target
from platform_memory.ingest.resolver import Resolver

pytestmark = pytest.mark.unit


def _resolver() -> Resolver:
    r = Resolver()
    r.add_node("BIZ-001", source_id="BIZ-001", relpath="решения/decision-001.md")
    r.add_node("T-001", source_id="T-001", relpath="задачи/T-001-выбор.md")
    r.add_node("продукты/альфа", relpath="продукты/альфа.md")
    r.add_node("Q-001", source_id="Q-001")
    return r


def test_resolve_by_id():
    r = _resolver()
    assert r.resolve(parse_frontmatter_link("BIZ-001")) == "BIZ-001"


def test_resolve_by_anchor_id():
    r = _resolver()
    assert r.resolve(parse_target("открытые-вопросы#Q-001|Q-001")) == "Q-001"


def test_resolve_by_path():
    r = _resolver()
    assert r.resolve(parse_target("продукты/альфа")) == "продукты/альфа"


def test_resolve_by_path_with_alias_id():
    r = _resolver()
    # путь ведёт на файл решения, алиас = id -> разрешаем по id
    assert r.resolve(parse_target("решения/decision-001|BIZ-001")) == "BIZ-001"


def test_resolve_relative_path():
    r = _resolver()
    ref = parse_target("../продукты/альфа")
    assert r.resolve(ref, source_relpath="задачи/T-001-выбор.md") == "продукты/альфа"


def test_resolve_by_basename():
    r = _resolver()
    assert r.resolve(parse_target("decision-001")) == "BIZ-001"


def test_unresolved_returns_none():
    r = _resolver()
    assert r.resolve(parse_target("несуществующий-файл")) is None


def test_dotfile_name_is_not_corrupted():
    # Имя, начинающееся с точки, не должно «терять» точку (баг lstrip('./')).
    r = Resolver()
    r.add_node("заметки/.скрытая", relpath="заметки/.скрытая.md")
    assert r.resolve(parse_target("заметки/.скрытая")) == "заметки/.скрытая"
