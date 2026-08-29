"""Feishu credentials and access-token broker.

The database stores only masked metadata and opaque Vault references. App
secrets, user access tokens, refresh tokens, OAuth state verifiers, and tenant
tokens must never appear in source config, job metadata, logs, or API output.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.models import (
    FeishuAppCredential,
    FeishuOAuthSession,
    FeishuUserGrant,
    KnowledgeSourceConnection,
    iso_utc,
    new_id,
)
from knowledge.service import KnowledgeServiceError, assert_writes_allowed_tolerant
from runtime_identity.paths import trusted_owner_user_id

logger = logging.getLogger(__name__)

FEISHU_API_BASES = {
    "https://open.feishu.cn",
    "https://open.larksuite.com",
}
TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
OAUTH_TOKEN_PATH = "/open-apis/authen/v2/oauth/token"
USER_INFO_PATH = "/open-apis/authen/v1/user_info"
DEFAULT_USER_SCOPES = (
    "wiki:wiki:readonly",
    "docx:document:readonly",
    "drive:drive:readonly",
    "docs:document.media:download",
    "bitable:app:readonly",
    "offline_access",
)

_APP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,160}$")


class FeishuConnectorError(KnowledgeServiceError):
    """A safe, user-facing Feishu connector error without secret material."""

    def __init__(self, message: str, *, code: int | str | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _credential_store():
    from provider_registry import LocalCredentialStore

    return LocalCredentialStore()


def normalize_api_base(value: str | None) -> str:
    normalized = (value or "https://open.feishu.cn").strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise FeishuConnectorError("飞书 API 地址必须是受支持的 HTTPS 官方域名。")
    if normalized not in FEISHU_API_BASES:
        raise FeishuConnectorError("仅允许连接飞书或 Lark 官方 OpenAPI 域名。")
    return normalized


def mask_app_id(app_id: str) -> str:
    if len(app_id) <= 8:
        return f"{app_id[:2]}••••{app_id[-2:]}"
    return f"{app_id[:6]}••••{app_id[-4:]}"


@dataclass(frozen=True)
class FeishuAppSecret:
    app_id: str
    app_secret: str


def _read_app_secret(app: FeishuAppCredential) -> FeishuAppSecret:
    from provider_registry import CredentialVaultDecryptionError

    try:
        raw = _credential_store().get(app.credential_ref)
    except CredentialVaultDecryptionError as exc:
        raise FeishuConnectorError("飞书应用凭据无法解密，请重新配置 App ID / App Secret。") from exc
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise FeishuConnectorError("飞书应用凭据不可用，请重新配置 App ID / App Secret。") from exc
    app_id = str(payload.get("app_id") or "").strip() if isinstance(payload, dict) else ""
    app_secret = str(payload.get("app_secret") or "") if isinstance(payload, dict) else ""
    if not app_id or not app_secret:
        raise FeishuConnectorError("飞书应用凭据不完整，请重新配置。")
    return FeishuAppSecret(app_id=app_id, app_secret=app_secret)


async def save_feishu_app(
    session: AsyncSession,
    *,
    app_id: str,
    app_secret: str,
    app_name: str = "",
    api_base_url: str = "https://open.feishu.cn",
    owner_id: str | None = None,
) -> FeishuAppCredential:
    await assert_writes_allowed_tolerant(session)
    normalized_id = app_id.strip()
    if not _APP_ID_RE.fullmatch(normalized_id):
        raise FeishuConnectorError("App ID 格式不正确。")
    if len(app_secret) < 8 or len(app_secret) > 500:
        raise FeishuConnectorError("App Secret 格式不正确。")
    api_base = normalize_api_base(api_base_url)
    app = FeishuAppCredential(
        id=new_id("fapp"),
        owner_id=owner_id or trusted_owner_user_id(),
        app_id_masked=mask_app_id(normalized_id),
        credential_ref="pending",
        api_base_url=api_base,
        app_name=app_name.strip(),
        status="pending_validation",
    )
    # Write the encrypted value first, then persist only the opaque reference.
    # A DB failure leaves an unreachable Vault entry, never a plaintext secret.
    reference = _credential_store().put(
        f"feishu-app-{app.id}",
        json.dumps({"app_id": normalized_id, "app_secret": app_secret}, separators=(",", ":")),
    )
    app.credential_ref = reference
    session.add(app)
    await session.flush()
    return app


async def rotate_feishu_app_secret(
    session: AsyncSession,
    *,
    app: FeishuAppCredential,
    app_id: str,
    app_secret: str,
) -> FeishuAppCredential:
    await assert_writes_allowed_tolerant(session)
    normalized_id = app_id.strip()
    if not _APP_ID_RE.fullmatch(normalized_id) or len(app_secret) < 8:
        raise FeishuConnectorError("App ID 或 App Secret 格式不正确。")
    app.credential_ref = _credential_store().put(
        f"feishu-app-{app.id}",
        json.dumps({"app_id": normalized_id, "app_secret": app_secret}, separators=(",", ":")),
    )
    app.app_id_masked = mask_app_id(normalized_id)
    app.status = "pending_validation"
    app.validated_at = None
    app.rotated_at = datetime.now(timezone.utc)
    tenant_token_broker.invalidate(app.id)
    await session.flush()
    return app


def delete_feishu_app_secret(app: FeishuAppCredential) -> None:
    try:
        _credential_store().delete(app.credential_ref)
    except Exception as exc:  # cleanup is best-effort after the catalog commit
        logger.warning("[feishu] encrypted app credential cleanup deferred app_id=%s error=%s", app.id, exc)
    tenant_token_broker.invalidate(app.id)


def feishu_app_to_dict(app: FeishuAppCredential) -> dict[str, Any]:
    credential = _credential_store().inspect(app.credential_ref)
    return {
        "id": app.id,
        "app_id_masked": app.app_id_masked,
        "app_name": app.app_name,
        "api_base_url": app.api_base_url,
        "tenant_key": app.tenant_key,
        "status": app.status,
        "credential_configured": bool(credential.get("credential_configured")),
        "credential_readable": bool(credential.get("credential_readable", True)),
        "credential_error": str(credential.get("credential_error") or ""),
        "validated_at": iso_utc(app.validated_at),
        "rotated_at": iso_utc(app.rotated_at),
        "created_at": iso_utc(app.created_at),
        "updated_at": iso_utc(app.updated_at),
    }


class FeishuHttpClient:
    def __init__(self, *, timeout_seconds: float = 20.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def request_json(
        self,
        method: str,
        *,
        api_base_url: str,
        path: str,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base = normalize_api_base(api_base_url)
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response: httpx.Response | None = None
                for attempt in range(3):
                    response = await client.request(
                        method,
                        f"{base}{path}",
                        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
                        json=json_body,
                        params=params,
                    )
                    retryable = response.status_code == 429 or response.status_code >= 500
                    if not retryable or attempt == 2:
                        break
                    retry_after = response.headers.get("retry-after", "")
                    try:
                        delay = min(2.0, max(0.05, float(retry_after)))
                    except (TypeError, ValueError):
                        delay = 0.2 * (2**attempt)
                    await asyncio.sleep(delay)
                assert response is not None
        except httpx.HTTPError as exc:
            raise FeishuConnectorError("无法连接飞书 OpenAPI，请检查网络后重试。") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise FeishuConnectorError("飞书 OpenAPI 返回了无法解析的响应。", status_code=response.status_code) from exc
        if not isinstance(payload, dict):
            raise FeishuConnectorError("飞书 OpenAPI 返回格式不正确。", status_code=response.status_code)
        code = payload.get("code")
        if response.status_code >= 400 or code not in {None, 0}:
            safe_message = str(payload.get("error_description") or payload.get("msg") or "飞书 OpenAPI 请求失败")
            raise FeishuConnectorError(safe_message, code=code, status_code=response.status_code)
        return payload

    async def download_trusted_url(self, url: str, *, max_bytes: int) -> tuple[bytes, str, str]:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            hostname.endswith(".feishu.cn")
            or hostname == "feishu.cn"
            or hostname.endswith(".larksuite.com")
            or hostname == "larksuite.com"
        ):
            raise FeishuConnectorError("飞书返回了不受信任的素材下载地址。")
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                for attempt in range(3):
                    retry_delay: float | None = None
                    async with client.stream("GET", url) as response:
                        retryable = response.status_code == 429 or response.status_code >= 500
                        if retryable and attempt < 2:
                            try:
                                retry_delay = min(2.0, max(0.05, float(response.headers.get("retry-after", ""))))
                            except (TypeError, ValueError):
                                retry_delay = 0.2 * (2**attempt)
                            await response.aread()
                        elif response.status_code >= 400:
                            raise FeishuConnectorError("飞书素材下载失败。", status_code=response.status_code)
                        else:
                            length = response.headers.get("content-length")
                            if length and int(length) > max_bytes:
                                raise FeishuConnectorError("飞书素材超过允许的大小上限。")
                            chunks: list[bytes] = []
                            size = 0
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > max_bytes:
                                    raise FeishuConnectorError("飞书素材超过允许的大小上限。")
                                chunks.append(chunk)
                            return (
                                b"".join(chunks),
                                response.headers.get("content-type", "application/octet-stream").split(";", 1)[0],
                                response.headers.get("content-disposition", ""),
                            )
                    if retry_delay is not None:
                        await asyncio.sleep(retry_delay)
                raise FeishuConnectorError("飞书素材下载失败。")
        except FeishuConnectorError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise FeishuConnectorError("飞书素材下载失败。") from exc


@dataclass(frozen=True)
class _CachedTenantToken:
    value: str
    expires_at: datetime


class TenantTokenBroker:
    def __init__(self, http_client: FeishuHttpClient | None = None) -> None:
        self.http_client = http_client or FeishuHttpClient()
        self._cache: dict[str, _CachedTenantToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def invalidate(self, app_id: str) -> None:
        self._cache.pop(app_id, None)

    async def get(self, session: AsyncSession, app: FeishuAppCredential, *, force_refresh: bool = False) -> str:
        now = datetime.now(timezone.utc)
        cached = self._cache.get(app.id)
        if not force_refresh and cached and cached.expires_at > now + timedelta(seconds=60):
            return cached.value
        lock = self._locks.setdefault(app.id, asyncio.Lock())
        async with lock:
            now = datetime.now(timezone.utc)
            cached = self._cache.get(app.id)
            if not force_refresh and cached and cached.expires_at > now + timedelta(seconds=60):
                return cached.value
            secret = _read_app_secret(app)
            payload = await self.http_client.request_json(
                "POST",
                api_base_url=app.api_base_url,
                path=TENANT_TOKEN_PATH,
                json_body={"app_id": secret.app_id, "app_secret": secret.app_secret},
            )
            token = str(payload.get("tenant_access_token") or "")
            if not token:
                raise FeishuConnectorError("飞书未返回 tenant_access_token。")
            try:
                expires_in = int(payload.get("expire"))
            except (TypeError, ValueError) as exc:
                raise FeishuConnectorError("飞书未返回有效的 tenant_access_token 有效期。") from exc
            if expires_in <= 0:
                raise FeishuConnectorError("飞书未返回有效的 tenant_access_token 有效期。")
            self._cache[app.id] = _CachedTenantToken(token, now + timedelta(seconds=expires_in))
            app.tenant_key = str(payload.get("tenant_key") or app.tenant_key or "")
            app.status = "ready"
            app.validated_at = now
            await session.flush()
            return token


tenant_token_broker = TenantTokenBroker()


def _validate_redirect_uri(value: str) -> str:
    normalized = value.strip()
    parsed = urlparse(normalized)
    is_loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not is_loopback:
        raise FeishuConnectorError("OAuth 回调地址必须使用 HTTPS；本机调试仅允许 localhost/127.0.0.1。")
    if not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise FeishuConnectorError("OAuth 回调地址格式不正确。")
    configured = {
        item.strip()
        for item in os.getenv("PUDDINGCLAW_FEISHU_OAUTH_REDIRECT_URIS", "").split(",")
        if item.strip()
    }
    if not configured:
        configured = {
            "http://127.0.0.1:3000/knowledge/feishu/oauth/callback",
            "http://localhost:3000/knowledge/feishu/oauth/callback",
        }
    if normalized not in configured:
        raise FeishuConnectorError(
            "OAuth 回调地址未在服务端允许列表中；请配置 PUDDINGCLAW_FEISHU_OAUTH_REDIRECT_URIS。"
        )
    return normalized


def _normalize_scopes(scopes: list[str] | tuple[str, ...] | None) -> list[str]:
    values = scopes or DEFAULT_USER_SCOPES
    normalized = list(dict.fromkeys(str(scope).strip() for scope in values if str(scope).strip()))
    if "offline_access" not in normalized:
        normalized.append("offline_access")
    return normalized


async def start_user_oauth(
    session: AsyncSession,
    *,
    app: FeishuAppCredential,
    source: KnowledgeSourceConnection,
    redirect_uri: str,
    scopes: list[str] | None = None,
    principal_id: str = "local",
) -> dict[str, Any]:
    if source.connector_key != "feishu_wiki" or source.auth_type != "user":
        raise FeishuConnectorError("只有用户身份飞书 Source 可以发起 OAuth。")
    safe_redirect = _validate_redirect_uri(redirect_uri)
    requested_scopes = _normalize_scopes(scopes)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    oauth_session = FeishuOAuthSession(
        id=new_id("foauth"),
        state_hash=hashlib.sha256(state.encode("utf-8")).hexdigest(),
        app_credential_id=app.id,
        source_connection_id=source.id,
        principal_id=principal_id,
        redirect_uri=safe_redirect,
        verifier_credential_ref="pending",
        requested_scopes=requested_scopes,
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    oauth_session.verifier_credential_ref = _credential_store().put(
        f"feishu-oauth-verifier-{oauth_session.id}", verifier
    )
    session.add(oauth_session)
    await session.flush()
    secret = _read_app_secret(app)
    accounts_host = "https://accounts.larksuite.com" if app.api_base_url.endswith("larksuite.com") else "https://accounts.feishu.cn"
    query = urlencode(
        {
            "client_id": secret.app_id,
            "redirect_uri": safe_redirect,
            "scope": " ".join(requested_scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return {
        "authorization_url": f"{accounts_host}/open-apis/authen/v1/authorize?{query}",
        "expires_at": oauth_session.expires_at.isoformat(),
        "scopes": requested_scopes,
    }


def _token_expiry(now: datetime, payload: dict[str, Any], key: str) -> datetime:
    try:
        seconds = int(payload.get(key))
    except (TypeError, ValueError) as exc:
        raise FeishuConnectorError(f"飞书未返回有效的 {key}。") from exc
    if seconds <= 0:
        raise FeishuConnectorError(f"飞书未返回有效的 {key}。")
    return now + timedelta(seconds=seconds)


def _grant_token_payload(payload: dict[str, Any]) -> dict[str, str]:
    access_token = str(payload.get("access_token") or "")
    refresh_token = str(payload.get("refresh_token") or "")
    if not access_token:
        raise FeishuConnectorError("飞书未返回 user_access_token。")
    if not refresh_token:
        raise FeishuConnectorError("飞书未返回 refresh_token，请确认已申请并授权 offline_access。")
    return {"access_token": access_token, "refresh_token": refresh_token}


async def complete_user_oauth(
    session: AsyncSession,
    *,
    state: str,
    code: str,
    expected_principal_id: str | None = None,
    http_client: FeishuHttpClient | None = None,
) -> tuple[FeishuUserGrant, KnowledgeSourceConnection, list[str]]:
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    oauth_session = (
        await session.execute(
            select(FeishuOAuthSession).where(FeishuOAuthSession.state_hash == state_hash).with_for_update()
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if oauth_session is None or oauth_session.status != "pending":
        raise FeishuConnectorError("OAuth state 无效或已经使用。")
    if expected_principal_id is not None and oauth_session.principal_id != expected_principal_id:
        raise FeishuConnectorError("OAuth state 与当前浏览器会话不匹配。")
    expires_at = oauth_session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        oauth_session.status = "expired"
        raise FeishuConnectorError("OAuth 授权已过期，请重新发起。")
    app = await session.get(FeishuAppCredential, oauth_session.app_credential_id)
    source = await session.get(KnowledgeSourceConnection, oauth_session.source_connection_id)
    if app is None or source is None:
        raise FeishuConnectorError("OAuth 绑定的飞书应用或 Source 已不存在。")
    from provider_registry import CredentialVaultDecryptionError

    try:
        verifier = _credential_store().get(oauth_session.verifier_credential_ref)
    except CredentialVaultDecryptionError as exc:
        raise FeishuConnectorError("飞书 OAuth 临时凭据无法解密，请重新发起授权。") from exc
    if not verifier:
        raise FeishuConnectorError("OAuth PKCE verifier 已失效，请重新发起授权。")
    secret = _read_app_secret(app)
    client = http_client or FeishuHttpClient()
    payload = await client.request_json(
        "POST",
        api_base_url=app.api_base_url,
        path=OAUTH_TOKEN_PATH,
        json_body={
            "grant_type": "authorization_code",
            "client_id": secret.app_id,
            "client_secret": secret.app_secret,
            "code": code,
            "redirect_uri": oauth_session.redirect_uri,
            "code_verifier": verifier,
        },
    )
    token_payload = _grant_token_payload(payload)
    user_info_payload = await client.request_json(
        "GET",
        api_base_url=app.api_base_url,
        path=USER_INFO_PATH,
        headers={"Authorization": f"Bearer {token_payload['access_token']}"},
    )
    user_info = user_info_payload.get("data") if isinstance(user_info_payload.get("data"), dict) else {}
    principal_id = oauth_session.principal_id
    grant = (
        await session.execute(
            select(FeishuUserGrant).where(
                FeishuUserGrant.app_credential_id == app.id,
                FeishuUserGrant.source_connection_id == source.id,
                FeishuUserGrant.principal_id == principal_id,
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        grant = FeishuUserGrant(
            id=new_id("fgrant"),
            app_credential_id=app.id,
            source_connection_id=source.id,
            principal_id=principal_id,
            token_credential_ref="pending",
        )
        session.add(grant)
    next_version = int(grant.token_version or 0) + 1
    old_reference = grant.token_credential_ref if grant.token_credential_ref != "pending" else ""
    grant.token_credential_ref = _credential_store().put(
        f"feishu-user-grant-{grant.id}-v{next_version}",
        json.dumps(token_payload, separators=(",", ":")),
    )
    grant.open_id = str(user_info.get("open_id") or "")
    grant.union_id = str(user_info.get("union_id") or "")
    grant.tenant_key = str(user_info.get("tenant_key") or "")
    grant.granted_scopes = str(payload.get("scope") or "").split() or list(oauth_session.requested_scopes or [])
    grant.access_expires_at = _token_expiry(now, payload, "expires_in")
    grant.refresh_expires_at = _token_expiry(
        now,
        payload,
        "refresh_token_expires_in" if "refresh_token_expires_in" in payload else "refresh_expires_in",
    )
    grant.token_version = next_version
    grant.status = "active"
    source.credential_ref = f"feishu-user-grant:{grant.id}"
    source.config_json = {**(source.config_json or {}), "app_credential_id": app.id, "user_grant_id": grant.id}
    source.status = "ready"
    oauth_session.status = "consumed"
    oauth_session.consumed_at = now
    await session.flush()
    # The caller owns the database transaction. Return the superseded Vault
    # references so they are deleted only after the catalog commit succeeds;
    # deleting them here could strand a rolled-back grant without tokens.
    cleanup_refs = [oauth_session.verifier_credential_ref]
    if old_reference:
        cleanup_refs.append(old_reference)
    return grant, source, cleanup_refs


def cleanup_oauth_credentials(references: list[str]) -> None:
    for reference in references:
        if reference:
            try:
                _credential_store().delete(reference)
            except Exception as exc:  # cleanup is best-effort after the catalog commit
                logger.warning("[feishu] superseded OAuth credential cleanup deferred error=%s", exc)


def feishu_user_grant_to_dict(grant: FeishuUserGrant) -> dict[str, Any]:
    return {
        "id": grant.id,
        "app_credential_id": grant.app_credential_id,
        "source_connection_id": grant.source_connection_id,
        "principal_id": grant.principal_id,
        "open_id_masked": mask_app_id(grant.open_id) if grant.open_id else "",
        "tenant_key": grant.tenant_key,
        "granted_scopes": list(grant.granted_scopes or []),
        "status": grant.status,
        "access_expires_at": iso_utc(grant.access_expires_at),
        "refresh_expires_at": iso_utc(grant.refresh_expires_at),
        "created_at": iso_utc(grant.created_at),
        "updated_at": iso_utc(grant.updated_at),
    }


class UserTokenBroker:
    def __init__(self, http_client: FeishuHttpClient | None = None) -> None:
        self.http_client = http_client or FeishuHttpClient()
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, session: AsyncSession, grant: FeishuUserGrant, *, force_refresh: bool = False) -> str:
        now = datetime.now(timezone.utc)
        expiry = grant.access_expires_at
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        token_data = self._read_tokens(grant)
        if not force_refresh and expiry and expiry > now + timedelta(minutes=2):
            return token_data["access_token"]
        lock = self._locks.setdefault(grant.id, asyncio.Lock())
        async with lock:
            # Reload under a row lock so refresh-token rotation is serialized
            # across PostgreSQL workers as well as within this process.
            locked_grant = (
                await session.execute(select(FeishuUserGrant).where(FeishuUserGrant.id == grant.id).with_for_update())
            ).scalar_one()
            expiry = locked_grant.access_expires_at
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            token_data = self._read_tokens(locked_grant)
            now = datetime.now(timezone.utc)
            if not force_refresh and expiry and expiry > now + timedelta(minutes=2):
                return token_data["access_token"]
            app = await session.get(FeishuAppCredential, locked_grant.app_credential_id)
            if app is None:
                raise FeishuConnectorError("用户授权绑定的飞书应用不存在。")
            secret = _read_app_secret(app)
            try:
                payload = await self.http_client.request_json(
                    "POST",
                    api_base_url=app.api_base_url,
                    path=OAUTH_TOKEN_PATH,
                    json_body={
                        "grant_type": "refresh_token",
                        "client_id": secret.app_id,
                        "client_secret": secret.app_secret,
                        "refresh_token": token_data["refresh_token"],
                    },
                )
            except FeishuConnectorError as exc:
                if exc.status_code in {400, 401, 403}:
                    locked_grant.status = "needs_reauth"
                    sources = (
                        await session.execute(
                            select(KnowledgeSourceConnection).where(
                                KnowledgeSourceConnection.id == locked_grant.source_connection_id
                            )
                        )
                    ).scalars()
                    for bound_source in sources:
                        bound_source.status = "needs_reauth"
                    await session.commit()
                raise
            next_tokens = _grant_token_payload(payload)
            next_version = locked_grant.token_version + 1
            old_reference = locked_grant.token_credential_ref
            locked_grant.token_credential_ref = _credential_store().put(
                f"feishu-user-grant-{locked_grant.id}-v{next_version}",
                json.dumps(next_tokens, separators=(",", ":")),
            )
            locked_grant.token_version = next_version
            locked_grant.access_expires_at = _token_expiry(now, payload, "expires_in")
            refresh_expiry_key = (
                "refresh_token_expires_in" if "refresh_token_expires_in" in payload else "refresh_expires_in"
            )
            locked_grant.refresh_expires_at = _token_expiry(now, payload, refresh_expiry_key)
            locked_grant.granted_scopes = str(payload.get("scope") or "").split() or locked_grant.granted_scopes
            locked_grant.status = "active"
            await session.commit()
            cleanup_oauth_credentials([old_reference])
            return next_tokens["access_token"]

    @staticmethod
    def _read_tokens(grant: FeishuUserGrant) -> dict[str, str]:
        from provider_registry import CredentialVaultDecryptionError

        try:
            raw = _credential_store().get(grant.token_credential_ref)
        except CredentialVaultDecryptionError as exc:
            raise FeishuConnectorError("飞书用户授权凭据无法解密，请重新授权。") from exc
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise FeishuConnectorError("飞书用户授权凭据不可用，请重新授权。") from exc
        if not isinstance(payload, dict) or not payload.get("access_token") or not payload.get("refresh_token"):
            raise FeishuConnectorError("飞书用户授权凭据不完整，请重新授权。")
        return {"access_token": str(payload["access_token"]), "refresh_token": str(payload["refresh_token"])}


user_token_broker = UserTokenBroker()


class FeishuOpenApi:
    """Authenticated Wiki/Docx/Bitable client with one token-refresh retry."""

    _INVALID_TOKEN_CODES = {99991661, 99991663, 99991664, 99991668}

    def __init__(
        self,
        *,
        http_client: FeishuHttpClient | None = None,
        tenant_broker: TenantTokenBroker | None = None,
        user_broker: UserTokenBroker | None = None,
    ) -> None:
        self.http_client = http_client or FeishuHttpClient()
        self.tenant_broker = tenant_broker or tenant_token_broker
        self.user_broker = user_broker or user_token_broker

    async def _binding(
        self, session: AsyncSession, source: KnowledgeSourceConnection
    ) -> tuple[FeishuAppCredential, FeishuUserGrant | None]:
        app_id = str((source.config_json or {}).get("app_credential_id") or "")
        app = await session.get(FeishuAppCredential, app_id) if app_id else None
        if app is None:
            raise FeishuConnectorError("飞书 Source 尚未绑定应用凭据。")
        if source.auth_type == "tenant":
            return app, None
        grant_id = str((source.config_json or {}).get("user_grant_id") or "")
        grant = await session.get(FeishuUserGrant, grant_id) if grant_id else None
        if grant is None or grant.status != "active":
            raise FeishuConnectorError("飞书用户授权不可用，请重新授权。")
        if grant.source_connection_id != source.id or grant.app_credential_id != app.id:
            raise FeishuConnectorError("飞书用户授权与当前 Source 或应用不匹配，请重新授权。")
        return app, grant

    async def _token(
        self,
        session: AsyncSession,
        app: FeishuAppCredential,
        grant: FeishuUserGrant | None,
        *,
        force_refresh: bool,
    ) -> str:
        if grant is None:
            return await self.tenant_broker.get(session, app, force_refresh=force_refresh)
        return await self.user_broker.get(session, grant, force_refresh=force_refresh)

    async def request(
        self,
        session: AsyncSession,
        source: KnowledgeSourceConnection,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        app, grant = await self._binding(session, source)
        token = await self._token(session, app, grant, force_refresh=False)
        for attempt in range(2):
            try:
                return await self.http_client.request_json(
                    method,
                    api_base_url=app.api_base_url,
                    path=path,
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                    json_body=json_body,
                )
            except FeishuConnectorError as exc:
                invalid_token = exc.status_code == 401 or exc.code in self._INVALID_TOKEN_CODES
                if attempt and invalid_token:
                    if grant is not None:
                        grant.status = "needs_reauth"
                    source.status = "needs_reauth"
                    await session.commit()
                    raise
                if not invalid_token:
                    raise
                token = await self._token(session, app, grant, force_refresh=True)
        raise FeishuConnectorError("飞书身份凭据不可用。")

    async def list_spaces(self, session: AsyncSession, source: KnowledgeSourceConnection) -> list[dict[str, Any]]:
        return await self._paginate(
            session,
            source,
            "/open-apis/wiki/v2/spaces",
            item_key="items",
            page_size=50,
        )

    async def list_nodes(
        self,
        session: AsyncSession,
        source: KnowledgeSourceConnection,
        *,
        space_id: str,
        parent_node_token: str | None = None,
    ) -> list[dict[str, Any]]:
        extra = {"parent_node_token": parent_node_token} if parent_node_token else {}
        return await self._paginate(
            session,
            source,
            f"/open-apis/wiki/v2/spaces/{space_id}/nodes",
            item_key="items",
            page_size=50,
            extra_params=extra,
        )

    async def get_node(
        self,
        session: AsyncSession,
        source: KnowledgeSourceConnection,
        *,
        node_token: str,
    ) -> dict[str, Any]:
        payload = await self.request(
            session,
            source,
            "GET",
            "/open-apis/wiki/v2/spaces/get_node",
            params={"token": node_token},
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return data.get("node") if isinstance(data.get("node"), dict) else data

    async def get_docx_document(
        self,
        session: AsyncSession,
        source: KnowledgeSourceConnection,
        *,
        document_id: str,
    ) -> dict[str, Any]:
        payload = await self.request(
            session,
            source,
            "GET",
            f"/open-apis/docx/v1/documents/{document_id}",
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return data.get("document") if isinstance(data.get("document"), dict) else data

    async def list_docx_blocks(
        self,
        session: AsyncSession,
        source: KnowledgeSourceConnection,
        *,
        document_id: str,
        document_revision_id: int = -1,
    ) -> list[dict[str, Any]]:
        return await self._paginate(
            session,
            source,
            f"/open-apis/docx/v1/documents/{document_id}/blocks",
            item_key="items",
            page_size=500,
            extra_params={"document_revision_id": document_revision_id},
        )

    async def list_bitable_tables(
        self,
        session: AsyncSession,
        source: KnowledgeSourceConnection,
        *,
        app_token: str,
    ) -> list[dict[str, Any]]:
        return await self._paginate(
            session,
            source,
            f"/open-apis/bitable/v1/apps/{app_token}/tables",
            item_key="items",
            page_size=100,
        )

    async def list_bitable_fields(
        self,
        session: AsyncSession,
        source: KnowledgeSourceConnection,
        *,
        app_token: str,
        table_id: str,
    ) -> list[dict[str, Any]]:
        return await self._paginate(
            session,
            source,
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            item_key="items",
            page_size=100,
        )

    async def list_bitable_records_page(
        self,
        session: AsyncSession,
        source: KnowledgeSourceConnection,
        *,
        app_token: str,
        table_id: str,
        view_id: str = "",
        page_size: int = 50,
        page_token: str = "",
        field_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read one bounded live page.  The caller owns presentation only;
        record values must not be persisted by the connector."""

        params: dict[str, Any] = {"page_size": max(1, min(int(page_size), 100))}
        if page_token:
            params["page_token"] = page_token
        if view_id:
            params["view_id"] = view_id
        if field_names:
            params["field_names"] = json.dumps(list(dict.fromkeys(field_names[:100])), ensure_ascii=False)
        payload = await self.request(
            session,
            source,
            "GET",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            params=params,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        return {
            "items": [item for item in items if isinstance(item, dict)],
            "has_more": bool(data.get("has_more")),
            "page_token": str(data.get("page_token") or ""),
            "total": data.get("total"),
        }

    async def download_media_assets(
        self,
        session: AsyncSession,
        source: KnowledgeSourceConnection,
        *,
        file_tokens: list[str],
        max_bytes_each: int = 50 * 1024 * 1024,
    ) -> dict[str, tuple[bytes, str, str]]:
        downloaded: dict[str, tuple[bytes, str, str]] = {}
        unique_tokens = list(dict.fromkeys(token for token in file_tokens if token))
        for offset in range(0, len(unique_tokens), 5):
            batch = unique_tokens[offset : offset + 5]
            payload = await self.request(
                session,
                source,
                "GET",
                "/open-apis/drive/v1/medias/batch_get_tmp_download_url",
                params={"file_tokens": batch},
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            urls = data.get("tmp_download_urls") if isinstance(data.get("tmp_download_urls"), list) else []
            by_token = {
                str(item.get("file_token") or ""): str(item.get("tmp_download_url") or "")
                for item in urls
                if isinstance(item, dict)
            }
            for token in batch:
                url = by_token.get(token, "")
                if not url:
                    raise FeishuConnectorError("飞书未返回素材下载地址。")
                downloaded[token] = await self.http_client.download_trusted_url(url, max_bytes=max_bytes_each)
        return downloaded

    async def get_doc_meta(
        self,
        session: AsyncSession,
        source: KnowledgeSourceConnection,
        *,
        doc_token: str,
        doc_type: str = "docx",
    ) -> dict[str, Any]:
        """Best-effort drive metadata (url/owner/timestamps). Returns {} when
        the app lacks the drive metadata scope — callers must fall back to
        wiki node fields."""
        try:
            payload = await self.request(
                session,
                source,
                "POST",
                "/open-apis/drive/v1/metas/batch_query",
                json_body={
                    "request_docs": [{"doc_token": doc_token, "doc_type": doc_type}],
                    "with_url": True,
                },
            )
        except FeishuConnectorError:
            return {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        metas = data.get("metas") if isinstance(data.get("metas"), list) else []
        for meta in metas:
            if isinstance(meta, dict) and str(meta.get("doc_token") or "") == doc_token:
                return meta
        return {}

    async def get_user_display_names(
        self,
        session: AsyncSession,
        source: KnowledgeSourceConnection,
        *,
        open_ids: list[str],
    ) -> dict[str, str]:
        """Best-effort open_id -> display name. Returns {} when the app lacks
        the contact scope."""
        unique = [open_id for open_id in dict.fromkeys(open_ids) if open_id]
        if not unique:
            return {}
        try:
            payload = await self.request(
                session,
                source,
                "GET",
                "/open-apis/contact/v3/users/batch",
                params={"user_ids": unique, "user_id_type": "open_id"},
            )
        except FeishuConnectorError:
            return {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        names: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            open_id = str(item.get("open_id") or "")
            name = str(item.get("name") or "")
            if open_id and name:
                names[open_id] = name
        return names

    async def _paginate(
        self,
        session: AsyncSession,
        source: KnowledgeSourceConnection,
        path: str,
        *,
        item_key: str,
        page_size: int,
        extra_params: dict[str, Any] | None = None,
        max_pages: int = 10_000,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = ""
        seen_tokens: set[str] = set()
        for _page in range(max_pages):
            params = {"page_size": page_size, **(extra_params or {})}
            if page_token:
                params["page_token"] = page_token
            payload = await self.request(session, source, "GET", path, params=params)
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            page_items = data.get(item_key) if isinstance(data.get(item_key), list) else []
            items.extend(item for item in page_items if isinstance(item, dict))
            if not data.get("has_more"):
                return items
            next_token = str(data.get("page_token") or "")
            if not next_token or next_token in seen_tokens:
                raise FeishuConnectorError("飞书分页游标异常，同步已停止以避免无限循环。")
            seen_tokens.add(next_token)
            page_token = next_token
        raise FeishuConnectorError("飞书分页数量超过安全上限，请缩小同步范围。")
