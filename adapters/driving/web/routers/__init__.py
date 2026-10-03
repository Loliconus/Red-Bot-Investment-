"""Роутеры Web GUI."""

from adapters.driving.web.routers import (
    admin,
    analysis,
    app_settings,
    config,
    journal,
    system,
    trading,
)

__all__ = ["admin", "analysis", "app_settings", "config", "journal", "system", "trading"]
