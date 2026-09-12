"""领域查询定义注册表：加载 configs/domain_queries.yaml，按 mtime 自动重载。

定义由服务端维护（SQL 模板 / 参数 / 结果契约 / 领域口径），以文件哈希生成
definition_version，随结果 DTO 返回，便于客户端判断定义是否变更。
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path
from typing import Mapping

import yaml

from .. import config
from .models import ParameterSpec, QuerySpec, QueryTemplate

DEFAULT_DOMAIN_QUERIES_PATH = Path(__file__).resolve().parents[2] / "configs" / "domain_queries.yaml"

_SLOT_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_VALUE_KINDS = {"date", "integer", "number", "enum", "string"}


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _definition_version(document: dict) -> str:
    payload = yaml.safe_dump(document, allow_unicode=True, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return "domain-v%s-%s" % (document.get("version", 1), digest)


def _build_template(name: str, doc: Mapping) -> QueryTemplate:
    sql = str(doc.get("sql") or "").strip()
    if not sql:
        raise ValueError("领域工具 %s 缺少 sql 模板" % name)

    parameters: list[ParameterSpec] = []
    for raw in doc.get("parameters") or []:
        spec = ParameterSpec(
            name=str(raw["name"]),
            kind=str(raw.get("type", "string")),
            required=bool(raw.get("required", False)),
            default=raw.get("default"),
            allowlist=tuple(str(item) for item in (raw.get("allowlist") or [])),
            min_value=raw.get("min"),
            max_value=raw.get("max"),
            max_length=int(raw.get("max_length", 256)),
            description=str(raw.get("description", "")),
        )
        if spec.kind not in _VALUE_KINDS and spec.kind != "identifier":
            raise ValueError("领域工具 %s 参数 %s 类型非法：%s" % (name, spec.name, spec.kind))
        parameters.append(spec)

    identifiers = {
        str(slot): {str(key): str(fragment) for key, fragment in mapping.items()}
        for slot, mapping in (doc.get("identifiers") or {}).items()
    }

    template = QueryTemplate(
        name=name,
        description=str(doc.get("description", "")),
        sql=sql,
        parameters=tuple(parameters),
        identifiers=identifiers,
        max_limit=int(doc.get("max_limit") or 200),
        result_columns=tuple(str(item) for item in (doc.get("result_columns") or [])),
        semantics={str(k): str(v) for k, v in (doc.get("semantics") or {}).items()},
    )
    _validate_template(template)
    return template


def _validate_template(template: QueryTemplate) -> None:
    """加载期校验：槽位必须定义、identifier 默认值必须在白名单内、必填参数必须声明。"""
    by_name = {spec.name: spec for spec in template.parameters}
    slots = set(_SLOT_RE.findall(template.sql))
    for slot in slots:
        if slot not in by_name and slot not in template.identifiers:
            raise ValueError(
                "领域工具 %s 的模板引用了未定义槽位 {%s}" % (template.name, slot)
            )
    for slot, mapping in template.identifiers.items():
        if not mapping:
            raise ValueError("领域工具 %s 标识符 %s 白名单为空" % (template.name, slot))
        spec = by_name.get(slot)
        if spec is None:
            raise ValueError("领域工具 %s 标识符 %s 缺少对应参数定义" % (template.name, slot))
        if spec.default is not None and str(spec.default) not in mapping:
            raise ValueError(
                "领域工具 %s 标识符 %s 默认值 %s 不在白名单内"
                % (template.name, slot, spec.default)
            )
    for spec in template.parameters:
        if spec.kind == "enum" and not spec.allowlist:
            raise ValueError("领域工具 %s 枚举参数 %s 白名单为空" % (template.name, spec.name))
        if spec.kind == "identifier" and spec.required:
            raise ValueError("领域工具 %s 标识符参数 %s 不应为必填" % (template.name, spec.name))


def load_domain_queries(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError("领域查询定义文件不存在：%s" % path)
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict) or not document.get("tools"):
        raise ValueError("领域查询定义缺少 tools 节点：%s" % path)
    return document


class DomainRegistry:
    """持有全部 QuerySpec，按 mtime + TTL 自动重载定义文件。"""

    _shared: "DomainRegistry | None" = None
    _shared_lock = threading.Lock()

    @classmethod
    def shared(cls) -> "DomainRegistry":
        if cls._shared is None:
            with cls._shared_lock:
                if cls._shared is None:
                    cls._shared = cls()
        return cls._shared

    def __init__(self, path: str | Path | None = None, reload_seconds: int | None = None):
        self.path = Path(path) if path else Path(
            config.DOMAIN_QUERIES_PATH or DEFAULT_DOMAIN_QUERIES_PATH
        )
        self._ttl = (
            config.POLICY_RELOAD_SECONDS if reload_seconds is None else reload_seconds
        )
        self._specs: dict[str, QuerySpec] = {}
        self._version = ""
        self._mtime: int | None = None
        self._checked = 0.0
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        document = load_domain_queries(self.path)
        version = _definition_version(document)
        specs: dict[str, QuerySpec] = {}
        for name, doc in document["tools"].items():
            template = _build_template(str(name), doc)
            specs[str(name)] = QuerySpec(
                name=str(name), template=template, definition_version=version
            )
        with self._lock:
            self._specs = specs
            self._version = version
            self._mtime = _mtime_ns(self.path)

    def _maybe_reload(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now - self._checked < self._ttl:
                return
            self._checked = now
            mtime = _mtime_ns(self.path)
            if mtime == self._mtime:
                return
        self._load()

    def get(self, name: str) -> QuerySpec:
        self._maybe_reload()
        with self._lock:
            if name not in self._specs:
                raise KeyError(name)
            return self._specs[name]

    def list(self) -> list[str]:
        self._maybe_reload()
        with self._lock:
            return sorted(self._specs)

    def definition_version(self) -> str:
        self._maybe_reload()
        with self._lock:
            return self._version

    def reload(self) -> None:
        self._load()

    def spec_snapshot(self) -> dict[str, dict]:
        """供 /domain 健康检查等展示：工具名 -> 契约摘要。"""
        self._maybe_reload()
        with self._lock:
            return {
                name: {
                    "description": spec.template.description,
                    "result_columns": list(spec.template.result_columns),
                    "max_limit": spec.template.max_limit,
                    "semantics": dict(spec.template.semantics),
                }
                for name, spec in sorted(self._specs.items())
            }


def get_domain_registry() -> DomainRegistry:
    return DomainRegistry.shared()
