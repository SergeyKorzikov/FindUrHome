"""Единая модель объявления и поискового запроса для всех источников."""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Mode(str, Enum):
    short = "short"  # посуточно / отели
    long = "long"    # долгосрочная аренда


class PriceUnit(str, Enum):
    night = "night"
    month = "month"
    total = "total"


class SearchQuery(BaseModel):
    mode: Mode = Mode.short
    location: str = Field(..., min_length=2, description="Город, район или адрес")
    checkin: Optional[date] = None
    checkout: Optional[date] = None
    guests: int = Field(2, ge=1, le=30)
    rooms_min: Optional[int] = Field(None, ge=0, le=20)
    price_min: Optional[float] = Field(None, ge=0)
    price_max: Optional[float] = Field(None, ge=0)
    rating_min: Optional[float] = Field(None, ge=0, le=10)
    property_types: list[str] = Field(default_factory=list)
    currency: str = "USD"
    sources: list[str] = Field(default_factory=list, description="Пусто = все доступные")
    sort: str = "relevance"  # relevance | price_asc | price_desc | rating
    limit: int = Field(200, ge=1, le=1000)

    @property
    def nights(self) -> Optional[int]:
        if self.checkin and self.checkout and self.checkout > self.checkin:
            return (self.checkout - self.checkin).days
        return None


class GeoArea(BaseModel):
    name: str
    lat: float
    lon: float
    # south, west, north, east
    bbox: tuple[float, float, float, float]
    country_code: Optional[str] = None
    city: Optional[str] = None


class Listing(BaseModel):
    id: str
    source: str
    mode: Mode
    title: str
    url: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    price_unit: Optional[PriceUnit] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    address: Optional[str] = None
    property_type: Optional[str] = None
    rooms: Optional[int] = None
    area_m2: Optional[float] = None
    rating: Optional[float] = None  # нормализовано к шкале 0–10
    reviews: Optional[int] = None
    images: list[str] = Field(default_factory=list)
    amenities: list[str] = Field(default_factory=list)
    also_on: list[dict] = Field(default_factory=list, description="Тот же объект на других сайтах")
    # заполняется агрегатором: цена в валюте запроса
    price_conv: Optional[float] = None
    distance_km: Optional[float] = None


class SourceInfo(BaseModel):
    id: str
    name: str
    modes: list[Mode]
    enabled: bool
    needs: Optional[str] = None
    description: str = ""


class SearchResult(BaseModel):
    query: SearchQuery
    area: Optional[GeoArea] = None
    listings: list[Listing]
    total: int
    source_stats: dict[str, dict]
