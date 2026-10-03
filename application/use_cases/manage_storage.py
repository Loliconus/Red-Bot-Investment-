"""Архивация, резервные копии и строго контролируемый restore.

Ни имя файла, ни путь из пользовательского запроса не передаются в ArchivePort.
Восстановление допускается только после *завершения* планировщика, не во время
паузы; исходный аудит при этом не затирается.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import psutil

from application.composition import AppContext

RESTORE_PHRASE = "ВОССТАНОВИТЬ"
BACKUP_ID = re.compile(r"^backup-\d{8}T\d{6}Z-[a-f0-9]{8}$")


@dataclass(frozen=True, slots=True)
class BackupInfo:
    id: str
    created_at: str
    size_bytes: int


async def storage_overview(context: AppContext) -> tuple[dict[str, int], dict[str, int]]:
    if context.archive is None:
        raise ValueError("Архив недоступен")
    return await context.archive.usage_by_layer(), await context.repository.table_sizes()


async def archive_now(context: AppContext, *, days: int | None = None) -> int:
    if context.archive is None:
        raise ValueError("Архив недоступен")
    retention = days or int(
        await context.repository.get_operational_value("warm_retention_days")
        or context.settings.storage.warm_retention_days
    )
    return await context.archive.archive_snapshots(
        older_than=context.clock.now() - timedelta(days=retention)
    )


async def compact_archive(context: AppContext) -> int:
    if context.archive is None:
        raise ValueError("Архив недоступен")
    return await context.archive.compact_cold_archive()


async def set_memory_limit(
    context: AppContext, mb: int, *, confirmed_warning: bool = False
) -> bool:
    if not 128 <= mb <= 16384:
        raise ValueError("Лимит памяти: от 128 до 16384 МБ")
    above_half_ram = mb * 1024**2 > psutil.virtual_memory().total // 2
    if above_half_ram and not confirmed_warning:
        raise ValueError("Выше 50% RAM компьютера: подтвердите предупреждение")
    await context.repository.set_memory_limit_mb(mb)
    await context.repository.set_operational_value("duckdb_memory_limit_mb", str(mb))
    context.storage_memory_limit_mb = mb
    return bool(above_half_ram)


async def set_retention(
    context: AppContext, *, hot_days: int, warm_days: int, disk_threshold_pct: int
) -> None:
    if not 1 <= hot_days < warm_days <= 3650 or not 50 <= disk_threshold_pct <= 95:
        raise ValueError("Hot: ≥1 день; warm: больше hot (≤3650); порог диска: 50–95%")
    for key, value in (
        ("hot_retention_days", hot_days),
        ("warm_retention_days", warm_days),
        ("disk_threshold_pct", disk_threshold_pct),
    ):
        await context.repository.set_operational_value(key, str(value))


def _backup_root(context: AppContext) -> Path:
    return context.settings.storage.data_dir.resolve() / "backups"


def _files_in_backup(directory: Path) -> list[Path]:
    return sorted(file for file in directory.glob("redbot_backup_*.parquet") if file.is_file())


def _checksum(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


async def create_backup(context: AppContext) -> BackupInfo:
    if context.archive is None:
        raise ValueError("Архив недоступен")
    stamp = context.clock.now().strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"backup-{stamp}-{uuid4().hex[:8]}"
    directory = _backup_root(context) / backup_id
    directory.mkdir(parents=True, exist_ok=False)
    try:
        await context.archive.export_backup(directory)
        files = await asyncio.to_thread(_files_in_backup, directory)
        hashes = await asyncio.to_thread(lambda: {file.name: _checksum(file) for file in files})
        manifest = {"id": backup_id, "created_at": context.clock.now().isoformat(), "files": hashes}
        await asyncio.to_thread(
            (directory / "manifest.json").write_text, json.dumps(manifest), "utf-8"
        )
    except Exception:
        # Неполная копия никогда не появляется в списке доступных для restore.
        raise
    return BackupInfo(
        id=backup_id,
        created_at=str(manifest["created_at"]),
        size_bytes=sum(p.stat().st_size for p in files),
    )


def _load_backup(context: AppContext, backup_id: str) -> tuple[Path, BackupInfo, dict[str, str]]:
    if not BACKUP_ID.fullmatch(backup_id):
        raise ValueError("Некорректный идентификатор бэкапа")
    root = _backup_root(context)
    directory = root / backup_id
    if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != root:
        raise ValueError("Бэкап не найден")
    manifest_file = directory / "manifest.json"
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise ValueError("Бэкап не завершён")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("id") != backup_id or not isinstance(manifest.get("files"), dict):
        raise ValueError("Повреждён манифест бэкапа")
    files: dict[str, str] = manifest["files"]
    size = 0
    for name in files:
        if not re.fullmatch(r"redbot_backup_[a-z_]+\.parquet", name):
            raise ValueError("Повреждён манифест бэкапа")
        file = directory / name
        if file.is_symlink() or not file.is_file():
            raise ValueError("Файл резервной копии отсутствует")
        size += file.stat().st_size
    return directory, BackupInfo(backup_id, str(manifest["created_at"]), size), files


async def list_backups(context: AppContext) -> list[BackupInfo]:
    root = _backup_root(context)
    if not root.exists():
        return []
    result = []
    for path in sorted(root.iterdir(), reverse=True):
        try:
            _, info, _ = _load_backup(context, path.name)
        except (ValueError, OSError, KeyError, json.JSONDecodeError):
            continue
        result.append(info)
    return result


async def restore_backup(context: AppContext, backup_id: str, *, confirmation: str) -> None:
    if context.scheduler is not None and context.scheduler.is_active:
        raise PermissionError("Сначала полностью остановите торговый процесс; PAUSED недостаточно")
    if confirmation != RESTORE_PHRASE:
        raise ValueError(f"Для восстановления введите {RESTORE_PHRASE}")
    if context.archive is None:
        raise ValueError("Архив недоступен")
    directory, _, hashes = _load_backup(context, backup_id)
    if not hashes:
        raise ValueError("Бэкап пуст")
    actual = await asyncio.to_thread(lambda: {name: _checksum(directory / name) for name in hashes})
    if actual != hashes:
        raise ValueError("Контрольная сумма резервной копии не совпадает")
    await context.archive.restore_backup(directory)
    context.restart_required = True  # контекст и адаптеры нельзя использовать со старым снимком
