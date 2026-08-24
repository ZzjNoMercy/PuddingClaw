"""Bitable link normalization for the Feishu knowledge connector.

The connector stores only a live-query locator.  Record values are never
materialized by this module.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.connectors.feishu import FeishuConnectorError, FeishuOpenApi
from knowledge.models import KnowledgeSourceConnection

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{4,220}$")


def _trusted_tenant_host(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    return host == "feishu.cn" or host.endswith(".feishu.cn") or host == "larksuite.com" or host.endswith(".larksuite.com")


def _safe_token(value: str, *, label: str, required: bool = False) -> str:
    token = value.strip()
    if not token and not required:
        return ""
    if not _TOKEN_RE.fullmatch(token):
        raise FeishuConnectorError(f"飞书{label}格式不正确。")
    return token


@dataclass(frozen=True)
class FeishuBitableReference:
    original_url: str
    entry_kind: str
    node_token: str = ""
    app_token: str = ""
    table_id: str = ""
    view_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def parse_feishu_bitable_url(value: str) -> FeishuBitableReference:
    """Parse official tenant `/wiki/` and `/base/` Bitable links."""

    raw = value.strip()
    if not raw or len(raw) > 2_000:
        raise FeishuConnectorError("请粘贴一个有效的飞书 Wiki 或多维表格链接。")
    parsed = urlparse(raw)
    if parsed.scheme != "https" or parsed.username or parsed.password or not _trusted_tenant_host(parsed.hostname or ""):
        raise FeishuConnectorError("仅支持飞书或 Lark 官方租户域名下的 HTTPS 链接。")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2 or segments[0] not in {"wiki", "base"}:
        raise FeishuConnectorError("链接必须是 /wiki/<node_token> 或 /base/<app_token>。")
    query = parse_qs(parsed.query, keep_blank_values=False)
    table_id = _safe_token((query.get("table") or [""])[0], label="数据表 ID")
    view_id = _safe_token((query.get("view") or [""])[0], label="视图 ID")
    if segments[0] == "base":
        return FeishuBitableReference(
            original_url=raw,
            entry_kind="direct_bitable",
            app_token=_safe_token(segments[1], label="多维表格 App Token", required=True),
            table_id=table_id,
            view_id=view_id,
        )
    return FeishuBitableReference(
        original_url=raw,
        entry_kind="wiki_bitable",
        node_token=_safe_token(segments[1], label="Wiki Node Token", required=True),
        table_id=table_id,
        view_id=view_id,
    )


async def resolve_feishu_bitable_reference(
    session: AsyncSession,
    source: KnowledgeSourceConnection,
    *,
    url: str,
    api: FeishuOpenApi,
) -> tuple[FeishuBitableReference, list[dict[str, Any]]]:
    """Resolve a Wiki pointer and validate the visible table set."""

    parsed = parse_feishu_bitable_url(url)
    if parsed.entry_kind == "wiki_bitable":
        node = await api.get_node(session, source, node_token=parsed.node_token)
        obj_type = str(node.get("obj_type") or "").lower()
        app_token = _safe_token(str(node.get("obj_token") or ""), label="多维表格 App Token", required=True)
        if obj_type != "bitable":
            raise FeishuConnectorError(f"该 Wiki 节点指向 {obj_type or '未知类型'}，不是多维表格。")
        parsed = FeishuBitableReference(
            original_url=parsed.original_url,
            entry_kind=parsed.entry_kind,
            node_token=parsed.node_token,
            app_token=app_token,
            table_id=parsed.table_id,
            view_id=parsed.view_id,
        )
    tables = await api.list_bitable_tables(session, source, app_token=parsed.app_token)
    if parsed.table_id and parsed.table_id not in {str(item.get("table_id") or "") for item in tables}:
        raise FeishuConnectorError("链接指定的数据表不在当前身份可见范围内。")
    return parsed, tables
