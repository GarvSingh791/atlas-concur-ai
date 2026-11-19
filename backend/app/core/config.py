"""
backend/app/core/config — thin re-export of the settings layer.

Import settings through core so app code stays decoupled from the config
package path, and the single import line here is the only seam to change if
the settings module ever moves.
"""

from config.settings import Settings, get_settings, settings  # noqa: F401

__all__ = ["Settings", "get_settings", "settings"]
