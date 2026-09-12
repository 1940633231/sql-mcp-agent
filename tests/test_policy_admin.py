"""Tests for policy administration tools."""
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
