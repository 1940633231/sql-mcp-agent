"""从 MCP 认证上下文构建请求身份。"""
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

from .. import config
from ..observability.context import get_trace_context
from .models import Principal, RequestContext


class AuthenticationError(PermissionError):
    """请求缺少可接受的认证身份时抛出。"""




def principal_for_subject(subject: str, policy_context=None) -> Principal | None:
    """为已认证 subject 解析服务端角色与属性。"""
    from ..authorization.manager import get_policy_manager

    policy_context = policy_context or get_policy_manager().get_context()
    policy = policy_context.policy
    principal_policy = policy.principals.get(subject)
    if principal_policy is None:
        return None
    return Principal(
        subject=principal_policy.subject,
        roles=frozenset(principal_policy.roles),
        attributes=principal_policy.attributes,
    )


def principal_for_access_token(access_token: AccessToken, policy_context=None) -> Principal | None:
    """优先使用服务端策略，其次使用已验证的 JWT claims 解析主体。"""
    from ..authorization.manager import get_policy_manager

    subject = access_token.subject or access_token.client_id
    policy_context = policy_context or get_policy_manager().get_context()
    policy = policy_context.policy
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

    if config.AUTH_PRINCIPAL_MODE not in {"claims", "mapped_claims"}:
        return None
    claimed_roles = _claim_list(claims.get(config.AUTH_JWT_ROLES_CLAIM))
    roles = _map_claim_roles(claimed_roles, policy)
    if not roles:
        return None
    return Principal(
        subject=subject,
        roles=frozenset(role.lower() for role in roles),
        attributes=claim_attributes,
    )


def _map_claim_roles(claimed_roles: list[str], policy) -> set[str]:
    mapped: set[str] = set()
    for claimed in claimed_roles:
        target = config.AUTH_JWT_ROLE_MAP.get(claimed)
        if target:
            mapped.add(target.lower())
    if config.AUTH_ALLOW_RAW_ROLE_CLAIMS:
        mapped.update(role.lower() for role in claimed_roles)
    for role in policy.roles.values():
        if role.bypass_rls or any(permission.startswith("policy:") for permission in role.permissions):
            mapped.discard(role.name)
    return mapped


def _claim_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in value.replace(",", " ").split() if item]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value if item]
    return [str(value)]


def current_request_context(source: str = "mcp", policy_context=None) -> RequestContext:
    """返回已认证的请求上下文；无法认证时按拒绝处理。

    HTTP 请求通过 MCP Bearer Token 中间件认证；stdio 属于受信任本地进程，
    因此使用配置的本地 principal。认证关闭时，直接调用服务也使用该身份。
    """
    from ..authorization.manager import get_policy_manager

    policy_context = policy_context or get_policy_manager().get_context()
    trace = get_trace_context()
    access_token = get_access_token()
    if access_token is not None:
        principal = principal_for_access_token(access_token, policy_context)
        if principal is None:
            subject = access_token.subject or access_token.client_id
            raise AuthenticationError("认证主体未配置可用的服务端权限映射：%s" % subject)
        return RequestContext(
            principal=principal,
            request_id=trace.request_id,
            trace_id=trace.trace_id,
            session_id=trace.session_id,
            client_id=trace.client_id,
            tool_name=trace.tool_name,
            source=source,
            auth_method="bearer",
            policy_version=policy_context.version,
            policy_hash=policy_context.policy_hash,
            policy_source=policy_context.source,
        )

    if config.AUTH_MODE != "disabled" and config.MCP_TRANSPORT != "stdio":
        raise AuthenticationError("请求缺少有效的 Bearer Token")


    policy = policy_context.policy
    principal = principal_for_subject(policy.default_principal)
    if principal is None:
        raise AuthenticationError("默认权限主体未配置：%s" % policy.default_principal)
    return RequestContext(
        principal=principal,
        request_id=trace.request_id,
        trace_id=trace.trace_id,
        session_id=trace.session_id,
        client_id=trace.client_id,
        tool_name=trace.tool_name,
        source=source,
        auth_method="local",
        policy_version=policy_context.version,
        policy_hash=policy_context.policy_hash,
        policy_source=policy_context.source,
    )
