"""领域参数绑定：把调用方参数渲染成「可校验、可绑定」的 SQL。

原则（V0.5 安全契约）：
  - 标识符槽位（granularity/sort_by/order_dir 等）只能内联 identifiers 白名单
    中的固定 SQL 片段；调用方传入的原始值不会作为文本进入 SQL；
  - 值槽位（start_date/limit/industry 等）先按 ParameterSpec 做类型/范围/白名单
    校验，再以 ? 占位符输出，由数据库驱动绑定执行；
  - 未知参数名、未知标识符、非法值一律抛 InvalidParameter（code=invalid_parameter），
    不静默忽略，防止绕过白名单。
"""
from __future__ import annotations

import re
from datetime import date as _date
from typing import Any, Mapping

from .models import QueryTemplate

# {slot} 槽位正则；槽位名与 SQL 文本分隔，片段中的 % / 引号不受影响
_SLOT_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_RANGE_START = _date(1970, 1, 1)
_DATE_RANGE_END = _date(2100, 12, 31)


class InvalidParameter(ValueError):
    """参数校验失败（工具层收敛为 {"error": ..., "code": "invalid_parameter"}）。"""

    def __init__(self, reason: str, field: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.field = field
        self.code = "invalid_parameter"


def _reject(reason: str, field: str = "") -> "InvalidParameter":
    prefix = ("%s: " % field) if field else ""
    return InvalidParameter(prefix + reason, field=field)


def _coerce_date(spec, raw: Any) -> str:
    if not isinstance(raw, str) or not _DATE_RE.match(raw):
        raise _reject("日期必须是 YYYY-MM-DD 格式", spec.name)
    try:
        value = _date.fromisoformat(raw)
    except ValueError:
        raise _reject("不是合法日期", spec.name) from None
    if not (_DATE_RANGE_START <= value <= _DATE_RANGE_END):
        raise _reject("日期超出允许范围（1970-01-01 ~ 2100-12-31）", spec.name)
    return raw


def _coerce_integer(spec, raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise _reject("必须是整数", spec.name)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _reject("必须是整数", spec.name) from None
    if str(value) != str(raw).strip() and not (isinstance(raw, float) and raw.is_integer()):
        raise _reject("必须是整数", spec.name)
    if spec.min_value is not None and value < spec.min_value:
        raise _reject("不能小于 %d" % spec.min_value, spec.name)
    if spec.max_value is not None and value > spec.max_value:
        raise _reject("不能大于 %d" % spec.max_value, spec.name)
    return value


def _coerce_number(spec, raw: Any) -> float:
    if isinstance(raw, bool):
        raise _reject("必须是数字", spec.name)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise _reject("必须是数字", spec.name) from None
    if spec.min_value is not None and value < spec.min_value:
        raise _reject("不能小于 %s" % spec.min_value, spec.name)
    if spec.max_value is not None and value > spec.max_value:
        raise _reject("不能大于 %s" % spec.max_value, spec.name)
    return value


def _coerce_enum(spec, raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise _reject("必须是非空字符串", spec.name)
    if raw not in spec.allowlist:
        allowed = " / ".join(spec.allowlist) or "（无合法值）"
        raise _reject("不在白名单内，可选值：%s" % allowed, spec.name)
    return raw


def _coerce_string(spec, raw: Any) -> str:
    if not isinstance(raw, str):
        raise _reject("必须是字符串", spec.name)
    if len(raw) > spec.max_length:
        raise _reject("长度不能超过 %d" % spec.max_length, spec.name)
    return raw


def _coerce(spec, raw: Any):
    """把调用方原始值按 ParameterSpec 校验并规范化为可绑定值。

    可选参数缺省（None）直接透传，由 COALESCE(?, ...) 语义表达「不过滤」；
    必填参数缺失在 _replace 中已提前拒绝。
    """
    if raw is None:
        return None
    if spec.kind == "date":
        return _coerce_date(spec, raw)
    if spec.kind == "integer":
        return _coerce_integer(spec, raw)
    if spec.kind == "number":
        return _coerce_number(spec, raw)
    if spec.kind == "enum":
        return _coerce_enum(spec, raw)
    if spec.kind == "string":
        return _coerce_string(spec, raw)
    raise _reject("不支持的参数类型：%s" % spec.kind, spec.name)


def _lookup_identifier(mapping: Mapping[str, str], field: str, raw: Any) -> str:
    if not isinstance(raw, str) or raw not in mapping:
        allowed = " / ".join(mapping) or "（无合法值）"
        raise _reject("不在白名单内，可选值：%s" % allowed, field)
    return mapping[raw]


def render(template: QueryTemplate, args: Mapping[str, Any]) -> tuple[str, tuple, dict]:
    """把参数渲染为 (sql, params, effective)。

    sql 中的值槽位输出为 ? 占位符；params 为按槽位出现顺序整理的绑定值元组；
    effective 为规范化后的生效参数（含默认值），供结果 DTO 的 meta.parameters 展示。
    返回的 sql 可直接进入 QueryService（AST Guard / RBAC / ACL / RLS / LIMIT / 审计），
    执行前由 Executor 把 ? 转为驱动占位符并绑定 params。
    """
    unknown = [key for key in args if not template.parameter(key) and key not in template.identifier_slots]
    if unknown:
        raise InvalidParameter("未知参数：%s" % ", ".join(sorted(unknown)))

    provided = {
        str(key): value for key, value in (args or {}).items()
        if value is not None and str(value) != ""
    }
    params: list[Any] = []
    effective: dict[str, Any] = {}

    def _replace(match: re.Match) -> str:
        slot = match.group(1)
        if slot in template.identifiers:
            spec = template.parameter(slot)
            raw = provided.get(slot, spec.default if spec else None)
            effective[slot] = raw
            return _lookup_identifier(template.identifiers[slot], slot, raw)
        spec = template.parameter(slot)
        if spec is None:
            raise _reject("模板引用了未定义参数槽位：%s" % slot, slot)
        raw = provided.get(slot, spec.default)
        if spec.required and (raw is None or str(raw) == ""):
            raise _reject("必填参数缺失", slot)
        value = _coerce(spec, raw)
        effective[slot] = value
        params.append(value)
        return "?"

    sql = _SLOT_RE.sub(_replace, template.sql)

    start = effective.get("start_date")
    end = effective.get("end_date")
    if start and end and start > end:
        raise _reject("start_date 不能晚于 end_date")
    return sql, tuple(params), effective
