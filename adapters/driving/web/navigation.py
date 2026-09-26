"""Sidebar создаётся из конфигурации, base.html не меняется при расширении."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NavigationItem:
    key: str
    label: str
    path: str
    icon: str
    priority: str


NAVIGATION = (
    NavigationItem("control", "Пульт управления", "/control", "◉", "P0"),
    NavigationItem("settings", "Настройки запуска", "/settings", "⚙", "P0"),
    NavigationItem("dashboard", "Дашборд", "/", "▦", "P1"),
    NavigationItem("chart", "График", "/chart", "⌁", "P1"),
    NavigationItem("instruments", "Инструменты и ТА", "/instruments", "◫", "P2"),
    NavigationItem("risk", "Риск-модуль", "/risk", "⚠", "P0"),
    NavigationItem("journal", "Журнал и самоанализ", "/journal", "≡", "P2"),
    NavigationItem("backtest", "Backtest Runner", "/backtest", "◈", "P2"),
    NavigationItem("storage", "Администрирование БД", "/admin/storage", "▤", "P0"),
    NavigationItem("security", "Безопасность", "/security", "◇", "P0"),
)
