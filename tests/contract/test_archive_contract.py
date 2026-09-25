"""Контрактные тесты ``ArchivePort``: в памяти и на Parquet."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from tests.fakes import FakeArchive

NOW = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)


@pytest.fixture(params=["memory", "parquet"])
async def archive(request: pytest.FixtureRequest, tmp_path: Any) -> Any:
    if request.param == "memory":
        yield FakeArchive()
        return

    from adapters.driven.storage.duckdb_repository import DuckDBRepository
    from adapters.driven.storage.parquet_archive import ParquetArchive

    repository = DuckDBRepository(tmp_path / "arch.duckdb", memory_limit_mb=256, threads=1)
    yield ParquetArchive(repository=repository, archive_dir=tmp_path / "archive")
    await repository.aclose()


async def test_usage_by_layer_has_all_layers(archive: Any) -> None:
    usage = await archive.usage_by_layer()
    assert {"hot", "warm", "cold"} <= set(usage)
    assert all(value >= 0 for value in usage.values())


async def test_total_usage_matches_sum(archive: Any) -> None:
    usage = await archive.usage_by_layer()
    assert await archive.total_usage_bytes() == sum(usage.values())


async def test_archive_snapshots_returns_count(archive: Any) -> None:
    moved = await archive.archive_snapshots(older_than=NOW)
    assert moved >= 0


async def test_compaction_never_returns_negative(archive: Any) -> None:
    assert await archive.compact_cold_archive() >= 0


async def test_export_and_restore_round_trip(archive: Any, tmp_path: Any) -> None:
    target = await archive.export_backup(tmp_path / "backup")
    assert target.exists()
    await archive.restore_backup(target)
