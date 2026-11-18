"""Craigslist: 700+ регионов (США, Канада, Европа, Азия…). Без ключа.
Длительная аренда — категория apa, посуточно — vac (vacation rentals).
Координаты/комнаты берутся из JSON-LD, цена и ссылка — из статичного списка."""
from __future__ import annotations

import hashlib
import html as htmllib
import math
import re
from typing import Optional

from ..models import GeoArea, Listing, Mode, PriceUnit, SearchQuery
from .base import Source, to_float, to_int
from .jsonld import BROWSER_UA, iter_ld_blocks

AREAS_URL = "https://reference.craigslist.org/Areas"
LI_RE = re.compile(r'<li class="cl-static-search-result"[^>]*title="(?P<title>[^"]*)"[^>]*>(?P<body>.*?)</li>', re.S)
URL_RE = re.compile(r'<a href="([^"]+)"')
PRICE_RE = re.compile(r'<div class="price">([^<]*)</div>')
CURRENCY = {"US": "USD", "CA": "CAD", "GB": "GBP", "AU": "AUD", "NZ": "NZD", "IN": "INR", "JP": "JPY"}

_areas_cache: Optional[list[dict]] = None


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def nearest_area(areas: list[dict], lat: float, lon: float, max_km: float = 120) -> Optional[dict]:
    best = min(areas, key=lambda a: haversine_km(lat, lon, a["Latitude"], a["Longitude"]), default=None)
    if best and haversine_km(lat, lon, best["Latitude"], best["Longitude"]) <= max_km:
        return best
    return None


def parse_search(html: str, mode: Mode, currency: str) -> list[Listing]:
    ld_items: list[dict] = []
    for block in iter_ld_blocks(html):
        if isinstance(block, dict) and block.get("@type") == "ItemList":
            ld_items = [x.get("item", {}) for x in block.get("itemListElement", [])]
    out = []
    for i, m in enumerate(LI_RE.finditer(html)):
        title = htmllib.unescape(m.group("title"))
        ld = ld_items[i] if i < len(ld_items) and htmllib.unescape(ld_items[i].get("name", "")) == title else \
            next((x for x in ld_items if htmllib.unescape(x.get("name", "")) == title), {})
        addr = ld.get("address", {}) if isinstance(ld.get("address"), dict) else {}
        body = m.group("body")
        um, pm = URL_RE.search(body), PRICE_RE.search(body)
        if not um:
            continue
        url = um.group(1)
        out.append(Listing(
            id="cl-" + hashlib.md5(url.encode()).hexdigest()[:12],
            source="craigslist",
            mode=mode,
            title=title,
            url=url,
            price=to_float(pm.group(1)) if pm else None,
            currency=currency,
            # в vacation rentals владельцы указывают цену за ночь/неделю/месяц по-разному
            price_unit=PriceUnit.month if mode == Mode.long else None,
            lat=to_float(ld.get("latitude")),
            lon=to_float(ld.get("longitude")),
            address=", ".join(x for x in [addr.get("addressLocality"), addr.get("addressRegion")] if x) or None,
            property_type=(ld.get("@type") or "apartment").lower(),
            rooms=to_int(ld.get("numberOfBedrooms")),
        ))
    return out


class CraigslistSource(Source):
    id = "craigslist"
    name = "Craigslist"
    modes = (Mode.long, Mode.short)
    description = "Объявления от владельцев, 700+ регионов мира"

    async def areas(self) -> list[dict]:
        global _areas_cache
        if _areas_cache is None:
            r = await self.client.get(AREAS_URL, headers={"User-Agent": BROWSER_UA}, timeout=20)
            r.raise_for_status()
            _areas_cache = r.json()
        return _areas_cache

    async def search(self, q: SearchQuery, area: GeoArea) -> list[Listing]:
        ar = nearest_area(await self.areas(), area.lat, area.lon)
        if not ar:
            return []
        cat = "apa" if q.mode == Mode.long else "vac"
        params: dict = {}
        if q.mode == Mode.long:
            if q.price_min:
                params["min_price"] = int(q.price_min)
            if q.price_max:
                params["max_price"] = int(q.price_max)
            if q.rooms_min:
                params["min_bedrooms"] = q.rooms_min
        r = await self.client.get(f"https://{ar['Hostname']}.craigslist.org/search/{cat}", params=params,
                                  headers={"User-Agent": BROWSER_UA}, timeout=30, follow_redirects=True)
        r.raise_for_status()
        return parse_search(r.text, q.mode, CURRENCY.get(ar.get("Country", ""), "USD"))
