"""Mock travel inventory service — static dataset + query helpers.

P3-T5 seed populates a static FLIGHTS / HOTELS dataset here.
Use search_flights() and search_hotels() to query the inventory directly,
or use MockInventoryProvider / get_inventory_provider() for the provider interface.

This is a data helper only — NOT the Phase-4 Inventory agent tool (P4-T4).
"""

from services.mock_inventory.dataset import Flight, Hotel
from services.mock_inventory.provider import MockInventoryProvider, get_inventory_provider
from services.mock_inventory.query import search_flights, search_hotels

__all__ = [
    "Flight",
    "Hotel",
    "search_flights",
    "search_hotels",
    "MockInventoryProvider",
    "get_inventory_provider",
]
