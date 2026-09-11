"""Column lineage for SELECT, CTE, and derived-table authorization."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlglot import exp

from ..security import parser


class SchemaCatalog(Protocol):
    def get_schema(self, table_name: str) -> list[dict]: ...


@dataclass(frozen=True)
class ColumnRef:
    table: str
    column: str


@dataclass(frozen=True)
class OutputLineage:
    name: str
    sources: frozenset[ColumnRef] = frozenset()


@dataclass(frozen=True)
class ScopeLineage:
    outputs: tuple[OutputLineage, ...] = ()
    complete: bool = True
    referenced_sources: frozenset[ColumnRef] = frozenset()

    def outputs_named(self, name: str) -> tuple[OutputLineage, ...]:
        lowered = name.lower()
        return tuple(output for output in self.outputs if output.name.lower() == lowered)


class LineageResolver:
    """Resolve physical source columns behind each SELECT output."""

    def __init__(self, tree: exp.Expression, catalog: SchemaCatalog | None = None):
        self.tree = tree
        self.catalog = catalog
        self._selects: dict[int, ScopeLineage] = {}
        self._cte_env: dict[str, ScopeLineage] = {}
        self._resolving: set[int] = set()

    def resolve(self) -> dict[int, ScopeLineage]:
        for cte in self.tree.find_all(exp.CTE):
            name = cte.alias_or_name.lower()
            self._cte_env[name] = self._resolve_select(cte.this)
        for select in self.tree.find_all(exp.Select):
            self._resolve_select(select)
        return self._selects

    def output_columns(self) -> tuple[str, ...]:
        scopes = self.resolve()
        if isinstance(self.tree, exp.Select):
            scope = scopes.get(id(self.tree)) or self._resolve_select(self.tree)
            return tuple(output.name for output in scope.outputs if output.name)
        first = self.tree.find(exp.Select)
        if first is None:
            return ()
        scope = scopes.get(id(first)) or self._resolve_select(first)
        return tuple(output.name for output in scope.outputs if output.name)

    def source_columns(self) -> set[ColumnRef]:
        result: set[ColumnRef] = set()
        for scope in self.resolve().values():
            result.update(scope.referenced_sources)
        return result

    def referenced_columns(self) -> set[ColumnRef]:
        result: set[ColumnRef] = set()
        for scope in self.resolve().values():
            for output in scope.outputs:
                result.update(output.sources)
        return result

    def source_scopes(
        self, select: exp.Select
    ) -> tuple[dict[str, ScopeLineage], dict[str, str]]:
        return self._resolve_sources(select)

    def _resolve_select(self, select: exp.Select) -> ScopeLineage:
        key = id(select)
        if key in self._selects:
            return self._selects[key]
        if key in self._resolving:
            return ScopeLineage(complete=False)
        self._resolving.add(key)
        try:
            source_scopes, physical_tables = self._resolve_sources(select)
            outputs: list[OutputLineage] = []
            referenced_sources: set[ColumnRef] = set()
            complete = all(scope.complete for scope in source_scopes.values())
            for projection in select.expressions:
                target = _star_target(projection)
                if target is not None:
                    if target:
                        scope = source_scopes.get(target)
                        if scope is None:
                            complete = False
                            continue
                        outputs.extend(scope.outputs)
                        complete = complete and scope.complete
                    else:
                        for scope in source_scopes.values():
                            outputs.extend(scope.outputs)
                            complete = complete and scope.complete
                    continue

                sources, projection_complete = _projection_sources(
                    projection, source_scopes, physical_tables
                )
                referenced_sources.update(sources)
                if projection.args.get("alias"):
                    output_name = projection.alias
                elif projection.alias_or_name and projection.alias_or_name != "*":
                    output_name = projection.alias_or_name
                elif isinstance(projection, exp.Column):
                    output_name = projection.name
                else:
                    output_name = projection.sql(dialect=parser.DIALECT)
                outputs.append(OutputLineage(output_name, frozenset(sources)))
                complete = complete and projection_complete

            scope = ScopeLineage(tuple(outputs), complete, frozenset(referenced_sources))
            self._selects[key] = scope
            return scope
        finally:
            self._resolving.discard(key)

    def _resolve_sources(
        self, select: exp.Select
    ) -> tuple[dict[str, ScopeLineage], dict[str, str]]:
        scopes: dict[str, ScopeLineage] = {}
        physical: dict[str, str] = {}
        candidates = []
        from_ = select.args.get("from_")
        if from_ is not None:
            candidates.append(from_.this)
        for join in select.args.get("joins") or []:
            candidates.append(join.this)

        for candidate in candidates:
            if isinstance(candidate, exp.Table):
                alias = candidate.alias_or_name
                name = parser._table_ref(candidate).lower()
                table = name.rsplit(".", 1)[-1]
                physical[alias] = table
                cte_scope = self._cte_env.get(candidate.name.lower())
                if cte_scope is not None:
                    scopes[alias] = cte_scope
                elif self.catalog is not None:
                    columns = _catalog_columns(self.catalog, table)
                    scopes[alias] = ScopeLineage(tuple(
                        OutputLineage(column, frozenset({ColumnRef(table, column)}))
                        for column in columns
                    ))
                else:
                    scopes[alias] = ScopeLineage(complete=False)
            elif isinstance(candidate, exp.Subquery):
                alias = candidate.alias_or_name
                if alias:
                    scopes[alias] = self._resolve_select(candidate.this)
        return scopes, physical


def _projection_sources(
    projection: exp.Expression,
    source_scopes: dict[str, ScopeLineage],
    physical_tables: dict[str, str],
) -> tuple[set[ColumnRef], bool]:
    sources: set[ColumnRef] = set()
    complete = True
    columns = list(_columns_in(projection))
    if not columns:
        return sources, True

    for column in columns:
        resolved, column_complete = _resolve_column(
            column, source_scopes, physical_tables
        )
        sources.update(resolved)
        complete = complete and column_complete
    return sources, complete


def _resolve_column(
    column: exp.Column,
    source_scopes: dict[str, ScopeLineage],
    physical_tables: dict[str, str],
) -> tuple[set[ColumnRef], bool]:
    name = column.name.lower()
    qualifier = column.table.lower() if column.table else ""
    if qualifier:
        scope = source_scopes.get(qualifier)
        if scope is not None:
            matches = scope.outputs_named(name)
            if matches:
                return set().union(*(match.sources for match in matches)), scope.complete
        table = physical_tables.get(qualifier)
        if table:
            return {ColumnRef(table, name)}, True
        return set(), False

    matches: list[OutputLineage] = []
    for scope in source_scopes.values():
        matches.extend(scope.outputs_named(name))
    if matches:
        return set().union(*(match.sources for match in matches)), all(
            scope.complete for scope in source_scopes.values()
        )
    if len(physical_tables) == 1:
        table = next(iter(physical_tables.values()))
        return {ColumnRef(table, name)}, True
    return set(), False


def _columns_in(expression: exp.Expression):
    if isinstance(expression, exp.Column) and not isinstance(expression.this, exp.Star):
        yield expression
    for node in expression.walk():
        if node is expression:
            continue
        if isinstance(node, exp.Column) and not isinstance(node.this, exp.Star):
            yield node


def _star_target(projection: exp.Expression) -> str | None:
    if isinstance(projection, exp.Star):
        return ""
    if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
        return projection.table
    return None


def _catalog_columns(catalog: SchemaCatalog, table: str) -> list[str]:
    schema = catalog.get_schema(table)
    return [
        str(row.get("Field") or row.get("field") or "")
        for row in schema
        if row.get("Field") or row.get("field")
    ]
