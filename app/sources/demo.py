"""Демо-источник: синтетические объявления вокруг найденного города.
Нужен, чтобы сайт работал сразу, без ключей. Отключается DISABLE_DEMO=1."""
from __future__ import annotations

import hashlib
import os
import random

from ..models import GeoArea, Listing, Mode, PriceUnit, SearchQuery
from .base import Source

SHORT_TYPES = ["hotel", "apartment", "hostel", "guest_house", "villa"]
LONG_TYPES = ["apartment", "studio", "house", "room"]
ADJ = ["Уютная", "Светлая", "Тихая", "Просторная", "Современная", "Видовая"]


class DemoSource(Source):
    id = "demo"
    name = "Демо-данные"
    modes = (Mode.short, Mode.long)
    description = "Синтетические объявления для проверки интерфейса"

    @property
    def enabled(self) -> bool:
        return os.getenv("DISABLE_DEMO") != "1"

    async def search(self, q: SearchQuery, area: GeoArea) -> list[Listing]:
        seed = int(hashlib.md5(f"{area.name}{q.mode}".encode()).hexdigest()[:8], 16)
        rnd = random.Random(seed)
        s, w, n, e = area.bbox
        out = []
        types = SHORT_TYPES if q.mode == Mode.short else LONG_TYPES
        for i in range(40):
            ptype = rnd.choice(types)
            rooms = rnd.randint(0 if ptype == "studio" else 1, 4)
            if q.mode == Mode.short:
                price = round(rnd.uniform(25, 320), 0)
                unit = PriceUnit.night
                title = f"{rnd.choice(ADJ)} {ptype.replace('_', ' ')} #{i + 1}"
            else:
                price = round(rnd.uniform(350, 3500), -1)
                unit = PriceUnit.month
                title = f"{rnd.choice(ADJ)} {ptype}, {rooms} комн."
            out.append(
                Listing(
                    id=f"demo-{q.mode.value}-{i}",
                    source=self.id,
                    mode=q.mode,
                    title=title,
                    url=None,
                    price=price,
                    currency=q.currency,
                    price_unit=unit,
                    lat=rnd.uniform(s, n),
                    lon=rnd.uniform(w, e),
                    address=area.name,
                    property_type=ptype,
                    rooms=rooms,
                    area_m2=round(rnd.uniform(18, 140)),
                    rating=round(rnd.uniform(6.0, 9.8), 1),
                    reviews=rnd.randint(0, 900),
                    images=[f"https://picsum.photos/seed/{seed + i}/480/320"],
                )
            )
        return out
