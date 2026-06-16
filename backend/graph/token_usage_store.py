"""Token 用量存储 — SQLite + jsonl 双写.

职责：每轮 LLM 调用后记录 token 用量，用于后续 TPM 估算和监控.
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 数据库文件路径（项目根目录下的 data/ 中）
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "token_usage.db"
JSONL_DIR = DATA_DIR / "stats" / "tokens"


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSONL_DIR.mkdir(parents=True, exist_ok=True)


def _get_conn() -> sqlite3.Connection:
    _ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            session_id TEXT,
            round_num INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            start_time REAL,
            timestamp REAL
        )
    """)
    conn.commit()
    return conn


# 懒加载连接（首次调用时初始化）
_conn: sqlite3.Connection | None = None


def _get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _get_conn()
    return _conn


def record_token_usage(
    user_id: str,
    session_id: str,
    round_num: int,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    start_time: float,
) -> None:
    """双写 token 用量到 SQLite + jsonl。任何一步失败静默跳过，不阻塞主流程。"""
    timestamp = time.time()
    record: dict[str, Any] = {
        "user_id": user_id,
        "session_id": session_id,
        "round_num": round_num,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "start_time": start_time,
        "timestamp": timestamp,
    }

    # 1. 写入 SQLite
    try:
        conn = _get_connection()
        conn.execute(
            """
            INSERT INTO token_usage
            (user_id, session_id, round_num, input_tokens, output_tokens, total_tokens, start_time, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, session_id, round_num, input_tokens, output_tokens, total_tokens, start_time, timestamp),
        )
        conn.commit()
    except Exception as e:
        logger.warning("[token_usage] SQLite write failed: %s", e)

    # 2. 写入 jsonl（按日期分文件）
    try:
        _ensure_dirs()
        date_str = datetime.now().strftime("%Y-%m-%d")
        jsonl_path = JSONL_DIR / f"{date_str}.jsonl"
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("[token_usage] jsonl write failed: %s", e)


def get_ranking(limit: int = 10) -> list[dict[str, Any]]:
    """用户 Token 消耗排行。"""
    try:
        conn = _get_connection()
        cur = conn.execute(
            """
            SELECT user_id,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(total_tokens) AS total_tokens
            FROM token_usage
            GROUP BY user_id
            ORDER BY total_tokens DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
        return [
            {
                "user_id": r[0],
                "input_tokens": r[1] or 0,
                "output_tokens": r[2] or 0,
                "total_tokens": r[3] or 0,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("[token_usage] get_ranking failed: %s", e)
        return []


def get_total() -> dict[str, Any]:
    """全局累计 Token 统计。"""
    try:
        conn = _get_connection()
        cur = conn.execute(
            """
            SELECT SUM(input_tokens), SUM(output_tokens), SUM(total_tokens), COUNT(*)
            FROM token_usage
            """
        )
        row = cur.fetchone()
        return {
            "total_input_tokens": row[0] or 0,
            "total_output_tokens": row[1] or 0,
            "total_tokens": row[2] or 0,
            "total_records": row[3] or 0,
        }
    except Exception as e:
        logger.warning("[token_usage] get_total failed: %s", e)
        return {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
            "total_records": 0,
        }


def get_daily(days: int = 7) -> list[dict[str, Any]]:
    """最近 N 天每日 Token 消耗趋势。"""
    try:
        conn = _get_connection()
        # SQLite 日期格式化
        cur = conn.execute(
            """
            SELECT DATE(datetime(timestamp, 'unixepoch')) AS day,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(total_tokens) AS total_tokens
            FROM token_usage
            WHERE timestamp >= strftime('%s', 'now', '-%d days')
            GROUP BY day
            ORDER BY day DESC
            """ % days,
        )
        rows = cur.fetchall()
        return [
            {
                "day": r[0],
                "input_tokens": r[1] or 0,
                "output_tokens": r[2] or 0,
                "total_tokens": r[3] or 0,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("[token_usage] get_daily failed: %s", e)
        return []
