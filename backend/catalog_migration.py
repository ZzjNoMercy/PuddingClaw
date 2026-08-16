"""Core Catalog 跨数据库迁移工具（PostgreSQL <-> SQLite）。

流程（双向对称，禁止长期双写）::

    停写(drain) -> 一致性导出 -> 导入临时库 -> 校验 -> 原子切换配置 -> 保留回滚

- 导出：源库一个读事务内按依赖顺序读出 12 张 Core 表，datetime 统一序列化为
  ISO 字符串（naive 按 UTC 视作 aware），JSON 列统一为原生对象。
- 导入：目标库先在 ``migrate_to_latest`` 下建 schema，要求每张 Core 表为空
  （防重复导入产生重复行），全程一个事务，失败回滚不留半迁移。
- 校验：逐表行数 + 主键集合 + 内容摘要（按主键排序的行规范化 sha256 链式
  聚合）一致；SQLite 目标加 ``PRAGMA foreign_key_check`` / ``integrity_check``；
  目标 schema version 必须等于 CURRENT_SCHEMA_VERSION。校验不过绝不进入配置切换。
- 切换：先 ``os.replace`` 落位 SQLite 文件（pg-to-sqlite），再原子写
  config.json 的 database 段（原文件备份为 ``config.json.pre-migration-<时间戳>``）。

明确排除：``core_runtime_control``（运行时状态）、``core_schema_migrations``
（目标端由 migrate_to_latest 自己写）、gbrain 独立 database、Milvus、用户业务
数据库、以文件为事实源的知识/Session/附件。源数据库一律不删不改。

用法（在 backend 目录下运行）::

    python -m catalog_migration pg-to-sqlite [--target-path PATH] [--skip-drain] [--drain-timeout N]
    python -m catalog_migration sqlite-to-pg --target-url URL [--skip-drain] [--drain-timeout N]
    python -m catalog_migration verify --source URL --target URL

各子命令以 JSON 打印迁移报告；中文诊断输出到 stderr，失败以非 0 退出。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import DateTime, Table, func, inspect, select, text
from sqlalchemy.event import listen
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.types import JSON

import db as db_module
from db import get_database_url, is_sqlite_url
from knowledge.models import Base
from runtime_identity.paths import PuddingClawPaths
from schema_migrations import CURRENT_SCHEMA_VERSION, current_schema_version, migrate_to_latest

try:
    from runtime_control import (
        MaintenanceConflictError,
        acquire_maintenance,
        enter_maintenance,
        get_state,
        queue_running_counts,
        release_maintenance,
        renew_maintenance,
    )

    _RUNTIME_CONTROL_AVAILABLE = True
except ImportError:  # pragma: no cover - runtime_control 由并行任务提供
    _RUNTIME_CONTROL_AVAILABLE = False

    class MaintenanceConflictError(RuntimeError):  # type: ignore[no-redef]
        """runtime_control 缺失时的占位异常（不会被抛出）。"""

    acquire_maintenance = None  # type: ignore[assignment]
    enter_maintenance = None  # type: ignore[assignment]
    get_state = None  # type: ignore[assignment]
    queue_running_counts = None  # type: ignore[assignment]
    release_maintenance = None  # type: ignore[assignment]
    renew_maintenance = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# 导出/导入顺序按表间依赖排列（被引用表在前）。
EXPORT_TABLES: tuple[str, ...] = (
    "knowledge_bases",
    "knowledge_documents",
    "read_later_items",
    "knowledge_database_sources",
    "knowledge_table_assets",
    "analytics_query_results",
    "worker_access_logs",
    "knowledge_import_jobs",
    "knowledge_import_events",
    "semantic_dimension_build_jobs",
    "semantic_dimension_build_events",
    "task_notifications",
)

# 明确排除：运行时状态与 schema 版本表（后者由目标端 migrate_to_latest 自己写）。
EXCLUDED_TABLES: tuple[str, ...] = ("core_runtime_control", "core_schema_migrations")

_DRAIN_POLL_INTERVAL_SECONDS = 2.0
# 维护租约必须覆盖整个迁移窗口，避免大目录迁移到一半租约过期被他人接管。
_MIN_MAINTENANCE_LEASE_SECONDS = 900


class CatalogMigrationError(RuntimeError):
    """迁移失败，message 为面向用户的中文诊断。"""


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _redact_url(url: str) -> str:
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url


def _default_owner() -> str:
    return f"catalog-migration-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def _create_engine(url: str) -> AsyncEngine:
    """为迁移创建独立引擎；SQLite 复用 db.py 的连接 PRAGMA 与显式 BEGIN 事件。

    isolation_level=None + 显式 BEGIN 让 schema DDL 与数据写入处于同一个
    SQLAlchemy 事务里，失败整体回滚，不留半迁移。
    """

    engine = create_async_engine(url, **db_module._engine_kwargs(url))
    if is_sqlite_url(url):
        listen(engine.sync_engine, "connect", db_module._apply_sqlite_connection_pragmas)
        listen(engine.sync_engine, "begin", db_module._emit_sqlite_begin)
    return engine


def _serialize_value(column: Any, value: Any) -> Any:
    """导出侧归一化：datetime -> ISO 字符串（naive 按 UTC），JSON 列保持原生对象。"""

    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(column.type, JSON) and isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def _deserialize_value(column: Any, value: Any) -> Any:
    """导入侧还原：DateTime 列的 ISO 字符串转回 aware UTC datetime。"""

    if value is None:
        return None
    if isinstance(column.type, DateTime) and isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return value


def _digest_value(column: Any, value: Any) -> Any:
    """内容摘要用的逐值规范化。

    复用导出侧归一（``_serialize_value``：datetime -> ISO、JSON 列的 str ->
    原生对象），再把 dict/list 按 sort_keys 序列化，使 PostgreSQL JSONB 读出
    的 dict 与 SQLite 读出的 JSON 字符串得到同一表示。
    """

    normalized = _serialize_value(column, value)
    if isinstance(normalized, (dict, list)):
        return json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return normalized


def _rows_content_digest(table: Table, rows: list[Any]) -> str:
    """对行集合做与数据库 collation 无关的稳定摘要。

    PostgreSQL 与 SQLite 对 ``-``、``_`` 等字符的排序规则可能不同，不能
    依赖 ``ORDER BY primary_key`` 的数据库返回顺序。这里先规范化主键与整行，
    再按 Python 的确定性字符串顺序排序，确保相同行集合跨方言得到相同摘要。
    """

    pk_column = table.primary_key.columns[0]
    canonical_rows: list[tuple[str, bytes]] = []
    for row in rows:
        mapping = row._mapping if hasattr(row, "_mapping") else row
        primary_key = json.dumps(
            _digest_value(pk_column, mapping[pk_column.name]),
            sort_keys=True,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        canonical = {
            column.name: _digest_value(column, mapping[column.name]) for column in table.c
        }
        payload = json.dumps(
            canonical,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
        canonical_rows.append((primary_key, payload))

    digest = hashlib.sha256()
    for _primary_key, payload in sorted(canonical_rows, key=lambda item: item[0]):
        digest.update(payload)
        digest.update(b"\n")
    return digest.hexdigest()


async def _table_content_digest(conn: AsyncConnection, table: Table) -> str:
    """读取整表并计算与数据库排序规则无关的内容摘要。"""

    result = await conn.execute(select(table))
    return _rows_content_digest(table, list(result))


async def export_catalog(source_url: str) -> dict[str, list[dict[str, Any]]]:
    """源库一个一致性读事务内按依赖顺序导出全部 Core 表。"""

    engine = _create_engine(source_url)
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                data: dict[str, list[dict[str, Any]]] = {}
                for name in EXPORT_TABLES:
                    table = Base.metadata.tables[name]
                    result = await conn.execute(select(table))
                    data[name] = [
                        {column.name: _serialize_value(column, row._mapping[column.name]) for column in table.c}
                        for row in result
                    ]
        return data
    finally:
        await engine.dispose()


async def _insert_rows(conn: AsyncConnection, table: Table, rows: list[dict[str, Any]]) -> None:
    """单表批量插入（独立成函数便于测试模拟导入中断）。"""

    await conn.execute(table.insert(), rows)


async def import_catalog(target_url: str, data: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """把导出数据导入空的目标库；全程一个事务，失败整体回滚。

    目标库先经 ``migrate_to_latest`` 建/升级 schema；随后要求每张 Core 表
    count==0，否则拒绝导入（防重复导入产生重复行）。
    """

    engine = _create_engine(target_url)
    try:
        imported: dict[str, int] = {}
        async with engine.begin() as conn:
            await conn.run_sync(migrate_to_latest)
            for name in EXPORT_TABLES:
                table = Base.metadata.tables[name]
                count = int((await conn.execute(select(func.count()).select_from(table))).scalar_one())
                if count:
                    raise CatalogMigrationError(
                        f"目标库非空（表 {name} 已有 {count} 行），为防止重复导入产生重复行，已拒绝执行；"
                        "请换一个空的目标库。"
                    )
            for name in EXPORT_TABLES:
                table = Base.metadata.tables[name]
                rows = [
                    {column.name: _deserialize_value(column, raw.get(column.name)) for column in table.c}
                    for raw in (data.get(name) or [])
                ]
                if rows:
                    await _insert_rows(conn, table, rows)
                imported[name] = len(rows)
        return {"tables": imported, "total_rows": sum(imported.values())}
    finally:
        await engine.dispose()


async def validate_migration(source_url: str, target_url: str) -> dict[str, Any]:
    """逐表校验行数、主键集合与内容摘要一致，并检查目标端完整性与 schema 版本。

    内容摘要：按主键排序后对每行的规范化表示（列名排序、datetime 归一 ISO、
    JSON 列 sort_keys 序列化）做 sha256 链式聚合，源/目标逐表比对——只改非
    主键字段也会被检出。任何失败都会让 ok=False 并附明细；调用方在校验不过
    时绝不进入配置切换。
    """

    result: dict[str, Any] = {
        "ok": True,
        "source_url": _redact_url(source_url),
        "target_url": _redact_url(target_url),
        "tables": {},
        "schema_version": None,
        "sqlite_checks": None,
        "errors": [],
    }
    source_engine = _create_engine(source_url)
    target_engine = _create_engine(target_url)
    try:
        async with source_engine.connect() as src, target_engine.connect() as tgt:
            for name in EXPORT_TABLES:
                table = Base.metadata.tables[name]
                pk_column = table.primary_key.columns[0]
                source_count = int((await src.execute(select(func.count()).select_from(table))).scalar_one())
                target_count = int((await tgt.execute(select(func.count()).select_from(table))).scalar_one())
                source_pks = {str(row[0]) for row in (await src.execute(select(pk_column))).all()}
                target_pks = {str(row[0]) for row in (await tgt.execute(select(pk_column))).all()}
                source_digest = await _table_content_digest(src, table)
                target_digest = await _table_content_digest(tgt, table)
                detail = {
                    "source_rows": source_count,
                    "target_rows": target_count,
                    "row_count_match": source_count == target_count,
                    "primary_keys_match": source_pks == target_pks,
                    "content_digest_match": source_digest == target_digest,
                    "source_digest": source_digest,
                    "target_digest": target_digest,
                }
                if source_count != target_count:
                    result["errors"].append(f"表 {name} 行数不一致：源 {source_count}，目标 {target_count}")
                if source_pks != target_pks:
                    missing = sorted(source_pks - target_pks)[:5]
                    extra = sorted(target_pks - source_pks)[:5]
                    result["errors"].append(
                        f"表 {name} 主键集合不一致：目标缺少 {missing}，目标多出 {extra}"
                    )
                if source_digest != target_digest:
                    result["errors"].append(f"表 {name} 内容摘要（digest）不一致：行内容与源库存在差异")
                result["tables"][name] = detail
            version = await tgt.run_sync(current_schema_version)
            result["schema_version"] = version
            if version != CURRENT_SCHEMA_VERSION:
                result["errors"].append(
                    f"目标库 schema version 为 {version}，期望 {CURRENT_SCHEMA_VERSION}"
                )
            if is_sqlite_url(target_url):
                fk_rows = (await tgt.execute(text("PRAGMA foreign_key_check"))).all()
                integrity = str((await tgt.execute(text("PRAGMA integrity_check"))).scalar_one())
                result["sqlite_checks"] = {
                    "foreign_key_violations": len(fk_rows),
                    "integrity_check": integrity,
                }
                if fk_rows:
                    result["errors"].append(f"目标 SQLite 存在 {len(fk_rows)} 条外键违规")
                if integrity != "ok":
                    result["errors"].append(f"目标 SQLite integrity_check={integrity}")
    finally:
        await source_engine.dispose()
        await target_engine.dispose()
    result["ok"] = not result["errors"]
    return result


async def _probe_runtime_state(engine: AsyncEngine) -> dict[str, Any] | None:
    """读取 runtime_control 状态。

    仅当 runtime_control 模块本身缺失（并行任务未交付，不应发生）时降级返回
    None；模块可用但读取失败时 fail closed——在多副本 PostgreSQL 上静默继续
    等于做了一次无停写迁移。
    """

    if not _RUNTIME_CONTROL_AVAILABLE:
        logger.warning("[catalog-migration] runtime_control 不可用，按 --skip-drain 语义继续（请确认已无写入）。")
        return None
    try:
        sessionmaker = async_sessionmaker(engine)
        async with sessionmaker() as session:
            return await get_state(session)
    except Exception as exc:
        raise CatalogMigrationError(
            f"无法读取 runtime_control 停写状态（{exc}），已中止迁移：多副本部署下继续等于无停写迁移。"
            "请先确认 Core schema 已升级到 v3（用当前版本正常启动一次 Backend 即可）；"
            "仅在单实例本地环境、且已确认无任何写入时，才可用 --skip-drain 跳过停写协议。"
        ) from exc


async def _drain_and_enter_maintenance(
    engine: AsyncEngine,
    *,
    owner: str,
    reason: str,
    drain_timeout: int,
) -> None:
    """acquire_maintenance -> 等两队列 running 归零 -> enter_maintenance。"""

    lease_seconds = max(_MIN_MAINTENANCE_LEASE_SECONDS, int(drain_timeout))
    sessionmaker = async_sessionmaker(engine)
    async with sessionmaker() as session:
        try:
            await acquire_maintenance(
                session, owner=owner, lease_seconds=lease_seconds, reason=reason or "catalog migration"
            )
        except MaintenanceConflictError as exc:
            raise CatalogMigrationError(f"无法获取维护租约：{exc}") from exc
    deadline = time.monotonic() + max(1, int(drain_timeout))
    while True:
        async with sessionmaker() as session:
            counts = await queue_running_counts(session)
        remaining = {name: queue["running"] for name, queue in counts.items() if queue.get("running")}
        if not remaining:
            break
        if time.monotonic() >= deadline:
            await _release_maintenance(engine, owner, reason="drain timeout, migration aborted")
            raise CatalogMigrationError(
                f"停写等待超时（{drain_timeout}s），仍有运行中任务："
                f"{json.dumps(remaining, ensure_ascii=False)}；已中止迁移，源库与配置均未变更。"
            )
        await asyncio.sleep(_DRAIN_POLL_INTERVAL_SECONDS)
    async with sessionmaker() as session:
        try:
            await enter_maintenance(session, owner=owner, lease_seconds=lease_seconds)
        except MaintenanceConflictError as exc:
            raise CatalogMigrationError(f"进入维护模式失败：{exc}") from exc


async def _release_maintenance(engine: AsyncEngine, owner: str, *, reason: str = "") -> None:
    """尽力释放维护租约；失败仅告警（租约到期后自动失效）。"""

    if not _RUNTIME_CONTROL_AVAILABLE:
        return
    try:
        sessionmaker = async_sessionmaker(engine)
        async with sessionmaker() as session:
            await release_maintenance(session, owner=owner, reason=reason)
    except Exception as exc:
        logger.warning("[catalog-migration] 释放维护租约失败（可等待租约自动过期）：%s", exc)


async def _renew_maintenance_loop(
    engine: AsyncEngine,
    owner: str,
    *,
    lease_seconds: int,
    stop: asyncio.Event,
    interval_seconds: float | None = None,
) -> None:
    """导出/导入/校验期间定期续租维护 lease（默认间隔 lease/3）。

    lease 固定为 max(900, drain_timeout)，长导出期间可能中途过期被他人接管；
    续租失败仅告警并按间隔重试，租约被接管（冲突）则记录错误后停止——迁移
    是否可信由最终 validate_migration 把关。
    """

    interval = interval_seconds if interval_seconds is not None else max(1.0, lease_seconds / 3)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass
        if not _RUNTIME_CONTROL_AVAILABLE:
            return
        try:
            sessionmaker = async_sessionmaker(engine)
            async with sessionmaker() as session:
                await renew_maintenance(session, owner=owner, lease_seconds=lease_seconds)
        except MaintenanceConflictError as exc:
            logger.error("[catalog-migration] 维护租约续租冲突，租约可能已被接管：%s", exc)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[catalog-migration] 维护租约续租失败（%s），将按间隔重试。", exc)


def _start_maintenance_renewal(engine: AsyncEngine, owner: str, *, lease_seconds: int) -> tuple[asyncio.Event, asyncio.Task[None]]:
    """启动后台续租任务；返回 (stop_event, task)，迁移收尾时 set 事件并等待 task。"""

    stop = asyncio.Event()
    task = asyncio.create_task(
        _renew_maintenance_loop(engine, owner, lease_seconds=lease_seconds, stop=stop),
        name="catalog-migration-lease-renewal",
    )
    return stop, task


async def _stop_maintenance_renewal(renewal: tuple[asyncio.Event, asyncio.Task[None]] | None) -> None:
    if renewal is None:
        return
    stop, task = renewal
    stop.set()
    await asyncio.gather(task, return_exceptions=True)


def _install_sqlite_file(tmp_path: Path, target_path: Path) -> str | None:
    """把校验通过的临时库原子落位；已存在的目标先保留为 .pre-migration-<时间戳>。"""

    previous: Path | None = None
    if target_path.exists():
        previous = target_path.with_name(f"{target_path.name}.pre-migration-{_utc_timestamp()}")
        if previous.exists():
            previous = target_path.with_name(
                f"{target_path.name}.pre-migration-{_utc_timestamp()}-{uuid.uuid4().hex[:6]}"
            )
        os.replace(target_path, previous)
    try:
        os.replace(tmp_path, target_path)
    except Exception:
        # 落位失败时尽力把原库放回去，避免数据丢失。
        if previous is not None and previous.exists() and not target_path.exists():
            os.replace(previous, target_path)
        raise
    # 旧库的 WAL/SHM 残留与新库文件不匹配，必须清掉。
    for suffix in ("-wal", "-shm"):
        target_path.with_name(target_path.name + suffix).unlink(missing_ok=True)
    return str(previous) if previous is not None else None


def _config_file_path() -> Path:
    import config as config_module

    if config_module.CONFIG_FILE is not None:
        return Path(config_module.CONFIG_FILE)
    return PuddingClawPaths.from_environment().root / "config.json"


def _write_database_config_section(database_section: dict[str, Any]) -> dict[str, Any]:
    """原子更新 config.json 的 database 段（迁移工具私有助手，不经 config.save_config）。

    先写同目录临时文件再 ``os.replace``；原文件备份为
    ``config.json.pre-migration-<时间戳>`` 供回滚。
    """

    path = _config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogMigrationError(f"无法解析现有配置文件 {path}：{exc}；已中止配置切换。") from exc
        if not isinstance(loaded, dict):
            raise CatalogMigrationError(f"配置文件 {path} 不是 JSON 对象；已中止配置切换。")
        raw = loaded
    backup_path: Path | None = None
    if path.exists():
        backup_path = path.with_name(f"{path.name}.pre-migration-{_utc_timestamp()}")
        if backup_path.exists():
            backup_path = path.with_name(f"{path.name}.pre-migration-{_utc_timestamp()}-{uuid.uuid4().hex[:6]}")
        shutil.copy2(path, backup_path)
    raw["database"] = database_section
    raw.setdefault("schema_version", 1)
    fd, tmp_name = tempfile.mkstemp(prefix=".config.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(raw, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return {
        "config_path": str(path),
        "backup_path": str(backup_path) if backup_path is not None else None,
        "database": database_section,
    }


def _postgres_config_section(target_url: str) -> dict[str, Any]:
    """从目标 URL 生成 database 配置段；密码按 Credential Vault 约定存 password_ref。"""

    parsed = urlparse(target_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    password = unquote(parsed.password or "")
    section: dict[str, Any] = {
        "provider": "postgresql",
        "source": "external",
        "host": parsed.hostname or "127.0.0.1",
        "port": int(parsed.port or 5432),
        "database": unquote(parsed.path.lstrip("/")),
        "username": unquote(parsed.username or ""),
    }
    if password:
        from provider_registry import LocalCredentialStore

        section["password_ref"] = LocalCredentialStore().put("database-config", password)
    return section


async def migrate_postgres_to_sqlite(
    *,
    target_path: Path | str | None = None,
    owner: str | None = None,
    reason: str = "",
    skip_drain: bool = False,
    drain_timeout: int = 300,
) -> dict[str, Any]:
    """把 Core Catalog 从当前配置的 PostgreSQL 迁移到本地 SQLite 文件。"""

    started_at = datetime.now(timezone.utc)
    source_url = get_database_url()
    if is_sqlite_url(source_url):
        raise CatalogMigrationError(
            "当前数据库是 SQLite，pg-to-sqlite 仅适用于 PostgreSQL 源；反向迁移请使用 sqlite-to-pg。"
        )
    if not source_url.startswith("postgresql"):
        raise CatalogMigrationError(
            f"当前数据库不是 PostgreSQL（{source_url.split('://', 1)[0]}），无法执行 pg-to-sqlite 迁移。"
        )
    owner = owner or _default_owner()
    if target_path is None:
        target_path = PuddingClawPaths.from_environment().databases() / "catalog.sqlite3"
    target_path = Path(target_path).expanduser()
    tmp_path = target_path.with_name(target_path.name + ".migrating")

    report: dict[str, Any] = {
        "ok": False,
        "direction": "postgresql->sqlite",
        "source_url": _redact_url(source_url),
        "target_path": str(target_path),
        "owner": owner,
        "skip_drain": skip_drain,
        "started_at": started_at.isoformat(),
        "source_backup_reminder": (
            "迁移不会修改源 PostgreSQL 中的任何数据；仍建议先对源库执行 pg_dump 逻辑备份。"
        ),
    }
    source_engine = _create_engine(source_url)
    maintenance_held = False
    renewal: tuple[asyncio.Event, asyncio.Task[None]] | None = None
    try:
        # 前置校验：源可读、目标目录可写、无其他进行中的迁移。
        async with source_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(target_path.parent, os.W_OK):
            raise CatalogMigrationError(f"目标目录不可写：{target_path.parent}")
        tmp_path.unlink(missing_ok=True)  # 上次失败残留的临时文件
        state = await _probe_runtime_state(source_engine)
        if (
            state is not None
            and state.get("write_mode") != "normal"
            and state.get("maintenance_owner") != owner
        ):
            raise CatalogMigrationError(
                f"检测到另一个维护/迁移流程正在进行（owner={state.get('maintenance_owner')}，"
                f"write_mode={state.get('write_mode')}），拒绝并发迁移。"
            )
        logger.warning(report["source_backup_reminder"])

        if state is None or skip_drain:
            logger.warning("[catalog-migration] 跳过停写（drain）；请确认已无应用写入源库。")
            report["drain"] = {"skipped": True}
        else:
            lease_seconds = max(_MIN_MAINTENANCE_LEASE_SECONDS, int(drain_timeout))
            await _drain_and_enter_maintenance(
                source_engine, owner=owner, reason=reason, drain_timeout=drain_timeout
            )
            maintenance_held = True
            renewal = _start_maintenance_renewal(source_engine, owner, lease_seconds=lease_seconds)
            report["drain"] = {"skipped": False, "write_mode": "maintenance", "lease_seconds": lease_seconds}

        data = await export_catalog(source_url)
        tmp_url = f"sqlite+aiosqlite:///{tmp_path}"
        import_result = await import_catalog(tmp_url, data)
        validation = await validate_migration(source_url, tmp_url)
        if not validation["ok"]:
            raise CatalogMigrationError(
                "迁移校验失败，已中止（源库与现有配置均未变更）：" + "；".join(validation["errors"])
            )
        for suffix in ("-wal", "-shm"):
            tmp_path.with_name(tmp_path.name + suffix).unlink(missing_ok=True)
        previous_catalog = _install_sqlite_file(tmp_path, target_path)
        config_result = _write_database_config_section({"provider": "sqlite", "source": "local_file"})

        report.update(
            {
                "ok": True,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "tables": import_result["tables"],
                "total_rows": import_result["total_rows"],
                "validation": validation,
                "previous_catalog": previous_catalog,
                "config": config_result,
                "rollback": (
                    "源 PostgreSQL 未被修改或删除。回滚：还原 config.json 备份"
                    f"（{config_result['backup_path']}）并重启；新 SQLite 文件可直接删除"
                    + (f"，旧 SQLite 文件保留在 {previous_catalog}。" if previous_catalog else "。")
                ),
            }
        )
        return report
    finally:
        await _stop_maintenance_renewal(renewal)
        if maintenance_held:
            await _release_maintenance(source_engine, owner, reason=f"catalog migration done: {reason}")
        await source_engine.dispose()
        tmp_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            tmp_path.with_name(tmp_path.name + suffix).unlink(missing_ok=True)


async def migrate_sqlite_to_postgres(
    *,
    target_url: str,
    owner: str | None = None,
    reason: str = "",
    skip_drain: bool = False,
    drain_timeout: int = 300,
) -> dict[str, Any]:
    """把 Core Catalog 从当前配置的本地 SQLite 迁移到目标 PostgreSQL。"""

    started_at = datetime.now(timezone.utc)
    source_url = get_database_url()
    if not is_sqlite_url(source_url):
        raise CatalogMigrationError(
            f"当前数据库不是 SQLite（{source_url.split('://', 1)[0]}），sqlite-to-pg 仅适用于 SQLite 源；"
            "反向迁移请使用 pg-to-sqlite。"
        )
    if not target_url.startswith("postgresql"):
        raise CatalogMigrationError(f"目标 URL 不是 PostgreSQL：{_redact_url(target_url)}")
    owner = owner or _default_owner()

    report: dict[str, Any] = {
        "ok": False,
        "direction": "sqlite->postgresql",
        "source_url": _redact_url(source_url),
        "target_url": _redact_url(target_url),
        "owner": owner,
        "skip_drain": skip_drain,
        "started_at": started_at.isoformat(),
    }

    # 迁移前在线备份 SQLite 源库，作为回滚文件。
    import catalog_backup

    try:
        manifest = catalog_backup.backup_catalog()
    except catalog_backup.CatalogBackupError as exc:
        raise CatalogMigrationError(f"迁移前备份失败，已中止：{exc}") from exc
    backup_path = catalog_backup._default_backup_dir() / str(manifest["backup_file"])
    report["sqlite_backup"] = {"path": str(backup_path), "manifest": manifest}

    # 目标 PG 必须为空：Core 表不存在（由 migrate_to_latest 建）或 count==0。
    target_engine = _create_engine(target_url)
    try:
        async with target_engine.connect() as conn:
            existing = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
            for name in EXPORT_TABLES:
                if name not in existing:
                    continue
                count = int(
                    (
                        await conn.execute(select(func.count()).select_from(Base.metadata.tables[name]))
                    ).scalar_one()
                )
                if count:
                    raise CatalogMigrationError(
                        f"目标 PostgreSQL 非空（表 {name} 已有 {count} 行），为防止重复导入已拒绝执行；"
                        "请换一个空的目标库。"
                    )
    finally:
        await target_engine.dispose()

    source_engine = _create_engine(source_url)
    maintenance_held = False
    renewal: tuple[asyncio.Event, asyncio.Task[None]] | None = None
    try:
        state = await _probe_runtime_state(source_engine)
        if (
            state is not None
            and state.get("write_mode") != "normal"
            and state.get("maintenance_owner") != owner
        ):
            raise CatalogMigrationError(
                f"检测到另一个维护/迁移流程正在进行（owner={state.get('maintenance_owner')}，"
                f"write_mode={state.get('write_mode')}），拒绝并发迁移。"
            )
        if state is None or skip_drain:
            logger.warning("[catalog-migration] 跳过停写（drain）；请确认已无应用写入源库。")
            report["drain"] = {"skipped": True}
        else:
            lease_seconds = max(_MIN_MAINTENANCE_LEASE_SECONDS, int(drain_timeout))
            await _drain_and_enter_maintenance(
                source_engine, owner=owner, reason=reason, drain_timeout=drain_timeout
            )
            maintenance_held = True
            renewal = _start_maintenance_renewal(source_engine, owner, lease_seconds=lease_seconds)
            report["drain"] = {"skipped": False, "write_mode": "maintenance", "lease_seconds": lease_seconds}

        data = await export_catalog(source_url)
        import_result = await import_catalog(target_url, data)
        validation = await validate_migration(source_url, target_url)
        if not validation["ok"]:
            raise CatalogMigrationError(
                "迁移校验失败，已中止（目标库已回滚、源库与配置未变更）：" + "；".join(validation["errors"])
            )
        config_result = _write_database_config_section(_postgres_config_section(target_url))

        report.update(
            {
                "ok": True,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "tables": import_result["tables"],
                "total_rows": import_result["total_rows"],
                "validation": validation,
                "config": config_result,
                "rollback": (
                    f"SQLite 源库未被修改；回滚备份文件：{backup_path}"
                    "（python -m catalog_backup restore 恢复 + 还原 config.json 备份 "
                    f"{config_result['backup_path']} 后重启）。"
                ),
            }
        )
        return report
    finally:
        await _stop_maintenance_renewal(renewal)
        if maintenance_held:
            await _release_maintenance(source_engine, owner, reason=f"catalog migration done: {reason}")
        await source_engine.dispose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m catalog_migration",
        description="Core Catalog 跨数据库迁移（PostgreSQL <-> SQLite）：停写 -> 导出 -> 导入 -> 校验 -> 原子切换。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_down = sub.add_parser("pg-to-sqlite", help="从当前配置的 PostgreSQL 迁移到本地 SQLite")
    p_down.add_argument("--target-path", type=Path, default=None, help="目标 SQLite 文件（默认 $PUDDINGCLAW_HOME/db/catalog.sqlite3）")
    p_down.add_argument("--skip-drain", action="store_true", help="跳过停写协议（需自行确认已无写入）")
    p_down.add_argument("--drain-timeout", type=int, default=300, help="等待运行中任务归零的超时秒数（默认 300）")
    p_down.add_argument("--reason", default="", help="迁移原因（记入维护租约）")

    p_up = sub.add_parser("sqlite-to-pg", help="从当前配置的 SQLite 迁移到目标 PostgreSQL")
    p_up.add_argument("--target-url", required=True, help="目标 PostgreSQL URL（postgresql[+asyncpg]://...）")
    p_up.add_argument("--skip-drain", action="store_true", help="跳过停写协议（需自行确认已无写入）")
    p_up.add_argument("--drain-timeout", type=int, default=300, help="等待运行中任务归零的超时秒数（默认 300）")
    p_up.add_argument("--reason", default="", help="迁移原因（记入维护租约）")

    p_verify = sub.add_parser("verify", help="只做迁移校验（行数/主键/完整性/schema 版本），不改任何数据")
    p_verify.add_argument("--source", required=True, help="源数据库 URL")
    p_verify.add_argument("--target", required=True, help="目标数据库 URL")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "pg-to-sqlite":
            result = asyncio.run(
                migrate_postgres_to_sqlite(
                    target_path=args.target_path,
                    reason=args.reason,
                    skip_drain=args.skip_drain,
                    drain_timeout=args.drain_timeout,
                )
            )
        elif args.command == "sqlite-to-pg":
            result = asyncio.run(
                migrate_sqlite_to_postgres(
                    target_url=args.target_url,
                    reason=args.reason,
                    skip_drain=args.skip_drain,
                    drain_timeout=args.drain_timeout,
                )
            )
        elif args.command == "verify":
            result = asyncio.run(validate_migration(args.source, args.target))
        else:  # pragma: no cover - argparse required=True 已拦截
            raise CatalogMigrationError(f"未知命令：{args.command}")
    except CatalogMigrationError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # 兜底，仍给出中文诊断
        print(f"错误：操作失败（{exc}）", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.command == "verify" and not result.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
