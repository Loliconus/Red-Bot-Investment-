"""TLS для T-Invest SDK.

SDK сам создает gRPC credentials и не принимает ``channel_credentials`` от
приложения. Поддерживаемая настройка — ``SSL_TBANK_VERIFY=True``: SDK берет
корневой сертификат НУЦ Минцифры РФ из собственного пакета. Проверка TLS
всегда включена; адаптеры T-Invest не создают клиент gRPC самостоятельно.

Нижележащие функции для собственного gRPC-кода оставлены как legacy helpers,
но адаптеры SDK их не используют.
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


def configure_sdk_tls() -> None:
    """Включает встроенный CA НУЦ Минцифры, который поддерживает SDK.

    SDK формирует gRPC credentials самостоятельно и не принимает
    ``channel_credentials`` в AsyncClient/AsyncSandboxClient. Поддерживаемый
    способ выбора встроенного корневого сертификата — ``SSL_TBANK_VERIFY``.
    """
    os.environ["SSL_TBANK_VERIFY"] = "True"
    logger.info("tls_configured", certificate_source="t_tech_sdk_embedded_russian_ca")


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
