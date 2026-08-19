"""Feishu connector credential and authentication API."""

from __future__ import annotations

import hashlib
import secrets

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
from knowledge.models import FeishuAppCredential, FeishuUserGrant, KnowledgeSourceConnection
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


feishu_open_api = FeishuOpenApi()


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
        "space_id": request.space_id.strip(),
        "root_node_token": request.root_node_token.strip(),
        "tenant_domain": tenant_domain,
        "publish_vector": request.publish_vector,
    }
    source.schedule_json = {**(source.schedule_json or {}), "interval_minutes": request.interval_minutes}
    await session.commit()
    return {"source": source_to_dict(source)}
