"""Feishu connector credential and authentication API."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db_session
from knowledge.connectors.feishu import (
    DEFAULT_USER_SCOPES,
    FeishuConnectorError,
    FeishuOpenApi,
    cleanup_oauth_credentials,
    complete_user_oauth,
    delete_feishu_app_secret,
    feishu_app_to_dict,
    feishu_user_grant_to_dict,
    rotate_feishu_app_secret,
    save_feishu_app,
    start_user_oauth,
    tenant_token_broker,
)
from knowledge.connectors.feishu_bitable import resolve_feishu_bitable_reference
from knowledge.models import (
    FeishuAppCredential,
    FeishuUserGrant,
    KnowledgeSourceConnection,
    KnowledgeSourceItem,
)
from knowledge.sources import source_to_dict
from runtime_identity.paths import trusted_owner_user_id

router = APIRouter(prefix="/knowledge/feishu", tags=["knowledge-feishu"])
_OAUTH_BINDING_COOKIE = "pudding_feishu_oauth_binding"


def _browser_principal(binding: str) -> str:
    return f"browser:{hashlib.sha256(binding.encode('utf-8')).hexdigest()}"


class FeishuAppCreateRequest(BaseModel):
    app_id: str = Field(min_length=6, max_length=160)
    app_secret: str = Field(min_length=8, max_length=500)
    app_name: str = Field(default="", max_length=200)
    api_base_url: str = Field(default="https://open.feishu.cn", max_length=300)


class FeishuAppRotateRequest(BaseModel):
    app_id: str = Field(min_length=6, max_length=160)
    app_secret: str = Field(min_length=8, max_length=500)


class FeishuTenantBindRequest(BaseModel):
    app_credential_id: str


class FeishuOAuthStartRequest(BaseModel):
    app_credential_id: str
    redirect_uri: str = Field(max_length=1000)
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_USER_SCOPES), max_length=30)


class FeishuOAuthCallbackRequest(BaseModel):
    state: str = Field(min_length=16, max_length=500)
    code: str = Field(min_length=2, max_length=1000)


class FeishuScopeRequest(BaseModel):
    space_id: str = Field(min_length=1, max_length=200)
    root_node_token: str = Field(default="", max_length=500)
    tenant_domain: str = Field(default="", max_length=300)
    publish_vector: bool = True
    interval_minutes: int = Field(default=60, ge=0, le=43_200)


class FeishuBitableResolveRequest(BaseModel):
    url: str = Field(min_length=12, max_length=2_000)


class FeishuBitableScopeRequest(BaseModel):
    url: str = Field(min_length=12, max_length=2_000)
    table_id: str = Field(default="", max_length=220)
    # None means a legacy client that only sends table_id.  An explicit empty
    # list is a valid deny-all scope and must never expand to every visible Sheet.
    table_ids: list[str] | None = Field(default=None, max_length=500)
    view_id: str = Field(default="", max_length=220)
    monitor_changes: bool = False
    interval_minutes: int = Field(default=0, ge=0, le=43_200)


class FeishuBitablePreviewRequest(FeishuBitableResolveRequest):
    table_id: str = Field(default="", max_length=220)
    view_id: str = Field(default="", max_length=220)
    page_size: int = Field(default=10, ge=1, le=20)


class FeishuBitableRelationRequest(BaseModel):
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=1_000)
    source_table_id: str = Field(min_length=1, max_length=220)
    source_field_id: str = Field(min_length=1, max_length=220)
    target_table_id: str = Field(min_length=1, max_length=220)
    target_field_id: str = Field(min_length=1, max_length=220)
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"] = "many_to_one"
    on_target_delete: Literal["retain_orphans", "restrict", "cascade"] = "retain_orphans"


feishu_open_api = FeishuOpenApi()


def _configured_bitable_relations(source: KnowledgeSourceConnection) -> list[dict[str, object]]:
    raw = (source.config_json or {}).get("bitable_relations")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict) and str(item.get("id") or "").strip()]


async def _bitable_schema_catalog(
    session: AsyncSession,
    source: KnowledgeSourceConnection,
) -> dict[str, dict[str, object]]:
    config = dict(source.config_json or {})
    configured_ids = config.get("table_ids")
    allowed = (
        {str(value).strip() for value in configured_ids if str(value).strip()}
        if str(config.get("source_mode") or "") == "bitable" and isinstance(configured_ids, list)
        else None
    )
    items = (
        await session.execute(
            select(KnowledgeSourceItem).where(
                KnowledgeSourceItem.source_connection_id == source.id,
                KnowledgeSourceItem.external_type == "bitable",
                KnowledgeSourceItem.status != "deleted",
            )
        )
    ).scalars().all()
    catalog: dict[str, dict[str, object]] = {}
    for item in items:
        metadata = dict(item.metadata_json or {})
        table_id = str(metadata.get("table_id") or "").strip()
        if not table_id or (allowed is not None and table_id not in allowed):
            continue
        fields = metadata.get("fields")
        catalog[table_id] = {
            "table_id": table_id,
            "table_name": item.title or table_id,
            "source_item_id": item.id,
            "fields": [dict(field) for field in fields if isinstance(field, dict)] if isinstance(fields, list) else [],
        }
    return catalog


def _relation_field(
    catalog: dict[str, dict[str, object]],
    *,
    table_id: str,
    field_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    table = catalog.get(table_id)
    if table is None:
        raise HTTPException(status_code=409, detail="关系端点的数据表尚未同步 Schema，请先同步多维表格结构。")
    fields = table.get("fields")
    for field in fields if isinstance(fields, list) else []:
        if isinstance(field, dict) and str(field.get("field_id") or "") == field_id:
            return table, field
    raise HTTPException(status_code=409, detail="关系端点字段已不存在，请刷新 Schema 后重新选择。")


def _relation_identity(payload: FeishuBitableRelationRequest) -> tuple[tuple[str, str], tuple[str, str]]:
    return (
        (payload.source_table_id.strip(), payload.source_field_id.strip()),
        (payload.target_table_id.strip(), payload.target_field_id.strip()),
    )


def _same_relation_mapping(relation: dict[str, object], identity: tuple[tuple[str, str], tuple[str, str]]) -> bool:
    left = (
        str(relation.get("source_table_id") or ""),
        str(relation.get("source_field_id") or ""),
    )
    right = (
        str(relation.get("target_table_id") or ""),
        str(relation.get("target_field_id") or ""),
    )
    return (left, right) == identity or (right, left) == identity


def _build_bitable_relation(
    source: KnowledgeSourceConnection,
    payload: FeishuBitableRelationRequest,
    *,
    catalog: dict[str, dict[str, object]],
    relation_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, object]:
    source_table, source_field = _relation_field(
        catalog,
        table_id=payload.source_table_id.strip(),
        field_id=payload.source_field_id.strip(),
    )
    target_table, target_field = _relation_field(
        catalog,
        table_id=payload.target_table_id.strip(),
        field_id=payload.target_field_id.strip(),
    )
    if source_table["table_id"] == target_table["table_id"] and source_field["field_id"] == target_field["field_id"]:
        raise HTTPException(status_code=400, detail="关系两端不能是同一个字段。")

    warnings: list[str] = []
    source_type = str(source_field.get("ui_type") or source_field.get("type") or "")
    target_type = str(target_field.get("ui_type") or target_field.get("type") or "")
    if source_type and target_type and source_type != target_type:
        warnings.append(f"字段类型不同：{source_type} → {target_type}，保存后需由查询结果验证值兼容性。")
    if payload.cardinality in {"one_to_one", "many_to_one"} and not bool(target_field.get("is_primary")):
        warnings.append("目标字段不是飞书主字段，当前只能确认 Schema，不能证明其值唯一。")

    now = datetime.now(timezone.utc).isoformat()
    identity = _relation_identity(payload)
    stable_id = relation_id or "brel_" + hashlib.sha256(
        f"{source.id}:{identity!r}".encode("utf-8")
    ).hexdigest()[:20]
    return {
        "id": stable_id,
        "name": payload.name.strip()
        or f"{source_table['table_name']} → {target_table['table_name']}",
        "description": payload.description.strip(),
        "source_table_id": str(source_table["table_id"]),
        "source_table_name": str(source_table["table_name"]),
        "source_field_id": str(source_field.get("field_id") or ""),
        "source_field_name": str(source_field.get("field_name") or source_field.get("field_id") or ""),
        "target_table_id": str(target_table["table_id"]),
        "target_table_name": str(target_table["table_name"]),
        "target_field_id": str(target_field.get("field_id") or ""),
        "target_field_name": str(target_field.get("field_name") or target_field.get("field_id") or ""),
        "cardinality": payload.cardinality,
        "on_target_delete": payload.on_target_delete,
        "validation_status": "needs_review" if warnings else "schema_valid",
        "validation_scope": "schema_only",
        "validation_warnings": warnings,
        "row_values_stored": False,
        "created_at": created_at or now,
        "updated_at": now,
    }


async def _get_app(session: AsyncSession, app_id: str) -> FeishuAppCredential:
    app = await session.get(FeishuAppCredential, app_id)
    if app is None or app.owner_id != trusted_owner_user_id():
        raise HTTPException(status_code=404, detail="飞书应用凭据不存在。")
    return app


@router.get("/apps")
async def list_feishu_apps(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(
        select(FeishuAppCredential)
        .where(FeishuAppCredential.owner_id == trusted_owner_user_id())
        .order_by(FeishuAppCredential.updated_at.desc())
    )
    return {"apps": [feishu_app_to_dict(app) for app in result.scalars()]}


@router.post("/apps", status_code=201)
async def create_feishu_app(
    request: FeishuAppCreateRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        app = await save_feishu_app(
            session,
            app_id=request.app_id,
            app_secret=request.app_secret,
            app_name=request.app_name,
            api_base_url=request.api_base_url,
        )
        await session.commit()
        await session.refresh(app)
        return {"app": feishu_app_to_dict(app)}
    except FeishuConnectorError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/apps/{app_credential_id}")
async def rotate_feishu_app(
    app_credential_id: str,
    request: FeishuAppRotateRequest,
    session: AsyncSession = Depends(get_db_session),
):
    app = await _get_app(session, app_credential_id)
    try:
        await rotate_feishu_app_secret(
            session,
            app=app,
            app_id=request.app_id,
            app_secret=request.app_secret,
        )
        await session.commit()
        await session.refresh(app)
        return {"app": feishu_app_to_dict(app)}
    except FeishuConnectorError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/apps/{app_credential_id}/test")
async def test_feishu_app(app_credential_id: str, session: AsyncSession = Depends(get_db_session)):
    app = await _get_app(session, app_credential_id)
    try:
        # Obtaining tenant_access_token is the authoritative App ID/Secret
        # validation. The token itself is intentionally never returned.
        await tenant_token_broker.get(session, app, force_refresh=True)
        await session.commit()
        await session.refresh(app)
        return {"ok": True, "app": feishu_app_to_dict(app)}
    except FeishuConnectorError as exc:
        app.status = "error"
        await session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/apps/{app_credential_id}")
async def delete_feishu_app(
    app_credential_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    app = await _get_app(session, app_credential_id)
    sources = (
        await session.execute(
            select(KnowledgeSourceConnection).where(KnowledgeSourceConnection.connector_key == "feishu_wiki")
        )
    ).scalars()
    if any(str((source.config_json or {}).get("app_credential_id") or "") == app.id for source in sources):
        raise HTTPException(status_code=409, detail="该凭据仍被飞书知识 Source 使用，请先停用并解除绑定。")
    grant = (
        await session.execute(select(FeishuUserGrant.id).where(FeishuUserGrant.app_credential_id == app.id).limit(1))
    ).scalar_one_or_none()
    if grant is not None:
        raise HTTPException(status_code=409, detail="该凭据仍有关联的用户授权，请在解除授权后再删除。")
    reference = app.credential_ref
    await session.delete(app)
    await session.commit()
    # Delete the encrypted material only after the catalog commit succeeds.
    app.credential_ref = reference
    delete_feishu_app_secret(app)
    return {"ok": True}


@router.post("/sources/{source_id}/tenant-auth")
async def bind_feishu_tenant_auth(
    source_id: str,
    request: FeishuTenantBindRequest,
    session: AsyncSession = Depends(get_db_session),
):
    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is None or source.connector_key != "feishu_wiki":
        raise HTTPException(status_code=404, detail="飞书知识 Source 不存在。")
    app = await _get_app(session, request.app_credential_id)
    try:
        await tenant_token_broker.get(session, app)
        if source.auth_type != "tenant":
            # Editing an existing source: switch user auth back to tenant auth.
            # Tenant auth never reads user grants, so detach them here; the
            # grant rows stay around (status needs_reauth) and complete_user_oauth
            # reactivates the same grant if the user switches back.
            source.auth_type = "tenant"
            source.config_json = {
                key: value for key, value in (source.config_json or {}).items() if key != "user_grant_id"
            }
            grants = (
                await session.execute(
                    select(FeishuUserGrant).where(FeishuUserGrant.source_connection_id == source.id)
                )
            ).scalars()
            for grant in grants:
                if grant.status == "active":
                    grant.status = "needs_reauth"
        source.config_json = {**(source.config_json or {}), "app_credential_id": app.id}
        source.credential_ref = f"feishu-app:{app.id}"
        source.status = "ready"
        await session.commit()
        return {"source": source_to_dict(source), "app": feishu_app_to_dict(app)}
    except FeishuConnectorError as exc:
        source.status = "needs_reauth"
        await session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sources/{source_id}/oauth/start")
async def start_feishu_user_oauth(
    source_id: str,
    payload: FeishuOAuthStartRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
):
    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is None or source.connector_key != "feishu_wiki":
        raise HTTPException(status_code=404, detail="飞书知识 Source 不存在。")
    app = await _get_app(session, payload.app_credential_id)
    binding = request.cookies.get(_OAUTH_BINDING_COOKIE) or secrets.token_urlsafe(32)
    response.set_cookie(
        _OAUTH_BINDING_COOKIE,
        binding,
        max_age=86_400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    try:
        if source.auth_type != "user":
            # Editing an existing source: switch tenant auth to user auth.
            # Mirror the initial pending_auth state; complete_user_oauth flips
            # the source back to ready once the grant is active.
            source.auth_type = "user"
            source.credential_ref = ""
            source.status = "pending_auth"
        result = await start_user_oauth(
            session,
            app=app,
            source=source,
            redirect_uri=payload.redirect_uri,
            scopes=payload.scopes,
            principal_id=_browser_principal(binding),
        )
        source.config_json = {**(source.config_json or {}), "app_credential_id": app.id}
        await session.commit()
        return result
    except FeishuConnectorError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/oauth/callback")
async def finish_feishu_user_oauth(
    payload: FeishuOAuthCallbackRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        binding = request.cookies.get(_OAUTH_BINDING_COOKIE)
        if not binding:
            raise FeishuConnectorError("OAuth 浏览器会话已失效，请重新发起授权。")
        grant, source, cleanup_refs = await complete_user_oauth(
            session,
            state=payload.state,
            code=payload.code,
            expected_principal_id=_browser_principal(binding),
        )
        await session.commit()
        await session.refresh(grant)
        cleanup_oauth_credentials(cleanup_refs)
        return {"ok": True, "grant": feishu_user_grant_to_dict(grant), "source": source_to_dict(source)}
    except FeishuConnectorError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sources/{source_id}/grant")
async def get_feishu_user_grant(source_id: str, session: AsyncSession = Depends(get_db_session)):
    grant = (
        await session.execute(
            select(FeishuUserGrant)
            .where(FeishuUserGrant.source_connection_id == source_id)
            .order_by(FeishuUserGrant.updated_at.desc())
        )
    ).scalars().first()
    if grant is None:
        return {"grant": None}
    return {"grant": feishu_user_grant_to_dict(grant)}


@router.get("/sources/{source_id}/spaces")
async def list_feishu_spaces(source_id: str, session: AsyncSession = Depends(get_db_session)):
    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is None or source.connector_key != "feishu_wiki":
        raise HTTPException(status_code=404, detail="飞书知识 Source 不存在。")
    try:
        spaces = await feishu_open_api.list_spaces(session, source)
        return {"spaces": spaces}
    except FeishuConnectorError as exc:
        if exc.status_code in {401, 403}:
            source.status = "needs_reauth"
            await session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sources/{source_id}/nodes")
async def list_feishu_nodes(
    source_id: str,
    space_id: str,
    parent_node_token: str = "",
    session: AsyncSession = Depends(get_db_session),
):
    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is None or source.connector_key != "feishu_wiki":
        raise HTTPException(status_code=404, detail="飞书知识 Source 不存在。")
    try:
        nodes = await feishu_open_api.list_nodes(
            session,
            source,
            space_id=space_id,
            parent_node_token=parent_node_token or None,
        )
        return {"nodes": nodes}
    except FeishuConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/sources/{source_id}/scope")
async def configure_feishu_scope(
    source_id: str,
    request: FeishuScopeRequest,
    session: AsyncSession = Depends(get_db_session),
):
    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is None or source.connector_key != "feishu_wiki":
        raise HTTPException(status_code=404, detail="飞书知识 Source 不存在。")
    tenant_domain = request.tenant_domain.strip().lower()
    if tenant_domain and not all(char.isalnum() or char in ".-" for char in tenant_domain):
        raise HTTPException(status_code=400, detail="飞书租户域名格式不正确。")
    source.config_json = {
        **(source.config_json or {}),
        "source_mode": "wiki",
        "space_id": request.space_id.strip(),
        "root_node_token": request.root_node_token.strip(),
        "tenant_domain": tenant_domain,
        "publish_vector": request.publish_vector,
    }
    source.schedule_json = {**(source.schedule_json or {}), "interval_minutes": request.interval_minutes}
    await session.commit()
    return {"source": source_to_dict(source)}


@router.post("/sources/{source_id}/bitable/resolve")
async def resolve_feishu_bitable(
    source_id: str,
    request: FeishuBitableResolveRequest,
    session: AsyncSession = Depends(get_db_session),
):
    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is None or source.connector_key != "feishu_wiki":
        raise HTTPException(status_code=404, detail="飞书知识 Source 不存在。")
    try:
        reference, tables = await resolve_feishu_bitable_reference(
            session,
            source,
            url=request.url,
            api=feishu_open_api,
        )
        return {"reference": reference.as_dict(), "tables": tables}
    except FeishuConnectorError as exc:
        if exc.status_code in {401, 403}:
            source.status = "needs_reauth"
            await session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sources/{source_id}/bitable/preview")
async def preview_feishu_bitable(
    source_id: str,
    request: FeishuBitablePreviewRequest,
    session: AsyncSession = Depends(get_db_session),
):
    """Return a small live preview without persisting record values."""

    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is None or source.connector_key != "feishu_wiki":
        raise HTTPException(status_code=404, detail="飞书知识 Source 不存在。")
    try:
        reference, tables = await resolve_feishu_bitable_reference(
            session,
            source,
            url=request.url,
            api=feishu_open_api,
        )
        visible = {str(item.get("table_id") or ""): item for item in tables}
        table_id = request.table_id.strip() or reference.table_id
        if not table_id and len(visible) == 1:
            table_id = next(iter(visible))
        if not table_id:
            raise FeishuConnectorError("该多维表格包含多个数据表，请先选择要预览的数据表。")
        if table_id not in visible:
            raise FeishuConnectorError("选择的数据表不在当前身份可见范围内。")
        fields = await feishu_open_api.list_bitable_fields(
            session,
            source,
            app_token=reference.app_token,
            table_id=table_id,
        )
        records = await feishu_open_api.list_bitable_records_page(
            session,
            source,
            app_token=reference.app_token,
            table_id=table_id,
            view_id=request.view_id.strip() or reference.view_id,
            page_size=request.page_size,
        )
        return {
            "live": True,
            "row_storage": False,
            "reference": reference.as_dict(),
            "table": visible[table_id],
            "fields": fields,
            "records": records,
        }
    except FeishuConnectorError as exc:
        if exc.status_code in {401, 403}:
            source.status = "needs_reauth"
            await session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/sources/{source_id}/bitable/scope")
async def configure_feishu_bitable_scope(
    source_id: str,
    request: FeishuBitableScopeRequest,
    session: AsyncSession = Depends(get_db_session),
):
    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is None or source.connector_key != "feishu_wiki":
        raise HTTPException(status_code=404, detail="飞书知识 Source 不存在。")
    try:
        reference, tables = await resolve_feishu_bitable_reference(
            session,
            source,
            url=request.url,
            api=feishu_open_api,
        )
        visible = {str(item.get("table_id") or ""): item for item in tables if str(item.get("table_id") or "")}
        requested_ids = (
            [str(item).strip() for item in request.table_ids if str(item).strip()]
            if request.table_ids is not None
            else []
        )
        if request.table_ids is None and request.table_id.strip():
            requested_ids = [request.table_id.strip()]
        # Visibility from Feishu is only a candidate set.  The saved table_ids
        # list is the explicit PuddingClaw/Agent allowlist and may be empty.
        selected_ids = list(dict.fromkeys(requested_ids))
        missing = [table_id for table_id in selected_ids if table_id not in visible]
        if missing:
            raise FeishuConnectorError("选择的数据表不在当前身份可见范围内。")
        preferred_table = request.table_id.strip() or reference.table_id
        if selected_ids and preferred_table not in selected_ids:
            preferred_table = selected_ids[0]
        if not selected_ids:
            preferred_table = ""
        view_id = (request.view_id.strip() or reference.view_id) if preferred_table else ""
        configured_tables = [
            {
                "table_id": table_id,
                "table_name": str(visible[table_id].get("name") or table_id),
                "view_id": view_id if table_id == preferred_table else "",
            }
            for table_id in selected_ids
        ]
        previous_app_token = str((source.config_json or {}).get("app_token") or "")
        preserved_relations = (
            _configured_bitable_relations(source) if previous_app_token == reference.app_token else []
        )
        source.config_json = {
            **(source.config_json or {}),
            "source_mode": "bitable",
            "source_url": reference.original_url,
            "entry_kind": reference.entry_kind,
            "node_token": reference.node_token,
            "app_token": reference.app_token,
            # Legacy default locator retained for old clients and exact-link
            # previews.  table_ids/tables are the authoritative source scope.
            "table_id": preferred_table,
            "table_name": str(visible[preferred_table].get("name") or preferred_table) if preferred_table else "",
            "table_ids": selected_ids,
            "tables": configured_tables,
            "bitable_relations": preserved_relations,
            "view_id": view_id,
            "storage_mode": "live",
            "row_storage": False,
            "monitor_changes": bool(request.monitor_changes),
        }
        source.schedule_json = {
            **(source.schedule_json or {}),
            "interval_minutes": request.interval_minutes if request.monitor_changes else 0,
        }
        source.status = "ready"
        await session.commit()
        return {"source": source_to_dict(source)}
    except FeishuConnectorError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sources/{source_id}/bitable/relations")
async def list_feishu_bitable_relations(
    source_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is None or source.connector_key != "feishu_wiki":
        raise HTTPException(status_code=404, detail="飞书知识 Source 不存在。")
    return {
        "relations": _configured_bitable_relations(source),
        "row_values_stored": False,
    }


@router.post("/sources/{source_id}/bitable/relations", status_code=201)
async def create_feishu_bitable_relation(
    source_id: str,
    request: FeishuBitableRelationRequest,
    session: AsyncSession = Depends(get_db_session),
):
    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is None or source.connector_key != "feishu_wiki":
        raise HTTPException(status_code=404, detail="飞书知识 Source 不存在。")
    if str((source.config_json or {}).get("source_mode") or "") != "bitable":
        raise HTTPException(status_code=409, detail="该来源不是独立多维表格连接。")
    catalog = await _bitable_schema_catalog(session, source)
    identity = _relation_identity(request)
    relations = _configured_bitable_relations(source)
    if any(_same_relation_mapping(relation, identity) for relation in relations):
        raise HTTPException(status_code=409, detail="这两个字段之间已经存在关系，不能创建重复 Join 路径。")
    relation = _build_bitable_relation(source, request, catalog=catalog)
    source.config_json = {**(source.config_json or {}), "bitable_relations": [*relations, relation]}
    await session.commit()
    return {"relation": relation}


@router.put("/sources/{source_id}/bitable/relations/{relation_id}")
async def update_feishu_bitable_relation(
    source_id: str,
    relation_id: str,
    request: FeishuBitableRelationRequest,
    session: AsyncSession = Depends(get_db_session),
):
    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is None or source.connector_key != "feishu_wiki":
        raise HTTPException(status_code=404, detail="飞书知识 Source 不存在。")
    relations = _configured_bitable_relations(source)
    existing = next((item for item in relations if str(item.get("id") or "") == relation_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="关系不存在。")
    identity = _relation_identity(request)
    if any(
        str(relation.get("id") or "") != relation_id and _same_relation_mapping(relation, identity)
        for relation in relations
    ):
        raise HTTPException(status_code=409, detail="这两个字段之间已经存在关系，不能创建重复 Join 路径。")
    catalog = await _bitable_schema_catalog(session, source)
    updated = _build_bitable_relation(
        source,
        request,
        catalog=catalog,
        relation_id=relation_id,
        created_at=str(existing.get("created_at") or "") or None,
    )
    source.config_json = {
        **(source.config_json or {}),
        "bitable_relations": [updated if str(item.get("id") or "") == relation_id else item for item in relations],
    }
    await session.commit()
    return {"relation": updated}


@router.delete("/sources/{source_id}/bitable/relations/{relation_id}")
async def delete_feishu_bitable_relation(
    source_id: str,
    relation_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    source = await session.get(KnowledgeSourceConnection, source_id)
    if source is None or source.connector_key != "feishu_wiki":
        raise HTTPException(status_code=404, detail="飞书知识 Source 不存在。")
    relations = _configured_bitable_relations(source)
    remaining = [item for item in relations if str(item.get("id") or "") != relation_id]
    if len(remaining) == len(relations):
        raise HTTPException(status_code=404, detail="关系不存在。")
    source.config_json = {**(source.config_json or {}), "bitable_relations": remaining}
    await session.commit()
    return {"ok": True}
