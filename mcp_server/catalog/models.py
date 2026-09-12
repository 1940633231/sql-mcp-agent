"""SchemaCatalog 的强类型数据模型。"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnSchema:
    name: str
    data_type: str = ""
    nullable: bool = True
    default: str | None = None
    comment: str = ""


@dataclass(frozen=True)
class TableSchema:
    table: str
    columns: tuple[ColumnSchema, ...] = ()
    primary_keys: tuple[str, ...] = ()
    foreign_keys: tuple[dict, ...] = ()
    indexes: tuple[dict, ...] = ()

    def to_legacy_rows(self) -> list[dict]:
        return [
            {
                "Field": column.name,
                "Type": column.data_type,
                "Null": "YES" if column.nullable else "NO",
                "Key": "PRI" if column.name in self.primary_keys else "",
                "Default": column.default,
                "Comment": column.comment,
            }
            for column in self.columns
        ]
