"""认证与授权共用的身份模型。"""
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


_ATTRIBUTE_PREFIX = "principal.attributes."


@dataclass(frozen=True)
class Principal:
    """已认证的调用者身份。

    角色与属性只来自服务端策略，不信任工具参数或模型生成内容。
    """

    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)
    attributes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def has_role(self, role: str) -> bool:
        return role.lower() in self.roles


def resolve_attribute(principal: Principal, expression: str):
    """解析白名单形式的 ``principal.attributes.*`` 策略表达式。"""
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
    """请求级不可变授权上下文。"""

    principal: Principal
    request_id: str = ""
    trace_id: str = ""
    session_id: str = ""
    client_id: str = ""
    tool_name: str = ""
    source: str = "local"
    auth_method: str = "local"
    policy_version: str = ""
    policy_hash: str = ""
    policy_source: str = ""
