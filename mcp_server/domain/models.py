"""领域查询定义模型（QueryTemplate / QuerySpec / ParameterSpec）。

- QueryTemplate：服务端维护的 SQL 模板 + 参数定义 + 标识符白名单。
- QuerySpec：工具契约（模板 + 结果列 + 领域口径 + 定义版本），由 Registry 按工具名暴露。
- ParameterSpec：单个参数的类型、必填、默认值、取值范围与白名单。

安全边界（V0.5 契约）：
  - 值参数（date/integer/number/enum/string）经类型与范围校验后，以占位符 ? 由
    数据库驱动绑定执行，值文本绝不进入 SQL 文本；
  - 标识符参数（granularity/sort_by/order_dir 等）只能从 identifiers 白名单映射出
    固定 SQL 片段内联，排序/分组/时间粒度等结构位不可被调用方自由注入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ParameterSpec:
    """单个参数的声明式约束。"""

    name: str
    kind: str = "string"                 # date | integer | number | enum | string | identifier
    required: bool = False
    default: Any = None
    allowlist: tuple[str, ...] = ()      # enum 合法取值（大小写敏感）
    min_value: int | None = None         # integer/number 下界
    max_value: int | None = None         # integer/number 上界
    max_length: int = 256                # string 长度上限
    description: str = ""


@dataclass(frozen=True)
class QueryTemplate:
    """SQL 模板：{slot} 槽位取自 parameters（值）或 identifiers（标识符）。"""

    name: str
    description: str
    sql: str
    parameters: tuple[ParameterSpec, ...] = ()
    identifiers: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    max_limit: int = 200
    result_columns: tuple[str, ...] = ()
    semantics: Mapping[str, str] = field(default_factory=dict)

    def parameter(self, name: str) -> ParameterSpec | None:
        for spec in self.parameters:
            if spec.name == name:
                return spec
        return None

    def value_slots(self) -> list[str]:
        """值槽位（按 SQL 出现顺序），即 ? 占位符的绑定顺序。"""
        return [spec.name for spec in self.parameters if spec.kind != "identifier"]

    @property
    def identifier_slots(self) -> set[str]:
        return set(self.identifiers)


@dataclass(frozen=True)
class QuerySpec:
    """一个领域工具的完整契约（模板 + 结果契约 + 定义版本）。"""

    name: str
    template: QueryTemplate
    definition_version: str
