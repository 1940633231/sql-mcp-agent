"""权限策略查看与热加载的 MCP 管理工具。"""
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
    """返回 active 策略的来源、版本、操作者和更新时间。"""
    _require_permission("policy:read")
    return _manager.status()


def reload_permission_policy() -> dict:
    """从后端存储强制重新加载 active 策略。"""
    _require_permission("policy:read")
    record = _manager.reload()
    return {
        "reloaded": True,
        "version": record.version,
        "source": record.source,
    }


def _short_value(value, maxlen: int = 120) -> str:
    """把 diff 的值截短成一行，避免 MCP 响应被超长值刷屏。"""
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = str(text).replace("\n", " ")
    return text if len(text) <= maxlen else text[:maxlen] + "..."


def _diff_documents(base: dict, target: dict) -> list[dict]:
    """结构化对比两个策略文档（规范化 dict），返回 add/remove/modify 变更列表。

    变更项含 ``op``（add/remove/modify）、``path``（点路径/下标路径）与人性化
    ``detail``。仅按顶层结构递归，字段自身的语义不做二次推断。
    """
    changes: list[dict] = []

    def _collect_added(value, path, changes):
        changes.append({"op": "add", "path": path, "detail": "+ %s = %s" % (path, _short_value(value))})

    def _collect_removed(value, path, changes):
        changes.append({"op": "remove", "path": path, "detail": "- %s = %s" % (path, _short_value(value))})

    def _diff_value(a, b, path, changes):
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                child = "%s.%s" % (path, key) if path else key
                if key not in a:
                    _collect_added(b[key], child, changes)
                elif key not in b:
                    _collect_removed(a[key], child, changes)
                else:
                    _diff_value(a[key], b[key], child, changes)
        elif isinstance(a, list) and isinstance(b, list):
            for idx in range(max(len(a), len(b))):
                child = "%s[%d]" % (path, idx)
                if idx >= len(b):
                    _collect_removed(a[idx], child, changes)
                elif idx >= len(a):
                    _collect_added(b[idx], child, changes)
                else:
                    _diff_value(a[idx], b[idx], child, changes)
        elif a != b:
            changes.append(
                {"op": "modify", "path": path, "detail": "%s: %s → %s" % (path, _short_value(a), _short_value(b))}
            )

    _diff_value(base, target, "", changes)
    return changes


def list_permission_policy_versions(limit: int = 20) -> list[dict]:
    """列出 MySQL 或当前文件中的最近策略版本，并附上每版相对前一版本的结构化 diff。

    diff 描述「该版本相对上一版本」改动（新增/删除/修改），其中最新（active）版本
    对比被它替换的上一版本；无相邻版本的旧版本 diff 为空列表。
    """
    _require_permission("policy:read")
    records = _manager.list_versions(limit=limit)
    result: list[dict] = []
    for i, record in enumerate(records):
        base = records[i + 1].document if i + 1 < len(records) else None
        result.append(
            {
                "version": record.version,
                "source": record.source,
                "actor": record.actor,
                "reason": record.reason,
                "updated_at": record.updated_at,
                "diff": _diff_documents(base, record.document) if base is not None else [],
            }
        )
    return result


def export_permission_policy() -> dict:
    """导出当前规范化策略文档。"""
    _require_permission("policy:read")
    return permission_policy_to_dict(_manager.get())


def publish_permission_policy(
    document: dict, reason: str = "", expected_version: str | None = None
) -> dict:
    """校验并发布新策略版本，随后立即热加载。"""
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
    """仅校验策略文档，不执行发布。"""
    _require_permission("policy:validate")
    return validate_policy_document(document).to_dict()


def register(server) -> None:
    server.tool(description="校验权限策略但不发布")(validate_permission_policy)
    server.tool(description="查看当前权限策略状态")(get_policy_status)
    server.tool(description="强制热重载权限策略")(reload_permission_policy)
    server.tool(description="列出最近的权限策略版本")(list_permission_policy_versions)
    server.tool(description="导出当前权限策略")(export_permission_policy)
    server.tool(description="发布并热更新权限策略")(publish_permission_policy)
