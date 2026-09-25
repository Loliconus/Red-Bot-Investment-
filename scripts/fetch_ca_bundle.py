"""
Скачивание PEM-бандла Russian Trusted CA (НУЦ Минцифры РФ).

Пишет в: config/certs/russian_trusted_ca.pem
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "config" / "certs" / "russian_trusted_ca.pem"

SOURCES = (
    # Часто уже готовый PEM-бандл (root+sub)
    "https://gu-st.ru/content/Other/doc/russiantrustedca.pem",
    # DER/PEM сертификаты (root/sub)
    "https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer",
    "https://gu-st.ru/content/Other/doc/russian_trusted_sub_ca.cer",
    # Оставим как fallback (иногда отдаёт HTML/портальную страницу)
    "https://www.gosuslugi.ru/crt/russian_trusted_root_ca.cer",
    "https://www.gosuslugi.ru/crt/russian_trusted_sub_ca.cer",
)

_PEM_CERT_RE = re.compile(
    rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----\s*",
    re.DOTALL,
)


def _looks_like_html(data: bytes) -> bool:
    head = data.lstrip()[:200].lower()
    return head.startswith(b"<") or b"<!doctype" in head or b"<html" in head


def _extract_pem_blocks(raw: bytes) -> list[bytes]:
    """
    Возвращает список PEM-блоков CERTIFICATE из raw.
    Поддерживает:
      - PEM (CERTIFICATE)
      - DER X.509 (через cryptography)
    """
    # Уже PEM?
    if raw.lstrip().startswith(b"-----BEGIN"):
        blocks = _PEM_CERT_RE.findall(raw)
        return blocks if blocks else [raw]

    # DER -> PEM через cryptography
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Нужен пакет 'cryptography' для конвертации DER→PEM. "
            "Установите: python -m pip install cryptography"
        ) from exc

    cert = x509.load_der_x509_certificate(raw)
    return [cert.public_bytes(Encoding.PEM)]


def _download(url: str) -> bytes:
    if url not in SOURCES or not url.startswith("https://"):
        raise ValueError("Источник сертификата отсутствует в HTTPS-списке")
    req = urllib.request.Request(  # noqa: S310 — проверен HTTPS-allowlist выше
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (fetch_ca_bundle.py; +https://example.invalid)",
            "Accept": "*/*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return resp.read()


def main() -> int:
    TARGET.parent.mkdir(parents=True, exist_ok=True)

    pem_chunks: list[bytes] = []
    errors: list[str] = []

    for url in SOURCES:
        print(f"Скачиваю {url} ...")
        try:
            raw = _download(url)
        except (OSError, ValueError) as exc:
            errors.append(f"{url}: download failed: {exc}")
            print(f"  не удалось скачать: {exc}", file=sys.stderr)
            continue

        if _looks_like_html(raw):
            # Диагностика: это не сертификат, а HTML
            preview = raw.lstrip()[:120].decode("utf-8", errors="replace")
            errors.append(f"{url}: got HTML instead of cert (starts with {preview!r})")
            print(
                "  пропущено: вместо сертификата пришёл HTML (портал/редирект/заглушка)",
                file=sys.stderr,
            )
            continue

        try:
            blocks = _extract_pem_blocks(raw)
            pem_chunks.extend(blocks)
            print(f"  ok: добавлено блоков: {len(blocks)}")
        except (RuntimeError, ValueError, TypeError) as exc:
            errors.append(f"{url}: parse/convert failed: {exc}")
            print(f"  пропущено: не удалось распарсить/сконвертировать: {exc}", file=sys.stderr)

    if not pem_chunks:
        print("Не удалось собрать bundle ни из одного источника.", file=sys.stderr)
        for e in errors[-10:]:
            print("  " + e, file=sys.stderr)
        return 1

    # Склейка с пустой строкой между сертификатами
    out = b"\n".join(chunk.rstrip() for chunk in pem_chunks) + b"\n"
    TARGET.write_bytes(out)
    print(f"Готово: {TARGET} (сертификатов/блоков: {len(pem_chunks)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
