"""Sidebar создаётся из конфигурации, base.html не меняется при расширении.

Структура следует мысли оператора: что бот делает → на каких правилах →
что уже сделал → как им управлять. Одна функция = один раздел.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NavigationItem:
    key: str
    label: str
    path: str
    icon: str
    group: str
    hint: str


NAVIGATION = (
    # --- что бот делает прямо сейчас ------------------------------------
    NavigationItem("dashboard", "Обзор", "/", "▤", "ТОРГОВЛЯ", "Пульс системы и портфель"),
    NavigationItem(
        "reasoning",
        "Мысли бота",
        "/reasoning",
        "✦",
        "ТОРГОВЛЯ",
        "Воронка решений и причины молчания",
    ),
    NavigationItem("chart", "Рынок", "/chart", "▲", "ТОРГОВЛЯ", "График, стакан, контекст"),
    # --- на каких правилах работает -------------------------------------
    NavigationItem(
        "instruments", "Инструменты", "/instruments", "◫", "СТРАТЕГИЯ", "Рабочая корзина и каталог"
    ),
    NavigationItem(
        "strategy", "Стратегия", "/strategy", "∑", "СТРАТЕГИЯ", "Confluence, пороги, ТА"
    ),
    NavigationItem("risk", "Риск", "/risk", "⚠", "СТРАТЕГИЯ", "Лимиты и защита капитала"),
    # --- что уже сделано --------------------------------------------------
    NavigationItem("journal", "Журнал", "/journal", "≡", "УЧЁТ", "Сделки, гипотезы, самооценка"),
    NavigationItem(
        "backtest",
        "Синтетический трейдер",
        "/backtest",
        "◈",
        "УЧЁТ",
        "3 вероятности · исследования · честный бэктест",
    ),
    # --- как системой управлять -------------------------------------------
    NavigationItem("control", "Пульт", "/control", "◉", "СИСТЕМА", "Запуск, пауза, задачи, логи"),
    NavigationItem(
        "storage", "Хранилище", "/admin/storage", "▦", "СИСТЕМА", "DuckDB, слои, SQL, бэкапы"
    ),
    NavigationItem(
        "settings", "Счёт и режим", "/settings", "⚙", "СИСТЕМА", "Контур, счета, запуск"
    ),
)
