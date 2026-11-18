"""FindUrHome: HTTP API + раздача фронтенда.  Запуск:  uvicorn app.main:app --reload"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .aggregator import aggregate
from .ai import Assistant, ChatRequest, ChatResponse
from .geo import geocode
from .models import Mode, SearchQuery, SearchResult, SourceInfo
from .sources import build_sources

try:  # .env необязателен
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

STATIC = Path(__file__).resolve().parents[1] / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(follow_redirects=True, limits=httpx.Limits(max_connections=50))
    app.state.sources = build_sources(app.state.client)
    app.state.assistant = Assistant(app.state.client, run_search)
    yield
    await app.state.client.aclose()


app = FastAPI(title="FindUrHome — агрегатор жилья со встроенным ИИ", version="0.1.0", lifespan=lifespan)


@app.get("/api/sources", response_model=list[SourceInfo])
async def list_sources():
    return [s.info() for s in app.state.sources.values()]


@app.get("/api/search", response_model=SearchResult)
async def search(
    location: str = Query(..., min_length=2),
    mode: Mode = Mode.short,
    checkin: Optional[date] = None,
    checkout: Optional[date] = None,
    guests: int = 2,
    rooms_min: Optional[int] = None,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    rating_min: Optional[float] = None,
    property_types: list[str] = Query(default=[]),
    currency: str = "USD",
    sources: list[str] = Query(default=[]),
    sort: str = "relevance",
    limit: int = 200,
):
    q = SearchQuery(
        mode=mode, location=location, checkin=checkin, checkout=checkout, guests=guests,
        rooms_min=rooms_min, price_min=price_min, price_max=price_max, rating_min=rating_min,
        property_types=[t for t in property_types if t], currency=currency.upper(),
        sources=[s for s in sources if s], sort=sort, limit=limit,
    )
    if q.checkin and q.checkout and q.checkout <= q.checkin:
        raise HTTPException(400, "Дата выезда должна быть позже даты заезда")
    try:
        return await run_search(q)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Геокодер недоступен: {e}")


async def run_search(q: SearchQuery) -> SearchResult:
    area = await geocode(app.state.client, q.location)
    if not area:
        raise LookupError(f"Не нашёл место «{q.location}»")
    items, stats = await aggregate(app.state.client, app.state.sources, q, area)
    return SearchResult(query=q, area=area, listings=items[: q.limit], total=len(items), source_stats=stats)


@app.get("/api/ai/status")
async def ai_status():
    return {"provider": app.state.assistant.provider}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    try:
        return await app.state.assistant.chat(req)
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
