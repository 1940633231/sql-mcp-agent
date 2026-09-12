"""权限策略结构、语义和安全校验测试。"""
from copy import deepcopy

import pytest
import yaml

from mcp_server.authorization.validation import load_policy_document

from mcp_server.authorization.policy import (
    load_permission_policy,
    permission_policy_to_dict,
    validate_policy_document,
)


def _document() -> dict:
    return permission_policy_to_dict(load_permission_policy())


def _codes(report) -> set[str]:
    return {issue.code for issue in report.errors}


def test_current_policy_passes_validation():
    report = validate_policy_document(_document())
    assert report.valid, report.summary()


def test_duplicate_yaml_keys_are_rejected(tmp_path):
    path = tmp_path / "duplicate.yaml"
    path.write_text("version: 1\nversion: 1\n", encoding="utf-8")
    with pytest.raises(yaml.constructor.ConstructorError):
        load_policy_document(path)


def test_version_is_required():
    document = _document()
    document.pop("version")
    assert "missing_version" in _codes(validate_policy_document(document))


def test_global_wildcard_is_rejected():
    document = _document()
    document["roles"]["analyst"]["permissions"] = ["*"]
    assert "global_wildcard" in _codes(validate_policy_document(document))


def test_unknown_fields_are_rejected():
    document = _document()
    document["extra"] = True
    document["roles"]["analyst"]["mystery"] = True
    codes = _codes(validate_policy_document(document))
    assert "unknown_field" in codes


def test_unknown_role_reference_is_rejected():
    document = _document()
    document["principals"]["alice"]["roles"] = ["missing_role"]
    assert "unknown_role" in _codes(validate_policy_document(document))


def test_invalid_effect_and_operator_are_rejected():
    document = _document()
    document["acl"][1]["effect"] = "audit"
    document["row_policies"][0]["predicate"]["operator"] = "regex"
    codes = _codes(validate_policy_document(document))
    assert "effect_value" in codes
    assert "row_policy_operator" in codes


def test_control_and_data_plane_cannot_be_mixed():
    document = _document()
    document["roles"]["policy_admin"]["permissions"].append("query:run")
    assert "control_data_mix" in _codes(validate_policy_document(document))


def test_empty_subjects_are_rejected():
    document = deepcopy(_document())
    document["acl"][1]["subjects"] = {"users": [], "roles": []}
    assert "subjects_empty" in _codes(validate_policy_document(document))
