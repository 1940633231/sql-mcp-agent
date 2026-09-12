"""Administrative MCP tools for permission-policy inspection and hot reload."""
import json

from ..auth.context import current_request_context
from ..authorization.manager import PolicyConflictError, get_policy_manager
from ..authorization.policy import permission_policy_to_dict, validate_policy_document

_manager = get_policy_manager()


def _require_permission(permission: str):
    context = current_request_context(source="policy-admin")
    permissions = _manager.get().permissions_for_roles(context.principal.roles)
    if permission not in permissions:
        raise PermissionError("缺少权限：%s" % permission)
    return context


def get_policy_status() -> dict:
    """Return the active policy source, version, actor, and update time."""
    _require_permission("policy:read")
    return _manager.status()


def reload_permission_policy() -> dict:
    """Force reload the active policy from its backing store."""
    _require_permission("policy:read")
    record = _manager.reload()
    return {
        "reloaded": True,
        "version": record.version,
        "source": record.source,
    }


def list_permission_policy_versions(limit: int = 20) -> list[dict]:
    """List recent policy versions from MySQL or the active file."""
    _require_permission("policy:read")
    return [
        {
            "version": record.version,
            "source": record.source,
            "actor": record.actor,
            "reason": record.reason,
            "updated_at": record.updated_at,
        }
        for record in _manager.list_versions(limit=limit)
    ]


def export_permission_policy() -> dict:
    """Export the active normalized policy document."""
    _require_permission("policy:read")
    return permission_policy_to_dict(_manager.get())


def publish_permission_policy(
    document: dict, reason: str = "", expected_version: str | None = None
) -> dict:
    """Validate and publish a new policy version, then hot-reload it."""
    context = _require_permission("policy:publish")
    payload_size = len(json.dumps(document, ensure_ascii=False).encode("utf-8"))
    if payload_size > 512 * 1024:
        raise ValueError("权限策略文档过大")
    report = validate_policy_document(document)
    if not report.valid:
        return {"published": False, "validation": report.to_dict()}
    try:
        record = _manager.publish(
            document,
            actor=context.principal.subject,
            reason=reason,
            expected_version=expected_version,
        )
    except PolicyConflictError as exc:
        return {"published": False, "code": "policy_conflict", "error": str(exc)}
    return {
        "published": True,
        "version": record.version,
        "source": record.source,
    }


def validate_permission_policy(document: dict) -> dict:
    """Validate a policy document without publishing it."""
    _require_permission("policy:validate")
    return validate_policy_document(document).to_dict()


def register(server) -> None:
    server.tool(description="校验权限策略但不发布")(validate_permission_policy)
    server.tool(description="查看当前权限策略状态")(get_policy_status)
    server.tool(description="强制热重载权限策略")(reload_permission_policy)
    server.tool(description="列出最近的权限策略版本")(list_permission_policy_versions)
    server.tool(description="导出当前权限策略")(export_permission_policy)
    server.tool(description="发布并热更新权限策略")(publish_permission_policy)
