"""策略管理工具测试。"""
import pytest

from mcp_server.auth.models import Principal, RequestContext
from mcp_server.authorization.manager import MemoryPolicyStore, PolicyManager
from mcp_server.authorization.policy import load_permission_policy, permission_policy_to_dict
from mcp_server.tools import policy_admin


def _context(policy, subject: str) -> RequestContext:
    raw = policy.principals[subject]
    return RequestContext(Principal(raw.subject, raw.roles, raw.attributes), source="test")


def test_admin_can_publish_and_reload(monkeypatch):
    policy = load_permission_policy()
    manager = PolicyManager(
        MemoryPolicyStore(permission_policy_to_dict(policy)),
        reload_seconds=0,
    )
    monkeypatch.setattr(policy_admin, "_manager", manager)
    monkeypatch.setattr(
        policy_admin,
        "current_request_context",
        lambda source="policy-admin": _context(policy, "policy-admin"),
    )

    status = policy_admin.get_policy_status()
    assert status["available"] is True
    assert policy_admin.validate_permission_policy(
        permission_policy_to_dict(policy)
    )["valid"] is True
    published = policy_admin.publish_permission_policy(
        permission_policy_to_dict(policy), reason="test"
    )
    assert published["published"] is True
    assert policy_admin.reload_permission_policy()["reloaded"] is True


def test_list_versions_includes_structured_diff(monkeypatch):
    policy = load_permission_policy()
    store = MemoryPolicyStore(permission_policy_to_dict(policy))
    manager = PolicyManager(store, reload_seconds=0)
    monkeypatch.setattr(policy_admin, "_manager", manager)
    monkeypatch.setattr(
        policy_admin,
        "current_request_context",
        lambda source="policy-admin": _context(policy, "policy-admin"),
    )

    # 变更：给 alice 加一个角色，再发布一版。
    changed = permission_policy_to_dict(policy)
    changed["principals"]["alice"]["roles"].append("viewer")
    manager.publish(changed, actor="policy-admin", reason="grant viewer to alice")

    versions = policy_admin.list_permission_policy_versions(limit=10)
    assert len(versions) == 2
    newest, older = versions[0], versions[1]
    # 最新版本相对旧版本应标注出一处新增（or roles 变更）。
    diff = newest["diff"]
    assert diff, "最新版本应包含 diff"
    assert any(item["op"] in {"add", "modify"} for item in diff)
    assert any("principals.alice" in item["path"] for item in diff)
    # 旧版本没有更早基线，diff 为空。
    assert older["diff"] == []


def test_viewer_cannot_publish(monkeypatch):
    policy = load_permission_policy()
    manager = PolicyManager(
        MemoryPolicyStore(permission_policy_to_dict(policy)),
        reload_seconds=0,
    )
    monkeypatch.setattr(policy_admin, "_manager", manager)
    monkeypatch.setattr(
        policy_admin,
        "current_request_context",
        lambda source="policy-admin": _context(policy, "auditor"),
    )

    with pytest.raises(PermissionError):
        policy_admin.get_policy_status()
