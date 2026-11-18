"""Airbnb без ключа: страница выдачи содержит данные объявлений в JSON
(<script id="data-deferred-state-0">). Работает в обоих режимах:
посуточно — на ваши даты; долгосрочно — запрос на 30 дней, цена пересчитывается в месяц."""
from __future__ import annotations

import base64
import json
import re
from datetime import date, timedelta
from typing import Any, Iterator
from urllib.parse import quote

from ..models import GeoArea, Listing, Mode, PriceUnit, SearchQuery
from .base import Source, dig, to_float, to_int
from .jsonld import BROWSER_UA

STATE_RE = re.compile(r'<script id="data-deferred-state-0"[^>]*>(.*?)</script>', re.S)
CUR_SYM = {"$": "USD", "€": "EUR", "£": "GBP", "₺": "TRY", "₽": "RUB", "₾": "GEL", "฿": "THB", "₹": "INR"}


def _walk(o: Any) -> Iterator[dict]:
    if isinstance(o, dict):
        if o.get("__typename") == "StaySearchResult":
            yield o
            return
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _price(r: dict) -> tuple[float | None, str | None, bool]:
    """-> (сумма, валюта, это_за_весь_срок)"""
    line = dig(r, "structuredDisplayPrice.primaryLine") or {}
    raw = line.get("discountedPrice") or line.get("price") or line.get("accessibilityLabel") or ""
    amount = to_float(re.split(r"\s(?:for|за)\s", raw)[0]) if raw else None
    cur = next((c for s, c in CUR_SYM.items() if s in raw), None)
    qualifier = (line.get("qualifier") or line.get("accessibilityLabel") or "").lower()
    is_total = "night" not in qualifier or bool(re.search(r"\d+\s+nights", qualifier))
    return amount, cur, is_total


AIRBNB_TYPES = [("hotel", "hotel"), ("hostel", "hostel"), ("guest", "guest_house"), ("villa", "villa"),
                ("room", "room"), ("studio", "studio"), ("loft", "apartment"), ("condo", "apartment"),
                ("apartment", "apartment"), ("flat", "apartment"), ("home", "house"), ("house", "house"),
                ("cottage", "house"), ("townhouse", "house")]


def airbnb_type(title: str, lines: str) -> str:
    """«Apartment in Lisbon» / «Room in …» / «Home in …» -> наш тип."""
    head = title.lower().split(" in ")[0]
    if "studio" in lines.lower():
        return "studio"
    for key, t in AIRBNB_TYPES:
        if key in head:
            return t
    return "vacation_rental"


def parse_search(html: str, mode: Mode, nights: int, currency: str) -> list[Listing]:
    m = STATE_RE.search(html)
    if not m:
        return []
    data = json.loads(m.group(1))
    out = []
    for r in _walk(data):
        ds = r.get("demandStayListing") or {}
        raw_id = ds.get("id") or ""
        try:
            lid = base64.b64decode(raw_id).decode().split(":")[-1]
        except Exception:
            lid = raw_id
        amount, cur, is_total = _price(r)
        per_night = amount / nights if amount is not None and is_total else amount
        price = round(per_night * 30, 0) if (per_night is not None and mode == Mode.long) else per_night
        lines = " ".join(x.get("body", "") for x in dig(r, "structuredContent.primaryLine", []) or [])
        bm = re.search(r"(\d+)\s+bedroom", lines)
        rating = re.match(r"([\d.]+)\s*\((\d[\d,]*)\)", r.get("avgRatingLocalized") or "")
        title = dig(ds, "description.name.localizedStringWithTranslationPreference") \
            or dig(r, "nameLocalized.localizedStringWithTranslationPreference") or r.get("title") or "Airbnb"
        out.append(Listing(
            id=f"airbnb-{lid}",
            source="airbnb",
            mode=mode,
            title=title,
            url=f"https://www.airbnb.com/rooms/{lid}" if lid.isdigit() else None,
            price=round(price, 2) if price is not None else None,
            currency=cur or currency,
            price_unit=PriceUnit.month if mode == Mode.long else PriceUnit.night,
            lat=to_float(dig(ds, "location.coordinate.latitude")),
            lon=to_float(dig(ds, "location.coordinate.longitude")),
            address=r.get("title"),
            property_type=airbnb_type(r.get("title") or "", lines),
            rooms=to_int(bm.group(1)) if bm else (0 if "studio" in lines.lower() else None),
            rating=round(float(rating.group(1)) * 2, 1) if rating else None,
            reviews=to_int(rating.group(2)) if rating else None,
            images=[p.get("picture") for p in (r.get("contextualPictures") or [])[:5] if p.get("picture")],
        ))
    return out


def page_cursors(html: str) -> list[str]:
    m = STATE_RE.search(html)
    if not m:
        return []
    for r in _walk_pag(json.loads(m.group(1))):
        return r
    return []


def _walk_pag(o: Any) -> Iterator[list[str]]:
    if isinstance(o, dict):
        if o.get("__typename") == "StaysSearchPaginationInfo":
            yield o.get("pageCursors") or []
            return
        for v in o.values():
            yield from _walk_pag(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_pag(v)


class AirbnbSource(Source):
    id = "airbnb"
    name = "Airbnb"
    modes = (Mode.short, Mode.long)
    description = "Квартиры и дома у хозяев, посуточно и помесячно, без ключа"

    async def search(self, q: SearchQuery, area: GeoArea) -> list[Listing]:
        if q.mode == Mode.long:
            checkin = date.today() + timedelta(days=14)
            checkout = checkin + timedelta(days=30)
        else:
            checkin = q.checkin or date.today() + timedelta(days=14)
            checkout = q.checkout or checkin + timedelta(days=2)
        nights = max((checkout - checkin).days, 1)
        place = ", ".join(area.name.split(", ")[:2]) if area.name else q.location
        params = {"checkin": checkin.isoformat(), "checkout": checkout.isoformat(),
                  "adults": q.guests, "currency": q.currency, "locale": "en"}
        if q.rooms_min:
            params["min_bedrooms"] = q.rooms_min
        url = f"https://www.airbnb.com/s/{quote(place)}/homes"
        headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en"}
        r = await self.client.get(url, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        items = parse_search(r.text, q.mode, nights, q.currency)
        for cursor in page_cursors(r.text)[1:3]:  # ещё 2 страницы
            try:
                r2 = await self.client.get(url, params={**params, "cursor": cursor}, headers=headers, timeout=30)
                items += parse_search(r2.text, q.mode, nights, q.currency)
            except Exception:
                break
        seen, out = set(), []
        for it in items:
            if it.id not in seen:
                seen.add(it.id)
                out.append(it)
        return out
