"""Query helpers for the mock travel inventory.

Provides simple in-memory filters over the static FLIGHTS and HOTELS datasets.
This is a data helper only — NOT the Phase-4 Inventory agent tool.

Locations resolve through locations.py first, so "Chicago", "Chicago (ORD)" and
"ord" all hit the ORD fixtures and a hotel search for "JFK" finds the New York
hotels. Fixed fixtures always win; only when no fixture matches does a search
fall back to the deterministic synthesized inventory (synthetic.py) for a known
route/city. Unknown routes/cities still return [].

Functions:
  search_flights(origin, destination, departure_date) -> list[Flight]
  search_hotels(city, check_in_date, check_out_date)  -> list[Hotel]
"""

from __future__ import annotations

from services.mock_inventory.dataset import FLIGHTS, HOTELS, Flight, Hotel
from services.mock_inventory.locations import resolve_airport, resolve_city
from services.mock_inventory.synthetic import synthesize_flights, synthesize_hotels


def search_flights(
    origin: str,
    destination: str,
    departure_date: str,
) -> list[Flight]:
    """Return all flights matching origin, destination, and departure_date.

    Matching is case-insensitive on origin/destination airport codes (city names
    and "City (CODE)" labels resolve to their code) and exact ISO date string
    match on departure_date. Fixture flights win; otherwise a known route yields
    deterministic synthesized flights for the date.

    Returns an empty list when nothing matches and the route is unknown —
    callers should handle that case.
    """
    origin_upper = resolve_airport(origin) or origin.upper()
    destination_upper = resolve_airport(destination) or destination.upper()
    fixtures = [
        f
        for f in FLIGHTS
        if f.origin.upper() == origin_upper
        and f.destination.upper() == destination_upper
        and f.departure_date == departure_date
    ]
    if fixtures:
        return fixtures
    return synthesize_flights(origin_upper, destination_upper, departure_date)


def search_hotels(
    city: str,
    check_in_date: str,
    check_out_date: str,
) -> list[Hotel]:
    """Return all hotels in *city* with overlapping check-in/check-out dates.

    A hotel overlaps when its check_in_date < requested check_out_date AND its
    check_out_date > requested check_in_date (standard interval overlap).
    City matching is case-insensitive; airport codes resolve to their city.
    Fixture hotels win; otherwise a known city yields deterministic synthesized
    hotels for the stay (>= 1 night).

    Returns an empty list when no hotels match.
    """
    canonical = resolve_city(city) or city
    city_lower = canonical.lower()
    fixtures = [
        h
        for h in HOTELS
        if h.city.lower() == city_lower
        and h.check_in_date < check_out_date
        and h.check_out_date > check_in_date
    ]
    if fixtures:
        return fixtures
    return synthesize_hotels(canonical, check_in_date, check_out_date)
