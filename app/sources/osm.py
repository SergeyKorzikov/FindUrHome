"""OpenStreetMap (Overpass API): все отели/хостелы/гостевые дома мира, бесплатно и без ключа.
Цен нет, зато покрытие даже там, где нет Booking. Данные © OpenStreetMap contributors (ODbL)."""
from __future__ import annotations

import asyncio
import os

import httpx

from ..models import GeoArea, Listing, Mode, SearchQuery
from .base import Source, to_float, to_int

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
TOURISM = "hotel|hostel|guest_house|apartment|motel|chalet"


class OSMSource(Source):
    id = "osm"
    name = "OpenStreetMap"
    modes = (Mode.short,)
    description = "Отели и хостелы со всего мира, без цен, без ключа"

    @property
    def enabled(self) -> bool:
        return os.getenv("DISABLE_OSM") != "1"

    @staticmethod
    def build_query(area: GeoArea, radius_m: int = 5000, limit: int = 300) -> str:
        return (
            f"[out:json][timeout:25];"
            f'nwr["tourism"~"^({TOURISM})$"](around:{radius_m},{area.lat},{area.lon});'
            f"out center tags {limit};"
        )

    @staticmethod
    def parse(data: dict) -> list[Listing]:
        out = []
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            name = tags.get("name") or tags.get("name:en")
            if not name:
                continue
            lat = el.get("lat") or el.get("center", {}).get("lat")
            lon = el.get("lon") or el.get("center", {}).get("lon")
            addr = ", ".join(
                x for x in [tags.get("addr:street"), tags.get("addr:housenumber"), tags.get("addr:city")] if x
            )
            stars = to_float(tags.get("stars"))
            out.append(
                Listing(
                    id=f"osm-{el.get('type')}-{el.get('id')}",
                    source="osm",
                    mode=Mode.short,
                    title=name,
                    url=tags.get("website") or tags.get("contact:website")
                    or f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
                    lat=lat,
                    lon=lon,
                    address=addr or None,
                    property_type=tags.get("tourism"),
                    rooms=to_int(tags.get("rooms")),
                    rating=stars * 2 if stars else None,  # 5 звёзд -> 10
                    amenities=[k for k in ("internet_access", "wheelchair", "swimming_pool") if tags.get(k) not in (None, "no")],
                )
            )
        return out

    async def search(self, q: SearchQuery, area: GeoArea) -> list[Listing]:
        query = self.build_query(area)

        async def one(url: str) -> list[Listing]:
            r = await self.client.post(url, data={"data": query}, timeout=35)
            r.raise_for_status()
            return self.parse(r.json())

        # опрашиваем зеркала параллельно, берём первый успешный ответ
        tasks = [asyncio.create_task(one(u)) for u in MIRRORS]
        last_err: BaseException | None = None
        try:
            for fut in asyncio.as_completed(tasks):
                try:
                    return await fut
                except (httpx.HTTPError, ValueError) as e:
                    last_err = e
        finally:
            for t in tasks:
                t.cancel()
        raise RuntimeError(f"Overpass недоступен: {last_err}")
