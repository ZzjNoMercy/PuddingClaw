"""catalog_migration 测试：真实文件 SQLite 上的导出/导入/校验/中断回滚闭环。

PostgreSQL 实机测试仅在 PUDDINGCLAW_TEST_PG_URL 存在时运行。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import catalog_migration
import runtime_control
from catalog_migration import (
    EXPORT_TABLES,
    CatalogMigrationError,
    export_catalog,
    import_catalog,
    validate_migration,
)
from knowledge.models import (
    Base,
    KnowledgeBase,
    KnowledgeDocument,
    KnowledgeImportJob,
    ReadLaterItem,
    SemanticDimensionBuildJob,
    TaskNotification,
)
from schema_migrations import CURRENT_SCHEMA_VERSION, migrate_to_latest

LEASE_EXPIRES = datetime(2026, 1, 1, tzinfo=timezone.utc)
NAIVE_CREATED_AT = datetime(2026, 3, 1, 8, 30, 0)


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _seed(session, *, naive_created: bool = False) -> dict[str, str]:
    """写入覆盖各类型的种子数据：KB/文档/稍后再读/导入任务(lease)/维度构建/通知。"""

    kb = KnowledgeBase(name="迁移测试库", description="catalog migration seed")
    if naive_created:
        kb.created_at = NAIVE_CREATED_AT
    session.add(kb)
    await session.flush()
    doc = KnowledgeDocument(
        knowledge_base_id=kb.id,
        title="测试文档",
        source_path="/tmp/seed.md",
        storage_path="knowledge/imported/seed.md",
        virtual_path="/knowledge/seed.md",
        content_sha256="a" * 64,
        publish_targets=["web"],
        doc_metadata={"lang": "zh", "pages": 3},
    )
    session.add(doc)
    await session.flush()
    item = ReadLaterItem(
        knowledge_base_id=kb.id,
        original_url="https://example.com/article",
        canonical_url="https://example.com/article",
        title="稍后再读",
        tags=["迁移", "测试"],
        document_id=doc.id,
    )
    session.add(item)
    job = KnowledgeImportJob(
        knowledge_base_id=kb.id,
        file_name="seed.md",
        source_path="/tmp/seed.md",
        status="failed",
        document_id=doc.id,
        lease_owner="worker-1",
        lease_expires_at=LEASE_EXPIRES,
        heartbeat_at=LEASE_EXPIRES,
        attempt=3,
    )
    session.add(job)
    await session.flush()
    sdb = SemanticDimensionBuildJob(
        dimension_id="dim-1",
        adapter="vanna",
        lease_owner="worker-2",
        attempt=2,
        requested_scope={"tables": ["t1"]},
    )
    session.add(sdb)
    notification = TaskNotification(
        subject_type="knowledge_import_job",
        subject_id=job.id,
        title="导入失败",
        payload={"job_id": job.id},
    )
    session.add(notification)
    await session.commit()
    return {"kb": kb.id, "doc": doc.id, "job": job.id, "sdb": sdb.id, "notification": notification.id}


def _make_source(tmp_path: Path, name: str = "source.sqlite3", *, naive_created: bool = False) -> tuple[Path, str, dict[str, str]]:
    """建一个真实 SQLite 源库（迁移到最新 schema + 种子数据）。"""

    db_path = tmp_path / name
    url = _sqlite_url(db_path)

    async def _setup() -> dict[str, str]:
        engine = catalog_migration._create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(migrate_to_latest)
            from sqlalchemy.ext.asyncio import async_sessionmaker

            sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
            async with sessionmaker() as session:
                return await _seed(session, naive_created=naive_created)
        finally:
            await engine.dispose()

    ids = asyncio.run(_setup())
    return db_path, url, ids


def _fetch_all(url: str, table_name: str) -> list[dict]:
    async def _run() -> list[dict]:
        engine = catalog_migration._create_engine(url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(select(Base.metadata.tables[table_name]))
                return [dict(row._mapping) for row in result]
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _table_counts(url: str) -> dict[str, int]:
    """目标库中已存在的 Core 表行数（表不存在则不计入，用于中断回滚断言）。"""

    async def _run() -> dict[str, int]:
        engine = catalog_migration._create_engine(url)
        try:
            async with engine.connect() as conn:
                names = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
                counts: dict[str, int] = {}
                for name in EXPORT_TABLES:
                    if name in names:
                        counts[name] = int(
                            (
                                await conn.execute(
                                    select(func.count()).select_from(Base.metadata.tables[name])
                                )
                            ).scalar_one()
                        )
                return counts
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_sqlite_to_sqlite_roundtrip(tmp_path):
    _src_path, src_url, ids = _make_source(tmp_path)
    tgt_url = _sqlite_url(tmp_path / "target.sqlite3")

    data = asyncio.run(export_catalog(src_url))
    assert set(data) == set(EXPORT_TABLES)
    # datetime 统一为 ISO 字符串，JSON 列为原生对象
    kb_row = data["knowledge_bases"][0]
    assert isinstance(kb_row["created_at"], str) and kb_row["created_at"].endswith("+00:00")
    assert data["knowledge_documents"][0]["doc_metadata"] == {"lang": "zh", "pages": 3}
    assert data["read_later_items"][0]["tags"] == ["迁移", "测试"]

    import_result = asyncio.run(import_catalog(tgt_url, data))
    assert import_result["tables"]["knowledge_bases"] == 1
    assert import_result["total_rows"] == 6

    validation = asyncio.run(validate_migration(src_url, tgt_url))
    assert validation["ok"], validation["errors"]
    assert validation["schema_version"] == CURRENT_SCHEMA_VERSION
    assert validation["sqlite_checks"] == {"foreign_key_violations": 0, "integrity_check": "ok"}
    for name in EXPORT_TABLES:
        detail = validation["tables"][name]
        assert detail["row_count_match"] and detail["primary_keys_match"], name
    assert validation["tables"]["knowledge_import_jobs"]["source_rows"] == 1

    # lease 字段与 attempt 值原样保留
    job = _fetch_all(tgt_url, "knowledge_import_jobs")[0]
    assert job["id"] == ids["job"]
    assert job["lease_owner"] == "worker-1"
    assert job["attempt"] == 3
    assert job["lease_expires_at"].replace(tzinfo=timezone.utc) == LEASE_EXPIRES
    assert job["heartbeat_at"].replace(tzinfo=timezone.utc) == LEASE_EXPIRES
    sdb = _fetch_all(tgt_url, "semantic_dimension_build_jobs")[0]
    assert sdb["lease_owner"] == "worker-2" and sdb["attempt"] == 2


def test_import_rejects_non_empty_target(tmp_path):
    _src_path, src_url, _ids = _make_source(tmp_path, "source.sqlite3")
    # 目标库先放一行
    _tgt_path, tgt_url, _tgt_ids = _make_source(tmp_path, "target.sqlite3")
    before = _table_counts(tgt_url)

    data = asyncio.run(export_catalog(src_url))
    with pytest.raises(CatalogMigrationError, match="目标库非空"):
        asyncio.run(import_catalog(tgt_url, data))
    # 拒绝后目标库未被半写：行数完全不变
    assert _table_counts(tgt_url) == before


def test_interrupted_import_rolls_back_and_retry_succeeds(tmp_path, monkeypatch):
    _src_path, src_url, _ids = _make_source(tmp_path)
    tgt_url = _sqlite_url(tmp_path / "target.sqlite3")
    data = asyncio.run(export_catalog(src_url))

    original_insert = catalog_migration._insert_rows

    async def failing_insert(conn, table, rows):
        if table.name == "knowledge_documents":  # 第二张表处模拟中断
            raise RuntimeError("模拟导入中断")
        await original_insert(conn, table, rows)

    monkeypatch.setattr(catalog_migration, "_insert_rows", failing_insert)
    with pytest.raises(RuntimeError, match="模拟导入中断"):
        asyncio.run(import_catalog(tgt_url, data))
    # 单事务回滚：包括第一张表在内什么都不留
    counts = _table_counts(tgt_url)
    assert all(count == 0 for count in counts.values()), counts

    monkeypatch.setattr(catalog_migration, "_insert_rows", original_insert)
    asyncio.run(import_catalog(tgt_url, data))
    validation = asyncio.run(validate_migration(src_url, tgt_url))
    assert validation["ok"], validation["errors"]


def test_validate_detects_target_divergence(tmp_path):
    _src_path, src_url, ids = _make_source(tmp_path)
    tgt_path = tmp_path / "target.sqlite3"
    tgt_url = _sqlite_url(tgt_path)
    data = asyncio.run(export_catalog(src_url))
    asyncio.run(import_catalog(tgt_url, data))
    assert asyncio.run(validate_migration(src_url, tgt_url))["ok"]

    # 篡改目标库：改掉 KB 主键（其文档/任务随即外键悬空）
    conn = sqlite3.connect(str(tgt_path))
    try:
        conn.execute("UPDATE knowledge_bases SET id = 'kb_tampered' WHERE id = ?", (ids["kb"],))
        conn.commit()
    finally:
        conn.close()

    validation = asyncio.run(validate_migration(src_url, tgt_url))
    assert not validation["ok"]
    assert any("knowledge_bases" in error for error in validation["errors"])
    assert validation["sqlite_checks"]["foreign_key_violations"] > 0


def test_naive_datetime_normalized_to_utc(tmp_path):
    _src_path, src_url, _ids = _make_source(tmp_path, naive_created=True)
    tgt_url = _sqlite_url(tmp_path / "target.sqlite3")

    data = asyncio.run(export_catalog(src_url))
    assert data["knowledge_bases"][0]["created_at"] == "2026-03-01T08:30:00+00:00"

    asyncio.run(import_catalog(tgt_url, data))
    imported = _fetch_all(tgt_url, "knowledge_bases")[0]
    # SQLite 读回为 naive；与源行按同一 UTC 语义相等
    assert imported["created_at"] == NAIVE_CREATED_AT
    source = _fetch_all(src_url, "knowledge_bases")[0]
    assert imported["created_at"] == source["created_at"]


def test_atomic_config_switch_to_sqlite(tmp_path):
    home = Path(os.environ["PUDDINGCLAW_HOME"])
    config_path = home / "config.json"
    home.mkdir(parents=True, exist_ok=True)
    original = {
        "schema_version": 1,
        "database": {"provider": "postgresql", "source": "external", "host": "db.internal"},
    }
    config_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

    result = catalog_migration._write_database_config_section(
        {"provider": "sqlite", "source": "local_file"}
    )

    from config import get_database_config

    effective = get_database_config()
    assert effective["provider"] == "sqlite"
    assert effective["source"] == "local_file"
    assert effective["url"] == ""

    backup_path = Path(result["backup_path"])
    assert backup_path.exists()
    assert ".pre-migration-" in backup_path.name
    assert json.loads(backup_path.read_text(encoding="utf-8")) == original


def test_postgres_config_section_uses_credential_vault(tmp_path):
    section = catalog_migration._postgres_config_section(
        "postgresql+asyncpg://user:secret@db.internal:5433/puddingclaw"
    )
    assert section["provider"] == "postgresql"
    assert section["source"] == "external"
    assert section["host"] == "db.internal"
    assert section["port"] == 5433
    assert section["database"] == "puddingclaw"
    assert section["username"] == "user"
    assert "password" not in section
    assert "url" not in section

    from provider_registry import LocalCredentialStore

    assert LocalCredentialStore().get(section["password_ref"]) == "secret"


PG_TEST_URL = os.environ.get("PUDDINGCLAW_TEST_PG_URL", "").strip()


def _asyncpg_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url.split("://", 1)[1]
    return url


@pytest.mark.skipif(not PG_TEST_URL, reason="PUDDINGCLAW_TEST_PG_URL 未设置，跳过 PostgreSQL 实机测试")
def test_postgres_to_sqlite_full_flow(tmp_path, monkeypatch):
    pg_url = _asyncpg_url(PG_TEST_URL)

    async def _prepare_pg() -> dict[str, str]:
        engine = catalog_migration._create_engine(pg_url)
        try:
            async with engine.begin() as conn:
                # 保证目标 PG 为空库后重建 schema
                for name in (*reversed(EXPORT_TABLES), "core_runtime_control", "core_schema_migrations"):
                    await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {name} CASCADE")
                await conn.run_sync(migrate_to_latest)
            from sqlalchemy.ext.asyncio import async_sessionmaker

            sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
            async with sessionmaker() as session:
                return await _seed(session)
        finally:
            await engine.dispose()

    ids = asyncio.run(_prepare_pg())
    monkeypatch.setenv("PUDDINGCLAW_DATABASE_URL", pg_url)

    target = tmp_path / "catalog.sqlite3"
    report = asyncio.run(
        catalog_migration.migrate_postgres_to_sqlite(
            target_path=target, reason="PostgreSQL 实机迁移测试"
        )
    )

    assert report["ok"], report
    assert report["validation"]["ok"]
    assert report["tables"]["knowledge_import_jobs"] == 1
    assert target.exists()
    job = _fetch_all(_sqlite_url(target), "knowledge_import_jobs")[0]
    assert job["id"] == ids["job"]
    assert job["lease_owner"] == "worker-1" and job["attempt"] == 3

    from config import get_database_config

    assert get_database_config()["provider"] == "sqlite"
    # 源 PG 未被删除：仍能连上且数据在
    assert _table_counts(pg_url)["knowledge_bases"] == 1


def test_validate_detects_content_tamper_beyond_primary_keys(tmp_path):
    """篡改非主键字段：行数与主键集合不变，只有内容摘要（digest）能检出。"""

    _src_path, src_url, ids = _make_source(tmp_path)
    tgt_path = tmp_path / "target.sqlite3"
    tgt_url = _sqlite_url(tgt_path)
    data = asyncio.run(export_catalog(src_url))
    asyncio.run(import_catalog(tgt_url, data))
    assert asyncio.run(validate_migration(src_url, tgt_url))["ok"]

    conn = sqlite3.connect(str(tgt_path))
    try:
        conn.execute("UPDATE knowledge_documents SET title = '已篡改' WHERE id = ?", (ids["doc"],))
        conn.commit()
    finally:
        conn.close()

    validation = asyncio.run(validate_migration(src_url, tgt_url))
    assert not validation["ok"]
    detail = validation["tables"]["knowledge_documents"]
    assert detail["row_count_match"] and detail["primary_keys_match"]
    assert not detail["content_digest_match"]
    assert any("knowledge_documents" in error and "摘要" in error for error in validation["errors"])


def test_content_digest_is_independent_of_database_row_order():
    """跨数据库 collation 不同，不得让相同行集合产生不同摘要。"""

    table = Base.metadata.tables["knowledge_import_jobs"]
    first = {column.name: None for column in table.c}
    first.update({"id": "job_underscore", "status": "queued", "attempt": 0})
    second = {column.name: None for column in table.c}
    second.update({"id": "job-hyphen", "status": "queued", "attempt": 0})

    forward = catalog_migration._rows_content_digest(table, [first, second])
    reverse = catalog_migration._rows_content_digest(table, [second, first])

    assert forward == reverse


def test_validate_digest_tolerates_json_string_vs_object(tmp_path):
    """JSON 列在源/目标分别为 str 或 dict 时规范化一致，不误报差异。"""

    _src_path, src_url, _ids = _make_source(tmp_path)
    tgt_url = _sqlite_url(tmp_path / "target.sqlite3")
    data = asyncio.run(export_catalog(src_url))
    # 模拟 PG JSONB 读出为 dict、SQLite 读出为 str 的混合情形
    data["knowledge_documents"][0]["doc_metadata"] = json.dumps({"pages": 3, "lang": "zh"}, ensure_ascii=False)
    asyncio.run(import_catalog(tgt_url, data))
    validation = asyncio.run(validate_migration(src_url, tgt_url))
    assert validation["ok"], validation["errors"]
    assert validation["tables"]["knowledge_documents"]["content_digest_match"]


def test_renew_maintenance_loop_extends_lease(tmp_path):
    """迁移期间的后台续租循环会持续延长 lease，直到 stop 事件。"""

    async def run() -> None:
        engine = catalog_migration._create_engine(_sqlite_url(tmp_path / "catalog.db"))
        try:
            async with engine.begin() as conn:
                await conn.run_sync(migrate_to_latest)
            sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
            async with sessionmaker() as session:
                await runtime_control.acquire_maintenance(session, owner="owner-a", lease_seconds=5)
                first = str((await runtime_control.get_state(session))["lease_expires_at"])
            stop = asyncio.Event()
            task = asyncio.create_task(
                catalog_migration._renew_maintenance_loop(
                    engine, "owner-a", lease_seconds=30, stop=stop, interval_seconds=0.05
                )
            )
            await asyncio.sleep(0.2)
            stop.set()
            await asyncio.gather(task)
            assert task.done() and not task.cancelled()
            async with sessionmaker() as session:
                renewed = str((await runtime_control.get_state(session))["lease_expires_at"])
            assert renewed > first
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_probe_runtime_state_fails_closed_on_unreadable_table(tmp_path):
    """runtime_control 模块可用但表读取失败（pre-v3 库）时 fail closed，不再静默降级。"""

    async def run() -> None:
        engine = catalog_migration._create_engine(_sqlite_url(tmp_path / "legacy.db"))
        try:
            async with engine.begin() as conn:
                # pre-v3 旧库形态：只有 ORM 表，没有 core_runtime_control
                await conn.run_sync(Base.metadata.create_all)
            with pytest.raises(CatalogMigrationError, match="--skip-drain"):
                await catalog_migration._probe_runtime_state(engine)
        finally:
            await engine.dispose()

    asyncio.run(run())
