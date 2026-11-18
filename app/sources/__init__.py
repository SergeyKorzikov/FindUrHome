"""Реестр источников: встроенные адаптеры + источники из config/sources.yaml."""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import yaml

from .airbnb import AirbnbSource
from .apify import ApifyConfigSource
from .base import Source
from .booking_rapidapi import BookingRapidApiSource
from .craigslist import CraigslistSource
from .demo import DemoSource
from .jsonld import JsonLdConfigSource
from .liteapi import LiteApiSource
from .osm import OSMSource
from .serpapi_hotels import SerpApiHotelsSource

BUILTIN: list[type[Source]] = [
    AirbnbSource,
    SerpApiHotelsSource,
    BookingRapidApiSource,
    LiteApiSource,
    OSMSource,
    CraigslistSource,
    DemoSource,
]

CONFIG_PATH = Path(os.getenv("SOURCES_CONFIG", Path(__file__).resolve().parents[2] / "config" / "sources.yaml"))


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def build_sources(client: httpx.AsyncClient, config: dict | None = None) -> dict[str, Source]:
    cfg = load_config() if config is None else config
    sources: list[Source] = [cls(client) for cls in BUILTIN]
    sources += [JsonLdConfigSource(client, c) for c in cfg.get("jsonld", []) or []]
    sources += [ApifyConfigSource(client, c) for c in cfg.get("apify", []) or []]
    return {s.id: s for s in sources}
