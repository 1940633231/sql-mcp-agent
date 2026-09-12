"""Migrate policy versions from the legacy business DB to the policy DB."""
import sys
from pathlib import Path
import json
import pymysql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server import config
from mcp_server.authorization.manager import MySqlPolicyStore
from mcp_server.authorization.policy import permission_policy_from_dict
from mcp_server.database.connection import db_cursor, policy_db_transaction


def _ensure_policy_database() -> None:
    params = config.get_policy_connection()
    database = params.pop("database")
    conn = pymysql.connect(**params, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE DATABASE IF NOT EXISTS `%s` DEFAULT CHARACTER SET utf8mb4" % database
            )
    finally:
        conn.close()


def migrate() -> None:
    _ensure_policy_database()
    MySqlPolicyStore().ensure_schema()

    with db_cursor() as cur:
        cur.execute("SHOW TABLES LIKE %s", (config.POLICY_VERSION_TABLE,))
        if not cur.fetchone():
            raise RuntimeError(
                "业务库中不存在 %s，无法迁移" % config.POLICY_VERSION_TABLE
            )
        cur.execute(
            "SELECT version, document_json, actor, reason, created_at "
            "FROM `%s` ORDER BY id" % config.POLICY_VERSION_TABLE
        )
        versions = cur.fetchall()
        cur.execute(
            "SELECT v.version FROM `%s` a JOIN `%s` v ON v.id=a.version_id WHERE a.id=1"
            % (config.POLICY_ACTIVE_TABLE, config.POLICY_VERSION_TABLE)
        )
        active = cur.fetchone()

    with policy_db_transaction() as cur:
        for row in versions:
            document = row["document_json"]
            if isinstance(document, str):
                document = json.loads(document)
            document = dict(document)
            document.setdefault("version", 1)
            permission_policy_from_dict(document)
            document = json.dumps(document, ensure_ascii=False, sort_keys=True)
            cur.execute(
                "INSERT INTO `%s` (version, document_json, actor, reason, created_at) "
                "VALUES (%%s, %%s, %%s, %%s, %%s) "
                "ON DUPLICATE KEY UPDATE document_json=VALUES(document_json)"
                % config.POLICY_VERSION_TABLE,
                (
                    row["version"],
                    document,
                    row.get("actor") or "",
                    row.get("reason") or "",
                    row.get("created_at"),
                ),
            )
        if active:
            cur.execute(
                "SELECT id FROM `%s` WHERE version=%%s" % config.POLICY_VERSION_TABLE,
                (active["version"],),
            )
            version_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO `%s` (id, version_id) VALUES (1, %%s) "
                "ON DUPLICATE KEY UPDATE version_id=VALUES(version_id)"
                % config.POLICY_ACTIVE_TABLE,
                (version_id,),
            )
    print("migrated %d policy versions" % len(versions))


if __name__ == "__main__":
    migrate()
