"""Параллельный опрос источников, нормализация цен, дедупликация, фильтры, сортировка."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from difflib import SequenceMatcher

import httpx

from .geo import convert, fx_rates
from .models import GeoArea, Listing, SearchQuery
from .sources.base import Source
from .sources.craigslist import haversine_km

SOURCE_TIMEOUT = float(os.getenv("SOURCE_TIMEOUT", "45"))
CACHE_TTL = int(os.getenv("CACHE_TTL", "1800"))
MAX_DISTANCE_KM = float(os.getenv("MAX_DISTANCE_KM", "60"))

_cache: dict[str, tuple[float, list[Listing]]] = {}


def _cache_key(source_id: str, q: SearchQuery) -> str:
    payload = q.model_dump(mode="json", include={"mode", "location", "checkin", "checkout", "guests",
                                                  "rooms_min", "price_min", "price_max", "currency", "sort"})
    return source_id + ":" + hashlib.md5(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def run_source(src: Source, q: SearchQuery, area: GeoArea) -> tuple[str, list[Listing], dict]:
    key = _cache_key(src.id, q)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return src.id, hit[1], {"ok": True, "count": len(hit[1]), "cached": True, "ms": 0}
    t0 = time.perf_counter()
    try:
        res = await asyncio.wait_for(src.search(q, area), timeout=SOURCE_TIMEOUT)
        _cache[key] = (time.time(), res)
        return src.id, res, {"ok": True, "count": len(res), "ms": int((time.perf_counter() - t0) * 1000)}
    except asyncio.TimeoutError:
        err = f"таймаут {SOURCE_TIMEOUT:.0f} с"
    except httpx.HTTPStatusError as e:
        err = f"HTTP {e.response.status_code}"
    except Exception as e:  # noqa: BLE001 — один упавший сайт не должен ронять поиск
        err = f"{type(e).__name__}: {e}"[:200]
    return src.id, [], {"ok": False, "count": 0, "error": err, "ms": int((time.perf_counter() - t0) * 1000)}


_norm_re = re.compile(r"[^a-z0-9а-яё ]+")
_stop = {"hotel", "the", "apartment", "apartments", "hostel", "by", "and", "&", "отель", "гостиница", "suites", "inn"}


def _norm(title: str) -> str:
    words = _norm_re.sub(" ", title.lower()).split()
    return " ".join(w for w in words if w not in _stop)


def same_property(a: Listing, b: Listing) -> bool:
    if a.source == b.source or None in (a.lat, a.lon, b.lat, b.lon):
        return False
    if haversine_km(a.lat, a.lon, b.lat, b.lon) > 0.25:  # type: ignore[arg-type]
        return False
    na, nb = _norm(a.title), _norm(b.title)
    if not na or not nb:
        return False
    return na in nb or nb in na or SequenceMatcher(None, na, nb).ratio() >= 0.78


def deduplicate(items: list[Listing]) -> list[Listing]:
    """Склеивает один и тот же объект с разных сайтов; лучшая цена — в основную карточку,
    остальные варианты — в also_on (сравнение цен между сайтами)."""
    grid: dict[tuple[int, int], list[Listing]] = {}
    out: list[Listing] = []
    for it in items:
        if it.lat is None or it.lon is None:
            out.append(it)
            continue
        cell = (int(it.lat * 200), int(it.lon * 200))  # ячейка ~500 м
        match = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for cand in grid.get((cell[0] + dx, cell[1] + dy), []):
                    if same_property(cand, it):
                        match = cand
                        break
                if match:
                    break
            if match:
                break
        if not match:
            grid.setdefault(cell, []).append(it)
            out.append(it)
            continue
        offer = {"source": it.source, "price": it.price_conv, "url": it.url}
        if it.price_conv is not None and (match.price_conv is None or it.price_conv < match.price_conv):
            match.also_on.append({"source": match.source, "price": match.price_conv, "url": match.url})
            match.source, match.price, match.price_conv, match.currency, match.url = (
                it.source, it.price, it.price_conv, it.currency, it.url or match.url)
        else:
            match.also_on.append(offer)
        match.images = match.images or it.images
        match.rating = match.rating or it.rating
        match.rooms = match.rooms if match.rooms is not None else it.rooms
    return out


def apply_filters(items: list[Listing], q: SearchQuery) -> list[Listing]:
    res = []
    types = {t.lower() for t in q.property_types}
    for it in items:
        if q.price_min is not None and (it.price_conv is None or it.price_conv < q.price_min):
            continue
        if q.price_max is not None and (it.price_conv is None or it.price_conv > q.price_max):
            continue
        if q.rooms_min and it.rooms is not None and it.rooms < q.rooms_min:
            continue
        if q.rating_min and (it.rating is None or it.rating < q.rating_min):
            continue
        if types and (it.property_type or "").lower() not in types:
            continue
        if it.distance_km is not None and it.distance_km > MAX_DISTANCE_KM:
            continue
        res.append(it)
    return res


def relevance(it: Listing) -> float:
    s = 0.0
    s += 3 if it.price_conv is not None else 0
    s += (it.rating or 6) / 2
    s += 1 if it.images else 0
    s += min(len(it.also_on), 3) * 0.7  # объект есть на нескольких сайтах — можно сравнить цены
    s -= min(it.distance_km or 0, 20) * 0.15
    s += 0.5 if it.reviews and it.reviews > 50 else 0
    return s


def sort_items(items: list[Listing], how: str) -> list[Listing]:
    big = float("inf")
    if how == "price_asc":
        return sorted(items, key=lambda x: x.price_conv if x.price_conv is not None else big)
    if how == "price_desc":
        return sorted(items, key=lambda x: -(x.price_conv or -1))
    if how == "rating":
        return sorted(items, key=lambda x: -(x.rating or 0))
    if how == "distance":
        return sorted(items, key=lambda x: x.distance_km if x.distance_km is not None else big)
    return sorted(items, key=relevance, reverse=True)


async def aggregate(client: httpx.AsyncClient, sources: dict[str, Source], q: SearchQuery, area: GeoArea):
    if q.sources:
        chosen = [s for s in sources.values() if s.id in q.sources]
    else:  # по умолчанию — все реальные источники; демо только если реальных нет
        chosen = [s for s in sources.values() if s.id != "demo"]
    chosen = [s for s in chosen if s.enabled and s.supports(q.mode)]
    if not chosen and not q.sources and "demo" in sources:
        chosen = [sources["demo"]]
    results = await asyncio.gather(*(run_source(s, q, area) for s in chosen), fx_rates(client, q.currency))
    rates = results[-1]
    stats: dict[str, dict] = {}
    items: list[Listing] = []
    for sid, listings, st in results[:-1]:  # type: ignore[misc]
        stats[sid] = st
        for l in listings:
            l = l.model_copy(deep=True)
            l.price_conv = convert(l.price, l.currency, q.currency, rates)
            for o in l.also_on:
                o["price"] = convert(o.get("price"), l.currency, q.currency, rates)
            if l.lat is not None and l.lon is not None:
                l.distance_km = round(haversine_km(area.lat, area.lon, l.lat, l.lon), 2)
            items.append(l)
    items = deduplicate(items)
    items = apply_filters(items, q)
    items = sort_items(items, q.sort)
    return items, stats
