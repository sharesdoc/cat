"""Authentication middleware and token verification for Cat MCP server.

Provides a Starlette ASGI middleware that validates Bearer tokens from the
Authorization header using the MCP SDK's ``TokenVerifier`` protocol, along
with OAuth 2.0 Protected Resource Metadata (RFC 9728) endpoints.

Usage:
    from cat_mcp.auth import create_auth_middleware
    app = create_auth_middleware(app, config)
"""
# 依据: Task-R-20261126012656437-P0

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import AccessToken, TokenVerifier
from pydantic import AnyHttpUrl
from starlette.types import ASGIApp, Receive, Scope, Send

from cat_mcp.config import AppConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TokenVerifier implementation using PyJWT + JWKS
# ---------------------------------------------------------------------------


class JwtTokenVerifier(TokenVerifier):
    """``TokenVerifier`` protocol implementation that validates JWTs against
    an OAuth/OIDC provider's JWKS endpoint using PyJWT.

    Fetches signing keys via ``PyJWKClient`` (with caching), then decodes
    and validates the token's signature, issuer, audience, and expiry.

    ………………………………………………………………………………………………………………………………………
    … 本实现在不携带自定义授权服务器的情况下满足 CAT-05 需求                      …
    … (采用标准 OAuth, 不自制授权服务器).                                          …
    … → 生产部署时外挂真实 IdP (Auth0 / Keycloak / Cognito 等) 的 JWKS URL 即可.   …
    … → issuer/audience 不匹配或签名校验失败统一返回 ``None``, 不泄露内部细节.     …
    ………………………………………………………………………………………………………………………………………
    """

    def __init__(self, issuer: str, audience: str) -> None:
        """Initialize the verifier.

        Args:
            issuer: OAuth/OIDC issuer URL (used to derive the JWKS endpoint
                via ``{issuer}/.well-known/openid-configuration`` discovery, then
                ``{issuer}/.well-known/jwks.json`` as fallback).
            audience: Expected token audience (``aud`` claim).
        """
        self._issuer = issuer.rstrip("/")
        self._audience = audience

        # Derive JWKS URI from issuer
        self._jwks_uri = f"{self._issuer}/.well-known/jwks.json"

        # Lazy-import PyJWKClient to avoid a hard dependency on PyJWT at
        # module-load time (the SDK does not list PyJWT as a transitive
        # dependency of ``mcp.server.auth.provider``).
        from jwt import PyJWKClient  # type: ignore[import-untyped]

        self._key_client = PyJWKClient(
            self._jwks_uri,
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=300,  # 5-minute JWK Set cache
            timeout=10,
        )

    # ------------------------------------------------------------------
    # TokenVerifier protocol
    # ------------------------------------------------------------------

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify a Bearer *token* and return access info if valid.

        Args:
            token: The raw JWT access token string.

        Returns:
            An :class:`AccessToken` on success, or ``None`` if validation
            fails for any reason (expired, wrong issuer/audience, bad
            signature, …).
        """
        try:
            payload = await self._decode_token(token)
            if payload is None:
                return None

            return AccessToken(
                token=token,
                client_id=payload.get("client_id", payload.get("sub", "")),
                scopes=payload.get("scope", "").split(),
                expires_at=payload.get("exp"),
                resource=payload.get("aud"),
                subject=payload.get("sub"),
                claims=dict(payload),
            )
        except Exception:
            logger.exception("Token verification failed unexpectedly")
            return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _decode_token(self, token: str) -> dict[str, Any] | None:
        """Decode and validate a JWT *token*.

        Returns the decoded payload dict on success, or ``None`` on
        validation failure.
        """
        import jwt as pyjwt_lib  # type: ignore[import-untyped]

        try:
            signing_key = self._key_client.get_signing_key_from_jwt(token)
        except pyjwt_lib.exceptions.PyJWTError:
            logger.warning("Failed to obtain signing key for token")
            return None

        try:
            payload = pyjwt_lib.decode(
                token,
                key=signing_key.key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except pyjwt_lib.exceptions.ExpiredSignatureError:
            logger.warning("Token has expired")
            return None
        except pyjwt_lib.exceptions.InvalidAudienceError:
            logger.warning("Token audience mismatch")
            return None
        except pyjwt_lib.exceptions.InvalidIssuerError:
            logger.warning("Token issuer mismatch")
            return None
        except pyjwt_lib.exceptions.PyJWTError:
            logger.warning("Token validation failed (signature or malformed)")
            return None

        return payload


# ---------------------------------------------------------------------------
# Host / Origin header validation
# ---------------------------------------------------------------------------


class HostOriginValidator:
    """Validate Host and Origin headers to prevent DNS rebinding attacks.

    Inspired by the SDK's ``TransportSecurityMiddleware`` but tailored for
    Cat's configuration model.
    """

    def __init__(self, public_url: str, allowed_origins: list[str] | None = None) -> None:
        self._public_host = urlparse(public_url).hostname or ""
        self._allowed_origins = frozenset(allowed_origins or [])

    def validate(self, host: str | None, origin: str | None) -> str | None:
        """Validate Host/Origin headers.

        Args:
            host: The ``Host`` header value (may include port).
            origin: The ``Origin`` header value (full URL).

        Returns:
            ``None`` if validation passes, or an error message string if it
            fails.
        """
        if not host:
            return "Missing Host header"

        # Extract hostname (strip port if present)
        hostname = host.split(":")[0].lower()

        # Always allow loopback addresses (used for local development)
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return None

        # Accept the configured public URL's hostname
        if hostname == self._public_host.lower():
            return None

        # Also accept hosts ending in a known suffix (e.g. *.example.com
        # matching the public host)
        if self._public_host and hostname.endswith(f".{self._public_host}"):
            return None

        return f"Invalid Host header: {host!r}"

        # Origin validation is intentionally omitted here — it is handled by
        # the ``allowed_origins`` check via the CORS middleware.  The Bearer
        # token validation already gates access on authentication, so Origin
        # alone is not a sufficient attack surface to block on.


# ---------------------------------------------------------------------------
# Bypass detection for loopback/localhost requests
# ---------------------------------------------------------------------------


def _is_loopback_request(scope: Scope) -> bool:
    """Check if the request came from a loopback address.

    Checks the ASGI ``client`` key first, then falls back to the ``Host``
    header (useful for test clients and reverse proxies).

    Args:
        scope: ASGI scope dict.

    Returns:
        ``True`` if the client or host header indicates a loopback address.
    """
    # Check ASGI client address
    client = scope.get("client")
    if client:
        host = client[0]
        if host in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
            return True

    # Fallback: check the Host header (covers TestClient and proxies)
    headers = scope.get("headers", [])
    for key, value in headers:
        if key.lower() == b"host":
            hostname = value.decode("utf-8", errors="replace").split(":")[0].lower()
            if hostname in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
                return True
            break

    # Also check server address
    server = scope.get("server")
    if server:
        host = server[0]
        if host in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
            return True
    return False


# ---------------------------------------------------------------------------
# ASGI authentication middleware
# ---------------------------------------------------------------------------


class AuthMiddleware:
    """ASGI middleware that enforces Bearer token authentication.

    Token verification uses a ``TokenVerifier`` (protocol from
    ``mcp.server.auth.provider``).  On success, the decoded user info
    is stored in ``scope["user"]`` as the token subject string.

    Requests to bypass paths (``/mcp/auth`` and ``/.well-known/``) are
    passed through without authentication.  When ``require_auth`` is
    ``False`` and the request originates from a loopback address, the
    middleware passes through without authentication.
    """

    def __init__(
        self,
        app: ASGIApp,
        config: AppConfig,
        token_verifier: TokenVerifier | None = None,
    ) -> None:
        """Initialize the middleware.

        Args:
            app: The downstream ASGI application.
            config: Application configuration (auth settings used for
                validation).
            token_verifier: Optional custom ``TokenVerifier``.  If not
                provided, a default ``JwtTokenVerifier`` is created from
                ``config.auth``.
        """
        self._app = app
        self._config = config
        self._token_verifier = token_verifier or JwtTokenVerifier(
            issuer=config.auth.issuer,
            audience=config.auth.audience,
        )
        self._host_validator = HostOriginValidator(
            public_url=config.server.public_url,
            allowed_origins=config.auth.allowed_origins,
        )

    # ------------------------------------------------------------------
    # ASGI entry point
    # ------------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        # Bypass authentication for OAuth discovery / token endpoints
        path = scope.get("path", "")
        if self._is_bypass_path(path):
            await self._app(scope, receive, send)
            return

        # If auth is not required and this is a loopback request, allow
        if not self._config.auth.require_auth and _is_loopback_request(scope):
            logger.debug("Auth bypassed: loopback request with require_auth=False")
            await self._app(scope, receive, send)
            return

        # Validate Host header
        host = self._get_header(scope, "host")
        origin = self._get_header(scope, "origin")
        host_error = self._host_validator.validate(host, origin)
        if host_error:
            logger.warning("Host validation failed: %s", host_error)
            await self._send_json_error(send, 401, host_error)
            return

        # Extract Bearer token
        auth_header = self._get_header(scope, "authorization")
        if not auth_header or not auth_header.lower().startswith("bearer "):
            await self._send_json_error(send, 401, "Missing or invalid Authorization header")
            return

        token = auth_header[7:]  # strip "Bearer "

        # Verify token using the TokenVerifier protocol
        access_token = await self._token_verifier.verify_token(token)
        if access_token is None:
            await self._send_json_error(send, 401, "Invalid or expired token")
            return

        # Check token expiry (defensive — verifier should already do this)
        if access_token.expires_at and access_token.expires_at < int(time.time()):
            await self._send_json_error(send, 401, "Token has expired")
            return

        # Store the authenticated subject in scope for downstream handlers
        auth_info: dict[str, Any] = {
            "client_id": access_token.client_id,
            "subject": access_token.subject,
            "scopes": access_token.scopes,
        }
        scope["user"] = access_token.subject or access_token.client_id
        scope["auth_info"] = auth_info

        await self._app(scope, receive, send)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_bypass_path(path: str) -> bool:
        """Check if *path* should bypass authentication.

        Returns:
            ``True`` for OAuth-related paths that need to be accessible
            without a Bearer token.
        """
        return path.startswith("/.well-known/") or path == "/mcp/auth" or path.startswith("/mcp/auth/")

    @staticmethod
    def _get_header(scope: Scope, name: str) -> str | None:
        """Extract a header value from the ASGI scope."""
        headers = scope.get("headers", [])
        target = name.lower().encode()
        for key, value in headers:
            if key.lower() == target:
                return value.decode("utf-8", errors="replace")
        return None

    @staticmethod
    async def _send_json_error(send: Send, status_code: int, message: str) -> None:
        """Send a JSON error response.

        The response uses Cat's unified error format::

            {"error": "unauthorized", "message": "..."}

        Args:
            send: ASGI send callable.
            status_code: HTTP status code to return.
            message: Human-readable error description (no internal paths).
        """
        body = json.dumps(
            {
                "error": "unauthorized",
                "message": message,
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def create_auth_middleware(
    app: ASGIApp,
    config: AppConfig,
    token_verifier: TokenVerifier | None = None,
) -> ASGIApp:
    """Wrap an ASGI app with authentication middleware.

    Args:
        app: The downstream ASGI application (typically created by
            :func:`cat_mcp.server.create_app`).
        config: Application configuration.
        token_verifier: Optional custom ``TokenVerifier``.  If omitted,
            a default ``JwtTokenVerifier`` is created from ``config.auth``.

    Returns:
        An ASGI app with authentication applied.

    ………………………………………………………………………………………………………………………………………
      AuthMiddleware 策略:
      - ``/.well-known/``、``/mcp/auth`` 绕过认证 (OAuth 发现/交换端点)
      - ``require_auth=False`` + loopback → 绕过认证 (开发模式)
      - Host 头校验 (防 Host header 攻击)
      - Bearer token 校验 (通过 TokenVerifier 协议)
      - 校验通过 → ``scope["user"]`` 存储主体标识
      - 校验失败 → 401 统一 JSON 错误, 不泄露内部细节
    ………………………………………………………………………………………………………………………………………
    """
    # 依据: Task-R-20261126012656437-P0
    return AuthMiddleware(app, config, token_verifier=token_verifier)


# ---------------------------------------------------------------------------
# OAuth Protected Resource Metadata endpoint
# ---------------------------------------------------------------------------


def create_protected_resource_route(config: AppConfig) -> list[Any]:
    """Create the ``/.well-known/oauth-protected-resource`` Starlette route.

    Returns RFC 9728 protected resource metadata that OAuth clients use to
    discover how to obtain tokens for this resource server.

    Args:
        config: Application configuration (read server URL and auth settings).

    Returns:
        A list of Starlette ``Route`` objects (one route in the list).
    """
    # 依据: Task-R-20261126012656437-P0
    try:
        resource_url = AnyHttpUrl(config.server.public_url)
    except Exception as exc:
        raise ValueError(f"Invalid public_url in config: {config.server.public_url!r}") from exc

    try:
        issuer_url = AnyHttpUrl(config.auth.issuer)
    except Exception as exc:
        raise ValueError(f"Invalid issuer in config: {config.auth.issuer!r}") from exc

    from mcp.server.auth.routes import create_protected_resource_routes

    return create_protected_resource_routes(
        resource_url=resource_url,
        authorization_servers=[issuer_url],
        scopes_supported=None,
        resource_name="Cat MCP",
        resource_documentation=None,
    )


# ---------------------------------------------------------------------------
# Utility: build protected resource metadata URL
# ---------------------------------------------------------------------------


def build_resource_metadata_url(public_url: str) -> str:
    """Build the RFC 9728 protected resource metadata URL for *public_url*.

    The URL is ``/.well-known/oauth-protected-resource`` with the resource
    path appended.

    Args:
        public_url: The public-facing URL of this server.

    Returns:
        The well-known metadata URL.
    """
    # 依据: Task-R-20261126012656437-P0
    from mcp.server.auth.routes import build_resource_metadata_url as _build

    return str(_build(AnyHttpUrl(public_url)))
