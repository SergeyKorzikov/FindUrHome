"""Мост к Apify: тысячи готовых скрейперов (Zillow, Idealista, Rightmove, Immobiliare,
Avito, Cian, Sahibinden, Airbnb…). Любой актор подключается записью в config/sources.yaml
(секция apify) с картой полей. Ключ: APIFY_TOKEN."""
from __future__ import annotations

import hashlib
import json

from ..models import GeoArea, Listing, Mode, PriceUnit, SearchQuery
from .base import Source, dig, to_float, to_int
from .jsonld import render_template

RUN_URL = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


def map_item(item: dict, fmap: dict, *, source: str, mode: Mode, unit: PriceUnit, currency: str | None) -> Listing:
    g = lambda key: dig(item, fmap[key]) if fmap.get(key) else None  # noqa: E731
    img = g("image")
    images = img if isinstance(img, list) else ([img] if img else [])
    url = g("url")
    return Listing(
        id=f"{source}-" + hashlib.md5(str(url or json.dumps(item, sort_keys=True, default=str)).encode()).hexdigest()[:12],
        source=source,
        mode=mode,
        title=str(g("title") or "Объявление")[:200],
        url=url,
        price=to_float(g("price")),
        currency=g("currency") or currency,
        price_unit=unit,
        lat=to_float(g("lat")),
        lon=to_float(g("lon")),
        address=g("address"),
        property_type=g("property_type"),
        rooms=to_int(g("rooms")),
        area_m2=to_float(g("area_m2")),
        rating=to_float(g("rating")),
        images=[i for i in images if isinstance(i, str)][:5],
    )


def render_input(obj, q: SearchQuery, area: GeoArea):
    if isinstance(obj, str):
        return render_template(obj, q, area)
    if isinstance(obj, list):
        return [render_input(x, q, area) for x in obj]
    if isinstance(obj, dict):
        return {k: render_input(v, q, area) for k, v in obj.items()}
    return obj


class ApifyConfigSource(Source):
    def __init__(self, client, cfg: dict):
        super().__init__(client)
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg.get("name", self.id)
        self.modes = tuple(Mode(m) for m in (cfg.get("modes") or [cfg.get("mode", "long")]))
        self.env_keys = ("APIFY_TOKEN",)
        self.description = cfg.get("description", f"Apify: {cfg.get('actor')}")
        self.countries = {c.lower() for c in cfg.get("countries", [])}

    @property
    def enabled(self) -> bool:
        return self.cfg.get("enabled", True) and super().enabled

    async def search(self, q: SearchQuery, area: GeoArea) -> list[Listing]:
        if self.countries and (area.country_code or "").lower() not in self.countries:
            return []
        actor = self.cfg["actor"].replace("/", "~")
        r = await self.client.post(RUN_URL.format(actor=actor),
                                   params={"token": self.env("APIFY_TOKEN"), "timeout": self.cfg.get("timeout", 90)},
                                   json=render_input(self.cfg.get("input", {}), q, area),
                                   timeout=self.cfg.get("timeout", 90) + 15)
        r.raise_for_status()
        unit = PriceUnit(self.cfg.get("price_unit", "month" if q.mode == Mode.long else "night"))
        return [map_item(it, self.cfg.get("map", {}), source=self.id, mode=q.mode, unit=unit,
                         currency=self.cfg.get("currency")) for it in r.json() if isinstance(it, dict)]
