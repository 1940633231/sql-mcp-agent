"""Identity models shared by authentication and authorization."""
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


_ATTRIBUTE_PREFIX = "principal.attributes."


@dataclass(frozen=True)
class Principal:
    """Authenticated caller identity.

    Roles and attributes are resolved from server-side policy, not from tool
    arguments or model-generated content.
    """

    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)
    attributes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def has_role(self, role: str) -> bool:
        return role.lower() in self.roles


def resolve_attribute(principal: Principal, expression: str):
    """Resolve a whitelisted ``principal.attributes.*`` policy expression."""
    if expression.startswith(_ATTRIBUTE_PREFIX):
        key = expression[len(_ATTRIBUTE_PREFIX):]
    elif expression.startswith("principal."):
        key = expression[len("principal."):]
    else:
        raise KeyError(expression)

    value = principal.attributes
    for part in key.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise KeyError(expression)
        value = value[part]
    return value


@dataclass(frozen=True)
class RequestContext:
    """Immutable per-request authorization context."""

    principal: Principal
    request_id: str = ""
    source: str = "local"
    auth_method: str = "local"
