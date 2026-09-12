"""SchemaCatalog 对外公开 API。"""
from .models import ColumnSchema, TableSchema
from .semantic import (
    ColumnDesc,
    SemanticHit,
    SemanticMetadata,
    TableDesc,
    load_semantics,
    normalize,
    search_schema,
)
from .service import CatalogStats, SchemaCatalog

__all__ = [
    "CatalogStats",
    "ColumnDesc",
    "ColumnSchema",
    "SchemaCatalog",
    "SemanticHit",
    "SemanticMetadata",
    "TableDesc",
    "TableSchema",
    "load_semantics",
    "normalize",
    "search_schema",
]
