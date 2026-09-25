"""Скачивание PEM-бандла Russian Trusted CA (НУЦ Минцифры РФ).

Запуск::

    python scripts/fetch_ca_bundle.py

Скрипт берёт сертификаты с официального портала Госуслуг и складывает в
``config/certs/russian_trusted_ca.pem``. Если автоматическое скачивание
недоступно, сделайте это вручную — инструкция в ``config/certs/README.md``.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

DEFAULT_SOURCES = (
    "https://www.gosuslugi.ru/crt/russian_trusted_root_ca.cer",
    "https://www.gosuslugi.ru/crt/russian_trusted_sub_ca.cer",
)

TARGET = Path(__file__).resolve().parents[1] / "config" / "certs" / "russian_trusted_ca.pem"


def _der_to_pem(data: bytes) -> str | None:
    """Конвертирует DER в PEM. Требуется cryptography; без неё возвращаем None."""
    try:  # pragma: no cover - зависит от окружения
        from cryptography import x509
    except Exception:  # noqa: BLE001 - cryptography может отсутствовать
        return None
    cert = x509.load_der_x509_certificate(data)
    return cert.public_bytes(
        __import__(
            "cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]
        ).Encoding.PEM
    ).decode("utf-8")


def main() -> int:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []

    for url in DEFAULT_SOURCES:
        print(f"Скачиваю {url} ...")
        try:
            with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
                raw = response.read()
        except Exception as exc:  # noqa: BLE001
            print(f"  не удалось: {exc}", file=sys.stderr)
            continue

        if raw.lstrip().startswith(b"-----BEGIN"):
            chunks.append(raw.decode("utf-8"))
            continue

        pem = _der_to_pem(raw)
        if pem:
            chunks.append(pem)
        else:
            print("  пропущено: нужен пакет cryptography для конвертации DER→PEM")

    if not chunks:
        print(
            "Не удалось получить ни одного сертификата. "
            "Скачайте файлы вручную: см. config/certs/README.md",
            file=sys.stderr,
        )
        return 1

    TARGET.write_text("\n".join(chunks), encoding="utf-8")
    print(f"Готово: {TARGET} ({len(chunks)} сертификатов)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
