"""领域查询编排：复用 QueryService 的完整安全链路，返回统一 DTO。

领域工具本身不直接访问数据库；本服务只负责：
  1. 从 Registry 取 QuerySpec（含 SQL 模板 / 参数定义 / 结果契约 / 领域口径）
  2. 参数绑定（binder.render：标识符白名单内联、值经校验后转 ? 占位符）
  3. 委托 QueryService 执行（AST Guard / RBAC / ACL / RLS / LIMIT / 审计 / 指标）
  4. 包装统一 DTO：data / columns / meta / definition_version / policy_version / truncated

错误收敛：未知工具 / 非法参数 / 安全拒绝 / 数据库错误都返回与 run_query 一致的
{"error": ..., "code": ...} 结构，保证 Agent 可读、可恢复。
"""
from __future__ import annotations

from typing import Any, Mapping

from ..auth.context import current_request_context
from ..auth.models import RequestContext
from ..authorization.manager import get_policy_manager
from ..services.query_service import QueryService
from .binder import InvalidParameter, render
from .registry import DomainRegistry, get_domain_registry
from .models import QuerySpec


class DomainQueryService:
    """领域查询的统一入口：run(tool_name, args) -> DTO / 错误结构。"""

    def __init__(
        self,
        registry: DomainRegistry | None = None,
        query_service: QueryService | None = None,
    ):
        self.registry = registry or get_domain_registry()
        self.query_service = query_service or QueryService()

    def run(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None = None,
        context: RequestContext | None = None,
    ) -> dict:
        try:
            spec = self.registry.get(tool_name)
        except KeyError:
            return {"error": "未知领域工具：%s" % tool_name, "code": "unknown_domain_tool"}

        try:
            sql, params, effective = render(spec.template, args or {})
        except InvalidParameter as exc:
            return {"error": str(exc), "code": exc.code}

        policy_context = get_policy_manager().get_context()
        if context is None:
            context = current_request_context(source="domain", policy_context=policy_context)
        result = self.query_service.run(sql, context=context, params=params)
        return self._wrap(spec, effective, result, policy_context)

    def _wrap(
        self,
        spec: QuerySpec,
        effective: Mapping[str, Any],
        result: dict,
        policy_context,
    ) -> dict:
        if "error" in result:
            # 与 run_query 一致：安全/执行错误原样透传，不套 DTO
            return result

        actual_columns = result.get("columns", [])
        contract_columns = list(spec.template.result_columns)
        if contract_columns and list(actual_columns) != contract_columns:
            # 结果契约：实际返回列必须与声明完全一致（顺序敏感），
            # 不一致说明模板 SQL 与结果契约不同步，属于定义缺陷，fail loud。
            return {
                "error": "结果契约不匹配：期望列 %s，实际返回 %s"
                % (contract_columns, actual_columns),
                "code": "result_contract_mismatch",
            }

        return {
            "data": result.get("rows", []),
            "columns": result.get("columns", []),
            "meta": {
                "tool": spec.name,
                "description": spec.template.description,
                "parameters": {str(k): v for k, v in effective.items()},
                "semantics": dict(spec.template.semantics),
                "row_count": result.get("row_count", 0),
                "elapsed_seconds": result.get("elapsed_seconds", 0.0),
                "result_bytes": result.get("result_bytes", 0),
                "truncated_reason": result.get("truncated_reason", ""),
            },
            "definition_version": spec.definition_version,
            "policy_version": policy_context.version,
            "truncated": result.get("truncated", False),
        }


def get_domain_query_service() -> DomainQueryService:
    return DomainQueryService()
