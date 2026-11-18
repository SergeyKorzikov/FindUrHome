"""Геокодинг (Nominatim/OSM) и курсы валют (open.er-api.com) — оба бесплатны и без ключа."""
from __future__ import annotations

import asyncio
import re
import time

import httpx

from .models import GeoArea
from .sources.base import USER_AGENT

_geo_cache: dict[str, GeoArea] = {}
_fx_cache: dict[str, tuple[float, dict[str, float]]] = {}


def name_variants(text: str) -> list[str]:
    """«в Стамбуле / в Москве / в Грузии» -> именительный падеж (перебором)."""
    words = text.strip().split()
    if not words or not re.fullmatch(r"[А-Яа-яЁё\-]+", words[0]):
        return [text]
    w, rest = words[0], " ".join(words[1:])
    out = [text]
    for suf, rep in (("ии", "ия"), ("е", ""), ("е", "а"), ("е", "я"), ("и", "ь"), ("и", "ы"), ("у", "а")):
        if w.endswith(suf) and len(w) > 3:
            out.append((w[: -len(suf)] + rep + " " + rest).strip())
    return list(dict.fromkeys(out))


PLACE_TYPES = {"city", "town", "village", "municipality", "state", "country", "suburb", "quarter",
               "neighbourhood", "city_district", "borough", "district", "province", "region", "county", "island"}


CENTER_RE = re.compile(r"\b(city\s+cent(?:er|re)|cent(?:er|re)|downtown|old\s+town|центр[а-я]*|в\s+центре|merkez)\b", re.I)


def clean_place(text: str) -> str:
    """«Lisbon city center» -> «Lisbon»: центр и так берётся по умолчанию,
    а с этими словами геокодер находит одноимённые посёлки (Lisbon Center, Illinois)."""
    cleaned = re.sub(r"\s{2,}", " ", CENTER_RE.sub(" ", text)).strip(" ,-")
    return cleaned or text


async def geocode(client: httpx.AsyncClient, text: str) -> GeoArea | None:
    text = clean_place(text)
    key = text.strip().lower()
    if key in _geo_cache:
        return _geo_cache[key]
    best: tuple[float, GeoArea] | None = None
    for i, variant in enumerate(name_variants(text)):
        if i:
            await asyncio.sleep(1.0)  # правила Nominatim: не чаще 1 запроса в секунду
        found = await _geocode_one(client, variant)
        if not found:
            continue
        area, score = found
        if best is None or score > best[0]:
            best = (score, area)
        if score >= 1.0 and i == 0:  # точное совпадение с населённым пунктом — дальше не ищем
            break
    if best:
        _geo_cache[key] = best[1]
        return best[1]
    return None


async def _geocode_one(client: httpx.AsyncClient, text: str) -> tuple[GeoArea, float] | None:
    r = await client.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": text, "format": "jsonv2", "limit": 1, "addressdetails": 1, "accept-language": "en"},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    d = data[0]
    s, n, w, e = (float(x) for x in d["boundingbox"])
    addr = d.get("address", {})
    area = GeoArea(
        name=d.get("display_name", text),
        lat=float(d["lat"]),
        lon=float(d["lon"]),
        bbox=(s, w, n, e),
        country_code=addr.get("country_code"),
        city=addr.get("city") or addr.get("town") or addr.get("village") or addr.get("state") or d.get("name"),
    )
    score = float(d.get("importance") or 0) + (1.0 if d.get("addresstype") in PLACE_TYPES else 0.0)
    return area, score


async def fx_rates(client: httpx.AsyncClient, base: str) -> dict[str, float]:
    """Сколько единиц валюты X за 1 base. Кэш 12 ч."""
    base = base.upper()
    hit = _fx_cache.get(base)
    if hit and time.time() - hit[0] < 12 * 3600:
        return hit[1]
    try:
        r = await client.get(f"https://open.er-api.com/v6/latest/{base}", timeout=15)
        r.raise_for_status()
        rates = r.json().get("rates", {})
    except Exception:
        rates = {}
    rates[base] = 1.0
    _fx_cache[base] = (time.time(), rates)
    return rates


def convert(amount: float | None, cur: str | None, target: str, rates: dict[str, float]) -> float | None:
    if amount is None:
        return None
    cur = (cur or target).upper()
    if cur == target.upper():
        return amount
    rate = rates.get(cur)
    return round(amount / rate, 2) if rate else None
