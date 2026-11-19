"""MockInventoryProvider: wraps existing query helpers behind a clean interface.

Intentional design note: flights and hotels have distinct parameter signatures
(origin/destination/date vs. city/check_in/check_out), so we expose two typed
methods rather than a single generic search(). This diverges from the idealized
search(..., segment_type=) in the design doc but is more type-safe and explicit.
The P4-T4 Inventory tool can call either method directly.
"""

from __future__ import annotations

import logging

from services.mock_inventory.dataset import Flight, Hotel
from services.mock_inventory.query import search_flights, search_hotels

logger = logging.getLogger(__name__)


class MockInventoryProvider:
    """Provider wrapping the P3-T5 static mock travel inventory.

    Returns frozen Flight / Hotel dataclasses (plain data, not ORM objects).
    Single adapter — no real/local split needed (data is always local).
    """

    def search_flights(
        self,
        origin: str,
        destination: str,
        departure_date: str,
    ) -> list[Flight]:
        """Return flights matching origin, destination, and departure_date (ISO date).

        Case-insensitive on airport codes. Returns empty list if none match.
        """
        logger.debug(
            "MockInventoryProvider.search_flights: %s→%s on %s",
            origin,
            destination,
            departure_date,
        )
        return search_flights(origin=origin, destination=destination, departure_date=departure_date)

    def search_hotels(
        self,
        city: str,
        check_in_date: str,
        check_out_date: str,
    ) -> list[Hotel]:
        """Return hotels in city with overlapping check_in/check_out dates (ISO dates).

        Case-insensitive on city. Returns empty list if none match.
        """
        logger.debug(
            "MockInventoryProvider.search_hotels: %s %s→%s",
            city,
            check_in_date,
            check_out_date,
        )
        return search_hotels(city=city, check_in_date=check_in_date, check_out_date=check_out_date)


def get_inventory_provider() -> MockInventoryProvider:
    """Return the mock inventory provider.

    Single adapter — no mode selector needed; the dataset is always local.
    Added for factory symmetry with the other four providers.
    """
    return MockInventoryProvider()
