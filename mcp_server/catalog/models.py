"""SchemaCatalog 的强类型数据模型。"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnSchema:
    name: str
    data_type: str = ""
    nullable: bool = True
    default: str | None = None
    comment: str = ""
    # V0.4 Schema Intelligence：业务语义字段（暗含默认值，可省略）
    label: str = ""
    description: str = ""
    synonyms: tuple[str, ...] = ()
    enum_values: tuple[dict, ...] = ()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.data_type,
            "nullable": self.nullable,
            "default": self.default,
            "comment": self.comment,
            "label": self.label,
            "description": self.description,
            "synonyms": list(self.synonyms),
            "enum_values": [dict(v) for v in self.enum_values],
        }


@dataclass(frozen=True)
class TableSchema:
    table: str
    columns: tuple[ColumnSchema, ...] = ()
    primary_keys: tuple[str, ...] = ()
    foreign_keys: tuple[dict, ...] = ()
    indexes: tuple[dict, ...] = ()
    # V0.4 Schema Intelligence：表级业务语义
    label: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "label": self.label,
            "description": self.description,
            "primary_keys": list(self.primary_keys),
            "foreign_keys": [dict(fk) for fk in self.foreign_keys],
            "indexes": [dict(i) for i in self.indexes],
            "columns": [column.to_dict() for column in self.columns],
        }

    def to_legacy_rows(self) -> list[dict]:
        return [
            {
                "Field": column.name,
                "Type": column.data_type,
                "Null": "YES" if column.nullable else "NO",
                "Key": "PRI" if column.name in self.primary_keys else "",
                "Default": column.default,
                "Comment": column.comment,
                "Label": column.label,
                "Description": column.description,
                "Synonyms": list(column.synonyms),
            }
            for column in self.columns
        ]
