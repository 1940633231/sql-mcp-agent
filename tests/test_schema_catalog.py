"""SchemaCatalog 缓存测试。"""
import contextlib

from mcp_server.catalog.service import SchemaCatalog


class FakeCursor:
    def __init__(self):
        self._rows = []

    def execute(self, sql, params=None):
        if sql.startswith("SHOW TABLES"):
            self._rows = [{"Tables_in_db": "company"}]
        elif sql.startswith("SHOW FULL COLUMNS"):
            self._rows = [
                {"Field": "company_id", "Type": "int", "Null": "NO", "Key": "PRI", "Default": None, "Comment": ""},
                {"Field": "name", "Type": "varchar(100)", "Null": "NO", "Key": "", "Default": None, "Comment": ""},
            ]
        elif sql.startswith("SHOW INDEX"):
            self._rows = [
                {"Key_name": "PRIMARY", "Column_name": "company_id", "Non_unique": 0, "Seq_in_index": 1}
            ]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)


@contextlib.contextmanager
def fake_db_cursor():
    yield FakeCursor()


def test_catalog_caches_tables_and_columns(monkeypatch):
    monkeypatch.setattr("mcp_server.catalog.service.db_cursor", fake_db_cursor)
    catalog = SchemaCatalog(ttl_seconds=60)

    assert catalog.list_tables() == ["company"]
    assert catalog.list_tables() == ["company"]
    assert catalog.get_columns("company")[0].name == "company_id"
    assert catalog.get_primary_keys("company") == ["company_id"]
    assert catalog.get_table("company").indexes[0]["unique"] is True

    stats = catalog.stats()
    assert stats.list_hits == 1
    assert stats.table_hits >= 1


def test_catalog_invalidate_forces_reload(monkeypatch):
    monkeypatch.setattr("mcp_server.catalog.service.db_cursor", fake_db_cursor)
    catalog = SchemaCatalog(ttl_seconds=60)
    catalog.get_table("company")
    catalog.invalidate("company")
    catalog.get_table("company")
    assert catalog.stats().table_misses == 2
