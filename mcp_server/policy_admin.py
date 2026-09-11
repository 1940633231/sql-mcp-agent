"""CLI for validating, publishing, and inspecting permission policies.

Examples:
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
    permission_policy_to_dict,
)


def _load_document(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def main() -> None:
    parser = argparse.ArgumentParser(description="权限策略管理")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="显示当前 active 策略")
    sub.add_parser("export", help="导出当前 active 策略")

    validate = sub.add_parser("validate", help="校验策略文件")
    validate.add_argument("path", nargs="?", default="configs/permissions.yaml")

    publish = sub.add_parser("publish", help="发布策略文件为新版本")
    publish.add_argument("path", nargs="?", default="configs/permissions.yaml")
    publish.add_argument("--actor", default="cli")
    publish.add_argument("--reason", default="")

    listing = sub.add_parser("list", help="列出策略版本")
    listing.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()
    manager = get_policy_manager()

    if args.command == "status":
        print(json.dumps(manager.status(), ensure_ascii=False, indent=2))
    elif args.command == "export":
        print(json.dumps(permission_policy_to_dict(manager.get()), ensure_ascii=False, indent=2))
    elif args.command == "validate":
        policy = load_permission_policy(args.path)
        print(
            "valid roles=%d principals=%d acl=%d row_policies=%d"
            % (
                len(policy.roles),
                len(policy.principals),
                len(policy.acl_rules),
                len(policy.row_policies),
            )
        )
    elif args.command == "publish":
        record = manager.publish(
            _load_document(args.path),
            actor=args.actor,
            reason=args.reason,
        )
        print(json.dumps({"version": record.version, "source": record.source}, ensure_ascii=False))
    elif args.command == "list":
        print(json.dumps(
            [record.__dict__ for record in manager.list_versions(args.limit)],
            ensure_ascii=False,
            indent=2,
        ))


if __name__ == "__main__":
    main()
