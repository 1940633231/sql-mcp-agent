"""Load authorization policy from ``configs/permissions.yaml``."""
from pathlib import Path
from types import MappingProxyType

import yaml

from .models import AclRule, PermissionPolicy, PolicySubjects, PrincipalPolicy, RolePolicy, RowPolicy

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "configs" / "permissions.yaml"


def _freeze_mapping(value: dict | None) -> MappingProxyType:
    return MappingProxyType(dict(value or {}))


def _subjects(data: dict | None) -> PolicySubjects:
    data = data or {}
    return PolicySubjects(
        users=frozenset(str(v).lower() for v in data.get("users") or []),
        roles=frozenset(str(v).lower() for v in data.get("roles") or []),
    )


def permission_policy_from_dict(data: dict | None) -> PermissionPolicy:
    data = data or {}

    roles: dict[str, RolePolicy] = {}
    for name, raw in (data.get("roles") or {}).items():
        raw = raw or {}
        permissions = {str(p).lower() for p in raw.get("permissions") or []}
        roles[str(name).lower()] = RolePolicy(
            name=str(name).lower(),
            permissions=frozenset(permissions),
            bypass_rls=bool(raw.get("bypass_rls", False)),
        )

    principals: dict[str, PrincipalPolicy] = {}
    for subject, raw in (data.get("principals") or {}).items():
        raw = raw or {}
        principals[str(subject)] = PrincipalPolicy(
            subject=str(subject),
            roles=frozenset(str(r).lower() for r in raw.get("roles") or []),
            attributes=_freeze_mapping(raw.get("attributes")),
        )

    acl_rules: list[AclRule] = []
    for raw in data.get("acl") or []:
        raw = raw or {}
        resource = raw.get("resource") or {}
        resource_type = str(resource.get("type", "table")).lower()
        if resource_type not in {"table", "column"}:
            raise ValueError("ACL resource.type 仅支持 table/column：%s" % resource_type)
        acl_rules.append(
            AclRule(
                subjects=_subjects(raw.get("subjects")),
                resource_type=resource_type,
                table=str(resource.get("table") or resource.get("name") or "*").lower(),
                column=str(resource.get("name") or "*").lower() if resource_type == "column" else "",
                action=str(raw.get("action") or "select").lower(),
                effect=str(raw.get("effect") or "allow").lower(),
            )
        )

    row_policies: list[RowPolicy] = []
    for raw in data.get("row_policies") or []:
        raw = raw or {}
        predicate = raw.get("predicate") or {}
        row_policies.append(
            RowPolicy(
                name=str(raw.get("name") or "row-policy"),
                subjects=_subjects(raw.get("subjects")),
                tables=frozenset(str(t).lower() for t in raw.get("tables") or []),
                actions=frozenset(str(a).lower() for a in raw.get("actions") or ["select"]),
                column=str(predicate.get("column") or "").lower(),
                operator=str(predicate.get("operator") or "eq").lower(),
                value_from=str(predicate.get("value_from") or ""),
                value=predicate.get("value"),
            )
        )

    if not roles:
        roles["admin"] = RolePolicy("admin", frozenset({"*"}), bypass_rls=True)
    if not principals:
        principals["local-dev"] = PrincipalPolicy("local-dev", frozenset({"admin"}))

    return PermissionPolicy(
        roles=roles,
        principals=principals,
        acl_rules=acl_rules,
        row_policies=row_policies,
        default_principal=str(data.get("default_principal") or "local-dev"),
    )


def permission_policy_to_dict(policy: PermissionPolicy) -> dict:
    """Serialize a normalized policy for persistence and admin APIs."""
    return {
        "default_principal": policy.default_principal,
        "roles": {
            name: {"permissions": sorted(role.permissions), "bypass_rls": role.bypass_rls}
            for name, role in sorted(policy.roles.items())
        },
        "principals": {
            subject: {"roles": sorted(principal.roles), "attributes": dict(principal.attributes)}
            for subject, principal in sorted(policy.principals.items())
        },
        "acl": [
            {
                "subjects": {
                    "users": sorted(rule.subjects.users),
                    "roles": sorted(rule.subjects.roles),
                },
                "resource": (
                    {"type": "column", "table": rule.table, "name": rule.column}
                    if rule.resource_type == "column"
                    else {"type": "table", "name": rule.table}
                ),
                "action": rule.action,
                "effect": rule.effect,
            }
            for rule in policy.acl_rules
        ],
        "row_policies": [
            {
                "name": row.name,
                "subjects": {
                    "users": sorted(row.subjects.users),
                    "roles": sorted(row.subjects.roles),
                },
                "tables": sorted(row.tables),
                "actions": sorted(row.actions),
                "predicate": {
                    "column": row.column,
                    "operator": row.operator,
                    **({"value_from": row.value_from} if row.value_from else {}),
                    **({"value": row.value} if row.value is not None else {}),
                },
            }
            for row in policy.row_policies
        ],
    }


def load_permission_policy(path: str | Path | None = None) -> PermissionPolicy:
    policy_path = Path(path) if path else DEFAULT_POLICY_PATH
    data = {}
    if policy_path.exists():
        data = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    return permission_policy_from_dict(data)
