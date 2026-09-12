"""授权策略与决策模型。"""
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from ..auth.models import Principal


@dataclass(frozen=True)
class RolePolicy:
    name: str
    permissions: frozenset[str]
    bypass_rls: bool = False


@dataclass(frozen=True)
class PrincipalPolicy:
    subject: str
    roles: frozenset[str]
    attributes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class PolicySubjects:
    users: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)

    def matches(self, principal: Principal) -> bool:
        user_match = bool(self.users and principal.subject.lower() in self.users)
        role_match = bool(self.roles and (self.roles & principal.roles))
        return user_match or role_match


@dataclass(frozen=True)
class AclRule:
    subjects: PolicySubjects
    resource_type: str
    table: str
    action: str
    effect: str
    column: str = ""

    @property
    def policy_id(self) -> str:
        target = self.table if not self.column else "%s.%s" % (self.table, self.column)
        return "acl:%s:%s:%s" % (self.resource_type, target, self.effect)


@dataclass(frozen=True)
class RowPolicy:
    name: str
    subjects: PolicySubjects
    tables: frozenset[str]
    actions: frozenset[str]
    column: str
    operator: str
    value_from: str = ""
    value: Any = None

    def applies_to_resource(self, table: str, action: str) -> bool:
        return action in self.actions and _matches_any(self.tables, table)


@dataclass
class PermissionPolicy:
    roles: dict[str, RolePolicy]
    principals: dict[str, PrincipalPolicy]
    acl_rules: list[AclRule]
    row_policies: list[RowPolicy]
    default_principal: str = "local-dev"

    def permissions_for_roles(self, roles: frozenset[str]) -> frozenset[str]:
        permissions: set[str] = set()
        for role in roles:
            role_policy = self.roles.get(role)
            if role_policy:
                permissions.update(role_policy.permissions)
        return frozenset(permissions)

    def bypass_rls_for(self, principal: Principal) -> bool:
        return any(
            self.roles.get(role) and self.roles[role].bypass_rls
            for role in principal.roles
        )


@dataclass(frozen=True)
class AuthorizationResult:
    allowed: bool
    sql: str = ""
    reason: str | None = None
    code: str = ""
    tables: tuple[str, ...] = ()
    policy_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    result_columns: tuple[str, ...] = ()

    @classmethod
    def approve(
        cls,
        sql: str,
        tables: list[str] | tuple[str, ...] = (),
        policy_ids: list[str] | tuple[str, ...] = (),
        warnings: list[str] | tuple[str, ...] = (),
        result_columns: list[str] | tuple[str, ...] = (),
    ) -> "AuthorizationResult":
        return cls(
            allowed=True,
            sql=sql,
            tables=tuple(tables),
            policy_ids=tuple(policy_ids),
            warnings=tuple(warnings),
            result_columns=tuple(result_columns),
        )

    @classmethod
    def reject(
        cls,
        reason: str,
        code: str,
        tables: list[str] | tuple[str, ...] = (),
        policy_ids: list[str] | tuple[str, ...] = (),
    ) -> "AuthorizationResult":
        return cls(
            allowed=False,
            reason=reason,
            code=code,
            tables=tuple(tables),
            policy_ids=tuple(policy_ids),
        )


def _matches(pattern: str, value: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return pattern == value


def _matches_any(patterns: frozenset[str], value: str) -> bool:
    return any(_matches(pattern, value) for pattern in patterns)
