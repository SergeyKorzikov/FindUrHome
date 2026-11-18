"""Встроенный ИИ-помощник FindUrHome.

Модель получает инструмент `search_housing`, сама превращает разговор
(«хочу двушку в Стамбуле до 1000$ возле метро») в структурированный запрос,
запускает агрегатор и обсуждает результаты. Найденное сразу показывается на сайте.

Провайдеры (переменная AI_PROVIDER):
  anthropic — Claude, ключ ANTHROPIC_API_KEY
  openai    — любой OpenAI-совместимый API: OpenAI, OpenRouter, Groq, локальная Ollama
              (AI_BASE_URL, AI_API_KEY)
  free      — по умолчанию, БЕЗ КЛЮЧА: бесплатная модель через Pollinations.ai
Если бесплатный сервис недоступен — простой разбор фраз (поиск всё равно запускается).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date, timedelta
from typing import Any, Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel, Field

from .models import Mode, SearchQuery, SearchResult

MAX_TOOL_ROUNDS = 4
FREE_URL = os.getenv("FREE_AI_URL", "https://text.pollinations.ai/openai")


class ChatMessage(BaseModel):
    role: str  # user | assistant
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1)
    context: Optional[dict] = None  # текущие фильтры и видимые результаты на странице


class ChatResponse(BaseModel):
    reply: str
    search: Optional[SearchResult] = None
    provider: str


SearchFn = Callable[[SearchQuery], Awaitable[SearchResult]]

SEARCH_TOOL = {
    "name": "search_housing",
    "description": (
        "Ищет жильё сразу на всех подключённых сайтах (Booking, Google Hotels, Airbnb/Vrbo через Google, "
        "Craigslist, Rentola, OpenStreetMap и др.). Результаты также показываются пользователю на карте. "
        "Вызывай, когда известно хотя бы место. Цены — в валюте currency; для mode=short цена за ночь, "
        "для mode=long — за месяц."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "Только город или район, на английском: 'Lisbon', 'Kadikoy, Istanbul'. Без слов center/downtown"},
            "mode": {"type": "string", "enum": ["short", "long"], "description": "short — посуточно/отели, long — аренда на месяцы"},
            "checkin": {"type": "string", "description": "YYYY-MM-DD, только для short"},
            "checkout": {"type": "string", "description": "YYYY-MM-DD, только для short"},
            "guests": {"type": "integer", "minimum": 1},
            "price_min": {"type": "number"},
            "price_max": {"type": "number"},
            "currency": {"type": "string", "description": "ISO-код, напр. USD, EUR, TRY"},
            "rooms_min": {"type": "integer", "description": "Минимум спален"},
            "rating_min": {"type": "number", "description": "Шкала 0–10 (как Booking): 4.5 из 5 звёзд = 9. Обычно не задавай"},
            "property_types": {"type": "array", "items": {"type": "string"},
                               "description": "hotel, apartment, hostel, guest_house, house, room, villa, vacation_rental"},
            "sort": {"type": "string", "enum": ["relevance", "price_asc", "price_desc", "rating", "distance"]},
        },
        "required": ["location", "mode"],
    },
}


def system_prompt(context: Optional[dict]) -> str:
    today = date.today().isoformat()
    ctx = ""
    if context:
        ctx = "\n\nСейчас на странице у пользователя:\n" + json.dumps(context, ensure_ascii=False)[:6000]
    return f"""Ты — ИИ-помощник сайта FindUrHome, агрегатора жилья со всего мира. Сегодня {today}.
Твоя задача — помочь человеку найти подходящее жильё: посуточно (отели, апартаменты) или на долгий срок.

Как работать:
- Отвечай на языке пользователя, коротко и по делу.
- ГЛАВНОЕ ПРАВИЛО: как только известно место — СРАЗУ вызывай search_housing, ничего не переспрашивая.
  Даты, бюджет, комнаты — бери из сообщения или оставляй пустыми. Уточнять можно ПОСЛЕ показа результатов.
- Никогда не переспрашивай то, что пользователь уже сказал. Без места — спроси только «где ищем?».
- Можно вызывать поиск несколько раз (например, сравнить два района).
- property_types указывай, только если человек явно исключает другие типы.
- КАК ССЫЛАТЬСЯ НА ВАРИАНТЫ: только меткой [#N], где N — поле n из результатов поиска.
  Сайт сам подставит вместо метки настоящее название, цену, рейтинг, расстояние и ссылку.
  НЕ пиши сам названия, цены, расстояния и ссылки объявлений — только метку и своё объяснение.
  Хорошо: «[#2] — дешевле всех и рейтинг выше 9; [#5] — если важна тишина».
  Плохо: таблицы с названиями и ценами.
- Выдели 3–5 лучших вариантов и объясни выбор; если в also_on объект есть на других сайтах — скажи, где дешевле.
- Будь честен: не выдумывай объявлений, цен и деталей, которых нет в результатах. Если источник
  вернул ошибку — скажи об этом. Цены у объявлений без дат могут быть ориентировочными.
- Для долгосрочной аренды напоминай о безопасности: не переводить залог до просмотра и договора,
  проверять собственника; объявления сильно дешевле рынка — повод насторожиться.
- Если знаешь город — можешь коротко подсказать, какие районы подходят под запрос (транспорт, тишина, цены),
  но отделяй свои общие знания от данных поиска.{ctx}"""


PLACEHOLDER_RE = re.compile(r"\[#(\d{1,3})\]")
URL_RE = re.compile(r"https?://[^\s)\]>]+")


def ground_reply(reply: str, res: Optional[SearchResult]) -> str:
    """Подставляет реальные данные вместо [#N] и вырезает ссылки, которых нет в результатах."""
    if not res:
        return URL_RE.sub("", reply) if "airbnb.com/rooms" in reply or "booking.com/hotel" in reply else reply
    cur = res.query.currency
    unit = "/ночь" if res.query.mode == Mode.short else "/мес"

    def card(m: re.Match) -> str:
        i = int(m.group(1)) - 1
        if not 0 <= i < len(res.listings):
            return ""
        l = res.listings[i]
        bits = [f"**{l.title}**"]
        if l.price_conv is not None:
            bits.append(f"{round(l.price_conv):,} {cur}{unit}".replace(",", " "))
        if l.rating:
            bits.append(f"★ {l.rating:.1f}")
        if l.rooms is not None:
            bits.append(f"{l.rooms} комн.")
        if l.distance_km is not None:
            bits.append(f"{l.distance_km} км от центра")
        bits.append(l.source)
        if "too_cheap" in l.flags:
            bits.append("⚠ подозрительно дёшево")
        return f"[#{i + 1}] " + " · ".join(bits) + (f" {l.url}" if l.url else "")

    reply = reply.replace("**", "")  # своё выделение модели путается с нашим — убираем
    known = {l.url for l in res.listings if l.url} | {o.get("url") for l in res.listings for o in l.also_on if o.get("url")}
    reply = URL_RE.sub(lambda m: m.group(0) if m.group(0) in known else "", reply)
    return PLACEHOLDER_RE.sub(card, reply)


def compact_result(res: SearchResult, top: int = 15) -> dict:
    return {
        "area": res.area.name if res.area else None,  # проверь, что это нужный город/страна
        "total_found": res.total,
        "sources": {k: (v.get("count") if v.get("ok") else f"ошибка: {v.get('error')}") for k, v in res.source_stats.items()},
        "currency": res.query.currency,
        "price_unit": "night" if res.query.mode == Mode.short else "month",
        "top": [
            {
                "n": i + 1, "id": l.id, "title": l.title, "source": l.source,
                "price": l.price_conv, "rooms": l.rooms, "area_m2": l.area_m2, "rating": l.rating,
                "reviews": l.reviews, "type": l.property_type, "km_to_center": l.distance_km,
                "also_on": [{"source": o.get("source"), "price": o.get("price")} for o in l.also_on][:5],
                "url": l.url,
                **({"warning": "цена сильно ниже рынка — возможна приманка/мошенничество"} if "too_cheap" in l.flags else {}),
            }
            for i, l in enumerate(res.listings[:top])
        ],
    }


def tool_input_to_query(args: dict) -> SearchQuery:
    a = {k: v for k, v in args.items() if v not in (None, "", [])}
    for k in ("checkin", "checkout"):
        if k in a:
            try:
                a[k] = date.fromisoformat(str(a[k])[:10])
            except ValueError:
                a.pop(k)
    if "currency" in a:
        a["currency"] = str(a["currency"]).upper()[:3]
    return SearchQuery(**a)


class Assistant:
    def __init__(self, client: httpx.AsyncClient, search_fn: SearchFn):
        self.client = client
        self.search_fn = search_fn

    @property
    def provider(self) -> str:
        p = os.getenv("AI_PROVIDER", "").lower()
        if p in ("anthropic", "openai", "free", "basic"):
            return p
        if os.getenv("ANTHROPIC_API_KEY"):
            return "anthropic"
        if os.getenv("AI_BASE_URL") or os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY"):
            return "openai"
        return "free"

    async def _run_tool(self, name: str, args: dict, state: dict) -> str:
        if name != "search_housing":
            return json.dumps({"error": f"нет инструмента {name}"})
        try:
            q = tool_input_to_query(args)
            if q.rating_min is not None and q.rating_min <= 5:  # модель указала по 5-балльной шкале
                q.rating_min = q.rating_min * 2
            res = await self.search_fn(q)
            relaxed: list[str] = []
            raw = sum(v.get("count", 0) for v in res.source_stats.values())
            if res.total == 0 and raw > 0:
                # фильтры отсекли всё, хотя сайты что-то вернули — ослабляем по шагам
                for field in ("property_types", "rating_min", "rooms_min", "price_min"):
                    if getattr(q, field) not in (None, [], 0, 0.0):
                        q = q.model_copy(update={field: [] if field == "property_types" else None})
                        relaxed.append(field)
                        res = await self.search_fn(q)
                        if res.total:
                            break
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"{type(e).__name__}: {e}"[:300]}, ensure_ascii=False)
        state["search"] = res
        out = compact_result(res)
        if relaxed:
            out["note"] = f"По исходным фильтрам ничего не было; ослабил: {', '.join(relaxed)}. Скажи об этом пользователю."
        return json.dumps(out, ensure_ascii=False)

    async def chat(self, req: ChatRequest) -> ChatResponse:
        prov = self.provider
        state: dict[str, Any] = {}
        if prov == "anthropic":
            reply = await self._anthropic(req, state)
        elif prov == "openai":
            reply = await self._openai(req, state)
        elif prov == "free":
            reply = None
            # у анонимного бесплатного доступа есть лимит частоты — повторяем с паузой
            for delay in (0, 4, 10):
                await asyncio.sleep(delay)
                try:
                    reply = await self._openai(req, state, url=FREE_URL, key=None, model=os.getenv("AI_MODEL", "openai"))
                    break
                except Exception:
                    continue
            if reply is None:  # не оставляем человека без ответа
                prov = "basic"
                reply = await self._basic(req, state)
        else:
            reply = await self._basic(req, state)
        if prov != "basic":
            reply = ground_reply(reply, state.get("search"))
        return ChatResponse(reply=reply, search=state.get("search"), provider=prov)

    # --- Claude -----------------------------------------------------------
    async def _anthropic(self, req: ChatRequest, state: dict) -> str:
        url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages"
        headers = {"x-api-key": os.getenv("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        msgs: list[dict] = [{"role": m.role, "content": m.content} for m in req.messages if m.content.strip()]
        for _ in range(MAX_TOOL_ROUNDS + 1):
            body = {"model": os.getenv("AI_MODEL", "claude-sonnet-5-5"), "max_tokens": 1500,
                    "system": system_prompt(req.context), "tools": [SEARCH_TOOL], "messages": msgs}
            r = await self.client.post(url, headers=headers, json=body, timeout=120)
            if r.status_code >= 400:
                raise RuntimeError(f"Anthropic API {r.status_code}: {r.text[:300]}")
            data = r.json()
            blocks = data.get("content", [])
            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            if data.get("stop_reason") != "tool_use" or not tool_uses:
                return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            msgs.append({"role": "assistant", "content": blocks})
            results = []
            for tu in tool_uses:
                out = await self._run_tool(tu["name"], tu.get("input", {}), state)
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out})
            msgs.append({"role": "user", "content": results})
        return "Я сделал несколько поисков — результаты на карте. Уточните, что важнее всего?"

    # --- OpenAI-совместимые (OpenAI, OpenRouter, Groq, Ollama…) ------------
    async def _openai(self, req: ChatRequest, state: dict, url: str | None = None,
                      key: str | None = "env", model: str | None = None) -> str:
        if url is None:
            url = (os.getenv("AI_BASE_URL") or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        if key == "env":
            key = os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY") or "none"
        model = model or os.getenv("AI_MODEL", "gpt-4o-mini")
        headers = {"content-type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        tool = {"type": "function", "function": {"name": SEARCH_TOOL["name"], "description": SEARCH_TOOL["description"],
                                                 "parameters": SEARCH_TOOL["input_schema"]}}
        msgs: list[dict] = [{"role": "system", "content": system_prompt(req.context)}]
        msgs += [{"role": m.role, "content": m.content} for m in req.messages if m.content.strip()]
        # слабые бесплатные модели любят переспрашивать: если в последнем сообщении
        # явно есть место, первый ход модели — обязательно поиск
        force = bool(parse_request(req.messages[-1].content).get("location")) and req.messages[-1].role == "user"
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            body = {"model": model, "messages": msgs, "tools": [tool]}
            if force and round_no == 0:
                body["tool_choice"] = "required"
            r = await self.client.post(url, headers=headers, json=body, timeout=120)
            if r.status_code >= 400:
                raise RuntimeError(f"AI API {r.status_code}: {r.text[:300]}")
            msg = r.json()["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "").strip()
            msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = await self._run_tool(c["function"]["name"], args, state)
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out})
        return "Я сделал несколько поисков — результаты на карте. Уточните, что важнее всего?"

    # --- Без ключа: простой разбор фразы ----------------------------------
    async def _basic(self, req: ChatRequest, state: dict) -> str:
        text = " ".join(m.content for m in req.messages if m.role == "user")
        last = req.messages[-1].content
        args = parse_request(text, last)
        if not args.get("location"):
            return ("Бесплатный ИИ-сервис сейчас не ответил, поэтому я в простом режиме и понимаю только прямые запросы. "
                    "Напишите, например: «квартира в Стамбуле на месяц до 1000 USD, 2 комнаты» или "
                    "«отель в Лиссабоне с 10 по 12 декабря». Попробуйте и обычный вопрос чуть позже.")
        out = json.loads(await self._run_tool("search_housing", args, state))
        if "error" in out:
            return f"Не получилось выполнить поиск: {out['error']}"
        place = (out.get("area") or args["location"]).split(",")[0]
        lines = [f"Искал: {place}, {'посуточно' if args['mode'] == 'short' else 'долгосрочно'}"
                 + (f", до {args['price_max']:.0f} {args.get('currency', 'USD')}" if args.get("price_max") else "")
                 + (f", от {args['rooms_min']} комн." if args.get("rooms_min") else "")
                 + f". Найдено {out['total_found']}. Лучшие:"]
        for t in out["top"][:5]:
            price = f"{t['price']:.0f} {out['currency']}" if t["price"] else "цена на сайте"
            lines.append(f"• {t['title']} — {price} ({t['source']})")
        lines.append("Все варианты на карте. (Простой режим: бесплатный ИИ временно не ответил.)")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
_RU_MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8,
              "сен": 9, "окт": 10, "ноя": 11, "дек": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5,
              "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_CUR = {"$": "USD", "usd": "USD", "долл": "USD", "€": "EUR", "eur": "EUR", "евро": "EUR", "₺": "TRY",
        "try": "TRY", "лир": "TRY", "₽": "RUB", "rub": "RUB", "руб": "RUB", "£": "GBP", "gbp": "GBP"}


def parse_request(text: str, last: str = "") -> dict:
    """Очень простой разбор без ИИ: место, режим, бюджет, комнаты, даты."""
    t = text.lower()
    args: dict[str, Any] = {}
    stop = {"месяц", "сутки", "день", "неделю", "центре", "центр", "районе", "городе", "квартире", "отеле",
            "декабре", "январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе", "сентябре",
            "октябре", "ноябре", "center", "the", "a", "december", "january", "summer", "winter"}
    loc = None
    for m in re.finditer(r"(?:\bв|\bво|\bin|\bnear|\bрядом с)\s+([A-Za-zА-Яа-яЁё\-]+(?:[ ,]+[A-ZА-ЯЁ][\wА-Яа-яЁё\-]+)*)", text):
        cand = m.group(1).strip(" ,")
        if cand.split()[0].lower() not in stop and cand[0].isupper():
            loc = cand  # последнее осмысленное упоминание
    if loc:
        args["location"] = _ru_city_nominative(loc)
    long_kw = re.search(r"месяц|долгоср|надолго|на год|аренд|снять квартир|rent\b|monthly|lease", t)
    short_kw = re.search(r"посуточ|отел|гостиниц|хостел|ноч|сутк|hotel|hostel|night|airbnb|booking", t)
    args["mode"] = "long" if long_kw and not short_kw else "short"
    pm = re.search(r"(?:до|не дороже|max|under|below|<)\s*(\d[\d\s.,]*)\s*(k|к|тыс)?\s*([$€₺₽£]|usd|eur|try|rub|gbp|долл\w*|евро|лир\w*|руб\w*)?", t)
    if pm:
        val = float(re.sub(r"[\s,]", "", pm.group(1)).rstrip("."))
        if pm.group(2):
            val *= 1000
        args["price_max"] = val
        if pm.group(3):
            for k, v in _CUR.items():
                if pm.group(3).startswith(k):
                    args["currency"] = v
    rm = re.search(r"(\d)\s*(?:-?\s*)(?:комн|к\b|bed|br\b|спал)", t) or re.search(r"\b(одно|дву|тр[её]|четыр[её])х?комн", t)
    if rm:
        word = {"одно": 1, "дву": 2, "тре": 3, "трё": 3, "четыре": 4, "четырё": 4}
        g = rm.group(1)
        args["rooms_min"] = int(g) if g.isdigit() else word.get(g, None)
    elif re.search(r"двушк", t):
        args["rooms_min"] = 2
    elif re.search(r"тр[её]шк", t):
        args["rooms_min"] = 3
    gm = re.search(r"(\d+)\s*(?:гост|человек|чел|взросл|guests|people|adults)", t)
    together = {"вдвоём": 2, "вдвоем": 2, "втроём": 3, "втроем": 3, "вчетвером": 4, "впятером": 5}
    if gm:
        args["guests"] = int(gm.group(1))
    else:
        for w, n in together.items():
            if w in t:
                args["guests"] = n
    dm = re.search(r"(?:с\s*)?(\d{1,2})\s*(?:по|-|–|—|до)\s*(\d{1,2})\s+([а-яa-z]{3})", t)
    if dm and dm.group(3)[:3] in _RU_MONTHS:
        mon = _RU_MONTHS[dm.group(3)[:3]]
        today = date.today()
        year = today.year + (1 if mon < today.month else 0)
        try:
            args["checkin"] = date(year, mon, int(dm.group(1))).isoformat()
            args["checkout"] = date(year, mon, int(dm.group(2))).isoformat()
        except ValueError:
            pass
    if args.get("mode") == "short" and "checkin" not in args and "location" in args:
        ci = date.today() + timedelta(days=14)
        args["checkin"], args["checkout"] = ci.isoformat(), (ci + timedelta(days=2)).isoformat()
    return args


def _ru_city_nominative(s: str) -> str:
    return s  # склонения перебирает геокодер (geo.name_variants)
