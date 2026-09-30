"""Границы ожидания аддитивной миграции наблюдений (MEM-ADR-019). БД не нужна."""

from __future__ import annotations

import pytest

from platform_memory.observations.store import ObservationStore

pytestmark = pytest.mark.unit


def test_migration_lock_wait_is_short():
    """Очередь запросов за ожидающим ALTER держится не дольше секунды на попытку."""
    assert ObservationStore.MIGRATION_LOCK_TIMEOUT_MS <= 1000
    # Короткое ожидание окупается числом попыток; пауза длиннее ожидания —
    # между попытками очередь за ALTER успевает рассосаться.
    assert ObservationStore.MIGRATION_ATTEMPTS >= 5
    assert (
        ObservationStore.MIGRATION_RETRY_PAUSE * 1000 >= ObservationStore.MIGRATION_LOCK_TIMEOUT_MS
    )
