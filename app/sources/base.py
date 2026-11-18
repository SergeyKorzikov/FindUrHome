"""Базовый класс адаптера. Чтобы добавить новый сайт — унаследуйтесь и зарегистрируйте в sources/__init__.py."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx

from ..models import GeoArea, Listing, Mode, SearchQuery, SourceInfo

USER_AGENT = "FindUrHome/0.1 (+https://github.com/SergeyKorzikov/FindUrHome)"


class Source(ABC):
    id: str = ""
    name: str = ""
    modes: tuple[Mode, ...] = (Mode.short,)
    env_keys: tuple[str, ...] = ()  # переменные окружения, без которых источник выключен
    description: str = ""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    # --- доступность ---------------------------------------------------
    def env(self, key: str) -> Optional[str]:
        return os.getenv(key) or None

    @property
    def enabled(self) -> bool:
        return all(self.env(k) for k in self.env_keys)

    def info(self) -> SourceInfo:
        missing = [k for k in self.env_keys if not self.env(k)]
        return SourceInfo(
            id=self.id,
            name=self.name,
            modes=list(self.modes),
            enabled=self.enabled,
            needs=", ".join(missing) or None,
            description=self.description,
        )

    def supports(self, mode: Mode) -> bool:
        return mode in self.modes

    # --- поиск ---------------------------------------------------------
    @abstractmethod
    async def search(self, q: SearchQuery, area: GeoArea) -> list[Listing]:
        ...


# --- утилиты для адаптеров --------------------------------------------

def dig(obj: Any, path: str, default: Any = None) -> Any:
    """Достать значение по пути 'a.b.0.c' из вложенных dict/list."""
    cur = obj
    if not path:
        return cur
    for part in path.split("."):
        if cur is None:
            return default
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return default
    return default if cur is None else cur


def to_float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = "".join(ch for ch in str(v) if ch.isdigit() or ch in ".,-")
    if not s:
        return None
    # "1.234,56" / "1,234.56" / "1 234"
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = s.replace(",", "") if len(tail) == 3 else head.replace(",", "") + "." + tail
    try:
        return float(s)
    except ValueError:
        return None


def to_int(v: Any) -> Optional[int]:
    f = to_float(v)
    return int(f) if f is not None else None
