"""Smoke tests for services/mock_inventory."""


def test_factory_returns_provider() -> None:
    from services.mock_inventory import MockInventoryProvider, get_inventory_provider

    p = get_inventory_provider()
    assert isinstance(p, MockInventoryProvider)


def test_search_flights_returns_results_from_existing_dataset() -> None:
    """Provider search_flights returns results from the P3-T5 dataset — no duplicate data."""
    from services.mock_inventory import Flight, get_inventory_provider
    from services.mock_inventory.dataset import FLIGHTS

    p = get_inventory_provider()
    # SFO→JFK exists in the dataset with departure_date="2026-06-10"
    results = p.search_flights("SFO", "JFK", "2026-06-10")
    for r in results:
        assert isinstance(r, Flight)
        # All returned flights must exist in the canonical FLIGHTS list (no second dataset)
        assert r in FLIGHTS, "Provider returned a flight not in the canonical P3-T5 dataset!"


def test_search_hotels_returns_results_from_existing_dataset() -> None:
    from services.mock_inventory import Hotel, get_inventory_provider
    from services.mock_inventory.dataset import HOTELS

    p = get_inventory_provider()
    # New York hotels have check_in_date="2026-06-10", check_out_date="2026-06-14"
    results = p.search_hotels("New York", "2026-06-10", "2026-06-14")
    for r in results:
        assert isinstance(r, Hotel)
        assert r in HOTELS, "Provider returned a hotel not in the canonical P3-T5 dataset!"


def test_search_no_match_returns_empty() -> None:
    from services.mock_inventory import get_inventory_provider

    p = get_inventory_provider()
    flights = p.search_flights("XXX", "YYY", "2099-01-01")
    assert flights == []


def test_backward_compat_direct_functions_still_work() -> None:
    """search_flights and search_hotels direct exports still work (backward compat)."""
    from services.mock_inventory import search_flights, search_hotels

    # Just calling them should not raise
    f = search_flights("SFO", "JFK", "2026-06-10")
    h = search_hotels("New York", "2026-06-10", "2026-06-14")
    assert isinstance(f, list)
    assert isinstance(h, list)
