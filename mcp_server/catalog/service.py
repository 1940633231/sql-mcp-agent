"""独立于查询执行的缓存 SchemaCatalog。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..database.connection import db_cursor
from ..security import parser
from . import semantic
from .models import ColumnSchema, TableSchema


@dataclass(frozen=True)
class CatalogStats:
    list_hits: int
    list_misses: int
    table_hits: int
    table_misses: int


def _mtime_ns(path) -> int | None:
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return None


class SchemaCatalog:

    _shared: "SchemaCatalog | None" = None
    _shared_lock = threading.Lock()

    @classmethod
    def shared(cls) -> "SchemaCatalog":
        if cls._shared is None:
            with cls._shared_lock:
                if cls._shared is None:
                    from .. import config
                    cls._shared = cls(
                        ttl_seconds=config.SCHEMA_CACHE_TTL_SECONDS,
                        max_entries=config.SCHEMA_CACHE_MAX_ENTRIES,
                    )
        return cls._shared

    """缓存表、列、主键、外键和索引，并按 TTL 自动过期。"""

    def __init__(self, ttl_seconds: int = 60, max_entries: int = 256):
        self.ttl_seconds = int(ttl_seconds)
        self.max_entries = int(max_entries)
        self._tables: list[str] | None = None
        self._tables_expire = 0.0
        self._cache: dict[str, tuple[float, TableSchema]] = {}
        self._list_hits = 0
        self._list_misses = 0
        self._table_hits = 0
        self._table_misses = 0
        self._lock = threading.RLock()
        self._semantic_path = config.SCHEMA_DESC_PATH or semantic.DEFAULT_SCHEMA_DESC_PATH
        self._semantic: semantic.SemanticMetadata = semantic.load_semantics(self._semantic_path)
        self._semantic_mtime = _mtime_ns(self._semantic_path)
        self._semantic_checked = time.monotonic()

    def list_tables(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            if self._tables is not None and now < self._tables_expire:
                self._list_hits += 1
                return list(self._tables)
            self._list_misses += 1
        with db_cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = [next(iter(row.values())) for row in cur.fetchall()]
        with self._lock:
            self._tables = tables
            self._tables_expire = now + self.ttl_seconds
        return list(tables)

    def get_table(self, table_name: str, refresh: bool = False) -> TableSchema:
        self._maybe_reload_semantics()
        self._validate_table(table_name)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(table_name)
            if cached and not refresh and now < cached[0]:
                self._table_hits += 1
                return cached[1]
            self._table_misses += 1
        schema = self._load_table(table_name)
        with self._lock:
            self._cache[table_name] = (now + self.ttl_seconds, schema)
            self._trim_cache()
        return schema

    def get_columns(self, table_name: str) -> list[ColumnSchema]:
        return list(self.get_table(table_name).columns)

    def get_table_dict(self, table_name: str) -> dict:
        """返回表的完整语义 DTO（表级元数据 + 外键 + 索引 + 列含枚举）。"""
        return self.get_table(table_name).to_dict()

    def get_schema(self, table_name: str) -> list[dict]:
        return self.get_table(table_name).to_legacy_rows()

    def get_primary_keys(self, table_name: str) -> list[str]:
        return list(self.get_table(table_name).primary_keys)

    def get_foreign_keys(self, table_name: str) -> list[dict]:
        return list(self.get_table(table_name).foreign_keys)

    def get_indexes(self, table_name: str) -> list[dict]:
        return list(self.get_table(table_name).indexes)

    def snapshot(self) -> list[TableSchema]:
        return [self.get_table(table) for table in self.list_tables()]

    def semantic_metadata(self) -> semantic.SemanticMetadata:
        """返回内存中的语义元数据（只读访问器）。"""
        return self._semantic

    def search(self, keyword: str, scan_all: bool = False) -> list[semantic.SemanticHit]:
        """轻量语义检索，覆盖语义元数据 + Schema Cache 物理对象。

        除 YAML 声明对象外，还按物理表名/列名兜底匹配。表列表/表结构来自
        Catalog 缓存；注意冷缓存或 TTL 过期时，物理名命中会触发一次表结构
        懒加载（数据库查询）。
        scan_all=True 时对全库每张表都懒加载并扫描物理列名，保证未缓存且表名
        未命中的物理列也能被检索到（代价：可能对每张表发一次结构查询）。
        """
        self._maybe_reload_semantics()
        hits = semantic.search_schema(self._semantic, keyword)
        needle = semantic.normalize(keyword)
        if not needle:
            return hits

        # table_hit_tables 只用于「去重表级命中」，不用于决定是否扫描物理列；
        # covered_cols 统一去重所有来源（语义 + 物理）的列级命中。
        table_hit_tables = {h.table for h in hits if h.column is None}
        covered_cols = {(h.table, h.column) for h in hits if h.column}

        for table in self.list_tables():
            if needle in semantic.normalize(table):
                # 表名命中：补表级命中（去重），并扫描该表物理列
                if table not in table_hit_tables:
                    hits.append(
                        semantic.SemanticHit(
                            table=table, column=None, kind="table",
                            matched_on=table, label="", description="",
                        )
                    )
                    table_hit_tables.add(table)
                for col in self.get_table(table).columns:
                    if needle in semantic.normalize(col.name) and (table, col.name) not in covered_cols:
                        covered_cols.add((table, col.name))
                        hits.append(
                            semantic.SemanticHit(
                                table=table, column=col.name, kind="column",
                                matched_on=col.name, label=col.label, description=col.description,
                            )
                        )
            elif scan_all:
                # 未命中表名：幂等地对每张表懒加载扫描物理列名（去重于 covered_cols），
                # 即使该表已因语义命中进入 table_hit_tables 也照常扫描其列。
                for col in self.get_table(table).columns:
                    if needle in semantic.normalize(col.name) and (table, col.name) not in covered_cols:
                        covered_cols.add((table, col.name))
                        hits.append(
                            semantic.SemanticHit(
                                table=table, column=col.name, kind="column",
                                matched_on=col.name, label=col.label, description=col.description,
                            )
                        )

        # 对已缓存表（不额外 DB）按物理列名兜底
        for tname, (_, schema) in list(self._cache.items()):
            for col in schema.columns:
                if needle in semantic.normalize(col.name) and (tname, col.name) not in covered_cols:
                    hits.append(
                        semantic.SemanticHit(
                            table=tname, column=col.name, kind="column",
                            matched_on=col.name, label=col.label, description=col.description,
                        )
                    )
                    covered_cols.add((tname, col.name))
        return hits

    def reload_semantics(self) -> None:
        """重新加载语义 YAML 并清空表缓存（下次 get_table 用新语义重建）。"""
        with self._lock:
            self._semantic = semantic.load_semantics(self._semantic_path)
            self._semantic_mtime = _mtime_ns(self._semantic_path)
            self._cache.clear()
            self._tables = None

    def _maybe_reload_semantics(self) -> None:
        """按 TTL 频率检查 YAML 文件 mtime，变更则自动重载语义并清空表缓存。

        mtime 为 None（文件被删除）同样触发重载——load_semantics() 对缺失文件
        返回空语义，因此删除配置文件会清空旧业务描述，而非继续沿用。
        """
        with self._lock:
            now = time.monotonic()
            if now - self._semantic_checked < self.ttl_seconds:
                return
            self._semantic_checked = now
            mtime = _mtime_ns(self._semantic_path)
            if mtime == self._semantic_mtime:
                return
        self.reload_semantics()

    def invalidate(self, table_name: str | None = None) -> None:
        with self._lock:
            if table_name is None:
                self._cache.clear()
                self._tables = None
                self._semantic = semantic.load_semantics(self._semantic_path)
            else:
                self._cache.pop(table_name, None)
                self._tables = None

    def stats(self) -> CatalogStats:
        with self._lock:
            return CatalogStats(
                list_hits=self._list_hits,
                list_misses=self._list_misses,
                table_hits=self._table_hits,
                table_misses=self._table_misses,
            )

    def _load_table(self, table_name: str) -> TableSchema:
        with db_cursor() as cur:
            cur.execute("SHOW FULL COLUMNS FROM `%s`" % table_name)
            rows = cur.fetchall()
            cur.execute("SHOW INDEX FROM `%s`" % table_name)
            index_rows = cur.fetchall()
            foreign_keys = self._foreign_keys(cur, table_name)
        primary_keys = tuple(
            row["Field"] for row in rows if str(row.get("Key") or "").upper() == "PRI"
        )
        col_meta = self._semantic.columns.get(table_name.lower(), {})
        columns = tuple(
            ColumnSchema(
                name=str(row["Field"]),
                data_type=str(row.get("Type") or ""),
                nullable=str(row.get("Null") or "").upper() == "YES",
                default=row.get("Default"),
                comment=str(row.get("Comment") or ""),
                label=(col_meta.get(str(row["Field"]).lower()) or semantic.ColumnDesc()).label,
                description=(col_meta.get(str(row["Field"]).lower()) or semantic.ColumnDesc()).description,
                synonyms=(col_meta.get(str(row["Field"]).lower()) or semantic.ColumnDesc()).synonyms,
                enum_values=(col_meta.get(str(row["Field"]).lower()) or semantic.ColumnDesc()).enum_values,
            )
            for row in rows
        )
        table_meta = self._semantic.tables.get(table_name.lower(), semantic.TableDesc())
        indexes = tuple(
            {
                "name": str(row.get("Key_name") or ""),
                "column": str(row.get("Column_name") or ""),
                "unique": not bool(row.get("Non_unique")),
                "sequence": int(row.get("Seq_in_index") or 0),
            }
            for row in index_rows
        )
        return TableSchema(
            table=table_name,
            columns=columns,
            primary_keys=primary_keys,
            foreign_keys=tuple(foreign_keys),
            indexes=indexes,
            label=table_meta.label,
            description=table_meta.description,
        )

    @staticmethod
    def _foreign_keys(cur, table_name: str) -> list[dict]:
        cur.execute(
            "SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, "
            "REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
            "AND REFERENCED_TABLE_NAME IS NOT NULL",
            (table_name,),
        )
        return list(cur.fetchall())

    @staticmethod
    def _validate_table(table_name: str) -> None:
        if not parser.is_valid_identifier(table_name, r"^[A-Za-z0-9_]+$"):
            raise ValueError("非法的表名")

    def _trim_cache(self) -> None:
        if len(self._cache) <= self.max_entries:
            return
        oldest = sorted(self._cache.items(), key=lambda item: item[1][0])
        for key, _ in oldest[: len(self._cache) - self.max_entries]:
            self._cache.pop(key, None)
