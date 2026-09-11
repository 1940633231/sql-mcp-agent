"""Administrative MCP tools for permission-policy inspection and hot reload."""
import json

from ..auth.context import current_request_context
from ..authorization.manager import get_policy_manager
from ..authorization.policy import permission_policy_to_dict

_manager = get_policy_manager()


def _require_policy_admin():
    context = current_request_context(source="policy-admin")
    permissions = _manager.get().permissions_for_roles(context.principal.roles)
    if "*" not in permissions and "policy:admin" not in permissions:
        raise PermissionError("无权管理权限策略")
    return context


def get_policy_status() -> dict:
    """Return the active policy source, version, actor, and update time."""
    _require_policy_admin()
    return _manager.status()


def reload_permission_policy() -> dict:
    """Force reload the active policy from its backing store."""
    _require_policy_admin()
    record = _manager.reload()
    return {
        "reloaded": True,
        "version": record.version,
        "source": record.source,
    }


def list_permission_policy_versions(limit: int = 20) -> list[dict]:
    """List recent policy versions from MySQL or the active file."""
    _require_policy_admin()
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
    _require_policy_admin()
    return permission_policy_to_dict(_manager.get())


def publish_permission_policy(document: dict, reason: str = "") -> dict:
    """Validate and publish a new policy version, then hot-reload it."""
    context = _require_policy_admin()
    payload_size = len(json.dumps(document, ensure_ascii=False).encode("utf-8"))
    if payload_size > 512 * 1024:
        raise ValueError("权限策略文档过大")
    record = _manager.publish(
        document,
        actor=context.principal.subject,
        reason=reason,
    )
    return {
        "published": True,
        "version": record.version,
        "source": record.source,
    }


def register(server) -> None:
    server.tool(description="查看当前权限策略状态")(get_policy_status)
    server.tool(description="强制热重载权限策略")(reload_permission_policy)
    server.tool(description="列出最近的权限策略版本")(list_permission_policy_versions)
    server.tool(description="导出当前权限策略")(export_permission_policy)
    server.tool(description="发布并热更新权限策略")(publish_permission_policy)
