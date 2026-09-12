"""V0.5 Domain Query Layer：领域查询定义、参数绑定与统一 DTO。

安全边界：领域工具不直接访问数据库；所有查询复用 QueryService 的完整安全链路
（AST Guard / RBAC / ACL / RLS / LIMIT / 审计 / 指标），值与标识符均不能绕过白名单。
"""
from .models import ParameterSpec, QuerySpec, QueryTemplate
from .registry import DomainRegistry, get_domain_registry

__all__ = [
    "DomainRegistry",
    "ParameterSpec",
    "QuerySpec",
    "QueryTemplate",
    "get_domain_registry",
]
