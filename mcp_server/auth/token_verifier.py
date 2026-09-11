"""Small static bearer-token verifier for development and local deployments.

Production deployments should replace this with an external OAuth 2.1 / JWT
TokenVerifier. The MCP server still enforces scopes and resource audience.
"""
import hmac

import jwt

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

from .. import config
from ..authorization.manager import get_permission_policy


class StaticTokenVerifier:
    """Verify tokens from ``AUTH_TOKENS_JSON`` and map them to principals."""

    def __init__(self, tokens: dict[str, str] | None = None):
        self._tokens = dict(tokens or config.AUTH_TOKENS)

    async def verify_token(self, token: str) -> AccessToken | None:
        policy = get_permission_policy()
        matched_subject: str | None = None
        for configured_token, subject in self._tokens.items():
            if hmac.compare_digest(token, configured_token):
                matched_subject = subject
                break
        if matched_subject is None or matched_subject not in policy.principals:
            return None

        principal = policy.principals[matched_subject]
        scopes = policy.permissions_for_roles(principal.roles)
        if "*" in scopes and config.AUTH_REQUIRED_SCOPES:
            scopes = frozenset(set(scopes) | set(config.AUTH_REQUIRED_SCOPES))
        return AccessToken(
            token=token,
            client_id=matched_subject,
            subject=matched_subject,
            scopes=sorted(scopes),
            resource=config.AUTH_RESOURCE_URL,
            claims={"iss": config.AUTH_ISSUER_URL},
        )


class JwtTokenVerifier:
    """OAuth 2.1 / JWT resource-server token verifier.

    Supports JWKS, static PEM public keys, and HS256 secrets. Signature, issuer,
    audience and expiry are all validated before an AccessToken is returned.
    """

    def __init__(self):
        self.issuer = config.AUTH_JWT_ISSUER
        self.audience = config.AUTH_JWT_AUDIENCE
        self.algorithms = list(config.AUTH_JWT_ALGORITHMS)
        if "NONE" in self.algorithms:
            raise ValueError("AUTH_JWT_ALGORITHMS 不允许使用 none")
        self._jwk_client = None
        self._static_key = config.AUTH_JWT_PUBLIC_KEY or config.AUTH_JWT_SECRET
        if config.AUTH_JWT_JWKS_URL:
            self._jwk_client = jwt.PyJWKClient(
                config.AUTH_JWT_JWKS_URL,
                cache_keys=True,
            )
        if not self._jwk_client and not self._static_key:
            raise ValueError("AUTH_MODE=jwt 时必须配置 JWKS URL 或静态公钥/密钥")
        if not self.issuer or not self.audience or not self.algorithms:
            raise ValueError("AUTH_MODE=jwt 时必须配置 issuer、audience 和 algorithms")

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = (
                self._jwk_client.get_signing_key_from_jwt(token).key
                if self._jwk_client else self._static_key
            )
            claims = jwt.decode(
                token,
                key,
                algorithms=self.algorithms,
                issuer=self.issuer,
                audience=self.audience,
                leeway=config.AUTH_JWT_LEEWAY,
                options={"require": ["exp", "iss", "sub"]},
            )
        except jwt.PyJWTError:
            return None

        subject = str(claims.get(config.AUTH_JWT_SUBJECT_CLAIM) or "")
        if not subject:
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or subject),
            subject=subject,
            scopes=_claim_list(claims.get(config.AUTH_JWT_SCOPES_CLAIM)),
            resource=config.AUTH_RESOURCE_URL,
            claims=claims,
        )


def _claim_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in value.replace(",", " ").split() if item]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value if item]
    return [str(value)]


def build_auth_settings() -> tuple[AuthSettings | None, TokenVerifier | None]:
    """Build MCP auth settings for static or external JWT mode."""
    mode = config.AUTH_MODE
    if mode == "disabled":
        return None, None

    if mode == "static":
        if not config.AUTH_TOKENS:
            raise ValueError("AUTH_MODE=static 时必须配置 AUTH_TOKENS_JSON")
        verifier = StaticTokenVerifier(config.AUTH_TOKENS)
        issuer_url = config.AUTH_ISSUER_URL
    elif mode == "jwt":
        verifier = JwtTokenVerifier()
        issuer_url = verifier.issuer
    else:
        raise ValueError("不支持的 AUTH_MODE：%s（可选 disabled/static/jwt）" % mode)

    settings = AuthSettings(
        issuer_url=issuer_url,
        resource_server_url=config.AUTH_RESOURCE_URL,
        required_scopes=config.AUTH_REQUIRED_SCOPES or None,
        validate_token_resource=True,
    )
    return settings, verifier
