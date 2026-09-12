"""tests/test_authorization.py — V0.3 权限系统单元测试（不依赖数据库）。"""
import asyncio
import time
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.provider import AccessToken

from mcp_server import config
from mcp_server.auth.context import principal_for_access_token

from mcp_server.auth.models import Principal, RequestContext, resolve_attribute
from mcp_server.auth.token_verifier import JwtTokenVerifier, StaticTokenVerifier
from mcp_server.authorization.policy import load_permission_policy
from mcp_server.authorization.models import (
    AclRule,
    PermissionPolicy,
    PolicySubjects,
    RolePolicy,
    RowPolicy,
)
from mcp_server.authorization.service import AuthorizationService
from mcp_server.security.models import QueryResult
from mcp_server.security.validator import SqlValidator
from mcp_server.services.query_service import QueryService
from mcp_server.services.query_service import _result_column_guard


class FakeCatalog:
    def get_schema(self, table_name: str) -> list[dict]:
        columns = {
            "company": [
                "company_id",
                "name",
                "industry",
                "headquarters",
                "founded_year",
                "employees",
            ],
            "sale_records": ["id", "company_id", "amount", "record_date"],
        }
        return [{"Field": column} for column in columns.get(table_name, [])]


@pytest.fixture
def policy():
    return load_permission_policy()


@pytest.fixture
def authz(policy):
    return AuthorizationService(policy)


def context_for(policy, subject: str) -> RequestContext:
    raw = policy.principals[subject]
    principal = Principal(raw.subject, raw.roles, raw.attributes)
    return RequestContext(principal=principal, source="test", auth_method="test")


class TestRbac:
    def test_analyst_has_query_permission(self, policy, authz):
        principal = context_for(policy, "alice").principal
        assert authz.has_permission(principal, "query:run")
        assert authz.has_permission(principal, "schema:read")

    def test_viewer_cannot_run_query(self, policy, authz):
        principal = context_for(policy, "auditor").principal
        assert not authz.has_permission(principal, "query:run")
        assert authz.has_permission(principal, "schema:list")

    def test_query_and_policy_admin_are_separated(self, policy, authz):
        query_admin = context_for(policy, "query-admin").principal
        policy_admin = context_for(policy, "policy-admin").principal
        assert authz.has_permission(query_admin, "query:run")
        assert not authz.has_permission(query_admin, "policy:publish")
        assert authz.has_permission(policy_admin, "policy:publish")
        assert not authz.has_permission(policy_admin, "query:run")

    def test_rbac_gate_rejects_query_for_viewer(self, policy, authz):
        result = authz.authorize_sql(
            "SELECT name FROM company", context_for(policy, "auditor"), FakeCatalog()
        )
        assert not result.allowed
        assert result.code == "permission_denied"


class TestTableAndColumnAcl:
    def test_analyst_can_read_allowed_tables(self, policy, authz):
        principal = context_for(policy, "alice").principal
        assert authz.authorize_table(principal, "company")
        assert authz.authorize_table(principal, "sale_records")

    def test_unknown_table_is_denied_by_default(self, policy, authz):
        principal = context_for(policy, "alice").principal
        assert not authz.authorize_table(principal, "orders")

    def test_denied_column(self, policy, authz):
        principal = context_for(policy, "alice").principal
        assert authz.authorize_column(principal, "company", "name")
        assert not authz.authorize_column(principal, "company", "employees")

    def test_star_expands_to_visible_columns(self, policy, authz):
        result = authz.authorize_sql(
            "SELECT * FROM company", context_for(policy, "alice"), FakeCatalog()
        )
        assert result.allowed
        assert "employees" not in result.sql
        assert "company.company_id" in result.sql
        assert "SELECT *" not in result.sql
        assert "employees" not in result.result_columns

    def test_star_without_catalog_fails_closed(self, policy, authz):
        result = authz.authorize_sql(
            "SELECT * FROM company", context_for(policy, "alice")
        )
        assert not result.allowed
        assert result.code == "authorization_rewrite_error"

    def test_complex_star_uses_lineage(self, policy, authz):
        result = authz.authorize_sql(
            "SELECT * FROM company c JOIN (SELECT 1 AS n) x ON 1=1",
            context_for(policy, "alice"),
            FakeCatalog(),
        )
        assert result.allowed
        assert "employees" not in result.sql
        assert "x.n" in result.sql

    def test_cte_star_lineage_filters_columns(self, policy, authz):
        result = authz.authorize_sql(
            "WITH x AS (SELECT * FROM company) SELECT * FROM x",
            context_for(policy, "alice"),
            FakeCatalog(),
        )
        assert result.allowed
        assert "employees" not in result.sql
        assert "x.company_id" in result.sql

    def test_derived_table_denied_column_is_rejected(self, policy, authz):
        result = authz.authorize_sql(
            "SELECT x.employees FROM (SELECT * FROM company) x",
            context_for(policy, "alice"),
            FakeCatalog(),
        )
        assert not result.allowed
        assert result.code == "column_permission_denied"

    def test_explicit_denied_column_is_rejected(self, policy, authz):
        result = authz.authorize_sql(
            "SELECT employees FROM company", context_for(policy, "alice"), FakeCatalog()
        )
        assert not result.allowed
        assert result.code == "column_permission_denied"


class TestRowLevelSecurity:
    def test_equality_policy_preserves_existing_where(self, policy, authz):
        result = authz.authorize_sql(
            "SELECT name FROM company WHERE founded_year > 2000",
            context_for(policy, "alice"),
            FakeCatalog(),
        )
        assert result.allowed
        assert "founded_year > 2000" in result.sql
        assert "company.industry = " in result.sql
        assert "analyst-company-industry" in result.policy_ids

    def test_in_policy_is_applied_to_sales(self, policy, authz):
        result = authz.authorize_sql(
            "SELECT amount FROM sale_records WHERE amount > 10",
            context_for(policy, "alice"),
            FakeCatalog(),
        )
        assert result.allowed
        assert "sale_records.company_id IN (9001, 9002)" in result.sql
        assert "analyst-sale-company-scope" in result.policy_ids

    def test_policy_values_are_literalized_safely(self, policy, authz):
        context = RequestContext(
            Principal(
                "alice", frozenset({"analyst"}), attributes={"industry": "x' OR 1=1 --"}
            )
        )
        result = authz.authorize_sql("SELECT name FROM company", context, FakeCatalog())
        assert result.allowed
        assert "x'' OR 1=1 --" in result.sql

    def test_query_admin_bypasses_rls(self, policy, authz):
        result = authz.authorize_sql(
            "SELECT name FROM company", context_for(policy, "query-admin"), FakeCatalog()
        )
        assert result.allowed
        assert "industry" not in result.sql

    def test_principal_attributes_resolve_through_mapping_proxy(self, policy):
        principal = context_for(policy, "alice").principal
        assert resolve_attribute(principal, "principal.attributes.industry")
        assert resolve_attribute(principal, "principal.attributes.company_ids") == [9001, 9002]

    def test_protected_table_without_applicable_policy_fails_closed(self):
        policy = PermissionPolicy(
            roles={"reader": RolePolicy("reader", frozenset({"query:run"}))},
            principals={},
            acl_rules=[
                AclRule(
                    subjects=PolicySubjects(roles=frozenset({"reader"})),
                    resource_type="table",
                    table="company",
                    action="select",
                    effect="allow",
                )
            ],
            row_policies=[
                RowPolicy(
                    name="other-role-only",
                    subjects=PolicySubjects(roles=frozenset({"other"})),
                    tables=frozenset({"company"}),
                    actions=frozenset({"select"}),
                    column="company_id",
                    operator="eq",
                    value=1,
                )
            ],
        )
        context = RequestContext(
            Principal("reader-user", frozenset({"reader"})), source="test"
        )
        result = AuthorizationService(policy).authorize_sql(
            "SELECT company_id FROM company", context, FakeCatalog()
        )
        assert not result.allowed
        assert result.code == "row_policy_denied"


class TestQueryServiceIntegration:
    def test_query_service_rewrites_before_execution(self, policy, monkeypatch):
        service = QueryService(
            validator=SqlValidator(),
            authorizer=AuthorizationService(policy),
        )
        monkeypatch.setattr(service.executor, "get_schema", FakeCatalog().get_schema)
        captured = {}

        def fake_run(sql, max_rows, timeout_seconds, max_result_bytes=None):
            captured["sql"] = sql
            return QueryResult(
                columns=["name"],
                rows=[{"name": "demo"}],
                row_count=1,
                elapsed_seconds=0.01,
                truncated=False,
            )

        monkeypatch.setattr(service.executor, "run", fake_run)
        result = service.run("SELECT * FROM company", context_for(policy, "alice"))
        assert result["row_count"] == 1
        assert "employees" not in captured["sql"]
        assert "company.industry = " in captured["sql"]
        assert captured["sql"].endswith("LIMIT 200")

    def test_query_service_denies_before_execution(self, policy, monkeypatch):
        service = QueryService(
            validator=SqlValidator(),
            authorizer=AuthorizationService(policy),
        )
        called = {"value": False}

        def fake_run(*args, **kwargs):
            called["value"] = True
            raise AssertionError("越权 SQL 不应执行")

        monkeypatch.setattr(service.executor, "run", fake_run)
        result = service.run("SELECT employees FROM company", context_for(policy, "alice"))
        assert result["code"] in {"column_not_allowed", "column_permission_denied"}
        assert called["value"] is False


class TestResultGuard:
    def test_guard_allows_declared_columns(self):
        assert _result_column_guard(["name"], ("name",)) is None

    def test_query_service_denies_unexpected_result_columns(self, policy, monkeypatch):
        service = QueryService(
            validator=SqlValidator(),
            authorizer=AuthorizationService(policy),
        )
        monkeypatch.setattr(service.executor, "get_schema", FakeCatalog().get_schema)

        def fake_run(sql, max_rows, timeout_seconds, max_result_bytes=None):
            return QueryResult(
                columns=["employees"], rows=[{"employees": 1}], row_count=1,
                elapsed_seconds=0.01, truncated=False,
            )

        monkeypatch.setattr(service.executor, "run", fake_run)
        result = service.run("SELECT name FROM company", context_for(policy, "alice"))
        assert result["code"] == "result_column_guard_denied"

    def test_guard_rejects_unexpected_columns(self):
        error = _result_column_guard(["employees"], ("name",))
        assert error and "employees" in error

    def test_guard_skips_unknown_star_projection(self):
        assert _result_column_guard(["anything"], ()) is None

    def test_guard_accepts_expression_column_name(self):
        assert _result_column_guard(["COUNT(*)"], ("COUNT(*)",)) is None


class TestSchemaVisibility:
    def test_table_and_column_discovery_is_filtered(self, policy, monkeypatch):
        from mcp_server.tools import schema as schema_tools

        class FakeExecutor:
            def list_tables(self):
                return ["company", "sale_records", "user"]

            def get_schema(self, table_name):
                return FakeCatalog().get_schema(table_name)

        monkeypatch.setattr(schema_tools, "_executor", FakeExecutor())
        monkeypatch.setattr(
            schema_tools,
            "current_request_context",
            lambda source="schema": context_for(policy, "alice"),
        )

        assert schema_tools.list_tables() == ["company", "sale_records"]
        visible = schema_tools.get_schema("company")
        assert "employees" not in {row["Field"] for row in visible}


class TestStaticAuthentication:
    def test_static_token_maps_to_server_side_principal(self, policy):
        verifier = StaticTokenVerifier({"alice-token": "alice"})
        token = asyncio.run(verifier.verify_token("alice-token"))
        assert token is not None
        assert token.subject == "alice"
        assert "query:run" in token.scopes
        assert token.resource

    def test_unknown_static_token_is_rejected(self):
        verifier = StaticTokenVerifier({"alice-token": "alice"})
        assert asyncio.run(verifier.verify_token("wrong-token")) is None


class TestExternalJwt:
    @staticmethod
    def _keys():
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return private_pem, public_pem.decode("utf-8")

    @staticmethod
    def _encode(private_pem, **overrides):
        claims = {
            "iss": "https://issuer.example",
            "aud": "http://127.0.0.1:8000/mcp",
            "sub": "oidc-user",
            "exp": int(time.time()) + 300,
            "roles": ["analyst"],
            "scope": "query:run schema:read",
            "industry": "电子",
            "company_ids": [9001, 9002],
        }
        claims.update(overrides)
        return jwt.encode(claims, private_pem, algorithm="RS256")

    def test_valid_jwt_is_verified(self, monkeypatch):
        private_pem, public_pem = self._keys()
        monkeypatch.setattr(config, "AUTH_JWT_ISSUER", "https://issuer.example")
        monkeypatch.setattr(config, "AUTH_JWT_AUDIENCE", "http://127.0.0.1:8000/mcp")
        monkeypatch.setattr(config, "AUTH_JWT_JWKS_URL", "")
        monkeypatch.setattr(config, "AUTH_JWT_PUBLIC_KEY", public_pem)
        monkeypatch.setattr(config, "AUTH_JWT_SECRET", "")
        monkeypatch.setattr(config, "AUTH_JWT_ALGORITHMS", ["RS256"])

        token = asyncio.run(JwtTokenVerifier().verify_token(self._encode(private_pem)))
        assert token is not None
        assert token.subject == "oidc-user"
        assert token.scopes == ["query:run", "schema:read"]
        assert token.claims["roles"] == ["analyst"]

    def test_wrong_audience_is_rejected(self, monkeypatch):
        private_pem, public_pem = self._keys()
        monkeypatch.setattr(config, "AUTH_JWT_ISSUER", "https://issuer.example")
        monkeypatch.setattr(config, "AUTH_JWT_AUDIENCE", "expected-audience")
        monkeypatch.setattr(config, "AUTH_JWT_JWKS_URL", "")
        monkeypatch.setattr(config, "AUTH_JWT_PUBLIC_KEY", public_pem)
        monkeypatch.setattr(config, "AUTH_JWT_SECRET", "")
        monkeypatch.setattr(config, "AUTH_JWT_ALGORITHMS", ["RS256"])

        assert asyncio.run(
            JwtTokenVerifier().verify_token(self._encode(private_pem))
        ) is None

    def test_jwt_claims_use_server_side_role_mapping(self, monkeypatch):
        monkeypatch.setattr(config, "AUTH_PRINCIPAL_MODE", "mapped_claims")
        monkeypatch.setattr(config, "AUTH_JWT_ROLE_MAP", {"idp-analyst": "analyst"})
        monkeypatch.setattr(config, "AUTH_JWT_ROLES_CLAIM", "roles")
        monkeypatch.setattr(
            config,
            "AUTH_JWT_ATTRIBUTES",
            {
                "industry": "industry",
                "company_ids": "company_ids",
            },
        )
        access_token = AccessToken(
            token="jwt", client_id="client", subject="oidc-user",
            scopes=["query:run"],
            claims={"roles": ["idp-analyst"], "industry": "电子", "company_ids": [9001]}
        )
        principal = principal_for_access_token(access_token)
        assert principal is not None
        assert principal.has_role("analyst")
        assert principal.attributes["industry"] == "电子"
        assert principal.attributes["company_ids"] == [9001]

    def test_policy_mode_rejects_unmapped_jwt_subject(self, monkeypatch):
        monkeypatch.setattr(config, "AUTH_PRINCIPAL_MODE", "policy")
        access_token = AccessToken(
            token="jwt", client_id="client", subject="unmapped-user",
            scopes=["query:run"], claims={"roles": ["analyst"]}
        )
        assert principal_for_access_token(access_token) is None

    def test_claims_cannot_escalate_to_protected_roles(self, monkeypatch):
        monkeypatch.setattr(config, "AUTH_PRINCIPAL_MODE", "claims")
        monkeypatch.setattr(config, "AUTH_ALLOW_RAW_ROLE_CLAIMS", True)
        access_token = AccessToken(
            token="jwt", client_id="client", subject="claim-admin",
            scopes=["query:run"], claims={"roles": ["query_admin", "policy_admin"]}
        )
        assert principal_for_access_token(access_token) is None

    def test_jwks_url_verification(self, monkeypatch):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        numbers = private_key.public_key().public_numbers()
        encode = lambda value: base64.urlsafe_b64encode(
            value.to_bytes((value.bit_length() + 7) // 8, "big")
        ).rstrip(b"=").decode("ascii")
        jwks = {"keys": [{
            "kty": "RSA", "kid": "test-key", "use": "sig", "alg": "RS256",
            "n": encode(numbers.n), "e": encode(numbers.e),
        }]}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps(jwks).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setattr(config, "AUTH_JWT_ISSUER", "https://issuer.example")
            monkeypatch.setattr(config, "AUTH_JWT_AUDIENCE", "http://127.0.0.1:8000/mcp")
            monkeypatch.setattr(
                config, "AUTH_JWT_JWKS_URL",
                "http://127.0.0.1:%d/jwks" % server.server_port,
            )
            monkeypatch.setattr(config, "AUTH_JWT_PUBLIC_KEY", "")
            monkeypatch.setattr(config, "AUTH_JWT_SECRET", "")
            monkeypatch.setattr(config, "AUTH_JWT_ALGORITHMS", ["RS256"])
            claims = {
                "iss": "https://issuer.example",
                "aud": "http://127.0.0.1:8000/mcp",
                "sub": "jwks-user",
                "exp": int(time.time()) + 300,
            }
            token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "test-key"})
            verified = asyncio.run(JwtTokenVerifier().verify_token(token))
            assert verified is not None
            assert verified.subject == "jwks-user"
        finally:
            server.shutdown()
            thread.join(timeout=5)
