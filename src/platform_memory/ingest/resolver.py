"""Резолвер целей ссылок: LinkRef -> natural_key существующего узла.

Каждый узел-файл индексируется по трём «ручкам»: стабильный id, относительный путь
(без .md) и basename. Q-узлы — только по id. Спорные basename логируются.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from dataclasses import dataclass, field

from platform_memory.core.ontology import is_natural_id
from platform_memory.ingest.links import LinkRef


def _norm_path(path: str) -> str:
    """Нормализовать путь-ссылку: posix, без .md, без ведущего ./ (normpath уже это делает)."""
    p = path.strip().replace("\\", "/")
    if p.endswith(".md"):
        p = p[:-3]
    if p.startswith("./"):
        p = p[2:]
    return posixpath.normpath(p)


@dataclass
class Resolver:
    """Индекс узлов по id, относительному пути и basename для разрешения ссылок."""

    by_id: dict[str, str] = field(default_factory=dict)
    by_relpath: dict[str, str] = field(default_factory=dict)
    by_basename: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    ambiguous: set[str] = field(default_factory=set)

    def add_node(
        self,
        natural_key: str,
        *,
        source_id: str | None = None,
        relpath: str | None = None,
    ) -> None:
        """Зарегистрировать узел во всех «ручках»: id, относительный путь и basename."""
        if source_id:
            self.by_id[source_id.strip()] = natural_key
        if is_natural_id(natural_key):
            self.by_id.setdefault(natural_key, natural_key)
        if relpath:
            norm = _norm_path(relpath)
            self.by_relpath[norm] = natural_key
            base = posixpath.basename(norm)
            if base and natural_key not in self.by_basename[base]:
                self.by_basename[base].append(natural_key)

    def resolve(self, ref: LinkRef, source_relpath: str | None = None) -> str | None:
        """Разрешить ссылку в natural_key по id, пути, относительному пути или basename."""
        # 1) стабильный id (включая якорь #Q-024 и алиас).
        for cid in ref.candidate_ids():
            if cid in self.by_id:
                return self.by_id[cid]

        target = ref.path_part
        if not target:
            # Ссылка только на якорь (#heading) того же файла — целью считаем сам id-якорь.
            if ref.anchor and ref.anchor in self.by_id:
                return self.by_id[ref.anchor]
            return None

        # 2) путь от корня vault.
        norm = _norm_path(target)
        if norm in self.by_relpath:
            return self.by_relpath[norm]

        # 3) относительный путь от каталога источника.
        if source_relpath and (target.startswith("../") or target.startswith("./")):
            base_dir = posixpath.dirname(source_relpath.replace("\\", "/"))
            joined = _norm_path(posixpath.join(base_dir, target))
            if joined in self.by_relpath:
                return self.by_relpath[joined]

        # 4) basename (если однозначно).
        base = posixpath.basename(norm)
        candidates = self.by_basename.get(base, [])
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            self.ambiguous.add(base)
        return None
