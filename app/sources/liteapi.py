"""LiteAPI (liteapi.travel) — B2B-API отелей с ~2 млн объектов и самостоятельной регистрацией.
Ключ: LITEAPI_KEY (sandbox-ключ бесплатный)."""
from __future__ import annotations

from datetime import date, timedelta

from ..models import GeoArea, Listing, Mode, PriceUnit, SearchQuery
from .base import Source, dig, to_float, to_int

API = "https://api.liteapi.travel/v3.0"


class LiteApiSource(Source):
    id = "liteapi"
    name = "LiteAPI (2M+ отелей)"
    modes = (Mode.short,)
    env_keys = ("LITEAPI_KEY",)
    description = "Глобальная база отелей с ценами"

    @staticmethod
    def parse(hotels: list[dict], rates: list[dict], nights: int, currency: str) -> list[Listing]:
        best: dict[str, tuple[float, str]] = {}
        for r in rates:
            hid = r.get("hotelId")
            for rt in r.get("roomTypes", []) or []:
                amount = to_float(dig(rt, "offerRetailRate.amount")) or to_float(dig(rt, "rates.0.retailRate.total.0.amount"))
                cur = dig(rt, "offerRetailRate.currency") or currency
                if amount and (hid not in best or amount < best[hid][0]):
                    best[hid] = (amount, cur)
        out = []
        for h in hotels:
            hid = h.get("id")
            price = best.get(hid)
            rating = to_float(h.get("rating"))
            out.append(
                Listing(
                    id=f"liteapi-{hid}",
                    source="liteapi",
                    mode=Mode.short,
                    title=h.get("name") or "Hotel",
                    url=None,
                    price=round(price[0] / nights, 2) if price else None,
                    currency=price[1] if price else None,
                    price_unit=PriceUnit.night,
                    lat=to_float(h.get("latitude")),
                    lon=to_float(h.get("longitude")),
                    address=h.get("address"),
                    property_type="hotel",
                    rating=rating,
                    reviews=to_int(h.get("reviewCount")),
                    images=[x for x in [h.get("main_photo"), h.get("thumbnail")] if x][:1],
                )
            )
        return out

    async def search(self, q: SearchQuery, area: GeoArea) -> list[Listing]:
        headers = {"X-API-Key": self.env("LITEAPI_KEY") or "", "accept": "application/json"}
        r = await self.client.get(f"{API}/data/hotels", headers=headers, timeout=30, params={
            "latitude": area.lat, "longitude": area.lon, "radius": 8000, "limit": 100})
        r.raise_for_status()
        hotels = r.json().get("data", []) or []
        if not hotels:
            return []
        checkin = q.checkin or date.today() + timedelta(days=14)
        checkout = q.checkout or checkin + timedelta(days=2)
        nights = max((checkout - checkin).days, 1)
        body = {
            "hotelIds": [h["id"] for h in hotels if h.get("id")],
            "occupancies": [{"adults": q.guests}],
            "currency": q.currency,
            "guestNationality": (area.country_code or "US").upper(),
            "checkin": checkin.isoformat(),
            "checkout": checkout.isoformat(),
        }
        rates: list[dict] = []
        try:
            rr = await self.client.post(f"{API}/hotels/rates", headers=headers, json=body, timeout=45)
            rr.raise_for_status()
            rates = rr.json().get("data", []) or []
        except Exception:
            pass  # покажем отели без цен
        return self.parse(hotels, rates, nights, q.currency)
