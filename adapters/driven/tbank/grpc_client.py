"""gRPC-подключение к T-Invest API.

Неймспейс SDK — ``t_tech.invest.grpc`` (старый ``tinkoff.invest`` удалён,
использовать его нельзя). Клиенты — контекстные менеджеры, поэтому канал
создаётся асинхронной фабрикой и живёт всё время работы процесса.

Импорт SDK — ленивый: если пакет не установлен, старт падает с понятной
ошибкой, а не с ``ModuleNotFoundError`` в случайном месте.

TLS: конструктор ``AsyncClient`` не принимает объект ``grpc.ChannelCredentials``
(в сигнатуре только token/target/sandbox_token/options/app_name/interceptors),
поэтому кастомный CA-бандл подключается через переменную окружения
``GRPC_DEFAULT_SSL_ROOTS_FILE_PATH`` — её читает сам gRPC C-core в момент
построения дефолтных SSL-credentials внутри SDK.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

from adapters.driven.tbank.tls import resolve_ca_path
from config.settings import Settings, target_for_mode

logger = structlog.get_logger(__name__)


class SdkUnavailableError(RuntimeError):
    """SDK T-Invest не установлен."""


def _import_async_client() -> Any:
    try:
        from t_tech.invest import AsyncClient  # pyright: ignore[reportMissingImports]
    except ImportError as exc:  # pragma: no cover - зависит от установки
        msg = (
            "Пакет t-tech-investments не установлен. Установите его из реестра Т-Банка:\n"
            "  pip install t-tech-investments "
            "--index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple"
        )
        raise SdkUnavailableError(msg) from exc
    return AsyncClient


class TInvestChannel:
    """Долгоживущее подключение к API."""

    __slots__ = ("_ca_path", "_client", "_services", "_target", "_token")

    def __init__(self, target: str, token: str, *, ca_path: Path) -> None:
        self._target = target
        self._token = token
        self._ca_path = ca_path
        self._client: Any = None
        self._services: Any = None

    @classmethod
    async def create(cls, settings: Settings) -> TInvestChannel:
        """Асинхронная фабрика: входит в контекст клиента SDK."""
        async_client = _import_async_client()
        target = target_for_mode(settings)
        ca_path = resolve_ca_path(settings.tbank.ca_bundle_path)

        # У AsyncClient нет kwarg для credentials — CA-бандл подключается
        # только через переменную окружения, которую читает gRPC C-core.
        os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = str(ca_path)

        channel = cls(
            target=target,
            token=settings.tbank.api_token.get_secret_value(),
            ca_path=ca_path,
        )

        client = async_client(
            token=channel._token,
            target=target,
            app_name="red-bot",
        )
        services = await client.__aenter__()
        channel._client = client
        channel._services = services
        logger.info("tinvest_channel_created", target=target, ca=str(ca_path))
        return channel

    @property
    def services(self) -> Any:
        """Объект ``services`` SDK: ``services.market_data``, ``services.orders``, ..."""
        if self._services is None:
            msg = "Канал не открыт: сначала вызовите TInvestChannel.create"
            raise RuntimeError(msg)
        return self._services

    @property
    def target(self) -> str:
        return self._target

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None
            self._services = None
            logger.info("tinvest_channel_closed", target=self._target)


async def create_channel(settings: Settings) -> TInvestChannel:
    """Публичная точка создания канала (используется в ``composition``)."""
    return await TInvestChannel.create(settings)
