"""Core SQLite Catalog 的在线备份、完整性校验与恢复。

仅支持 SQLite provider；PostgreSQL 服务端部署应使用数据库级逻辑备份方案
（如 pg_dump），本模块会拒绝执行并给出中文诊断。

用法（在 backend 目录下运行）::

    python -m catalog_backup backup [--dest DIR]
    python -m catalog_backup verify PATH
    python -m catalog_backup restore PATH [--target PATH]
    python -m catalog_backup list [--dest DIR]

各子命令以 JSON 打印结果；失败时向 stderr 输出中文诊断并以非 0 退出。

备份机制：对源库执行 ``VACUUM INTO`` 产出一颗一致性快照（SQLite 会自动
处理 WAL，无需手工 checkpoint），再对快照跑 ``PRAGMA integrity_check``
并附带 ``<备份名>.manifest.json`` 清单（schema 版本、sha256、大小等）。
恢复前会探测 ``$PUDDINGCLAW_HOME/state/backend.lease``：Backend 正在运行
时拒绝恢复，避免与在线写入冲突。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_SQLITE_URL_PREFIXES = ("sqlite+aiosqlite:///", "sqlite:///")
_VERSION_TABLE = "core_schema_migrations"


class CatalogBackupError(RuntimeError):
    """备份/校验/恢复失败，message 为面向用户的中文诊断。"""


def _app_version() -> str:
    pyproject = Path(__file__).resolve().parent / "pyproject.toml"
    if tomllib is None:
        return "unknown"
    try:
        with pyproject.open("rb") as fh:
            return str(tomllib.load(fh).get("project", {}).get("version") or "unknown")
    except Exception:
        return "unknown"


def _catalog_db_path() -> Path:
    """从 get_database_url() 解析 SQLite 文件路径；非 SQLite 拒绝执行。"""

    from db import get_database_url, is_sqlite_url

    url = get_database_url()
    if not is_sqlite_url(url):
        raise CatalogBackupError(
            f"当前数据库不是 SQLite（{url.split('://', 1)[0]}），本工具仅支持 SQLite 备份；"
            "PostgreSQL 服务端部署请使用数据库级逻辑备份方案（如 pg_dump/pg_basebackup）。"
        )
    for prefix in _SQLITE_URL_PREFIXES:
        if url.startswith(prefix):
            # 绝对路径的 URL 形如 sqlite+aiosqlite:////Users/...（前缀后仍以 / 开头）。
            path = url[len(prefix):]
            if not path.startswith("/"):
                path = "/" + path
            return Path(path)
    raise CatalogBackupError(f"无法从数据库 URL 解析 SQLite 文件路径：{url}")


def _default_backup_dir() -> Path:
    from runtime_identity.paths import PuddingClawPaths

    return PuddingClawPaths.from_environment().databases() / "backups"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity_check(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else ""
    finally:
        conn.close()


def _schema_version(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        try:
            row = conn.execute(f"SELECT MAX(version) FROM {_VERSION_TABLE}").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _manifest_path(backup_path: Path) -> Path:
    return backup_path.with_name(backup_path.name + ".manifest.json")


def backup_catalog(dest_dir: Path | None = None) -> dict:
    """在线备份 Core SQLite Catalog，返回伴随清单（manifest）dict。"""

    source = _catalog_db_path()
    if not source.exists():
        raise CatalogBackupError(f"目录数据库文件不存在：{source}")
    dest = Path(dest_dir) if dest_dir is not None else _default_backup_dir()
    dest.mkdir(parents=True, exist_ok=True)

    tmp_path = dest / f".catalog-backup-{uuid.uuid4().hex[:8]}.tmp"
    if tmp_path.exists():
        tmp_path.unlink()
    # VACUUM INTO 产出包含 WAL 已提交内容的一致性独立快照；目标必须不存在。
    escaped = str(tmp_path).replace("'", "''")
    conn = sqlite3.connect(str(source))
    try:
        conn.isolation_level = None  # VACUUM 不能在事务里执行
        conn.execute(f"VACUUM INTO '{escaped}'")
    except sqlite3.Error as exc:
        tmp_path.unlink(missing_ok=True)
        raise CatalogBackupError(f"备份快照失败（VACUUM INTO）：{exc}") from exc
    finally:
        conn.close()

    try:
        check = _integrity_check(tmp_path)
        if check != "ok":
            raise CatalogBackupError(f"备份快照完整性校验失败（integrity_check={check}），已删除快照。")
        schema_version = _schema_version(tmp_path)
        digest = _sha256(tmp_path)
        size = tmp_path.stat().st_size

        final_path = dest / f"catalog-{_utc_timestamp()}.sqlite3"
        if final_path.exists():  # 同一秒内重复备份
            final_path = dest / f"catalog-{_utc_timestamp()}-{uuid.uuid4().hex[:6]}.sqlite3"
        os.replace(tmp_path, final_path)

        manifest = {
            "backup_file": final_path.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "app_version": _app_version(),
            "schema_version": schema_version,
            "sha256": digest,
            "size_bytes": size,
            "integrity_check": "ok",
            "source_path": str(source),
        }
        _manifest_path(final_path).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def verify_backup(backup_path: Path) -> dict:
    """校验备份文件：存在性、清单 sha256/大小、SQLite integrity_check。"""

    path = Path(backup_path)
    if not path.exists():
        raise CatalogBackupError(f"备份文件不存在：{path}")

    manifest_file = _manifest_path(path)
    has_manifest = manifest_file.exists()
    if has_manifest:
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogBackupError(f"备份清单无法解析：{manifest_file}（{exc}）") from exc
        expected_sha = str(manifest.get("sha256") or "")
        if expected_sha:
            actual_sha = _sha256(path)
            if actual_sha != expected_sha:
                raise CatalogBackupError(
                    f"备份文件 sha256 与清单不一致（文件可能被篡改或损坏）：{path}"
                )
        expected_size = manifest.get("size_bytes")
        if expected_size is not None and path.stat().st_size != int(expected_size):
            raise CatalogBackupError(f"备份文件大小与清单不一致（文件可能被截断）：{path}")

    check = _integrity_check(path)
    if check != "ok":
        raise CatalogBackupError(f"备份文件完整性校验失败（integrity_check={check}）：{path}")
    return {
        "path": str(path),
        "integrity_check": "ok",
        "schema_version": _schema_version(path),
        "sha256": _sha256(path),
        "manifest": has_manifest,
    }


def _backend_lease_held() -> bool:
    """探测 backend.lease 是否被其他进程持有；fcntl 不可用时跳过检查。"""

    if fcntl is None:
        logger.warning("[catalog-backup] 当前平台不支持 flock，跳过 Backend 运行状态检查。")
        return False
    from runtime_identity.paths import PuddingClawPaths

    state_dir = PuddingClawPaths.from_environment().state()
    lease_path = state_dir / "backend.lease"
    state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lease_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def restore_backup(backup_path: Path, *, target: Path | None = None) -> dict:
    """把备份恢复到目标库文件；Backend 运行中或备份损坏时拒绝执行。"""

    info = verify_backup(backup_path)
    backup = Path(backup_path)

    if _backend_lease_held():
        raise CatalogBackupError(
            "Backend 正在运行（持有 backend.lease），恢复会覆盖在线数据库；"
            "请先停止 Backend 再执行恢复。"
        )

    if target is None:
        from runtime_identity.paths import PuddingClawPaths

        target = PuddingClawPaths.from_environment().databases() / "catalog.sqlite3"
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    # 先复制到 target 同目录临时文件并复检完整性，再原子切换。
    tmp_path = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex[:8]}.tmp")
    shutil.copyfile(backup, tmp_path)
    try:
        check = _integrity_check(tmp_path)
        if check != "ok":
            raise CatalogBackupError(f"恢复副本完整性校验失败（integrity_check={check}），已中止恢复。")

        previous: Path | None = None
        if target.exists():
            previous = target.with_name(f"{target.name}.pre-restore-{_utc_timestamp()}")
            if previous.exists():
                previous = target.with_name(
                    f"{target.name}.pre-restore-{_utc_timestamp()}-{uuid.uuid4().hex[:6]}"
                )
            os.replace(target, previous)
        try:
            os.replace(tmp_path, target)
        except Exception:
            # 切换失败时尽力把原库放回去，避免数据丢失。
            if previous is not None and previous.exists() and not target.exists():
                os.replace(previous, target)
            raise
    finally:
        tmp_path.unlink(missing_ok=True)

    # 旧库的 WAL/SHM 残留与新库文件不匹配，必须清掉，否则 SQLite 可能误放旧帧。
    for suffix in ("-wal", "-shm"):
        sidecar = target.with_name(target.name + suffix)
        sidecar.unlink(missing_ok=True)

    return {
        "restored": str(target),
        "previous": str(previous) if previous is not None else None,
        "schema_version": info["schema_version"],
        "sha256": info["sha256"],
    }


def list_backups(dest_dir: Path | None = None) -> list[dict]:
    """按创建时间倒序列出备份；无清单的裸文件也列出（manifest=False）。"""

    dest = Path(dest_dir) if dest_dir is not None else _default_backup_dir()
    if not dest.exists():
        return []
    entries: list[dict] = []
    for path in dest.glob("catalog-*.sqlite3"):
        manifest_file = _manifest_path(path)
        if manifest_file.exists():
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = {}
            entry = {
                "path": str(path),
                "backup_file": manifest.get("backup_file") or path.name,
                "created_at": manifest.get("created_at"),
                "app_version": manifest.get("app_version"),
                "schema_version": manifest.get("schema_version"),
                "sha256": manifest.get("sha256"),
                "size_bytes": manifest.get("size_bytes") or path.stat().st_size,
                "manifest": bool(manifest),
            }
        else:
            stat = path.stat()
            entry = {
                "path": str(path),
                "backup_file": path.name,
                "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "app_version": None,
                "schema_version": None,
                "sha256": None,
                "size_bytes": stat.st_size,
                "manifest": False,
            }
        entries.append(entry)
    entries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return entries


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m catalog_backup",
        description="Core SQLite Catalog 在线备份 / 校验 / 恢复（仅支持 SQLite）。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup", help="在线备份目录数据库")
    p_backup.add_argument("--dest", type=Path, default=None, help="备份目录（默认 $PUDDINGCLAW_HOME/db/backups）")

    p_verify = sub.add_parser("verify", help="校验备份文件完整性")
    p_verify.add_argument("path", type=Path, help="备份文件路径")

    p_restore = sub.add_parser("restore", help="把备份恢复到目录数据库")
    p_restore.add_argument("path", type=Path, help="备份文件路径")
    p_restore.add_argument("--target", type=Path, default=None, help="目标库文件（默认 $PUDDINGCLAW_HOME/db/catalog.sqlite3）")

    p_list = sub.add_parser("list", help="列出现有备份")
    p_list.add_argument("--dest", type=Path, default=None, help="备份目录（默认 $PUDDINGCLAW_HOME/db/backups）")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = backup_catalog(args.dest)
        elif args.command == "verify":
            result = verify_backup(args.path)
        elif args.command == "restore":
            result = restore_backup(args.path, target=args.target)
        elif args.command == "list":
            result = list_backups(args.dest)
        else:  # pragma: no cover - argparse required=True 已拦截
            raise CatalogBackupError(f"未知命令：{args.command}")
    except CatalogBackupError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # 兜底，仍给出中文诊断
        print(f"错误：操作失败（{exc}）", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
