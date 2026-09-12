"""tests/test_policy_manager.py — policy persistence and hot reload tests."""
import pytest

from mcp_server.authorization.manager import MemoryPolicyStore, PolicyConflictError, PolicyManager
from mcp_server.authorization.policy import (
    load_permission_policy,
    permission_policy_from_dict,
    permission_policy_to_dict,
)


def test_policy_document_round_trip():
    policy = load_permission_policy()
    document = permission_policy_to_dict(policy)
    restored = permission_policy_from_dict(document)
    assert permission_policy_to_dict(restored) == document


def test_memory_store_publish_and_hot_reload():
    store = MemoryPolicyStore(permission_policy_to_dict(load_permission_policy()))
    manager = PolicyManager(store, reload_seconds=0)
    original = permission_policy_to_dict(manager.get())

    updated = dict(original)
    updated["default_principal"] = "alice"
    store.publish(updated, actor="test", reason="hot reload")

    assert manager.get().default_principal == "alice"
    assert manager.status()["actor"] == "test"


def test_expected_version_prevents_lost_update():
    store = MemoryPolicyStore(permission_policy_to_dict(load_permission_policy()))
    manager = PolicyManager(store, reload_seconds=0)
    document = permission_policy_to_dict(manager.get())

    try:
        manager.publish(document, expected_version="stale-version")
    except PolicyConflictError:
        pass
    else:
        raise AssertionError("expected version conflict")


def test_invalid_policy_is_rejected_before_publish():
    store = MemoryPolicyStore()
    with pytest.raises(ValueError):
        store.publish({
            "roles": {},
            "acl": [{"resource": {"type": "unsupported"}}],
        })
