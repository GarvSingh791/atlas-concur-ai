"""Static mock travel inventory dataset for Atlas P3-T5.

Contains FLIGHTS and HOTELS lists with varied price, cabin class, supplier, and
sustainability score, covering the seeded SFO↔JFK route plus a few others.

Each dataset intentionally includes at least one out-of-policy option so later
policy in/out testing has material:
  - One business-class short-haul flight (< 6h, violates AIR-CABIN rule)
  - One NYC hotel at USD 400/night (exceeds the USD 300 Tier 1 cap per HOTEL-CAP rule)

This is a data module only — NOT the Phase-4 Inventory agent tool.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Flight:
    flight_id: str
    origin: str
    destination: str
    departure_date: str  # ISO date "YYYY-MM-DD"
    departure_time: str  # "HH:MM"
    arrival_time: str  # "HH:MM"
    duration_hours: float  # scheduled gate-to-gate
    cabin_class: str  # economy / premium_economy / business
    airline: str
    flight_number: str
    price_usd: float
    sustainability_score: int  # 1 (low-emission) – 5 (high-emission)
    is_direct: bool
    notes: str = ""


@dataclass(frozen=True)
class Hotel:
    hotel_id: str
    city: str
    name: str
    brand: str  # Marriott International / Hilton / Independent / etc.
    check_in_date: str  # ISO date "YYYY-MM-DD"
    check_out_date: str  # ISO date "YYYY-MM-DD"
    nightly_rate_usd: float
    room_type: str  # Standard / Deluxe / Suite
    sustainability_score: int  # 1 (low-impact) – 5 (high-impact)
    notes: str = ""


# ---------------------------------------------------------------------------
# Flights
# ---------------------------------------------------------------------------

FLIGHTS: list[Flight] = [
    # ── SFO → JFK (5.5h, in-policy economy options) ────────────────────────
    Flight(
        flight_id="UA201-2026-06-10",
        origin="SFO",
        destination="JFK",
        departure_date="2026-06-10",
        departure_time="07:00",
        arrival_time="15:30",
        duration_hours=5.5,
        cabin_class="economy",
        airline="United Airlines",
        flight_number="UA201",
        price_usd=320.00,
        sustainability_score=3,
        is_direct=True,
        notes="Preferred carrier, in-policy economy",
    ),
    Flight(
        flight_id="DL408-2026-06-10",
        origin="SFO",
        destination="JFK",
        departure_date="2026-06-10",
        departure_time="09:15",
        arrival_time="17:45",
        duration_hours=5.5,
        cabin_class="economy",
        airline="Delta Air Lines",
        flight_number="DL408",
        price_usd=298.00,
        sustainability_score=2,
        is_direct=True,
        notes="Preferred carrier, lowest-emission direct option",
    ),
    # ── SFO → JFK (5.5h, OUT-OF-POLICY: business class on short-haul) ──────
    Flight(
        flight_id="UA201B-2026-06-10",
        origin="SFO",
        destination="JFK",
        departure_date="2026-06-10",
        departure_time="07:00",
        arrival_time="15:30",
        duration_hours=5.5,
        cabin_class="business",
        airline="United Airlines",
        flight_number="UA201",
        price_usd=1450.00,
        sustainability_score=4,
        is_direct=True,
        notes="OUT-OF-POLICY: business class on <6h flight (AIR-CABIN rule)",
    ),
    # ── JFK → SFO (5.5h return, economy options) ────────────────────────────
    Flight(
        flight_id="UA202-2026-06-14",
        origin="JFK",
        destination="SFO",
        departure_date="2026-06-14",
        departure_time="08:00",
        arrival_time="11:30",
        duration_hours=5.5,
        cabin_class="economy",
        airline="United Airlines",
        flight_number="UA202",
        price_usd=315.00,
        sustainability_score=3,
        is_direct=True,
        notes="Preferred carrier, in-policy economy return",
    ),
    Flight(
        flight_id="B6800-2026-06-14",
        origin="JFK",
        destination="SFO",
        departure_date="2026-06-14",
        departure_time="06:30",
        arrival_time="10:05",
        duration_hours=5.6,
        cabin_class="economy",
        airline="JetBlue",
        flight_number="B6800",
        price_usd=270.00,
        sustainability_score=3,
        is_direct=True,
        notes="Non-preferred carrier but cheapest; within 10% triggers AIR-PREF check",
    ),
    # ── SFO → LHR (10.5h, valid business class route) ───────────────────────
    Flight(
        flight_id="LH455-2026-07-01",
        origin="SFO",
        destination="LHR",
        departure_date="2026-07-01",
        departure_time="16:00",
        arrival_time="10:30",
        duration_hours=10.5,
        cabin_class="business",
        airline="Lufthansa",
        flight_number="LH455",
        price_usd=3200.00,
        sustainability_score=4,
        is_direct=True,
        notes="Preferred carrier, in-policy business class on >10h route",
    ),
    Flight(
        flight_id="LH455E-2026-07-01",
        origin="SFO",
        destination="LHR",
        departure_date="2026-07-01",
        departure_time="16:00",
        arrival_time="10:30",
        duration_hours=10.5,
        cabin_class="economy",
        airline="Lufthansa",
        flight_number="LH455",
        price_usd=820.00,
        sustainability_score=2,
        is_direct=True,
        notes="Economy on long-haul, lower sustainability impact, lower cost",
    ),
    # ── JFK → ORD (2.5h, domestic short-haul) ───────────────────────────────
    Flight(
        flight_id="UA110-2026-08-15",
        origin="JFK",
        destination="ORD",
        departure_date="2026-08-15",
        departure_time="11:00",
        arrival_time="12:30",
        duration_hours=2.5,
        cabin_class="economy",
        airline="United Airlines",
        flight_number="UA110",
        price_usd=180.00,
        sustainability_score=3,
        is_direct=True,
        notes="Short domestic hop, in-policy economy",
    ),
]

# ---------------------------------------------------------------------------
# Hotels
# ---------------------------------------------------------------------------

HOTELS: list[Hotel] = [
    # ── New York City (JFK dest, Tier 1 city cap = $300) ────────────────────
    Hotel(
        hotel_id="NYC-MARRIOTT-TIMES-001",
        city="New York",
        name="New York Marriott Marquis",
        brand="Marriott International",
        check_in_date="2026-06-10",
        check_out_date="2026-06-14",
        nightly_rate_usd=239.00,
        room_type="Standard",
        sustainability_score=2,
        notes="Preferred brand, within Tier 1 cap of $300/night",
    ),
    Hotel(
        hotel_id="NYC-HILTON-MIDTOWN-001",
        city="New York",
        name="New York Hilton Midtown",
        brand="Hilton",
        check_in_date="2026-06-10",
        check_out_date="2026-06-14",
        nightly_rate_usd=245.00,
        room_type="Standard",
        sustainability_score=2,
        notes="Preferred brand, within Tier 1 cap of $300/night",
    ),
    # ── New York City (OUT-OF-POLICY: exceeds $300 Tier 1 cap) ──────────────
    Hotel(
        hotel_id="NYC-BOUTIQUE-001",
        city="New York",
        name="The Luxury Loft NYC",
        brand="Independent",
        check_in_date="2026-06-10",
        check_out_date="2026-06-14",
        nightly_rate_usd=400.00,
        room_type="Deluxe",
        sustainability_score=4,
        notes="OUT-OF-POLICY: $400/night exceeds Tier 1 cap of $300 (HOTEL-CAP rule)",
    ),
    # ── San Francisco (Tier 1 city cap = $300) ───────────────────────────────
    Hotel(
        hotel_id="SFO-MARRIOTT-UNION-001",
        city="San Francisco",
        name="San Francisco Marriott Union Square",
        brand="Marriott International",
        check_in_date="2026-06-14",
        check_out_date="2026-06-15",
        nightly_rate_usd=228.00,
        room_type="Standard",
        sustainability_score=2,
        notes="Preferred brand, within Tier-1 cap",
    ),
    # ── Chicago (Tier 2 cap = $225) ─────────────────────────────────────────
    Hotel(
        hotel_id="ORD-HILTON-CHICAGO-001",
        city="Chicago",
        name="Hilton Chicago",
        brand="Hilton",
        check_in_date="2026-08-15",
        check_out_date="2026-08-17",
        nightly_rate_usd=175.00,
        room_type="Standard",
        sustainability_score=2,
        notes="Preferred brand, within Tier 2 cap of $225/night",
    ),
    Hotel(
        hotel_id="ORD-INDEP-CHICAGO-001",
        city="Chicago",
        name="Downtown Chicago Suites",
        brand="Independent",
        check_in_date="2026-08-15",
        check_out_date="2026-08-17",
        nightly_rate_usd=165.00,
        room_type="Standard",
        sustainability_score=3,
        notes="Non-preferred but cheapest, within Tier 2 cap",
    ),
    # ── London (Tier 1 city cap = $300) ─────────────────────────────────────
    Hotel(
        hotel_id="LHR-MARRIOTT-HEATHROW-001",
        city="London",
        name="London Heathrow Marriott",
        brand="Marriott International",
        check_in_date="2026-07-01",
        check_out_date="2026-07-05",
        nightly_rate_usd=248.00,
        room_type="Standard",
        sustainability_score=2,
        notes="Preferred brand, within Tier 1 London cap of $300/night",
    ),
]
