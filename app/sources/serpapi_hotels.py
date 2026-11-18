"""Google Hotels через SerpApi — по сути метапоиск: одна выдача содержит цены
Booking, Expedia, Agoda, Hotels.com, Trip.com, сайтов отелей, а также Airbnb/Vrbo
(режим vacation rentals). Ключ: SERPAPI_KEY (есть бесплатный тариф)."""
from __future__ import annotations

import asyncio
import os
import re
from datetime import date, timedelta

from ..models import GeoArea, Listing, Mode, PriceUnit, SearchQuery
from .base import Source, dig, to_float, to_int

API = "https://serpapi.com/search.json"


class SerpApiHotelsSource(Source):
    id = "google_hotels"
    name = "Google Hotels (Booking, Expedia, Agoda, Airbnb…)"
    modes = (Mode.short,)
    env_keys = ("SERPAPI_KEY",)
    description = "Метапоиск: цены с десятков сайтов бронирования в одной выдаче"

    def params(self, q: SearchQuery, area: GeoArea, vacation: bool) -> dict:
        checkin = q.checkin or date.today() + timedelta(days=14)
        checkout = q.checkout or checkin + timedelta(days=2)
        p = {
            "engine": "google_hotels",
            "q": q.location,
            "check_in_date": checkin.isoformat(),
            "check_out_date": checkout.isoformat(),
            "adults": q.guests,
            "currency": q.currency,
            "hl": "en",
            "api_key": self.env("SERPAPI_KEY"),
        }
        if area.country_code:
            p["gl"] = area.country_code.lower()
        if q.price_min:
            p["min_price"] = int(q.price_min)
        if q.price_max:
            p["max_price"] = int(q.price_max)
        if q.sort == "price_asc":
            p["sort_by"] = 3
        elif q.sort == "rating":
            p["sort_by"] = 8
        if vacation:
            p["vacation_rentals"] = "true"
            if q.rooms_min:
                p["bedrooms"] = q.rooms_min
        return p

    @staticmethod
    def parse(data: dict, mode: Mode = Mode.short) -> list[Listing]:
        out = []
        for i, p in enumerate(data.get("properties", [])):
            price = to_float(dig(p, "rate_per_night.extracted_lowest"))
            info = " ".join(p.get("essential_info", []) or [])
            m = re.search(r"(\d+)\s+bedroom", info)
            also_on = [
                {"source": x.get("source"), "price": to_float(dig(x, "rate_per_night.extracted_lowest")),
                 "url": x.get("link")}
                for x in p.get("prices", []) or [] if x.get("source")
            ]
            rating = to_float(p.get("overall_rating"))
            ptype = (p.get("type") or "hotel").lower().replace(" ", "_")
            out.append(
                Listing(
                    id=f"gh-{p.get('property_token') or i}",
                    source="google_hotels",
                    mode=mode,
                    title=p.get("name") or "Без названия",
                    url=p.get("link") or (also_on[0]["url"] if also_on else None),
                    price=price,
                    currency=dig(data, "search_parameters.currency"),
                    price_unit=PriceUnit.night,
                    lat=to_float(dig(p, "gps_coordinates.latitude")),
                    lon=to_float(dig(p, "gps_coordinates.longitude")),
                    property_type=ptype,
                    rooms=to_int(m.group(1)) if m else None,
                    rating=rating * 2 if rating else None,  # 5 -> 10
                    reviews=to_int(p.get("reviews")),
                    images=[x.get("original_image") or x.get("thumbnail") for x in (p.get("images") or [])[:5]
                            if x.get("original_image") or x.get("thumbnail")],
                    amenities=(p.get("amenities") or [])[:12],
                    also_on=also_on,
                )
            )
        return out

    async def _one(self, q: SearchQuery, area: GeoArea, vacation: bool) -> list[Listing]:
        r = await self.client.get(API, params=self.params(q, area, vacation), timeout=40)
        r.raise_for_status()
        return self.parse(r.json())

    async def search(self, q: SearchQuery, area: GeoArea) -> list[Listing]:
        tasks = [self._one(q, area, False)]
        if os.getenv("SERPAPI_VACATION", "1") == "1":
            tasks.append(self._one(q, area, True))
        res = await asyncio.gather(*tasks, return_exceptions=True)
        ok = [x for x in res if isinstance(x, list)]
        if not ok:
            raise res[0]  # type: ignore[misc]
        return [l for part in ok for l in part]
