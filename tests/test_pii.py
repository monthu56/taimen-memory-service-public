"""Тесты детектора/маскера ПДн (152-ФЗ): РФ-паттерны, негативы, идемпотентность."""

from __future__ import annotations

import pytest

from platform_memory.core.pii import detect_pii, mask_pii

pytestmark = pytest.mark.unit


# ---------------- позитивы по категориям ----------------


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("звоните +7 999 123-45-67 после обеда", "phone"),
        ("тел. 8 (495) 123 45 67", "phone"),
        ("+79991234567 — мобильный", "phone"),
        ("почта ivanov@example.ru рабочая", "email"),
        ("паспорт 45 06 123456 выдан ОВД", "passport_rf"),
        ("СНИЛС 123-456-789 00 подтверждён", "snils"),
        ("СНИЛС 123-456-789-00 через дефис", "snils"),
        ("ИНН 7707083893 организации", "inn"),
        ("ИНН: 500100732259 физлица", "inn"),
        ("карта 4111 1111 1111 1111 привязана", "card"),  # валидная по Луну
        ("карта 4111-1111-1111-1111", "card"),
    ],
)
def test_detect_positive(text, category):
    found = detect_pii(text)
    assert found.get(category, 0) >= 1, f"{category} не найдена в: {text!r} → {found}"


# ---------------- негативы: обычный контент не должен срабатывать ----------------


@pytest.mark.parametrize(
    "text",
    [
        "Карта москвича действует 5 лет с даты выпуска.",
        "Регламент KB-001, категория internet, срок 30 дней.",
        "Заявление подано 12.05.2026, номер обращения 12345.",
        "ИНН указывается в личном кабинете",  # маркер без числа
        "просто число 9991234567 без префикса телефона",  # 10 цифр ≠ ИНН без маркера
        "код заказа 1234 5678 9012 3456",  # 16 цифр, но невалидна по Луну
        "версия 45 06 продукта",  # похоже на серию паспорта, но без номера
    ],
)
def test_detect_negative(text):
    assert detect_pii(text) == {}, f"ложное срабатывание на: {text!r}"


# ---------------- маскирование ----------------


def test_mask_replaces_and_counts():
    text = "Иванов, тел +7 999 123-45-67, почта ivanov@example.ru, СНИЛС 123-456-789 00"
    masked, found = mask_pii(text)
    assert "[ПДн:phone]" in masked and "[ПДн:email]" in masked and "[ПДн:snils]" in masked
    assert "+7 999" not in masked and "ivanov@" not in masked and "123-456-789" not in masked
    assert found == {"phone": 1, "email": 1, "snils": 1}
    assert "Иванов" in masked  # ФИО регексами не детектим (покрывается явной меткой)


def test_mask_idempotent():
    masked1, _ = mask_pii("тел +7 999 123-45-67")
    masked2, found2 = mask_pii(masked1)
    assert masked2 == masked1
    assert found2 == {}


def test_mask_clean_text_untouched():
    text = "Карта москвича действует 5 лет."
    masked, found = mask_pii(text)
    assert masked == text and found == {}


def test_card_luhn_filter():
    valid = "4111 1111 1111 1111"  # проходит Луна
    invalid = "1234 5678 9012 3456"  # не проходит
    assert detect_pii(valid) == {"card": 1}
    assert detect_pii(invalid) == {}


def test_multiple_occurrences_counted():
    text = "тел +7 999 123-45-67 и запасной 8 916 000 11 22"
    _, found = mask_pii(text)
    assert found == {"phone": 2}
