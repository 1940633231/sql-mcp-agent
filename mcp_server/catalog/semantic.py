"""Schema Intelligence：业务描述与语义元数据加载 + 轻量别名检索。

纯内存：元数据来自外置 YAML（默认 <项目根>/configs/schema_desc.yaml，
可用 SCHEMA_DESC_PATH 覆盖），不依赖数据库、不新增 SQL 面，贴合全局只读架构。

- load_semantics(): YAML + 兜底默认值的加载模式（仿 security/policy.py）。
- search_schema(): 归一化（去 _/空格、转小写）后的子串匹配，返回结构化命中。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .. import config

# 默认元数据文件：<项目根>/configs/schema_desc.yaml
DEFAULT_SCHEMA_DESC_PATH = Path(__file__).resolve().parents[2] / "configs" / "schema_desc.yaml"


@dataclass(frozen=True)
class ColumnDesc:
    label: str = ""
    description: str = ""
    synonyms: tuple[str, ...] = ()
    enum_values: tuple[dict, ...] = ()


@dataclass(frozen=True)
class TableDesc:
    label: str = ""
    description: str = ""


@dataclass(frozen=True)
class SemanticMetadata:
    """已归一化的语义元数据（表/列名均小写存储）。"""

    tables: dict[str, TableDesc] = field(default_factory=dict)
    columns: dict[str, dict[str, ColumnDesc]] = field(default_factory=dict)


@dataclass(frozen=True)
class SemanticHit:
    table: str
    column: str | None  # None 表示表级命中
    kind: str  # "table" | "column"
    matched_on: str
    label: str
    description: str

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "column": self.column if self.column is not None else "",
            "kind": self.kind,
            "matched_on": self.matched_on,
            "label": self.label,
            "description": self.description,
        }


def load_semantics(path: str | Path | None = None) -> SemanticMetadata:
    """读取 YAML 语义元数据；文件缺失、畸形、缺字段时均安全回退空元数据，绝不抛错。"""
    if path is None:
        path = config.SCHEMA_DESC_PATH or DEFAULT_SCHEMA_DESC_PATH
    p = Path(path)
    data = {}
    if p.exists():
        try:
            loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            loaded = None
        if isinstance(loaded, dict):
            data = loaded

    raw_tables = data.get("tables")
    raw_columns = data.get("columns")
    raw_tables = raw_tables if isinstance(raw_tables, dict) else {}
    raw_columns = raw_columns if isinstance(raw_columns, dict) else {}

    tables: dict[str, TableDesc] = {}
    for name, t in raw_tables.items():
        t = t if isinstance(t, dict) else {}
        tables[str(name).lower()] = TableDesc(
            label=str(t.get("label") or ""),
            description=str(t.get("description") or ""),
        )

    columns: dict[str, dict[str, ColumnDesc]] = {}
    for table, cols in raw_columns.items():
        table = str(table).lower()
        if not isinstance(cols, dict):
            continue
        col_map = columns.setdefault(table, {})
        for col, c in cols.items():
            c = c if isinstance(c, dict) else {}
            syns = c.get("synonyms") or []
            synonyms = (
                tuple(str(s) for s in syns)
                if isinstance(syns, (list, tuple))
                else ()
            )
            evs = c.get("enum_values") or []
            enum_values = ()
            if isinstance(evs, (list, tuple)):
                enum_values = tuple(dict(v) for v in evs if isinstance(v, dict))
            col_map[str(col).lower()] = ColumnDesc(
                label=str(c.get("label") or ""),
                description=str(c.get("description") or ""),
                synonyms=synonyms,
                enum_values=enum_values,
            )
    return SemanticMetadata(tables=tables, columns=columns)


def _norm(value: str) -> str:
    """归一化：去 `_`/空白并转小写，用于匹配比较。"""
    return re.sub(r"[\s_]+", "", (value or "")).lower()


def normalize(value: str) -> str:
    """公开归一化入口，供检索与后续叠加物理名匹配复用。"""
    return _norm(value)


def _rank_match(needle: str, key: str, candidates: list[tuple[str, int]]) -> int | None:
    """按优先级返回命中最高的分档（rank 越小越优先）。

    分档：
      1 精确名（归一化后与 keyword 完全相等）
      2 label / 别名命中
      3 description / synonyms 命中的子串
    返回命中档位；无命中返回 None。
    """
    best: int | None = None
    for candidate, rank in candidates:
        if not needle:
            continue
        norm_candidate = _norm(candidate)
        if needle == key and norm_candidate == needle:
            return 1
        # 分档：完全相等视为精确名，否则按传入 rank 档位子串匹配
        hit = needle in norm_candidate
        if hit:
            current = 1 if norm_candidate == needle else rank
            if best is None or current < best:
                best = current
    return best


def _rank_table(needle: str, key: str, table: str, desc: TableDesc) -> tuple[int | None, str]:
    """表级命中判定，返回 (档位, matched_on)。"""
    # 候选按 (文本, 档位)：name=1(精确由函数处理), label=2, description=3
    candidates = [
        (table, 2),
        (desc.label, 2),
        (desc.description, 3),
    ]
    rank = _rank_match(needle, key, candidates)
    if rank is None:
        return rank, ""
    # 反查具体命中来源，用于 matched_on 展示
    for text, _ in candidates:
        if text and needle in _norm(text) or (text and _norm(text) == needle):
            return rank, text
    return rank, table


def _rank_column(needle: str, key: str, col: str, desc: ColumnDesc) -> tuple[int | None, str]:
    """列级命中判定，返回 (档位, matched_on)。"""
    candidates = [
        (col, 2),
        (desc.label, 2),
    ]
    for syn in desc.synonyms:
        candidates.append((syn, 3))
    if desc.description:
        candidates.append((desc.description, 3))
    rank = _rank_match(needle, key, candidates)
    if rank is None:
        return rank, ""
    for text, _ in candidates:
        if text and needle in _norm(text) or (text and _norm(text) == needle):
            return rank, text
    return rank, col


def search_schema(metadata: SemanticMetadata, keyword: str) -> list[SemanticHit]:
    """轻量别名检索。

    覆盖范围：
      - 表：name / label / description
      - 列：name / label / description / synonyms
    优先级分档：精确名 > label 命中 > description / synonyms 命中；
    同档内按 表名 -> 列名 字典序，保证输出稳定可复现。
    """
    key = (keyword or "").strip()
    if not key:
        return []
    needle = _norm(key)

    ranked_hits: list[tuple[int, SemanticHit]] = []

    for table, desc in metadata.tables.items():
        rank, matched = _rank_table(needle, key, table, desc)
        if rank is not None:
            ranked_hits.append(
                (
                    rank,
                    SemanticHit(
                        table=table, column=None, kind="table", matched_on=matched,
                        label=desc.label, description=desc.description,
                    ),
                )
            )

    for table, cols in metadata.columns.items():
        for col, desc in cols.items():
            rank, matched = _rank_column(needle, key, col, desc)
            if rank is not None:
                ranked_hits.append(
                    (
                        rank,
                        SemanticHit(
                            table=table, column=col, kind="column", matched_on=matched,
                            label=desc.label, description=desc.description,
                        ),
                    )
                )

    ranked_hits.sort(key=lambda item: (item[0], item[1].table, item[1].column or ""))
    return [hit for _, hit in ranked_hits]