"""Build request identity from the MCP authentication context."""
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

from .. import config
from .models import Principal, RequestContext


class AuthenticationError(PermissionError):
    """Raised when a request has no acceptable authenticated identity."""




def principal_for_subject(subject: str) -> Principal | None:
    """Resolve server-side roles and attributes for an authenticated subject."""
    from ..authorization.manager import get_permission_policy

    policy = get_permission_policy()
    principal_policy = policy.principals.get(subject)
    if principal_policy is None:
        return None
    return Principal(
        subject=principal_policy.subject,
        roles=frozenset(principal_policy.roles),
        attributes=principal_policy.attributes,
    )


def principal_for_access_token(access_token: AccessToken) -> Principal | None:
    """Resolve a principal using policy first and verified JWT claims second."""
    from ..authorization.manager import get_permission_policy

    subject = access_token.subject or access_token.client_id
    policy = get_permission_policy()
    principal_policy = policy.principals.get(subject)

    claims = access_token.claims or {}
    claim_attributes = {
        target: claims.get(source)
        for source, target in config.AUTH_JWT_ATTRIBUTES.items()
        if source in claims
    }

    if principal_policy is not None:
        attributes = dict(claim_attributes)
        attributes.update(principal_policy.attributes)
        return Principal(
            subject=principal_policy.subject,
            roles=frozenset(principal_policy.roles),
            attributes=attributes,
        )

    if config.AUTH_PRINCIPAL_MODE not in {"hybrid", "claims"}:
        return None
    roles = _claim_list(claims.get(config.AUTH_JWT_ROLES_CLAIM))
    if not roles:
        return None
    return Principal(
        subject=subject,
        roles=frozenset(role.lower() for role in roles),
        attributes=claim_attributes,
    )


def _claim_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in value.replace(",", " ").split() if item]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value if item]
    return [str(value)]


def current_request_context(source: str = "mcp") -> RequestContext:
    """Return the authenticated request context or fail closed.

    HTTP requests authenticate through MCP's bearer-token middleware. stdio is
    a trusted local process boundary and therefore uses the configured local
    principal. Direct unit/service calls also use that local principal when
    authentication is disabled.
    """
    access_token = get_access_token()
    if access_token is not None:
        principal = principal_for_access_token(access_token)
        if principal is None:
            subject = access_token.subject or access_token.client_id
            raise AuthenticationError("认证主体未配置可用的服务端权限映射：%s" % subject)
        return RequestContext(
            principal=principal,
            source=source,
            auth_method="bearer",
        )

    if config.AUTH_MODE != "disabled" and config.MCP_TRANSPORT != "stdio":
        raise AuthenticationError("请求缺少有效的 Bearer Token")

    from ..authorization.manager import get_permission_policy

    policy = get_permission_policy()
    principal = principal_for_subject(policy.default_principal)
    if principal is None:
        raise AuthenticationError("默认权限主体未配置：%s" % policy.default_principal)
    return RequestContext(
        principal=principal,
        source=source,
        auth_method="local",
    )
