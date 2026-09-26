# Vendored from graphify (https://github.com/safishamsi/graphify), MIT License.
# Copyright (c) 2026 Safi Shamsi. See THIRD_PARTY.md for the full license text.
# Перенесён практически дословно — это инфраструктурный примитив (MinHash + band-LSH),
# не зависящий от модели данных graphify.
"""MinHash + band-LSH — datasketch-совместимый drop-in (без scipy).

datasketch.lsh держит ``from scipy.integrate import quad`` на уровне модуля, а
array_api_compat-слой scipy лениво подгружает numpy.testing, который на импорте
вызывает platform.machine() и под EDR в корпоративном Windows плодит cmd.exe —
импорт виснет на минуты. Этот модуль покрывает только тот срез API MinHash/MinHashLSH,
который нужен fuzzy_resolver.py; семейство хэшей (перестановки по простому Мерсенна)
и структура band-LSH эквивалентны datasketch, поэтому качество дедупа не меняется.
"""

from __future__ import annotations

import hashlib
import struct

import numpy as np

_MP = np.uint64((1 << 61) - 1)  # простое Мерсенна для семейства хэшей
_MH = np.uint64(0xFFFF_FFFF)  # маска до 32-битных значений

# По одному массиву коэффициентов (a, b) на num_perm, общий для всех инстансов.
_MH_COEFFS: dict[int, tuple[np.ndarray, np.ndarray]] = {}


def _mh_coeffs(num_perm: int) -> tuple[np.ndarray, np.ndarray]:
    if num_perm not in _MH_COEFFS:
        rng = np.random.RandomState(1)
        a = rng.randint(1, int(_MP), num_perm, dtype=np.uint64)
        b = rng.randint(0, int(_MP), num_perm, dtype=np.uint64)
        _MH_COEFFS[num_perm] = (a, b)
    return _MH_COEFFS[num_perm]


class MinHash:
    """MinHash-скетч — тот же API, что у datasketch.MinHash для используемого подмножества."""

    __slots__ = ("num_perm", "hashvalues", "_a", "_b")

    def __init__(self, num_perm: int = 128) -> None:
        self.num_perm = num_perm
        self.hashvalues = np.full(num_perm, int(_MH), dtype=np.uint64)
        self._a, self._b = _mh_coeffs(num_perm)

    def update(self, v: bytes) -> None:
        hv = np.uint64(struct.unpack("<I", hashlib.sha1(v).digest()[:4])[0])
        phv = np.bitwise_and((self._a * hv + self._b) % _MP, _MH)
        self.hashvalues = np.minimum(self.hashvalues, phv)


def _lsh_integrate(f, lo: float, hi: float, n: int = 128) -> float:
    """Численное интегрирование — замена scipy.integrate.quad для подбора параметров LSH."""
    h = (hi - lo) / n
    return h * sum(f(lo + i * h) for i in range(n))


_LSH_PARAMS_CACHE: dict[tuple[float, int], tuple[int, int]] = {}


def _optimal_lsh_params(threshold: float, num_perm: int) -> tuple[int, int]:
    """Найти (bands, rows), минимизирующие взвешенную ошибку FP+FN, без scipy."""
    key = (threshold, num_perm)
    if key in _LSH_PARAMS_CACHE:
        return _LSH_PARAMS_CACHE[key]
    best_err, best = float("inf"), (1, 1)
    for b in range(1, num_perm + 1):
        for r in range(1, num_perm // b + 1):
            fp = _lsh_integrate(
                lambda s, _b=float(b), _r=float(r): 1 - (1 - s**_r) ** _b,
                0.0,
                threshold,
            )
            fn = _lsh_integrate(
                lambda s, _b=float(b), _r=float(r): 1 - (1 - (1 - s**_r) ** _b),
                threshold,
                1.0,
            )
            err = 0.5 * fp + 0.5 * fn
            if err < best_err:
                best_err, best = err, (b, r)
    _LSH_PARAMS_CACHE[key] = best
    return best


class MinHashLSH:
    """Band-hashing LSH — тот же API, что у datasketch.MinHashLSH для используемого подмножества."""

    def __init__(self, threshold: float = 0.5, num_perm: int = 128) -> None:
        self.b, self.r = _optimal_lsh_params(threshold, num_perm)
        self._tables: list[dict[bytes, list[str]]] = [{} for _ in range(self.b)]
        self._keys: set[str] = set()

    def insert(self, key: str, minhash: MinHash) -> None:
        if key in self._keys:
            raise ValueError(f"Key {key!r} already exists in MinHashLSH")
        self._keys.add(key)
        hv = minhash.hashvalues
        for i, table in enumerate(self._tables):
            band = hv[i * self.r : (i + 1) * self.r].tobytes()
            table.setdefault(band, []).append(key)

    def query(self, minhash: MinHash) -> list[str]:
        hv = minhash.hashvalues
        candidates: set[str] = set()
        for i, table in enumerate(self._tables):
            band = hv[i * self.r : (i + 1) * self.r].tobytes()
            candidates.update(table.get(band, []))
        return list(candidates)
