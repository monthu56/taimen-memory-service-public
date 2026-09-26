"""Фазовый тайминг запроса: ``Server-Timing`` и лог по фазам (амендмент MEM-ADR-020).

Движок и HTTP-слой отмечают фазы (``with phases("connect"): …``) в своём наборе
``Phases``; набор запроса (``request_phases()``) заводит middleware сервиса при
``CB_SERVER_TIMING=true`` — фазы движка сливаются в него (``merge``), и ответ получает
заголовок ``Server-Timing``. Без набора запроса фазы остаются только локальными
(например, ``stats.phases_ms`` пакета typed-контекста). Набор — в ``ContextVar``:
синхронный хендлер FastAPI выполняется в threadpool с копией контекста, изменяемый
объект набора общий.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_current: ContextVar[Phases | None] = ContextVar("platform_memory_phases", default=None)


class Phases:
    """Длительности фаз в миллисекундах; повтор фазы суммируется."""

    def __init__(self) -> None:
        self.ms: dict[str, float] = {}

    def add(self, name: str, seconds: float) -> None:
        self.ms[name] = self.ms.get(name, 0.0) + seconds * 1000

    @contextmanager
    def __call__(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - t0)

    def rounded(self) -> dict[str, float]:
        return {k: round(v, 1) for k, v in self.ms.items()}

    def header(self) -> str:
        """Значение ``Server-Timing``: ``имя;dur=мс`` через запятую."""
        return ", ".join(f"{k};dur={v:.1f}" for k, v in self.ms.items())


def request_phases() -> Phases | None:
    """Набор фаз текущего запроса (None — тайминг выключен)."""
    return _current.get()


def start_request() -> tuple[Phases, Token]:
    phases = Phases()
    return phases, _current.set(phases)


def end_request(token: Token) -> None:
    _current.reset(token)


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Фаза набора текущего запроса; без набора — ничего не меряет."""
    phases = _current.get()
    if phases is None:
        yield
        return
    with phases(name):
        yield


def merge(local: Phases, prefix: str = "") -> None:
    """Добавить фазы ``local`` в набор текущего запроса (если он есть)."""
    phases = _current.get()
    if phases is None or phases is local:
        return
    for name, ms in local.ms.items():
        phases.add(prefix + name, ms / 1000)
