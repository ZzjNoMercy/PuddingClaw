"""catalog_backup 模块测试：真实文件 SQLite 上的备份/校验/恢复闭环。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend_lease import BackendInstanceLease
from catalog_backup import (
    CatalogBackupError,
    backup_catalog,
    list_backups,
    restore_backup,
    verify_backup,
)
from knowledge.models import KnowledgeBase
from runtime_identity.paths import PuddingClawPaths
from schema_migrations import CURRENT_SCHEMA_VERSION, migrate_to_latest


def _make_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, rows: int = 3) -> Path:
    """建一个真实 SQLite catalog（迁移到最新 schema + 若干 KnowledgeBase 行），
    并把 PUDDINGCLAW_DATABASE_URL 指过去，让 backup_catalog 走真实路径。"""

    db_path = tmp_path / "catalog.sqlite3"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("PUDDINGCLAW_DATABASE_URL", url)

    async def _setup() -> None:
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(migrate_to_latest)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            for index in range(rows):
                session.add(KnowledgeBase(name=f"测试知识库-{index}"))
            await session.commit()
        await engine.dispose()

    asyncio.run(_setup())
    return db_path


def _count_rows(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM knowledge_bases").fetchone()[0])
    finally:
        conn.close()


def test_backup_and_verify(tmp_path, monkeypatch):
    db_path = _make_catalog(tmp_path, monkeypatch)

    manifest = backup_catalog(dest_dir=tmp_path / "backups")

    backup_path = tmp_path / "backups" / manifest["backup_file"]
    assert backup_path.exists()
    for field in (
        "backup_file",
        "created_at",
        "app_version",
        "schema_version",
        "sha256",
        "size_bytes",
        "integrity_check",
        "source_path",
    ):
        assert field in manifest, f"manifest 缺少字段 {field}"
    assert manifest["integrity_check"] == "ok"
    assert manifest["schema_version"] == CURRENT_SCHEMA_VERSION
    assert manifest["source_path"] == str(db_path)
    assert manifest["size_bytes"] == backup_path.stat().st_size
    digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    assert manifest["sha256"] == digest
    # 清单文件与备份同名伴随
    manifest_file = backup_path.with_name(backup_path.name + ".manifest.json")
    assert json.loads(manifest_file.read_text(encoding="utf-8"))["sha256"] == digest

    info = verify_backup(backup_path)
    assert info["integrity_check"] == "ok"
    assert info["schema_version"] == CURRENT_SCHEMA_VERSION
    assert info["sha256"] == digest
    assert info["manifest"] is True

    backups = list_backups(tmp_path / "backups")
    assert [entry["backup_file"] for entry in backups] == [manifest["backup_file"]]


def test_backup_excludes_wal_consistency(tmp_path, monkeypatch):
    """源库处于 WAL 模式且有未 checkpoint 的已提交数据时，备份仍完整。"""

    db_path = _make_catalog(tmp_path, monkeypatch, rows=2)

    # 保持连接打开（阻止关闭时自动 checkpoint），数据留在 -wal 里。
    live = sqlite3.connect(str(db_path))
    try:
        assert live.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        for index in range(50):
            live.execute(
                "INSERT INTO knowledge_bases (id, name, description, created_at, updated_at) "
                "VALUES (?, ?, '', '2026-01-01', '2026-01-01')",
                (f"kb_wal_{index}", f"WAL数据-{index}"),
            )
        live.commit()
        assert Path(str(db_path) + "-wal").exists(), "前提失败：WAL 文件应存在且未 checkpoint"

        manifest = backup_catalog(dest_dir=tmp_path / "backups")
    finally:
        live.close()

    backup_path = tmp_path / "backups" / manifest["backup_file"]
    # 主库 2 行 + WAL 里 50 行，快照必须全部包含
    assert _count_rows(backup_path) == 52
    conn = sqlite3.connect(str(backup_path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM knowledge_bases WHERE id LIKE 'kb_wal_%'"
        ).fetchone()[0] == 50
    finally:
        conn.close()


def test_verify_rejects_corruption(tmp_path, monkeypatch):
    _make_catalog(tmp_path, monkeypatch)
    manifest = backup_catalog(dest_dir=tmp_path / "backups")
    backup_path = tmp_path / "backups" / manifest["backup_file"]

    # 截断一半，模拟损坏
    size = backup_path.stat().st_size
    with backup_path.open("r+b") as fh:
        fh.truncate(size // 2)

    with pytest.raises(CatalogBackupError):
        verify_backup(backup_path)


def test_restore_roundtrip(tmp_path, monkeypatch):
    db_path = _make_catalog(tmp_path, monkeypatch, rows=5)
    manifest = backup_catalog(dest_dir=tmp_path / "backups")
    backup_path = tmp_path / "backups" / manifest["backup_file"]

    # 改坏目标库：删光行再写入垃圾页之外的损坏（直接截断覆盖）
    db_path.write_bytes(b"corrupted-not-a-database" * 16)
    assert not db_path.read_bytes().startswith(b"SQLite format 3")

    result = restore_backup(backup_path, target=db_path)

    assert result["restored"] == str(db_path)
    assert result["schema_version"] == CURRENT_SCHEMA_VERSION
    assert result["sha256"] == manifest["sha256"]
    assert result["previous"] is not None
    previous = Path(result["previous"])
    assert previous.exists()
    assert ".pre-restore-" in previous.name
    # 保留的是被改坏的旧库，可回滚
    assert previous.read_bytes() == b"corrupted-not-a-database" * 16
    assert _count_rows(db_path) == 5
    assert verify_backup(db_path)["integrity_check"] == "ok"


def test_restore_refuses_when_backend_running(tmp_path, monkeypatch):
    db_path = _make_catalog(tmp_path, monkeypatch)
    manifest = backup_catalog(dest_dir=tmp_path / "backups")
    backup_path = tmp_path / "backups" / manifest["backup_file"]

    lease = BackendInstanceLease()
    if not lease.enforced:
        pytest.skip("当前平台不支持 flock，跳过 lease 拒绝恢复测试")
    state_dir = PuddingClawPaths.from_environment().state()
    assert lease.acquire(state_dir)
    try:
        with pytest.raises(CatalogBackupError, match="Backend 正在运行"):
            restore_backup(backup_path, target=db_path)
    finally:
        lease.release()

    # release 之后可以正常恢复
    result = restore_backup(backup_path, target=db_path)
    assert result["restored"] == str(db_path)


def test_non_sqlite_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "PUDDINGCLAW_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:5432/puddingclaw"
    )
    with pytest.raises(CatalogBackupError, match="PostgreSQL"):
        backup_catalog(dest_dir=tmp_path / "backups")
    # 不该产出任何备份文件
    assert not (tmp_path / "backups").exists()
