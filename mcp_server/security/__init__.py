"""SQL 只读安全防线。

分工：
    - models     数据模型（策略、解析结果、校验结果、查询结果）
    - parser     基于 sqlglot 的 AST 解析（语句拆分 / 类型识别 / 表列函数抽取）
    - policy     安全策略的加载与规则匹配（关键字 / 正则 / 系统库 / 标识符）
    - validator  校验流水线：parser + policy -> ValidationResult
"""
