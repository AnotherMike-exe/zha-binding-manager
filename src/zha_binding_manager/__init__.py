"""ZHA Binding Manager — query, edit, validate, and apply Zigbee binding and
group configurations against a Home Assistant / ZHA network."""

from .manager import main

__version__ = "0.2.0"
__all__ = ["main", "__version__"]
