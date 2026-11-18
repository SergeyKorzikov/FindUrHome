"""Цикл «модель -> инструмент поиска -> модель» на подставных ответах API."""
import asyncio
import json

import httpx

from app.ai import Assistant, ChatMessage, ChatRequest
from app.models import GeoArea, Listing, Mode, SearchQuery, SearchResult


async def fake_search(q: SearchQuery) -> SearchResult:
    area = GeoArea(name="Lisbon", lat=38.7, lon=-9.1, bbox=(38.6, -9.2, 38.8, -9.0), city="Lisbon")
    l = Listing(id="1", source="booking", mode=q.mode, title="Casa Azul", price_conv=80, lat=38.7, lon=-9.1)
    return SearchResult(query=q, area=area, listings=[l], total=1, source_stats={"booking": {"ok": True, "count": 1}})


def run(coro):
    return asyncio.run(coro)


def test_anthropic_tool_loop(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    calls = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append(body)
        if len(calls) == 1:
            assert body["tools"][0]["name"] == "search_housing"
            return httpx.Response(200, json={"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "Ищу"},
                {"type": "tool_use", "id": "tu1", "name": "search_housing",
                 "input": {"location": "Lisbon", "mode": "short", "price_max": 100}}]})
        tool_result = body["messages"][-1]["content"][0]
        assert tool_result["type"] == "tool_result" and "Casa Azul" in tool_result["content"]
        return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": "Нашёл Casa Azul за 80"}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            a = Assistant(c, fake_search)
            return await a.chat(ChatRequest(messages=[ChatMessage(role="user", content="жильё в Лиссабоне до 100")]))

    res = run(go())
    assert res.provider == "anthropic" and "Casa Azul" in res.reply
    assert res.search and res.search.query.price_max == 100


def test_openai_compatible_tool_loop(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AI_PROVIDER", "openai")
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:11434/v1")
    step = {"n": 0}

    def handler(req: httpx.Request):
        step["n"] += 1
        body = json.loads(req.content)
        if step["n"] == 1:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "search_housing",
                                                              "arguments": json.dumps({"location": "Lisbon", "mode": "long"})}}]}}]})
        assert body["messages"][-1]["role"] == "tool"
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Готово"}}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await Assistant(c, fake_search).chat(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))

    res = run(go())
    assert res.reply == "Готово" and res.search.query.mode == Mode.long


def test_basic_mode_without_keys(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "AI_BASE_URL", "AI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AI_PROVIDER", "basic")

    async def go():
        async with httpx.AsyncClient() as c:
            return await Assistant(c, fake_search).chat(ChatRequest(messages=[ChatMessage(role="user", content="квартира в Лиссабоне на месяц")]))

    res = run(go())
    assert res.provider == "basic" and "Casa Azul" in res.reply


def test_free_provider_is_default_and_falls_back(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "AI_PROVIDER", "AI_BASE_URL", "AI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    def handler(req):
        return httpx.Response(503, text="busy")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            a = Assistant(c, fake_search)
            assert a.provider == "free"
            return await a.chat(ChatRequest(messages=[ChatMessage(role="user", content="квартира в Лиссабоне на месяц")]))

    res = run(go())
    assert res.provider == "basic" and "Casa Azul" in res.reply


def test_ground_reply_replaces_placeholders_and_drops_fake_urls():
    from app.ai import ground_reply
    res = run(fake_search(SearchQuery(location="Lisbon", currency="EUR")))
    res.listings[0].url = "https://www.airbnb.com/rooms/1"
    out = ground_reply("Берите [#1]. Ещё есть https://www.airbnb.com/rooms/999 и [#7].", res)
    assert "Casa Azul" in out and "80 EUR/ночь" in out and "rooms/1" in out
    assert "rooms/999" not in out and "[#7]" not in out
