"""Product gap 8 — deterministic ground-transport sub-type classifier (hermetic)."""

from __future__ import annotations

import pytest

from ai_agents.tools.ground_transport import (
    BROAD,
    CAR_RENTAL,
    PARKING,
    RAIL,
    RIDESHARE_TAXI,
    TRANSIT,
    classify_ground_transport,
    mentions_tip,
    premium_parking,
    rail_class,
    stated_hours,
)


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (("Uber",), RIDESHARE_TAXI),
        (("UBER *TRIP HELP.UBER.COM",), RIDESHARE_TAXI),
        (("Lyft", "ride to client"), RIDESHARE_TAXI),
        (("Yellow Cab Co",), RIDESHARE_TAXI),
        (("Taxi from JFK",), RIDESHARE_TAXI),
        (("Hertz",), CAR_RENTAL),
        (("Enterprise Rent-A-Car",), CAR_RENTAL),
        (("Avis Budget Group", "car rental 3 days"), CAR_RENTAL),
        (("Amtrak",), RAIL),
        (("Eurostar", "London to Paris"), RAIL),
        (("Deutsche Bahn",), RAIL),
        (("Train ticket",), RAIL),
        (("MTA MetroCard",), TRANSIT),
        (("BART",), TRANSIT),
        (("Light rail day pass",), TRANSIT),  # "light rail" is transit, not rail
        (("Caltrain",), TRANSIT),
        (("SFO Long Term Parking",), PARKING),
        (("The Parking Spot",), PARKING),
        (("SpotHero",), PARKING),
    ],
)
def test_single_family_is_classified(texts: tuple[str, ...], expected: str) -> None:
    result = classify_ground_transport(*texts)
    assert result.subtype == expected
    assert result.classified and not result.ambiguous


@pytest.mark.parametrize(
    "texts",
    [
        ("Uber to Penn Station", "train connection"),  # rideshare + rail
        ("Hertz", "parking at airport"),  # rental + parking
        ("Taxi and subway",),  # rideshare + transit
    ],
)
def test_ambiguous_fails_safe_to_broad(texts: tuple[str, ...]) -> None:
    result = classify_ground_transport(*texts)
    assert result.subtype == BROAD
    assert result.ambiguous and not result.classified


@pytest.mark.parametrize(
    "texts",
    [
        (),
        (None, "", "   "),
        ("Acme Transport LLC",),  # nothing recognised
        ("Cabernet Sauvignon",),  # never a substring match ("cab")
        ("Aviso Consulting",),  # "avis" only as a whole word
        ("Business lunch",),  # "bus" only as a whole word
        ("Trainer fee",),  # "train" only as a whole word
    ],
)
def test_unrecognised_is_broad_and_unclassified(texts: tuple[str | None, ...]) -> None:
    result = classify_ground_transport(*texts)
    assert result.subtype == BROAD
    assert not result.classified and not result.ambiguous


def test_segment_type_names_the_family() -> None:
    assert classify_ground_transport(None, segment_type="rail").subtype == RAIL
    assert classify_ground_transport("Acme", segment_type="car").subtype == CAR_RENTAL
    # The segment contradicting the words is ambiguous -> broad.
    assert classify_ground_transport("Uber", segment_type="rail").subtype == BROAD
    assert classify_ground_transport("Amtrak", segment_type="rail").subtype == RAIL


def test_classifier_is_deterministic() -> None:
    texts = ("Uber", "airport", "Amtrak", "Hertz", "SpotHero")
    for _ in range(3):
        assert classify_ground_transport(*texts) == classify_ground_transport(*texts)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Acela First Class", "first"),
        ("1st class seat", "first"),
        ("Business Premier", "business"),
        ("Standard class", "standard"),
        ("Economy fare", "standard"),
        ("Coach", "standard"),
        ("standard, upgraded to first", None),  # both named: ambiguous
        ("Amtrak", None),
    ],
)
def test_rail_class(text: str, expected: str | None) -> None:
    assert rail_class(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2h30m journey", 2.5),
        ("3.5 hours", 3.5),
        ("3-hour trip", 3.0),
        ("2 hrs", 2.0),
        ("2h then 4h", None),  # two durations: ambiguous
        ("no duration", None),
        ("99 hours", None),  # implausible
    ],
)
def test_stated_hours(text: str, expected: float | None) -> None:
    assert stated_hours(text) == expected


def test_tip_and_premium_lot_words() -> None:
    assert mentions_tip("incl. tip") and mentions_tip("Gratuity")
    assert not mentions_tip("tipping point") and not mentions_tip(None)
    assert premium_parking("Valet parking") == "valet"
    assert premium_parking("Short-term garage") == "short-term"
    assert premium_parking("Economy lot") is None
