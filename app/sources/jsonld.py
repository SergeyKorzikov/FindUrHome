"""Универсальный парсер schema.org JSON-LD.

Огромное число сайтов недвижимости (ради SEO) кладёт объявления в
<script type="application/ld+json">. Поэтому новый сайт часто подключается
одной записью в config/sources.yaml — без написания кода.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any, Iterable, Optional
from urllib.parse import quote, quote_plus, urljoin

from ..models import GeoArea, Listing, Mode, PriceUnit, SearchQuery
from .base import USER_AGENT, Source, dig, to_float, to_int

LD_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
LISTING_TYPES = {
    "realestatelisting", "apartment", "house", "room", "accommodation", "residence",
    "singlefamilyresidence", "apartmentcomplex", "hotel", "hostel", "lodgingbusiness",
    "vacationrental", "bedandbreakfast", "motel", "resort", "campground", "suite", "hotelroom",
}
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    if isinstance(t, str):
        t = [t]
    return {str(x).lower() for x in (t or [])}


def _qv(v: Any) -> Any:
    """QuantitativeValue -> число."""
    return v.get("value") if isinstance(v, dict) else v


def _first(*vals: Any) -> Any:
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def iter_ld_blocks(html: str) -> Iterable[Any]:
    for m in LD_RE.finditer(html):
        raw = m.group(1).strip()
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            try:  # иногда внутри лишние управляющие символы
                yield json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except json.JSONDecodeError:
                continue


def find_listing_nodes(obj: Any) -> list[dict]:
    found: list[dict] = []

    def walk(o: Any) -> None:
        if isinstance(o, list):
            for x in o:
                walk(x)
            return
        if not isinstance(o, dict):
            return
        types = _types(o)
        offers = o.get("offers")
        is_summary = isinstance(offers, dict) and "aggregateoffer" in _types(offers)
        if types & LISTING_TYPES and o.get("name") and not is_summary:
            found.append(o)
            return
        for k, v in o.items():
            if k not in ("@context", "publisher", "breadcrumb", "author"):
                walk(v)

    walk(obj)
    return found


def node_to_listing(node: dict, *, source: str, mode: Mode, base_url: str = "",
                    price_unit: Optional[PriceUnit] = None, default_currency: Optional[str] = None) -> Listing:
    offers = node.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    offers = offers if isinstance(offers, dict) else {}
    offered = offers.get("itemOffered") if isinstance(offers.get("itemOffered"), dict) else {}
    geo = _first(node.get("geo"), offered.get("geo")) or {}
    addr = _first(node.get("address"), offered.get("address"))
    if isinstance(addr, dict):
        addr = ", ".join(str(addr[k]) for k in ("streetAddress", "addressLocality", "addressCountry") if addr.get(k))
    img = node.get("image") or offered.get("image")
    if isinstance(img, dict):
        img = img.get("url")
    images = [i if isinstance(i, str) else i.get("url") for i in (img if isinstance(img, list) else [img]) if i]
    url = _first(node.get("url"), offers.get("url"))
    if url and base_url:
        url = urljoin(base_url, url)
    rating = to_float(dig(node, "aggregateRating.ratingValue"))
    best = to_float(dig(node, "aggregateRating.bestRating")) or 5
    price = to_float(_first(offers.get("price"), dig(offers, "priceSpecification.price"), offers.get("lowPrice")))
    types = _types(node) | _types(offered)
    ptype = next((t for t in ("room", "house", "apartment", "hotel", "hostel", "vacationrental") if t in types), None)
    lid = hashlib.md5((url or node.get("name", "")).encode()).hexdigest()[:12]
    return Listing(
        id=f"{source}-{lid}",
        source=source,
        mode=mode,
        title=str(node.get("name"))[:200],
        url=url,
        price=price,
        currency=_first(offers.get("priceCurrency"), dig(offers, "priceSpecification.priceCurrency"), default_currency),
        price_unit=price_unit or (PriceUnit.month if mode == Mode.long else PriceUnit.night),
        lat=to_float(_first(geo.get("latitude"), node.get("latitude"))),
        lon=to_float(_first(geo.get("longitude"), node.get("longitude"))),
        address=addr if isinstance(addr, str) else None,
        property_type=ptype,
        rooms=to_int(_qv(_first(node.get("numberOfBedrooms"), offered.get("numberOfBedrooms"),
                                node.get("numberOfRooms"), offered.get("numberOfRooms")))),
        area_m2=to_float(_qv(_first(node.get("floorSize"), offered.get("floorSize")))),
        rating=round(rating * 10 / best, 1) if rating else None,
        reviews=to_int(dig(node, "aggregateRating.reviewCount")),
        images=images[:5],
    )


def extract_listings(html: str, **kw: Any) -> list[Listing]:
    out: list[Listing] = []
    seen: set[str] = set()
    for block in iter_ld_blocks(html):
        for node in find_listing_nodes(block):
            try:
                l = node_to_listing(node, **kw)
            except Exception:
                continue
            if l.id not in seen:
                seen.add(l.id)
                out.append(l)
    return out


def render_template(tpl: str, q: SearchQuery, area: GeoArea, page: int = 1) -> str:
    checkin = q.checkin or date.today() + timedelta(days=14)
    checkout = q.checkout or checkin + timedelta(days=2)
    city = area.city or q.location
    vals = {
        # сайтам отдаём английское название из геокодера: «в Стамбуле» -> Istanbul
        "location": quote(city), "location_q": quote_plus(city), "query_raw": quote_plus(q.location),
        "city": quote(city), "city_slug": re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-"),
        "lat": area.lat, "lon": area.lon, "country": (area.country_code or "").lower(),
        "checkin": checkin.isoformat(), "checkout": checkout.isoformat(), "guests": q.guests,
        "price_min": int(q.price_min or 0), "price_max": int(q.price_max or 0) or "",
        "rooms_min": q.rooms_min or "", "currency": q.currency, "page": page,
    }
    return tpl.format(**vals)


class JsonLdConfigSource(Source):
    """Источник, описанный в config/sources.yaml (секция jsonld)."""

    def __init__(self, client, cfg: dict):
        super().__init__(client)
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg.get("name", self.id)
        self.modes = tuple(Mode(m) for m in (cfg.get("modes") or [cfg.get("mode", "long")]))
        self.env_keys = tuple(cfg.get("env_keys", []))
        self.description = cfg.get("description", "JSON-LD")
        self.countries = {c.lower() for c in cfg.get("countries", [])}

    @property
    def enabled(self) -> bool:
        return self.cfg.get("enabled", True) and super().enabled

    async def search(self, q: SearchQuery, area: GeoArea) -> list[Listing]:
        if self.countries and (area.country_code or "").lower() not in self.countries:
            return []
        tpl = self.cfg["url"].get(q.mode.value) if isinstance(self.cfg["url"], dict) else self.cfg["url"]
        if not tpl:
            return []
        pages = int(self.cfg.get("pages", 1))
        out: list[Listing] = []
        seen: set[str] = set()
        for page in range(1, pages + 1):
            url = render_template(tpl, q, area, page)
            r = await self.client.get(url, headers={"User-Agent": BROWSER_UA if self.cfg.get("browser_ua", True) else USER_AGENT,
                                                    "Accept-Language": "en"}, timeout=30, follow_redirects=True)
            if r.status_code == 404:  # многие сайты отвечают 404, если по городу нет объявлений
                break
            r.raise_for_status()
            batch = extract_listings(r.text, source=self.id, mode=q.mode, base_url=str(r.url),
                                     price_unit=PriceUnit(self.cfg["price_unit"]) if self.cfg.get("price_unit") else None,
                                     default_currency=self.cfg.get("currency"))
            new = [l for l in batch if l.id not in seen]
            if not new:
                break
            seen.update(l.id for l in new)
            out.extend(new)
            if "{page}" not in tpl:
                break
        return out
