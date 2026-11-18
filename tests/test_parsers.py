import json
from datetime import date

from app.aggregator import apply_filters, deduplicate
from app.ai import parse_request, tool_input_to_query
from app.geo import convert, name_variants
from app.models import Listing, Mode, SearchQuery
from app.sources.base import dig, to_float
from app.sources.craigslist import nearest_area, parse_search
from app.sources.jsonld import extract_listings, render_template
from app.sources.osm import OSMSource
from app.sources.serpapi_hotels import SerpApiHotelsSource


def ld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


def test_to_float_formats():
    assert to_float("$2,875") == 2875
    assert to_float("1.234,56 €") == 1234.56
    assert to_float("84 000 TRY") == 84000
    assert to_float(None) is None


def test_dig():
    assert dig({"a": [{"b": 5}]}, "a.0.b") == 5
    assert dig({"a": 1}, "x.y", "d") == "d"


def test_jsonld_realestate_listing():
    html = ld({"@type": "SearchResultsPage", "mainEntity": {"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "item": {"@type": "RealEstateListing", "name": "Flat A", "url": "/l/1",
         "image": "https://img/1.jpg",
         "offers": {"@type": "Offer", "price": 750, "priceCurrency": "EUR", "itemOffered": {
             "@type": "Apartment", "geo": {"latitude": 52.5, "longitude": 13.4},
             "floorSize": {"value": 60}, "numberOfBedrooms": {"value": 2}}}}}]}}) + ld(
        {"@type": ["Apartment", "Product"], "name": "Summary",
         "offers": {"@type": "AggregateOffer", "lowPrice": 0}})
    items = extract_listings(html, source="x", mode=Mode.long, base_url="https://site.com/s")
    assert len(items) == 1  # агрегированная «шапка» отброшена
    l = items[0]
    assert (l.price, l.currency, l.rooms, l.area_m2, l.lat) == (750, "EUR", 2, 60, 52.5)
    assert l.url == "https://site.com/l/1"


def test_craigslist_parse_merges_ld_and_html():
    html = ld({"@type": "ItemList", "itemListElement": [
        {"item": {"name": "Nice 2BR", "@type": "Apartment", "latitude": 40.7, "longitude": -73.9, "numberOfBedrooms": 2}}]})
    html += ('<li class="cl-static-search-result" title="Nice 2BR"><a href="https://cl/1">'
             '<div class="title">Nice 2BR</div><div class="price">$2,500</div></a></li>')
    (l,) = parse_search(html, Mode.long, "USD")
    assert (l.price, l.rooms, l.lat, l.url) == (2500, 2, 40.7, "https://cl/1")


def test_nearest_area():
    areas = [{"Hostname": "newyork", "Latitude": 40.7, "Longitude": -74.0},
             {"Hostname": "berlin", "Latitude": 52.5, "Longitude": 13.4}]
    assert nearest_area(areas, 52.52, 13.41)["Hostname"] == "berlin"
    assert nearest_area(areas, 0, 0) is None


def test_osm_parse():
    data = {"elements": [{"type": "node", "id": 1, "lat": 1, "lon": 2, "tags": {"name": "H", "tourism": "hotel", "stars": "4"}},
                         {"type": "way", "id": 2, "center": {"lat": 3, "lon": 4}, "tags": {"tourism": "hostel"}}]}
    items = OSMSource.parse(data)
    assert len(items) == 1 and items[0].rating == 8


def test_serpapi_parse_also_on():
    data = {"search_parameters": {"currency": "USD"}, "properties": [{
        "name": "Hotel X", "property_token": "t1", "type": "hotel", "overall_rating": 4.5, "reviews": 100,
        "rate_per_night": {"extracted_lowest": 120}, "gps_coordinates": {"latitude": 1, "longitude": 2},
        "prices": [{"source": "Booking.com", "rate_per_night": {"extracted_lowest": 125}, "link": "b"},
                   {"source": "Agoda", "rate_per_night": {"extracted_lowest": 120}, "link": "a"}]}]}
    (l,) = SerpApiHotelsSource.parse(data)
    assert l.price == 120 and l.rating == 9 and len(l.also_on) == 2


def L(**kw):
    base = dict(id="x", source="a", mode=Mode.short, title="Hotel Alpha", lat=41.0, lon=29.0)
    base.update(kw)
    return Listing(**base)


def test_dedup_keeps_cheapest_and_lists_others():
    a = L(id="1", source="booking", title="Hotel Alpha Istanbul", price_conv=100, url="b")
    b = L(id="2", source="google_hotels", title="Alpha Istanbul", lat=41.0005, price_conv=90, url="g")
    c = L(id="3", source="osm", title="Totally Different", lat=41.0005)
    out = deduplicate([a, b, c])
    assert len(out) == 2
    merged = next(x for x in out if x.id == "1")
    assert merged.price_conv == 90 and merged.source == "google_hotels"
    assert merged.also_on[0]["source"] == "booking"


def test_filters():
    q = SearchQuery(location="xx", price_max=100, rooms_min=2)
    items = [L(id="1", price_conv=90, rooms=2), L(id="2", price_conv=150, rooms=3), L(id="3", price_conv=50, rooms=1)]
    assert [i.id for i in apply_filters(items, q)] == ["1"]


def test_convert():
    rates = {"USD": 1, "EUR": 0.9, "TRY": 40}
    assert convert(80, "TRY", "USD", rates) == 2.0
    assert convert(10, "XXX", "USD", rates) is None


def test_template_uses_english_city():
    from app.models import GeoArea
    area = GeoArea(name="Istanbul, Turkey", lat=41, lon=29, bbox=(40, 28, 42, 30), city="Istanbul", country_code="tr")
    q = SearchQuery(location="Стамбуле", mode=Mode.long)
    assert render_template("https://s/?l={location_q}&p={page}", q, area, 2) == "https://s/?l=Istanbul&p=2"


def test_name_variants():
    assert "Стамбул" in name_variants("Стамбуле")
    assert "Москва" in name_variants("Москве")
    assert name_variants("Berlin") == ["Berlin"]


def test_parse_request_ru():
    a = parse_request("хочу снять двушку в Стамбуле на месяц до 1000$")
    assert a["location"] == "Стамбуле" and a["mode"] == "long" and a["price_max"] == 1000
    assert a["rooms_min"] == 2 and a["currency"] == "USD"
    b = parse_request("отель в Лиссабоне с 10 по 12 декабря на 3 человек")
    assert b["mode"] == "short" and b["guests"] == 3 and b["checkin"].endswith("-12-10")


def test_tool_input_to_query():
    q = tool_input_to_query({"location": "Paris", "mode": "short", "checkin": "2027-01-05", "currency": "eur", "price_max": None})
    assert q.checkin == date(2027, 1, 5) and q.currency == "EUR" and q.price_max is None


def test_clean_place():
    from app.geo import clean_place
    assert clean_place("Lisbon city center") == "Lisbon"
    assert clean_place("центр Стамбула") == "Стамбула"
    assert clean_place("Kadikoy, Istanbul") == "Kadikoy, Istanbul"


def test_parse_request_center_and_together():
    a = parse_request("Еду в Лиссабон 10-14 декабря вдвоём, хочу квартиру в центре до 120 евро")
    assert a["location"] == "Лиссабон" and a["guests"] == 2 and a["checkin"].endswith("-12-10")
    assert a["checkout"].endswith("-12-14") and a["currency"] == "EUR"


def test_flag_outliers():
    from app.aggregator import flag_outliers
    items = [L(id=str(i), price_conv=1000 + i * 10, rooms=1) for i in range(10)] + [L(id="x", price_conv=150, rooms=1)]
    flag_outliers(items)
    assert items[-1].flags == ["too_cheap"] and not items[0].flags
