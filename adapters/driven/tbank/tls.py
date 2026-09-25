"""TLS: доверие к российским корневым сертификатам.

T-Invest API отдаёт сертификаты НУЦ Минцифры РФ, которых нет в стандартных
хранилищах доверия Python и ОС. Правильное решение — **расширить доверие**,
а не отключить проверку: ``verify=False`` превращает MITM в реальную угрозу и
недопустим в боевом контуре.

Схема:
* если найден PEM-бандл из ``config/certs`` — строим доверие на нём
  (плюс системные корни, чтобы не сломать остальные соединения);
* если бандла нет и включён аварийный флаг — доверяем системным корням,
  а в LIVE это запрещено валидатором ``Settings``;
* ``create_ssl_channel_credentials`` формирует credentials для gRPC.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_CA_PATH = Path("config/certs/russian_trusted_ca.pem")


class TLSConfigurationError(RuntimeError):
    """Проблема с настройкой TLS. Процесс обязан упасть, а не «работать как-нибудь»."""


def load_ca_bundle(path: Path) -> bytes:
    """Читает PEM-бандл. Кидает ``TLSConfigurationError``, если файла нет."""
    if not path.exists():
        msg = (
            f"Не найден PEM-бандл корневых сертификатов: {path}. "
            "Положите russian_trusted_ca.pem (см. config/certs/README.md) "
            "или запустите python scripts/fetch_ca_bundle.py"
        )
        raise TLSConfigurationError(msg)

    data = path.read_bytes()
    if b"BEGIN CERTIFICATE" not in data:
        msg = f"Файл {path} не похож на PEM-бандл сертификатов"
        raise TLSConfigurationError(msg)
    return data


def system_ca_bundle() -> bytes | None:
    """Системные корни доверия, чтобы не потерять обычный HTTPS."""
    try:  # pragma: no cover - зависит от наличия certifi
        import certifi

        return Path(certifi.where()).read_bytes()
    except Exception:  # noqa: BLE001 - certifi может отсутствовать
        return None


def combined_ca_bundle(path: Path) -> bytes:
    """Бандл = российские корни + системные."""
    russian = load_ca_bundle(path)
    system = system_ca_bundle()
    if system:
        return russian + b"\n" + system
    return russian


def configure_environment(ca_path: Path) -> None:
    """Прокидывает CA-бандл в переменные окружения для HTTP-клиентов."""
    resolved = str(ca_path.resolve())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", resolved)
    os.environ.setdefault("SSL_CERT_FILE", resolved)
    os.environ.setdefault("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", resolved)


def create_ssl_channel_credentials(
    ca_path: Path = DEFAULT_CA_PATH,
    *,
    insecure_dev_only: bool = False,
) -> Any:
    """Формирует ``grpc.ssl_channel_credentials`` с доверенными корнями.

    ``insecure_dev_only`` — аварийный люк для локальной отладки в sandbox.
    В LIVE он заблокирован валидатором настроек, здесь мы только предупреждаем.
    """
    import grpc

    if insecure_dev_only:
        logger.error(
            "tls_insecure_mode",
            message="Проверка сертификатов ослаблена: только для локальной отладки!",
        )
        return grpc.ssl_channel_credentials()

    root_certificates = combined_ca_bundle(ca_path)
    configure_environment(ca_path)
    logger.info("tls_configured", ca_bundle=str(ca_path))
    return grpc.ssl_channel_credentials(root_certificates=root_certificates)


def resolve_ca_path(raw: Path | str | None) -> Path:
    """Определяет путь к бандлу: из настроек, из env, либо дефолтный."""
    if raw:
        return Path(raw)
    env_path = os.environ.get("REDBOT_CA_BUNDLE")
    if env_path:
        return Path(env_path)
    return DEFAULT_CA_PATH
