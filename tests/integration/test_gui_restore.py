"""Восстановление БД: fail-closed на RUNNING, подтверждении и SHA-256.

Реальный DuckDB/ParquetArchive, без брокера и сети. Аудит не перезаписывается.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from application.composition import AppContext
from application.scheduler import Scheduler
from application.use_cases.bootstrap_database import seed_instrument
from application.use_cases.manage_storage import create_backup, restore_backup
from core.ports.persistence import GuiAuditEntry
from tests.integration.test_web_app import duckdb_context as db_fixture  # noqa: F401


async def test_restore_requires_stop_phrase_checksum_and_keeps_audit(
    db_fixture: AppContext,  # noqa: F811 — pytest fixture
) -> None:
    ctx = db_fixture
    audit = GuiAuditEntry(
        id=uuid4(),
        ts=ctx.clock.now(),
        section="storage",
        action="CREATE BACKUP",
        before={},
        after={},
        outcome="200",
    )
    await ctx.repository.append_gui_audit(audit)
    await ctx.repository.save_instrument(seed_instrument("uid-before", "SBER", 10))
    backup = await create_backup(ctx)
    assert backup.size_bytes > 0
    await ctx.repository.save_instrument(seed_instrument("uid-after", "GAZP", 10))

    scheduler = Scheduler()
    scheduler._running = True
    ctx.scheduler = scheduler
    with pytest.raises(PermissionError, match="полностью остановите"):
        await restore_backup(ctx, backup.id, confirmation="ВОССТАНОВИТЬ")
    scheduler._running = False
    with pytest.raises(ValueError, match="ВОССТАНОВИТЬ"):
        await restore_backup(ctx, backup.id, confirmation="нет")

    root = ctx.settings.storage.data_dir / "backups" / backup.id
    manifest = root / "manifest.json"
    assert manifest.exists()
    target = next(root.glob("redbot_backup_*.parquet"))
    original = target.read_bytes()
    target.write_bytes(original + b"tampered")
    with pytest.raises(ValueError, match="Контрольная сумма"):
        await restore_backup(ctx, backup.id, confirmation="ВОССТАНОВИТЬ")
    assert any(item.uid == "uid-after" for item in await ctx.repository.list_instruments())

    target.write_bytes(original)
    await restore_backup(ctx, backup.id, confirmation="ВОССТАНОВИТЬ")
    instruments = await ctx.repository.list_instruments()
    assert [item.uid for item in instruments] == ["uid-before"]
    entries = await ctx.repository.list_gui_audit()
    assert entries[0].id == audit.id
    assert ctx.restart_required
