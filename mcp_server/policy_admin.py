"""用于校验、发布和查看权限策略的 CLI。

示例：
    python -m mcp_server.policy_admin status
    python -m mcp_server.policy_admin validate configs/permissions.yaml
    python -m mcp_server.policy_admin publish configs/permissions.yaml --actor admin --reason "v0.3"
    python -m mcp_server.policy_admin list --limit 10
"""
import argparse
import json
from pathlib import Path

import yaml

from .authorization.manager import get_policy_manager
from .authorization.policy import (
    load_permission_policy,
    permission_policy_from_dict,
    permission_policy_to_dict,
)

from .authorization.policy import validate_policy_document


def _load_document(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def main() -> None:
    parser = argparse.ArgumentParser(description="权限策略管理")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="显示当前 active 策略")
    sub.add_parser("export", help="导出当前 active 策略")

    validate = sub.add_parser("validate", help="校验策略文件")
    validate.add_argument("path", nargs="?", default="configs/permissions.yaml")

    compile_parser = sub.add_parser("compile", help="校验并编译策略文件")
    compile_parser.add_argument("path", nargs="?", default="configs/permissions.yaml")

    publish = sub.add_parser("publish", help="发布策略文件为新版本")
    publish.add_argument("path", nargs="?", default="configs/permissions.yaml")
    publish.add_argument("--actor", default="cli")
    publish.add_argument("--reason", default="")
    publish.add_argument("--expected-version", default=None)

    listing = sub.add_parser("list", help="列出策略版本")
    listing.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()
    manager = get_policy_manager()

    if args.command == "status":
        print(json.dumps(manager.status(), ensure_ascii=False, indent=2))
    elif args.command == "export":
        print(json.dumps(permission_policy_to_dict(manager.get()), ensure_ascii=False, indent=2))
    elif args.command == "validate":
        report = validate_policy_document(_load_document(args.path))
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        if not report.valid:
            raise SystemExit(1)
    elif args.command == "publish":
        record = manager.publish(
            _load_document(args.path),
            actor=args.actor,
            reason=args.reason,
            expected_version=args.expected_version,
        )
        print(json.dumps({"version": record.version, "source": record.source}, ensure_ascii=False))
    elif args.command == "compile":
        policy = permission_policy_from_dict(_load_document(args.path))
        print(json.dumps({
            "compiled": True,
            "roles": len(policy.roles),
            "principals": len(policy.principals),
            "acl": len(policy.acl_rules),
            "row_policies": len(policy.row_policies),
        }, ensure_ascii=False))
    elif args.command == "list":
        print(json.dumps(
            [record.__dict__ for record in manager.list_versions(args.limit)],
            ensure_ascii=False,
            indent=2,
        ))


if __name__ == "__main__":
    main()
