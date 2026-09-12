"""授权决策点与 SQL 策略改写器。"""
import hashlib
from typing import Protocol

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from ..auth.models import Principal, RequestContext, resolve_attribute
from ..observability.fingerprint import raw_sql_hash, sql_fingerprint
from ..observability.metrics import metrics
from ..security import parser
from .audit import log_event
from .lineage import LineageResolver
from .models import AclRule, AuthorizationResult, PermissionPolicy, RowPolicy



class ColumnPermissionDenied(PermissionError):
    """完成列血缘解析后，列级授权检查失败。"""


class SchemaCatalog(Protocol):
    def get_schema(self, table_name: str) -> list[dict]: ...


class AuthorizationService:
    """执行 RBAC/ACL 判定，并为 RLS 和 ``*`` 列可见性改写 SQL。"""

    def __init__(self, policy: PermissionPolicy | None = None):
        if policy is None:
            from .manager import get_permission_policy

            policy = get_permission_policy()
        self.policy = policy

    def for_policy(self, policy: PermissionPolicy) -> "AuthorizationService":
        if policy is self.policy:
            return self
        return AuthorizationService(policy)

    def has_permission(self, principal: Principal, permission: str) -> bool:
        permissions = self.policy.permissions_for_roles(principal.roles)
        normalized = permission.lower()
        if normalized.startswith("policy:") or normalized == "rls:bypass":
            return normalized in permissions
        return "*" in permissions or normalized in permissions

    def authorize_table(self, principal: Principal, table: str, action: str = "select") -> bool:
        table = _bare(table)
        action = action.lower()
        rules = self._applicable_table_rules(principal, table, action)
        if any(rule.effect == "deny" for rule in rules):
            return False
        return any(rule.effect == "allow" for rule in rules)

    def column_policy_defined(self, principal: Principal, table: str, action: str = "select") -> bool:
        table = _bare(table)
        return bool(self._applicable_column_rules(principal, table, action))

    def authorize_column(
        self,
        principal: Principal,
        table: str,
        column: str,
        action: str = "select",
    ) -> bool:
        table = _bare(table)
        column = column.lower()
        if not self.authorize_table(principal, table, action):
            return False
        rules = self._applicable_column_rules(principal, table, action)
        if not rules:
            return True
        if any(rule.effect == "deny" and _matches(rule.column, column) for rule in rules):
            return False
        allow_rules = [rule for rule in rules if rule.effect == "allow"]
        if allow_rules:
            return any(_matches(rule.column, column) for rule in allow_rules)
        return True

    def visible_columns(
        self,
        principal: Principal,
        table: str,
        columns: list[str],
        action: str = "select",
    ) -> list[str]:
        return [
            column for column in columns
            if self.authorize_column(principal, table, column, action)
        ]

    def authorize_sql(
        self,
        sql: str,
        context: RequestContext,
        catalog: SchemaCatalog | None = None,
    ) -> AuthorizationResult:
        try:
            tree = parse_one(sql, read=parser.DIALECT)
        except ParseError:
            return AuthorizationResult.reject("无法解析授权 SQL", "authorization_parse_error")

        if not self.has_permission(context.principal, "query:run"):
            result = AuthorizationResult.reject("无权执行查询", "permission_denied")
            self._audit(context, sql, result)
            return result

        tables = [_bare(table) for table in parser.extract_tables(sql)]
        for table in tables:
            if not self.authorize_table(context.principal, table):
                result = AuthorizationResult.reject(
                    "无权访问表：%s" % table,
                    "table_permission_denied",
                    tables=tables,
                )
                self._audit(context, sql, result)
                return result

        for table, column in parser.extract_column_refs(sql):
            if table.lower() in _derived_aliases(tree):
                continue
            if not self.authorize_column(context.principal, table, column):
                result = AuthorizationResult.reject(
                    "无权访问列：%s.%s" % (table, column),
                    "column_permission_denied",
                    tables=tables,
                )
                self._audit(context, sql, result)
                return result

        warnings: list[str] = []
        try:
            self._authorize_lineage(tree, context.principal, catalog, tables)
            self._expand_stars(tree, context.principal, catalog, warnings)
            policy_ids = self._apply_row_policies(tree, context.principal)
        except ColumnPermissionDenied as exc:
            result = AuthorizationResult.reject(str(exc), "column_permission_denied", tables=tables)
            self._audit(context, sql, result)
            return result
        except PermissionError as exc:
            result = AuthorizationResult.reject(str(exc), "row_policy_denied", tables=tables)
            self._audit(context, sql, result)
            return result
        except ValueError as exc:
            result = AuthorizationResult.reject(str(exc), "authorization_rewrite_error", tables=tables)
            self._audit(context, sql, result)
            return result

        rewritten = tree.sql(dialect=parser.DIALECT)
        result = AuthorizationResult.approve(
            rewritten,
            tables=tables,
            policy_ids=policy_ids,
            warnings=warnings,
            result_columns=_result_column_names(tree),
        )
        self._audit(context, sql, result)
        return result

    def _applicable_table_rules(
        self,
        principal: Principal,
        table: str,
        action: str,
    ) -> list[AclRule]:
        return [
            rule for rule in self.policy.acl_rules
            if rule.resource_type == "table"
            and rule.action == action
            and rule.subjects.matches(principal)
            and _matches(rule.table, table)
        ]

    def _applicable_column_rules(
        self,
        principal: Principal,
        table: str,
        action: str,
    ) -> list[AclRule]:
        return [
            rule for rule in self.policy.acl_rules
            if rule.resource_type == "column"
            and rule.action == action
            and rule.subjects.matches(principal)
            and _matches(rule.table, table)
        ]

    def _authorize_lineage(
        self,
        tree,
        principal: Principal,
        catalog: SchemaCatalog | None,
        tables: list[str],
    ) -> None:
        restricted = any(self.column_policy_defined(principal, table) for table in tables)
        complex_source = bool(tree.find(exp.CTE) or tree.find(exp.Subquery))
        if not restricted or (catalog is None and not complex_source):
            return
        resolver = LineageResolver(tree, catalog)
        scopes = resolver.resolve()
        if not all(scope.complete for scope in scopes.values()):
            raise ValueError("无法完整解析 CTE/派生表列血缘")
        for ref in resolver.source_columns():
            if not self.authorize_column(principal, ref.table, ref.column):
                raise ColumnPermissionDenied("无权访问列：%s.%s" % (ref.table, ref.column))

    def _expand_stars(
        self,
        tree,
        principal: Principal,
        catalog: SchemaCatalog | None,
        warnings: list[str],
    ) -> None:
        if not any(
            self.column_policy_defined(principal, _bare(table.name))
            for table in tree.find_all(exp.Table)
        ):
            return
        resolver = LineageResolver(tree, catalog)
        resolver.resolve()
        for select in tree.find_all(exp.Select):
            source_scopes, physical_tables = resolver.source_scopes(select)
            restricted = any(
                self.column_policy_defined(principal, table)
                for table in physical_tables.values()
            ) or any(
                self.column_policy_defined(principal, ref.table)
                for scope in source_scopes.values()
                for output in scope.outputs
                for ref in output.sources
            )
            if not restricted:
                continue
            if any(not scope.complete for scope in source_scopes.values()):
                raise ValueError("无法完整解析 SELECT * 的列血缘")

            projections: list[exp.Expression] = []
            for projection in select.expressions:
                target = _star_target(projection)
                if target is None:
                    projections.append(projection)
                    continue
                if target:
                    scope = source_scopes.get(target)
                    if scope is None:
                        raise ValueError("无法解析列权限所需的表别名：%s" % target)
                    projections.extend(_visible_scope_columns(scope, target, principal, self))
                    continue
                if not source_scopes:
                    raise ValueError("无法解析 SELECT * 的来源表")
                for alias, scope in source_scopes.items():
                    projections.extend(_visible_scope_columns(scope, alias, principal, self))
            if not projections:
                raise PermissionError("查询没有可访问列")
            select.set("expressions", projections)
            warnings.append("SELECT * / 派生列已按列权限展开")

    def _apply_row_policies(self, tree, principal: Principal) -> list[str]:
        applied: list[str] = []
        bypass = self.policy.bypass_rls_for(principal)
        cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        for table_node in list(tree.find_all(exp.Table)):
            table = _bare(table_node.name)
            if table_node.name.lower() in cte_names:
                continue
            protected = any(
                policy.applies_to_resource(table, "select")
                for policy in self.policy.row_policies
            )
            applicable = [
                policy for policy in self.policy.row_policies
                if policy.applies_to_resource(table, "select") and policy.subjects.matches(principal)
            ]
            if protected and not applicable and not bypass:
                raise PermissionError("表 %s 没有适用的行级策略" % table)
            if bypass:
                continue
            predicates = [
                self._row_predicate(policy, principal, table_node.alias_or_name)
                for policy in applicable
            ]
            if not predicates:
                continue
            filtered = exp.select("*").from_(table_node.copy())
            filtered.where(
                predicates[0] if len(predicates) == 1 else exp.and_(*predicates),
                copy=False,
            )
            table_node.replace(
                exp.Subquery(
                    this=filtered,
                    alias=exp.TableAlias(this=exp.to_identifier(table_node.alias_or_name)),
                )
            )
            applied.extend(policy.name for policy in applicable)
        return applied

    def _row_predicate(self, policy: RowPolicy, principal: Principal, qualifier: str):
        if not policy.column or not parser.is_valid_identifier(policy.column, r"^[A-Za-z0-9_]+$"):
            raise ValueError("非法行级策略列：%s" % policy.column)
        try:
            value = resolve_attribute(principal, policy.value_from) if policy.value_from else policy.value
        except KeyError:
            raise PermissionError("行级策略缺少主体属性：%s" % policy.value_from)

        column = exp.column(policy.column, table=qualifier)
        if policy.operator == "eq":
            return exp.EQ(this=column, expression=_literal(value))
        if policy.operator == "ne":
            return exp.NEQ(this=column, expression=_literal(value))
        if policy.operator == "lt":
            return exp.LT(this=column, expression=_literal(value))
        if policy.operator == "lte":
            return exp.LTE(this=column, expression=_literal(value))
        if policy.operator == "gt":
            return exp.GT(this=column, expression=_literal(value))
        if policy.operator == "gte":
            return exp.GTE(this=column, expression=_literal(value))
        if policy.operator == "in":
            if not isinstance(value, (list, tuple, set, frozenset)):
                raise PermissionError("IN 行级策略值必须是数组：%s" % policy.value_from)
            return exp.In(this=column, expressions=[_literal(item) for item in value])
        raise ValueError("不支持的行级策略操作符：%s" % policy.operator)

    @staticmethod
    def _audit(context: RequestContext, sql: str, result: AuthorizationResult) -> None:
        metrics.inc(
            "authz_decisions_total",
            {"decision": "allow" if result.allowed else "deny", "code": result.code or "ok"},
        )
        log_event(
            "authorization_decision",
            decision="allow" if result.allowed else "deny",
            principal=context.principal.subject,
            roles=sorted(context.principal.roles),
            auth_method=context.auth_method,
            source=context.source,
            request_id=context.request_id,
            trace_id=context.trace_id,
            session_id=context.session_id,
            client_id=context.client_id,
            tool_name=context.tool_name,
            tables=list(result.tables),
            policies=list(result.policy_ids),
            policy_version=context.policy_version,
            policy_hash=context.policy_hash,
            sql_fingerprint=sql_fingerprint(sql),
            raw_sql_hash=raw_sql_hash(sql),
            result_columns=list(result.result_columns),
            warnings=list(result.warnings),
            code=result.code,
            reason=result.reason,
            sql_sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        )


def _derived_aliases(tree) -> set[str]:
    aliases = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    aliases.update(
        subquery.alias_or_name.lower()
        for subquery in tree.find_all(exp.Subquery)
        if subquery.alias_or_name
    )
    return aliases


def _star_target(projection: exp.Expression) -> str | None:
    """``*`` 返回空字符串，``t.*`` 返回别名，普通投影返回 ``None``。"""
    if isinstance(projection, exp.Star):
        return ""
    if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
        return projection.table
    return None


def _visible_scope_columns(
    scope,
    alias: str,
    principal: Principal,
    authz: AuthorizationService,
) -> list[exp.Expression]:
    projections: list[exp.Expression] = []
    for output in scope.outputs:
        if output.sources and not all(
            authz.authorize_column(principal, ref.table, ref.column)
            for ref in output.sources
        ):
            continue
        if output.name:
            projections.append(exp.column(output.name, table=alias))
    if not projections:
        raise ColumnPermissionDenied("来源 %s 没有可访问列" % alias)
    return projections


def _result_column_names(tree) -> tuple[str, ...]:
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select is None:
        return ()
    names: list[str] = []
    for projection in select.expressions:
        if _star_target(projection) is not None:
            return ()
        candidates: list[str] = []
        if projection.args.get("alias"):
            candidates.append(projection.alias)
        alias_or_name = projection.alias_or_name
        if alias_or_name and alias_or_name != "*":
            candidates.append(alias_or_name)
        if isinstance(projection, exp.Column):
            candidates.append(projection.name)
        candidates.append(projection.sql(dialect=parser.DIALECT))
        for name in candidates:
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _literal(value):
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, int):
        return exp.Literal.number(value)
    if isinstance(value, float):
        return exp.Literal.number(value)
    if value is None:
        return exp.Null()
    return exp.Literal.string(str(value))


def _bare(value: str) -> str:
    return (value or "").strip("`\"[]").rsplit(".", 1)[-1].lower()


def _matches(pattern: str, value: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return pattern == value
