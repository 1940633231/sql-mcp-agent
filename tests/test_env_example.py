"""`.env.example` 最小可复制模板回归测试。"""
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"

_CONFIG_PREFIXES = (
    "AUTH_",
    "POLICY_",
    "AUDIT_",
    "DB_",
    "SCHEMA_",
    "DOMAIN_",
    "MCP_",
    "MYSQL_",
    "OTEL_",
    "ALERT_",
    "LLM_",
    "MAX_",
    "REQUEST_",
    "RETRY_",
    "SHUTDOWN_",
)


def test_env_example_is_minimal_copyable():
    values = {
        key: value
        for key, value in dotenv_values(ENV_EXAMPLE).items()
        if value is not None
    }

    # Active lines must not rely on empty strings to mean "use default".
    assert all(value != "" for value in values.values()), values
    assert values["POLICY_STORE"] == "file"
    assert values["AUTH_MODE"] == "disabled"
    assert values["AUTH_RESOURCE_URL"] == "http://127.0.0.1:8000/mcp"
    assert "AUTH_TOKENS_JSON" not in values

    env = os.environ.copy()
    for key in list(env):
        if key.startswith(_CONFIG_PREFIXES) or key in {
            "BLOCKED_TOOLS", "RETRY_SAFE_TOOLS",
        }:
            env.pop(key, None)
    env.update(values)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import mcp_server.server; import agent.config; print('ok')",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
