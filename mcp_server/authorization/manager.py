"""Permission-policy persistence, versioning, and hot reload."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import yaml

from .. import config
from ..database.connection import policy_db_cursor, policy_db_transaction
from .models import PermissionPolicy
from .audit import log_event
from .policy import (
    DEFAULT_POLICY_PATH,
    load_policy_document,
    load_permission_policy,
    permission_policy_from_dict,
    permission_policy_to_dict,
)


class PolicyConflictError(RuntimeError):
    """The active policy changed since the publisher loaded it."""



@dataclass(frozen=True)
class PolicyRecord:
    version: str
    document: dict
    source: str
    actor: str = ""
    reason: str = ""
    updated_at: str = ""


class PolicyStore(Protocol):
    def get_active(self) -> PolicyRecord | None: ...

    def publish(
        self, document: dict, actor: str = "", reason: str = "", expected_version: str | None = None
    ) -> PolicyRecord: ...

    def list_versions(self, limit: int = 20) -> list[PolicyRecord]: ...


class FilePolicyStore:
    """Read and atomically replace the YAML policy file."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_POLICY_PATH

    def _read(self) -> dict:
        return load_policy_document(self.path)

    def get_active(self) -> PolicyRecord | None:
        if not self.path.exists():
            return None
        document = self._read()
        return PolicyRecord(
            version=_document_version(document, mtime=self.path.stat().st_mtime_ns),
            document=document,
            source="file:%s" % self.path,
            updated_at=datetime.fromtimestamp(
                self.path.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        )

    def publish(
        self, document: dict, actor: str = "", reason: str = "", expected_version: str | None = None
    ) -> PolicyRecord:
        _assert_expected(self.get_active(), expected_version)
        permission_policy_from_dict(document)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        record = self.get_active()
        if record is None:
            raise RuntimeError("策略文件写入后无法读取")
        log_event("policy_published", source=record.source, version=record.version, actor=actor, reason=reason)
        return record

    def list_versions(self, limit: int = 20) -> list[PolicyRecord]:
        record = self.get_active()
        return [record] if record else []


class MemoryPolicyStore:
    """In-memory store used by tests and embedded deployments."""

    def __init__(self, document: dict | None = None):
        self._records: list[PolicyRecord] = []
        if document is not None:
            self.publish(document, actor="seed", reason="initial")

    def get_active(self) -> PolicyRecord | None:
        return self._records[-1] if self._records else None

    def publish(
        self, document: dict, actor: str = "", reason: str = "", expected_version: str | None = None
    ) -> PolicyRecord:
        _assert_expected(self.get_active(), expected_version)
        permission_policy_from_dict(document)
        record = PolicyRecord(
            version=_document_version(document),
            document=json.loads(json.dumps(document)),
            source="memory",
            actor=actor,
            reason=reason,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._records.append(record)
        return record

    def list_versions(self, limit: int = 20) -> list[PolicyRecord]:
        return list(reversed(self._records[-limit:]))


class MySqlPolicyStore:
    """Versioned policy storage in MySQL."""

    def __init__(self):
        self._schema_ready = False

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        version_table = config.POLICY_VERSION_TABLE
        active_table = config.POLICY_ACTIVE_TABLE
        audit_table = config.POLICY_AUDIT_TABLE
        with policy_db_cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS `%s` ("
                "id BIGINT AUTO_INCREMENT PRIMARY KEY,"
                "version VARCHAR(96) NOT NULL UNIQUE,"
                "document_json JSON NOT NULL,"
                "actor VARCHAR(190) NOT NULL DEFAULT '',"
                "reason VARCHAR(500) NOT NULL DEFAULT '',"
                "created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" % version_table
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS `%s` ("
                "id BIGINT AUTO_INCREMENT PRIMARY KEY,"
                "event VARCHAR(32) NOT NULL,"
                "version VARCHAR(96) NOT NULL,"
                "actor VARCHAR(190) NOT NULL DEFAULT '',"
                "reason VARCHAR(500) NOT NULL DEFAULT '',"
                "created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),"
                "INDEX idx_policy_events_version (version)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" % audit_table
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS `%s` ("
                "id TINYINT PRIMARY KEY,"
                "version_id BIGINT NOT NULL,"
                "updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) "
                "ON UPDATE CURRENT_TIMESTAMP(6),"
                "CONSTRAINT `fk_policy_active_version` FOREIGN KEY (version_id) "
                "REFERENCES `%s`(id)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" % (active_table, version_table)
            )
        self._schema_ready = True

    def get_active(self) -> PolicyRecord | None:
        self.ensure_schema()
        sql = (
            "SELECT v.version, v.document_json, v.actor, v.reason, v.created_at "
            "FROM `%s` a JOIN `%s` v ON v.id=a.version_id WHERE a.id=1"
            % (config.POLICY_ACTIVE_TABLE, config.POLICY_VERSION_TABLE)
        )
        with policy_db_cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        if not row:
            return None
        document = row["document_json"]
        if isinstance(document, str):
            document = json.loads(document)
        return PolicyRecord(
            version=row["version"],
            document=document,
            source="mysql",
            actor=row.get("actor") or "",
            reason=row.get("reason") or "",
            updated_at=str(row.get("created_at") or ""),
        )

    def publish(
        self, document: dict, actor: str = "", reason: str = "", expected_version: str | None = None
    ) -> PolicyRecord:
        permission_policy_from_dict(document)
        self.ensure_schema()
        version = _document_version(document)
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True)
        with policy_db_transaction() as cur:
            cur.execute(
                "SELECT v.version FROM `%s` a JOIN `%s` v ON v.id=a.version_id "
                "WHERE a.id=1 FOR UPDATE" % (config.POLICY_ACTIVE_TABLE, config.POLICY_VERSION_TABLE)
            )
            active = cur.fetchone()
            active_version = active["version"] if active else None
            if expected_version and active_version != expected_version:
                raise PolicyConflictError(
                    "active policy changed: expected %s, got %s"
                    % (expected_version, active_version)
                )
            cur.execute(
                "INSERT INTO `%s` (version, document_json, actor, reason) "
                "VALUES (%%s, %%s, %%s, %%s) "
                "ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)"
                % config.POLICY_VERSION_TABLE,
                (version, payload, actor, reason),
            )
            version_id = cur.lastrowid
            if not version_id:
                cur.execute(
                    "SELECT id FROM `%s` WHERE version=%%s" % config.POLICY_VERSION_TABLE,
                    (version,),
                )
                version_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO `%s` (id, version_id) VALUES (1, %%s) "
                "ON DUPLICATE KEY UPDATE version_id=VALUES(version_id)"
                % config.POLICY_ACTIVE_TABLE,
                (version_id,),
            )
            cur.execute(
                "INSERT INTO `%s` (event, version, actor, reason) VALUES (%%s, %%s, %%s, %%s)"
                % config.POLICY_AUDIT_TABLE,
                ("publish", version, actor, reason),
            )
        log_event("policy_published", source="mysql", version=version, actor=actor, reason=reason)
        record = self.get_active()
        if record is None:
            raise RuntimeError("策略发布后无法读取 active 版本")
        return record

    def list_versions(self, limit: int = 20) -> list[PolicyRecord]:
        self.ensure_schema()
        limit = max(1, min(int(limit), 200))
        sql = (
            "SELECT version, document_json, actor, reason, created_at "
            "FROM `%s` ORDER BY id DESC LIMIT %d" % (config.POLICY_VERSION_TABLE, limit)
        )
        with policy_db_cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        records = []
        for row in rows:
            document = row["document_json"]
            if isinstance(document, str):
                document = json.loads(document)
            records.append(
                PolicyRecord(
                    version=row["version"],
                    document=document,
                    source="mysql",
                    actor=row.get("actor") or "",
                    reason=row.get("reason") or "",
                    updated_at=str(row.get("created_at") or ""),
                )
            )
        return records


class PolicyManager:
    """Cache the active policy and refresh it from its backing store."""

    def __init__(self, store: PolicyStore | None = None, reload_seconds: int | None = None):
        self._store = store or _build_store()
        self._reload_seconds = (
            config.POLICY_RELOAD_SECONDS if reload_seconds is None else reload_seconds
        )
        self._cached: PolicyRecord | None = None
        self._cached_policy: PermissionPolicy | None = None
        self._last_check = 0.0
        self._lock = threading.RLock()

    @property
    def store(self) -> PolicyStore:
        return self._store

    def get(self, force: bool = False) -> PermissionPolicy:
        with self._lock:
            now = time.monotonic()
            if (
                not force
                and self._cached_policy is not None
                and now - self._last_check < self._reload_seconds
            ):
                return self._cached_policy

            record = self._store.get_active()
            if record is None:
                if isinstance(self._store, MySqlPolicyStore):
                    seed = load_permission_policy()
                    record = self._store.publish(
                        permission_policy_to_dict(seed),
                        actor="bootstrap",
                        reason="seed from configs/permissions.yaml",
                    )
                else:
                    record = FilePolicyStore().get_active()
            if record is None:
                raise RuntimeError("没有可用的权限策略")

            if self._cached is None or record.version != self._cached.version:
                self._cached_policy = permission_policy_from_dict(record.document)
                self._cached = record
                log_event("policy_loaded", source=record.source, version=record.version)
            self._last_check = now
            if self._cached_policy is None:
                raise RuntimeError("权限策略加载失败")
            return self._cached_policy

    def reload(self) -> PolicyRecord:
        self.get(force=True)
        if self._cached is None:
            raise RuntimeError("权限策略重载失败")
        return self._cached

    def status(self) -> dict:
        self.get(force=True)
        if self._cached is None:
            return {"available": False}
        return {
            "available": True,
            "source": self._cached.source,
            "version": self._cached.version,
            "actor": self._cached.actor,
            "reason": self._cached.reason,
            "updated_at": self._cached.updated_at,
        }

    def publish(
        self, document: dict, actor: str = "", reason: str = "", expected_version: str | None = None
    ) -> PolicyRecord:
        record = self._store.publish(
            document, actor=actor, reason=reason, expected_version=expected_version
        )
        with self._lock:
            self._cached = record
            self._cached_policy = permission_policy_from_dict(record.document)
            self._last_check = time.monotonic()
        return record

    def list_versions(self, limit: int = 20) -> list[PolicyRecord]:
        return self._store.list_versions(limit=limit)


def _build_store() -> PolicyStore:
    if config.POLICY_STORE == "file":
        return FilePolicyStore()
    if config.POLICY_STORE == "mysql":
        return MySqlPolicyStore()
    raise ValueError("不支持的 POLICY_STORE：%s（可选 file/mysql）" % config.POLICY_STORE)


def _assert_expected(active: PolicyRecord | None, expected_version: str | None) -> None:
    if expected_version and (active is None or active.version != expected_version):
        current = active.version if active else None
        raise PolicyConflictError(
            "active policy changed: expected %s, got %s" % (expected_version, current)
        )


def _document_version(document: dict, mtime: int | None = None) -> str:
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:32]
    if mtime is not None:
        return "file-%s-%s" % (digest, mtime)
    return "policy-%s" % digest


_manager: PolicyManager | None = None
_manager_lock = threading.Lock()


def get_policy_manager() -> PolicyManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = PolicyManager()
    return _manager


def get_permission_policy() -> PermissionPolicy:
    return get_policy_manager().get()
