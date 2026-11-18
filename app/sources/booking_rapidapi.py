"""Booking.com через RapidAPI (API 'booking-com15' от DataCrawler).
Ключ: RAPIDAPI_KEY. Хост можно поменять через BOOKING_RAPIDAPI_HOST."""
from __future__ import annotations

from datetime import date, timedelta

from ..models import GeoArea, Listing, Mode, PriceUnit, SearchQuery
from .base import Source, dig, to_float, to_int


class BookingRapidApiSource(Source):
    id = "booking"
    name = "Booking.com"
    modes = (Mode.short,)
    env_keys = ("RAPIDAPI_KEY",)
    description = "Отели и апартаменты Booking.com с живыми ценами"

    @property
    def host(self) -> str:
        return self.env("BOOKING_RAPIDAPI_HOST") or "booking-com15.p.rapidapi.com"

    @property
    def headers(self) -> dict:
        return {"x-rapidapi-key": self.env("RAPIDAPI_KEY") or "", "x-rapidapi-host": self.host}

    @staticmethod
    def parse(data: dict, nights: int, checkin: date, checkout: date, adults: int) -> list[Listing]:
        out = []
        for h in dig(data, "data.hotels", []) or []:
            p = h.get("property", {})
            total = to_float(dig(p, "priceBreakdown.grossPrice.value"))
            hid = h.get("hotel_id") or p.get("id")
            score = to_float(p.get("reviewScore"))
            out.append(
                Listing(
                    id=f"booking-{hid}",
                    source="booking",
                    mode=Mode.short,
                    title=p.get("name") or "Booking property",
                    url=(f"https://www.booking.com/hotel.html?hotel_id={hid}&checkin={checkin}"
                         f"&checkout={checkout}&group_adults={adults}") if hid else None,
                    price=round(total / nights, 2) if total else None,
                    currency=dig(p, "priceBreakdown.grossPrice.currency"),
                    price_unit=PriceUnit.night,
                    lat=to_float(p.get("latitude")),
                    lon=to_float(p.get("longitude")),
                    property_type="hotel" if to_int(p.get("propertyClass")) else "apartment",
                    rating=score,  # Booking уже 0–10
                    reviews=to_int(p.get("reviewCount")),
                    images=[u.replace("square60", "max500") for u in (p.get("photoUrls") or [])[:5]],
                )
            )
        return out

    async def search(self, q: SearchQuery, area: GeoArea) -> list[Listing]:
        base = f"https://{self.host}/api/v1/hotels"
        r = await self.client.get(f"{base}/searchDestination", params={"query": q.location},
                                  headers=self.headers, timeout=30)
        r.raise_for_status()
        dests = dig(r.json(), "data", []) or []
        if not dests:
            return []
        d = dests[0]
        checkin = q.checkin or date.today() + timedelta(days=14)
        checkout = q.checkout or checkin + timedelta(days=2)
        nights = max((checkout - checkin).days, 1)
        params = {
            "dest_id": d.get("dest_id"),
            "search_type": d.get("search_type") or d.get("dest_type", "city").upper(),
            "arrival_date": checkin.isoformat(),
            "departure_date": checkout.isoformat(),
            "adults": q.guests,
            "room_qty": 1,
            "page_number": 1,
            "currency_code": q.currency,
            "languagecode": "en-us",
        }
        if q.price_min:
            params["price_min"] = int(q.price_min * nights)
        if q.price_max:
            params["price_max"] = int(q.price_max * nights)
        r = await self.client.get(f"{base}/searchHotels", params=params, headers=self.headers, timeout=40)
        r.raise_for_status()
        return self.parse(r.json(), nights, checkin, checkout, q.guests)
