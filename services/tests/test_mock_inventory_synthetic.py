"""Mock inventory: location resolution + deterministic synthesized options.

The fixed fixtures only cover a few 2026 dates, so any real planner date used to
return "No options found". Known routes/cities now synthesize stable options for
ANY date, while fixture searches stay byte-identical.
"""

from __future__ import annotations

import pytest

from services.mock_inventory import search_flights, search_hotels
from services.mock_inventory.dataset import FLIGHTS, HOTELS
from services.mock_inventory.locations import resolve_airport, resolve_city


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("SFO", "SFO"),
        ("sfo", "SFO"),
        ("Chicago", "ORD"),
        ("Chicago (ORD)", "ORD"),
        ("Chicago, IL", "ORD"),
        ("New York (NYC)", "JFK"),
        ("NYC", "JFK"),
        ("London Heathrow", "LHR"),
        ("Boston", None),
        ("BOS", None),
        ("", None),
        (None, None),
    ],
)
def test_resolve_airport(raw: str | None, code: str | None) -> None:
    assert resolve_airport(raw) == code


def test_resolve_city() -> None:
    assert resolve_city("JFK") == "New York"
    assert resolve_city("Chicago (ORD)") == "Chicago"
    assert resolve_city("Atlantis") is None


def test_fixture_searches_are_unchanged() -> None:
    flights = search_flights("SFO", "JFK", "2026-06-10")
    assert {f.flight_id for f in flights} == {
        "UA201-2026-06-10",
        "DL408-2026-06-10",
        "UA201B-2026-06-10",
    }
    assert all(f in FLIGHTS for f in flights)
    hotels = search_hotels("New York", "2026-06-10", "2026-06-14")
    assert {h.hotel_id for h in hotels} == {
        "NYC-MARRIOTT-TIMES-001",
        "NYC-HILTON-MIDTOWN-001",
        "NYC-BOUTIQUE-001",
    }


def test_city_names_and_codes_resolve_to_fixtures() -> None:
    assert search_flights("San Francisco", "New York", "2026-06-10") == search_flights(
        "SFO", "JFK", "2026-06-10"
    )
    # A hotel search by the destination airport code finds the city's hotels.
    assert search_hotels("JFK", "2026-06-10", "2026-06-14") == search_hotels(
        "New York", "2026-06-10", "2026-06-14"
    )


def test_any_date_on_a_known_route_synthesizes_deterministic_flights() -> None:
    first = search_flights("ORD", "JFK", "2026-11-17")
    again = search_flights("Chicago", "New York", "2026-11-17")
    assert first == again  # stable across calls and spellings
    assert len(first) == 3
    assert {f.cabin_class for f in first} == {"economy", "business"}
    assert all(f.origin == "ORD" and f.destination == "JFK" for f in first)
    assert all(f.departure_date == "2026-11-17" for f in first)
    assert len({f.flight_id for f in first}) == 3
    assert not set(f.flight_id for f in first) & {f.flight_id for f in FLIGHTS}
    # Business on a 2.5h hop is the out-of-policy material; economy is cheaper.
    economy = [f.price_usd for f in first if f.cabin_class == "economy"]
    business = [f.price_usd for f in first if f.cabin_class == "business"]
    assert max(economy) < min(business)


def test_prices_vary_by_date_but_not_by_call() -> None:
    a = [f.price_usd for f in search_flights("SFO", "JFK", "2026-12-01")]
    b = [f.price_usd for f in search_flights("SFO", "JFK", "2026-12-02")]
    assert a == [f.price_usd for f in search_flights("SFO", "JFK", "2026-12-01")]
    assert a != b


def test_synthesized_hotels_cover_the_stay_with_one_over_cap() -> None:
    hotels = search_hotels("New York", "2026-11-17", "2026-11-19")
    assert len(hotels) == 3
    assert all(h.check_in_date == "2026-11-17" for h in hotels)
    assert all(h.check_out_date == "2026-11-19" for h in hotels)
    rates = sorted(h.nightly_rate_usd for h in hotels)
    assert rates[1] < 300.0 < rates[2]  # two under the Tier 1 cap, one over
    chicago = search_hotels("Chicago", "2026-11-17", "2026-11-18")
    assert sorted(h.nightly_rate_usd for h in chicago)[1] < 225.0  # Tier 2 cap


def test_synthetic_city_caps_match_corpus_figures() -> None:
    """Issue 6: the synthesized rates scale with the canonical TEP-FY26 caps
    (figures.LODGING_CAP via city_tier), never a stale stub figure."""
    from database.seeds.corpus import figures as F
    from services.mock_inventory import synthetic

    for city, cap in synthetic._CITY_CAP.items():
        assert cap == float(F.LODGING_CAP[F.city_tier(city)]), city
    assert synthetic._DEFAULT_CAP == float(F.LODGING_CAP["tier3"])


@pytest.mark.parametrize(
    ("origin", "destination", "day"),
    [
        ("BOS", "LAX", "2030-01-01"),  # unknown airports
        ("SFO", "SFO", "2026-11-17"),  # same airport
        ("SFO", "JFK", "not-a-date"),
    ],
)
def test_unknown_or_invalid_routes_stay_empty(origin: str, destination: str, day: str) -> None:
    assert search_flights(origin, destination, day) == []


def test_zero_night_or_unknown_city_hotel_search_is_empty() -> None:
    assert search_hotels("New York", "2026-11-17", "2026-11-17") == []
    assert search_hotels("Atlantis", "2026-11-17", "2026-11-19") == []
    assert HOTELS  # fixtures untouched
