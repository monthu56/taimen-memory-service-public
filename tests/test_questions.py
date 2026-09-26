import pytest

from platform_memory.ingest.questions import parse_questions

pytestmark = pytest.mark.unit

REGISTRY = """
# Открытые вопросы

## Открытые

| ID | Вопрос | Источник | Владелец | Открыт | Статус | Шаг |
|---|---|---|---|---|---|---|
| Q-001 | Какое имя? | [[продукты/альфа]] | Тест | 2026-01-10 | open | дождаться [[задачи/T-002\\|T-002]] |
| Q-002 | Нужен ли домен? | [[решения/decision-001\\|BIZ-001]] | Тест | 2026-01-11 | blocked | позже |

## Закрытые

| ID | Вопрос | Ответ | Источник | Закрыт |
|---|---|---|---|---|
| Q-003 | ~~Старый?~~ | Да | [[решения/decision-001\\|BIZ-001]] | 2026-01-12 |
"""


def test_parse_open_blocked_closed():
    rows = parse_questions(REGISTRY)
    by_id = {r.qid: r for r in rows}
    assert set(by_id) == {"Q-001", "Q-002", "Q-003"}
    assert by_id["Q-001"].status == "open"
    assert by_id["Q-002"].status == "blocked"
    assert by_id["Q-003"].status == "closed"


def test_question_text_and_links():
    rows = parse_questions(REGISTRY)
    q1 = next(r for r in rows if r.qid == "Q-001")
    assert "имя" in q1.text.lower()
    targets = {ref.candidate_ids()[0] for ref in q1.links if ref.candidate_ids()}
    assert "T-002" in targets


def test_escaped_pipe_in_question_column_keeps_clean_title():
    # Экранированный '\|' в колонке «Вопрос» не должен резать ячейку и портить title.
    registry = (
        "## Открытые\n"
        "| ID | Вопрос | Источник | Статус |\n"
        "|---|---|---|---|\n"
        "| Q-009 | связать с [[задачи/T-009\\|T-009]] срочно | x | blocked |\n"
    )
    rows = parse_questions(registry)
    q9 = next(r for r in rows if r.qid == "Q-009")
    assert q9.status == "blocked"
    assert q9.text == "связать с [[задачи/T-009|T-009]] срочно"
    assert "T-009" in {ref.candidate_ids()[0] for ref in q9.links if ref.candidate_ids()}
