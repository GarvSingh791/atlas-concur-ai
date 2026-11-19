"""Deterministic synthesized inventory for dates the fixed fixtures do not cover.

The static FLIGHTS / HOTELS fixtures (dataset.py) only exist for a handful of
2026 dates, so a real request ("next Tuesday") would otherwise always return
"no options". For the airports/cities the mock inventory knows (locations.py),
this module synthesizes a stable set of options for ANY date:

  * Same inputs -> byte-identical options (prices are seeded from a SHA-256 of
    route + date, never from random/time), so a re-run on selection finds the
    same option_id and ranking stays deterministic.
  * Every synthesized set keeps the policy test material of the fixtures:
    two in-policy economy fares (preferred carriers) + one business fare, and
    two under-cap hotels (preferred brands) + one over-cap independent hotel.
  * Unknown routes/cities still return [] — the inventory never invents a
    destination it does not serve.

Fixtures always win: query.py only falls back to this module when no fixture
matches, so every existing fixture-based test/flow is unchanged.
"""

from __future__ import annotations

import hashlib
from datetime import date

from services.mock_inventory.dataset import Flight, Hotel
from services.mock_inventory.locations import AIRPORT_CITIES

# Scheduled block time (hours) per unordered airport pair.
_ROUTE_HOURS: dict[frozenset[str], float] = {
    frozenset({"SFO", "JFK"}): 5.5,
    frozenset({"SFO", "ORD"}): 4.3,
    frozenset({"JFK", "ORD"}): 2.5,
    frozenset({"SFO", "LHR"}): 10.5,
    frozenset({"JFK", "LHR"}): 7.0,
    frozenset({"ORD", "LHR"}): 8.2,
}

_INTERNATIONAL = frozenset({"LHR"})

# (airline, IATA carrier prefix)
_DOMESTIC_CARRIERS: tuple[tuple[str, str], ...] = (
    ("United Airlines", "UA"),
    ("Delta Air Lines", "DL"),
)
_INTL_CARRIERS: tuple[tuple[str, str], ...] = (
    ("Lufthansa", "LH"),
    ("United Airlines", "UA"),
)

# Nightly cap the seeded HOTEL-CAP rule applies per city.  Mirrors the canonical
# TEP-FY26 figures (database/seeds/corpus/figures.py LODGING_CAP via city_tier:
# Tier 1 = 300, Tier 2 = 225, Tier 3 = 160) — services never import database/,
# so this copy is parity-tested in services/tests/test_mock_inventory_synthetic.py.
_CITY_CAP: dict[str, float] = {
    "New York": 300.0,
    "San Francisco": 300.0,
    "London": 300.0,
    "Chicago": 225.0,
}
_DEFAULT_CAP = 160.0  # Tier 3 — all other locations


def _seed(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _jitter(seed: int, slot: int, spread: float) -> float:
    """A stable multiplier in [1 - spread, 1 + spread] for a given slot."""
    bucket = (seed >> (slot * 6)) % 41  # 0..40
    return 1.0 + spread * (bucket - 20) / 20.0


def _valid_iso(raw: str) -> bool:
    try:
        date.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    return True


def _add_hours(hhmm: str, hours: float) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    total = (h * 60 + m + int(round(hours * 60))) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def synthesize_flights(origin: str, destination: str, departure_date: str) -> list[Flight]:
    """Return 3 deterministic flights for a known route on any valid ISO date."""
    route = frozenset({origin, destination})
    hours = _ROUTE_HOURS.get(route)
    if hours is None or origin == destination or not _valid_iso(departure_date):
        return []

    seed = _seed("flight", origin, destination, departure_date)
    base = round(hours * 52 + 40, 0)  # economy anchor fare, USD
    carriers = _INTL_CARRIERS if route & _INTERNATIONAL else _DOMESTIC_CARRIERS
    flight_base = 100 + seed % 800  # stable 3-digit flight numbers per route+date
    route_tag = f"{origin}{destination}"

    specs = (
        # (carrier index, cabin, departure, fare multiplier, sustainability, number offset)
        (0, "economy", "07:00", 1.00, 3, 0),
        (1, "economy", "09:30", 0.93, 2, 1),
        (0, "business", "07:00", 4.50, 4, 0),
    )
    flights: list[Flight] = []
    for slot, (ci, cabin, dep, mult, sustain, offset) in enumerate(specs):
        airline, prefix = carriers[ci]
        number = f"{prefix}{flight_base + offset}"
        suffix = "B" if cabin == "business" else ""
        price = round(base * mult * _jitter(seed, slot, 0.08), 0)
        flights.append(
            Flight(
                flight_id=f"{number}{suffix}-{route_tag}-{departure_date}",
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                departure_time=dep,
                arrival_time=_add_hours(dep, hours),
                duration_hours=hours,
                cabin_class=cabin,
                airline=airline,
                flight_number=number,
                price_usd=float(price),
                sustainability_score=sustain,
                is_direct=True,
                notes="Synthesized mock fare (deterministic per route + date)",
            )
        )
    return flights


def synthesize_hotels(city: str, check_in_date: str, check_out_date: str) -> list[Hotel]:
    """Return 3 deterministic hotels for a known city and a stay of >= 1 night."""
    if city not in AIRPORT_CITIES.values():
        return []
    if not (_valid_iso(check_in_date) and _valid_iso(check_out_date)):
        return []
    if check_out_date <= check_in_date:
        return []

    cap = _CITY_CAP.get(city, _DEFAULT_CAP)
    seed = _seed("hotel", city, check_in_date)
    code = next(c for c, name in AIRPORT_CITIES.items() if name == city)
    specs = (
        # (brand, name suffix, room type, fraction of cap, sustainability)
        ("Marriott International", "Marriott Downtown", "Standard", 0.90, 2),
        ("Hilton", "Hilton City Centre", "Standard", 0.86, 2),
        ("Independent", "Grand Boutique", "Deluxe", 1.55, 4),
    )
    hotels: list[Hotel] = []
    for slot, (brand, label, room, frac, sustain) in enumerate(specs):
        rate = cap * frac * _jitter(seed, slot, 0.04)
        # Keep in-policy rates strictly under the cap and the boutique strictly over it.
        rate = min(rate, cap - 1.0) if frac < 1.0 else max(rate, cap + 50.0)
        brand_tag = brand.split()[0].upper()
        hotels.append(
            Hotel(
                hotel_id=f"{code}-{brand_tag}-SYN-{check_in_date}",
                city=city,
                name=f"{city} {label}",
                brand=brand,
                check_in_date=check_in_date,
                check_out_date=check_out_date,
                nightly_rate_usd=float(round(rate, 0)),
                room_type=room,
                sustainability_score=sustain,
                notes="Synthesized mock rate (deterministic per city + check-in)",
            )
        )
    return hotels
