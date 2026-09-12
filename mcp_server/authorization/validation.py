"""Schema, semantic, and security validation for permission policies."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 1
POLICY_DOCUMENT_MAX_BYTES = 512 * 1024
POLICY_DOCUMENT_MAX_DEPTH = 20

_ALLOWED_ACTIONS = {"select"}
_ALLOWED_EFFECTS = {"allow", "deny"}
_ROW_OPERATORS = {"eq", "ne", "lt", "lte", "gt", "gte", "in"}
_PROTECTED_PREFIXES = ("policy:",)
_PROTECTED_PERMISSIONS = {"rls:bypass"}
_IDENTIFIER_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")


class PolicyValidationError(ValueError):
    def __init__(self, report: "PolicyValidationReport"):
        self.report = report
        super().__init__(report.summary())


@dataclass(frozen=True)
class PolicyIssue:
    level: str
    code: str
    path: str
    message: str


@dataclass
class PolicyValidationReport:
    issues: list[PolicyIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[PolicyIssue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> list[PolicyIssue]:
        return [issue for issue in self.issues if issue.level == "warning"]

    @property
    def valid(self) -> bool:
        return not self.errors

    def error(self, code: str, path: str, message: str) -> None:
        self.issues.append(PolicyIssue("error", code, path, message))

    def warning(self, code: str, path: str, message: str) -> None:
        self.issues.append(PolicyIssue("warning", code, path, message))

    def summary(self) -> str:
        if self.valid:
            return "policy validation passed (%d warnings)" % len(self.warnings)
        first = self.errors[0]
        return "policy validation failed: %s at %s: %s" % (first.code, first.path, first.message)

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": [issue.__dict__ for issue in self.errors],
            "warnings": [issue.__dict__ for issue in self.warnings],
        }


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "duplicate key: %s" % key,
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_policy_document(path: str | Path) -> dict:
    policy_path = Path(path)
    if not policy_path.exists():
        return {}
    return yaml.load(policy_path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader) or {}


def validate_policy_document(data: Any) -> PolicyValidationReport:
    report = PolicyValidationReport()
    _validate_shape(data, report)
    if report.errors:
        return report
    _validate_schema(data, report)
    if report.errors:
        return report
    normalized = _normalize(data, report)
    if report.errors:
        return report
    _validate_semantics(normalized, report)
    _validate_security(normalized, report)
    return report


def build_policy(data: dict):
    from .models import AclRule, PermissionPolicy, PolicySubjects, PrincipalPolicy, RolePolicy, RowPolicy
    from types import MappingProxyType

    normalized = _normalize(data, PolicyValidationReport())
    roles = {
        name: RolePolicy(name, frozenset(role["permissions"]), role["bypass_rls"])
        for name, role in normalized["roles"].items()
    }
    principals = {
        subject: PrincipalPolicy(
            subject,
            frozenset(principal["roles"]),
            MappingProxyType(dict(principal["attributes"])),
        )
        for subject, principal in normalized["principals"].items()
    }
    acl_rules = [
        AclRule(
            PolicySubjects(
                frozenset(rule["subjects"]["users"]),
                frozenset(rule["subjects"]["roles"]),
            ),
            rule["resource_type"],
            rule["table"],
            rule["action"],
            rule["effect"],
            rule["column"] if rule["resource_type"] == "column" else "",
        )
        for rule in normalized["acl"]
    ]
    row_policies = [
        RowPolicy(
            row["name"],
            PolicySubjects(
                frozenset(row["subjects"]["users"]),
                frozenset(row["subjects"]["roles"]),
            ),
            frozenset(row["tables"]),
            frozenset(row["actions"]),
            row["column"],
            row["operator"],
            row["value_from"],
            row["value"],
        )
        for row in normalized["row_policies"]
    ]
    return PermissionPolicy(
        roles,
        principals,
        acl_rules,
        row_policies,
        normalized["default_principal"],
    )


def compile_policy(data: Any):
    report = validate_policy_document(data)
    if not report.valid:
        raise PolicyValidationError(report)
    return build_policy(data)


def _validate_shape(data: Any, report: PolicyValidationReport) -> None:
    if not isinstance(data, dict):
        report.error("schema_root", "$", "策略文档顶层必须是 object")
        return
    if len(yaml.safe_dump(data, allow_unicode=True).encode("utf-8")) > POLICY_DOCUMENT_MAX_BYTES:
        report.error("document_too_large", "$", "策略文档超过大小上限")
    if _depth(data) > POLICY_DOCUMENT_MAX_DEPTH:
        report.error("document_too_deep", "$", "策略文档嵌套层级过深")
    known = {"version", "default_principal", "roles", "principals", "acl", "row_policies"}
    for key in data:
        if key not in known:
            report.error("unknown_field", "$.%s" % key, "未知字段")
    if "version" not in data:
        report.error("missing_version", "$.version", "缺少策略版本号")


def _validate_schema(data: dict, report: PolicyValidationReport) -> None:
    if str(data.get("version")) != str(SCHEMA_VERSION):
        report.error("unsupported_version", "$.version", "仅支持策略版本 %s" % SCHEMA_VERSION)
    if not isinstance(data.get("default_principal"), str) or not data.get("default_principal", "").strip():
        report.error("string_required", "$.default_principal", "default_principal 必须是非空字符串")
    _validate_roles_schema(data.get("roles"), report)
    _validate_principals_schema(data.get("principals"), report)
    _validate_acl_schema(data.get("acl", []), report)
    _validate_row_schema(data.get("row_policies", []), report)


def _validate_roles_schema(roles, report: PolicyValidationReport) -> None:
    if not isinstance(roles, dict) or not roles:
        report.error("roles_type", "$.roles", "roles 必须是非空 object")
        return
    for name, raw in roles.items():
        path = "$.roles.%s" % name
        if not _valid_identifier(name) or str(name).lower() != str(name):
            report.error("role_name", path, "角色名只允许小写字母数字下划线")
        if not isinstance(raw, dict):
            report.error("role_type", path, "角色定义必须是 object")
            continue
        _unknown(raw, {"permissions", "bypass_rls"}, path, report)
        if not isinstance(raw.get("permissions"), list) or not raw.get("permissions"):
            report.error("permissions_type", path + ".permissions", "permissions 必须是非空数组")
        elif not all(isinstance(item, str) and item.strip() for item in raw["permissions"]):
            report.error("permission_type", path + ".permissions", "permission 必须是非空字符串")
        if not isinstance(raw.get("bypass_rls", False), bool):
            report.error("bypass_rls_type", path + ".bypass_rls", "bypass_rls 必须是 boolean")


def _validate_principals_schema(principals, report: PolicyValidationReport) -> None:
    if not isinstance(principals, dict) or not principals:
        report.error("principals_type", "$.principals", "principals 必须是非空 object")
        return
    for subject, raw in principals.items():
        path = "$.principals.%s" % subject
        if not isinstance(subject, str) or not subject.strip():
            report.error("principal_name", path, "principal subject 必须是非空字符串")
        if not isinstance(raw, dict):
            report.error("principal_type", path, "principal 定义必须是 object")
            continue
        _unknown(raw, {"roles", "attributes"}, path, report)
        if not isinstance(raw.get("roles"), list) or not raw.get("roles"):
            report.error("principal_roles", path + ".roles", "principal.roles 必须是非空数组")
        elif not all(isinstance(item, str) and item.strip() for item in raw["roles"]):
            report.error("principal_role_type", path + ".roles", "角色引用必须是非空字符串")
        if not isinstance(raw.get("attributes", {}), dict):
            report.error("attributes_type", path + ".attributes", "attributes 必须是 object")


def _validate_acl_schema(acl, report: PolicyValidationReport) -> None:
    if not isinstance(acl, list):
        report.error("acl_type", "$.acl", "acl 必须是数组")
        return
    for index, raw in enumerate(acl):
        path = "$.acl[%d]" % index
        if not isinstance(raw, dict):
            report.error("acl_item_type", path, "ACL 规则必须是 object")
            continue
        _unknown(raw, {"subjects", "resource", "action", "effect"}, path, report)
        _validate_subjects(raw.get("subjects"), path + ".subjects", report)
        if not isinstance(raw.get("resource"), dict):
            report.error("resource_type", path + ".resource", "resource 必须是 object")
        else:
            _unknown(raw["resource"], {"type", "table", "name"}, path + ".resource", report)
        if not isinstance(raw.get("action"), str):
            report.error("action_type", path + ".action", "action 必须是字符串")
        if not isinstance(raw.get("effect"), str):
            report.error("effect_type", path + ".effect", "effect 必须是字符串")


def _validate_row_schema(rows, report: PolicyValidationReport) -> None:
    if not isinstance(rows, list):
        report.error("row_policies_type", "$.row_policies", "row_policies 必须是数组")
        return
    for index, raw in enumerate(rows):
        path = "$.row_policies[%d]" % index
        if not isinstance(raw, dict):
            report.error("row_policy_type", path, "RLS 规则必须是 object")
            continue
        _unknown(raw, {"name", "subjects", "tables", "actions", "predicate"}, path, report)
        if not isinstance(raw.get("name"), str) or not raw.get("name", "").strip():
            report.error("row_policy_name", path + ".name", "RLS 规则必须有非空名称")
        _validate_subjects(raw.get("subjects"), path + ".subjects", report)
        if not isinstance(raw.get("tables"), list) or not raw.get("tables"):
            report.error("row_policy_tables", path + ".tables", "tables 必须是非空数组")
        if not isinstance(raw.get("actions"), list) or not raw.get("actions"):
            report.error("row_policy_actions", path + ".actions", "actions 必须是非空数组")
        if not isinstance(raw.get("predicate"), dict):
            report.error("predicate_type", path + ".predicate", "predicate 必须是 object")
        else:
            _unknown(raw["predicate"], {"column", "operator", "value", "value_from"}, path + ".predicate", report)


def _normalize(data: dict, report: PolicyValidationReport) -> dict:
    out = {
        "default_principal": str(data.get("default_principal") or ""),
        "roles": {},
        "principals": {},
        "acl": [],
        "row_policies": [],
    }
    seen = set()
    for name, raw in (data.get("roles") or {}).items():
        key = str(name).lower()
        if key in seen:
            report.error("duplicate_role", "$.roles.%s" % name, "角色名归一后重复")
        seen.add(key)
        out["roles"][key] = {
            "permissions": [str(item).lower() for item in raw.get("permissions") or []],
            "bypass_rls": bool(raw.get("bypass_rls", False)),
        }
    for subject, raw in (data.get("principals") or {}).items():
        out["principals"][str(subject)] = {
            "roles": [str(item).lower() for item in raw.get("roles") or []],
            "attributes": dict(raw.get("attributes") or {}),
        }
    for index, raw in enumerate(data.get("acl") or []):
        resource = raw.get("resource") or {}
        out["acl"].append({
            "index": index,
            "subjects": _norm_subjects(raw.get("subjects")),
            "resource_type": str(resource.get("type", "")).lower(),
            "table": str(resource.get("table") or resource.get("name") or "").lower(),
            "column": str(resource.get("name") or "").lower(),
            "action": str(raw.get("action", "")).lower(),
            "effect": str(raw.get("effect", "")).lower(),
        })
    for index, raw in enumerate(data.get("row_policies") or []):
        predicate = raw.get("predicate") or {}
        out["row_policies"].append({
            "index": index,
            "name": str(raw.get("name") or ""),
            "subjects": _norm_subjects(raw.get("subjects")),
            "tables": [str(item).lower() for item in raw.get("tables") or []],
            "actions": [str(item).lower() for item in raw.get("actions") or []],
            "column": str(predicate.get("column") or "").lower(),
            "operator": str(predicate.get("operator") or "").lower(),
            "value_from": str(predicate.get("value_from") or ""),
            "value": predicate.get("value"),
            "has_value": "value" in predicate,
        })
    return out


def _validate_semantics(data: dict, report: PolicyValidationReport) -> None:
    roles = set(data["roles"])
    if data["default_principal"] not in data["principals"]:
        report.error("default_principal_missing", "$.default_principal", "默认 principal 不存在")
    for subject, raw in data["principals"].items():
        for role in raw["roles"]:
            if role not in roles:
                report.error("unknown_role", "$.principals.%s.roles" % subject, "引用不存在的角色：%s" % role)
    for rule in data["acl"]:
        path = "$.acl[%d]" % rule["index"]
        if rule["resource_type"] not in {"table", "column"}:
            report.error("resource_kind", path + ".resource.type", "type 仅支持 table/column")
        if not _valid_resource(rule["table"]):
            report.error("resource_table", path + ".resource", "非法表名或通配符")
        if rule["resource_type"] == "column" and not _valid_resource(rule["column"]):
            report.error("resource_column", path + ".resource.name", "非法列名或通配符")
        if rule["action"] not in _ALLOWED_ACTIONS:
            report.error("action_value", path + ".action", "当前仅允许 select")
        if rule["effect"] not in _ALLOWED_EFFECTS:
            report.error("effect_value", path + ".effect", "effect 仅允许 allow/deny")
        for role in rule["subjects"]["roles"]:
            if role not in roles:
                report.error("unknown_role", path + ".subjects.roles", "引用不存在的角色：%s" % role)
    names = set()
    for row in data["row_policies"]:
        path = "$.row_policies[%d]" % row["index"]
        if row["name"] in names:
            report.error("duplicate_row_policy", path + ".name", "RLS 名称重复")
        names.add(row["name"])
        if not row["tables"] or not all(_valid_resource(table) for table in row["tables"]):
            report.error("row_policy_tables", path + ".tables", "RLS 表名非法或为空")
        if not row["actions"] or any(action not in _ALLOWED_ACTIONS for action in row["actions"]):
            report.error("row_policy_actions", path + ".actions", "RLS action 仅允许 select")
        if not _valid_identifier(row["column"]):
            report.error("row_policy_column", path + ".predicate.column", "RLS 列名非法")
        if row["operator"] not in _ROW_OPERATORS:
            report.error("row_policy_operator", path + ".predicate.operator", "不支持的 RLS operator")
        if row["has_value"] and row["value_from"]:
            report.error("row_policy_value_conflict", path + ".predicate", "value 与 value_from 不能同时设置")
        if not row["has_value"] and not row["value_from"]:
            report.error("row_policy_value_missing", path + ".predicate", "必须设置 value 或 value_from")
        if row["value_from"] and not row["value_from"].startswith("principal."):
            report.error("row_policy_value_from", path + ".predicate.value_from", "必须引用 principal")
        if row["operator"] == "in" and row["has_value"] and not isinstance(row["value"], list):
            report.error("row_policy_in_value", path + ".predicate.value", "IN 的 value 必须是数组")
        for role in row["subjects"]["roles"]:
            if role not in roles:
                report.error("unknown_role", path + ".subjects.roles", "引用不存在的角色：%s" % role)


def _validate_security(data: dict, report: PolicyValidationReport) -> None:
    for role_name, role in data["roles"].items():
        permissions = set(role["permissions"])
        path = "$.roles.%s" % role_name
        if "*" in permissions:
            report.error("global_wildcard", path + ".permissions", "禁止全局 * 权限")
        if role["bypass_rls"] and any(_is_protected_permission(item) for item in permissions):
            report.error("control_data_mix", path, "控制面权限不能与 RLS bypass 放在同一角色")
        if "query:run" in permissions and any(_is_protected_permission(item) for item in permissions):
            report.error("control_data_mix", path, "查询权限与策略控制面权限必须分离")
    default_roles = set(data["principals"].get(data["default_principal"], {}).get("roles") or [])
    for role_name in default_roles:
        role = data["roles"].get(role_name, {})
        if "policy:publish" in role.get("permissions", []) or role.get("bypass_rls"):
            report.warning("privileged_default_principal", "$.default_principal", "默认 principal 不应拥有控制面或 RLS bypass 权限")
    for rule in data["acl"]:
        if rule["effect"] == "allow" and rule["table"] == "*":
            report.warning("broad_table_grant", "$.acl[%d]" % rule["index"], "允许访问所有表，需确认最小权限")
        if rule["effect"] == "allow" and rule["column"] == "*":
            report.warning("broad_column_grant", "$.acl[%d]" % rule["index"], "允许访问所有列，需确认最小权限")


def _validate_subjects(value, path: str, report: PolicyValidationReport) -> None:
    if not isinstance(value, dict):
        report.error("subjects_type", path, "subjects 必须是 object")
        return
    _unknown(value, {"users", "roles"}, path, report)
    users, roles = value.get("users", []), value.get("roles", [])
    if not isinstance(users, list) or not isinstance(roles, list):
        report.error("subjects_fields", path, "users/roles 必须是数组")
        return
    if not users and not roles:
        report.error("subjects_empty", path, "subjects 至少需要 user 或 role")


def _norm_subjects(value) -> dict:
    value = value or {}
    return {
        "users": [str(item) for item in value.get("users") or []],
        "roles": [str(item).lower() for item in value.get("roles") or []],
    }


def _unknown(data: dict, known: set[str], path: str, report: PolicyValidationReport) -> None:
    for key in data:
        if key not in known:
            report.error("unknown_field", "%s.%s" % (path, key), "未知字段")


def _valid_identifier(value) -> bool:
    return bool(value) and all(char in _IDENTIFIER_CHARS for char in str(value))


def _valid_resource(value) -> bool:
    if not value:
        return False
    if value == "*":
        return True
    if value.endswith("*"):
        return _valid_identifier(value[:-1])
    return _valid_identifier(value)


def _is_protected_permission(permission: str) -> bool:
    return permission in _PROTECTED_PERMISSIONS or permission.startswith(_PROTECTED_PREFIXES)


def _depth(value, level: int = 0) -> int:
    if isinstance(value, dict):
        return max([level] + [_depth(item, level + 1) for item in value.values()])
    if isinstance(value, list):
        return max([level] + [_depth(item, level + 1) for item in value])
    return level
