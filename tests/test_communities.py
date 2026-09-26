"""Юнит-тесты разметки сообществ (части без БД)."""

from __future__ import annotations

import networkx as nx
import pytest

from platform_memory.graph.communities import _label_community

pytestmark = pytest.mark.unit


def _graph(labels: dict[str, str]) -> nx.Graph:
    G = nx.Graph()
    for nk, label in labels.items():
        G.add_node(nk, label=label, type="task")
    keys = list(labels)
    for a, b in zip(keys, keys[1:], strict=False):
        G.add_edge(a, b)
    return G


class _StubLLM:
    def __init__(self, text: str) -> None:
        self._text = text

    def answer(self, system: str, user: str) -> str:
        self._last_user = user
        return self._text


def test_label_community_returns_clean_label():
    G = _graph({"n1": "Договор аренды", "n2": "Платёж", "n3": "Контрагент"})
    label = _label_community(G, ["n1", "n2", "n3"], llm=_StubLLM('"Финансы и право"'))
    assert label == "Финансы и право"  # кавычки сняты


def test_label_community_takes_first_line_and_truncates():
    G = _graph({"n1": "A", "n2": "B"})
    llm = _StubLLM("Тема\nлишняя болтовня модели")
    assert _label_community(G, ["n1", "n2"], llm=llm) == "Тема"


def test_label_community_passes_titles_to_llm():
    G = _graph({"n1": "Альфа", "n2": "Бета"})
    llm = _StubLLM("X")
    _label_community(G, ["n1", "n2"], llm=llm)
    assert "Альфа" in llm._last_user and "Бета" in llm._last_user


def test_label_community_survives_llm_failure():
    class _Boom:
        def answer(self, system: str, user: str) -> str:
            raise RuntimeError("LLM down")

    G = _graph({"n1": "A", "n2": "B"})
    assert _label_community(G, ["n1", "n2"], llm=_Boom()) == ""
