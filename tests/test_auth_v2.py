from __future__ import annotations

import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from bcp_engine.auth import (
    AuthError,
    BcpInvocation,
    ExplicitWindowsContextRequired,
    SecretResolver,
    SecretValue,
    authentication_identity,
    assert_bcp_has_no_password,
    build_bcp_invocation,
    build_odbc_connection_string,
    build_odbc_request,
    minimal_subprocess_environment,
)
from bcp_engine.bcp import BcpRunner
from bcp_engine.config import read_config
from bcp_engine.util import REDACTED, RedactingFormatter, SecretRedactor


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


class SecretProviderTests(unittest.TestCase):
    def test_prompt_is_cached_per_explicit_endpoint_credential(self):
        calls: list[str] = []
        resolver = SecretResolver(prompt=lambda label: calls.append(label) or "s enhá;}[]")
        auth = {
            "type": "sql",
            "username": "leitor",
            "password": {"provider": "prompt", "reference": None},
        }
        first = resolver.resolve_auth(auth, endpoint_name="origem")
        second = resolver.resolve_auth(auth, endpoint_name="origem")
        other_endpoint = resolver.resolve_auth(auth, endpoint_name="destino")
        self.assertIs(first, second)
        self.assertIsNot(first, other_endpoint)
        self.assertEqual(len(calls), 2)
        self.assertEqual(str(first), REDACTED)
        self.assertNotIn(first.reveal(), repr(first))

    def test_environment_reference_is_explicit_and_cached(self):
        environment = {"BCP_PASSWORD": "á senha com espaços;{}"}
        resolver = SecretResolver(environment=environment)
        spec = {"provider": "env", "reference": "BCP_PASSWORD"}
        first = resolver.resolve(spec, credential_key="origem")
        second = resolver.resolve(spec, credential_key="destino")
        self.assertIs(first, second)
        self.assertEqual(first.reveal(), environment["BCP_PASSWORD"])
        with self.assertRaisesRegex(AuthError, "não definida"):
            resolver.resolve(
                {"provider": "env", "reference": "AUSENTE"},
                credential_key="origem",
            )

    def test_windows_credential_manager_provider_is_injectable_and_cached(self):
        calls: list[str] = []

        def reader(reference: str) -> str:
            calls.append(reference)
            return "credencial Ünica;}"

        resolver = SecretResolver(credential_reader=reader)
        spec = {"provider": "windows_credential_manager", "reference": "BCP/teste"}
        one = resolver.resolve(spec, credential_key="origem")
        two = resolver.resolve(spec, credential_key="destino")
        self.assertIs(one, two)
        self.assertEqual(calls, ["BCP/teste"])


class RedactionTests(unittest.TestCase):
    def test_special_password_is_removed_from_messages_and_exception_chain(self):
        secret = 's enhá;Ü}[]"\''
        redactor = SecretRedactor()
        SecretValue(secret, redactor)
        self.assertEqual(redactor.redact(f"PWD={{{secret}}}; falhou"), f"PWD={REDACTED}; falhou")
        try:
            try:
                raise ValueError(f"driver devolveu {secret}")
            except ValueError as cause:
                raise RuntimeError(f"Password={secret}") from cause
        except RuntimeError as error:
            rendered = redactor.exception_text(error)
        self.assertNotIn(secret, rendered)
        self.assertGreaterEqual(rendered.count(REDACTED), 2)

    def test_logging_formatter_redacts_exception_traceback(self):
        secret = "unicode π senha; }"
        redactor = SecretRedactor()
        SecretValue(secret, redactor)
        formatter = RedactingFormatter("%(levelname)s %(message)s", redactor=redactor)
        try:
            raise RuntimeError(f"ODBC PWD={secret}")
        except RuntimeError:
            record = logging.LogRecord(
                "teste", logging.ERROR, __file__, 1, "falhou com %s", (secret,), sys.exc_info()
            )
        rendered = formatter.format(record)
        self.assertNotIn(secret, rendered)
        self.assertIn(REDACTED, rendered)


class OdbcBuilderTests(unittest.TestCase):
    def test_endpoint_dsn_is_used_only_by_odbc_while_bcp_uses_network_target(self):
        password = "senha-dsn"
        config = read_config(EXAMPLES / "config.auth-sql.json")
        config["source"]["odbc_dsn"] = "BCP_ORIGEM_LOCAL"
        config["source"]["port"] = 1544
        resolver = SecretResolver(environment={"BCP_SOURCE_SQL_PASSWORD": password})

        request = build_odbc_request(
            config["source"], config, resolver, endpoint_name="origem"
        )
        self.assertIn("DSN=BCP_ORIGEM_LOCAL", request.connection_string)
        self.assertNotIn("DRIVER=", request.connection_string)
        self.assertNotIn("SERVER=", request.connection_string)

        invocation = build_bcp_invocation(
            config["source"], config, resolver, endpoint_name="origem"
        )
        self.assertNotIn("-D", invocation.arguments)
        self.assertNotIn("BCP_ORIGEM_LOCAL", invocation.arguments)
        server_position = invocation.arguments.index("-S") + 1
        self.assertEqual(
            invocation.arguments[server_position], r"SQL-ORIGEM\INST01,1544"
        )

    def test_connection_timeout_is_propagated_to_odbc_and_bcp(self):
        config = read_config(EXAMPLES / "config.auth-sql.json")
        config["connection_timeout_seconds"] = 41
        config["sql_timeout_seconds"] = 59
        config["source"]["connection_timeout_seconds"] = 7
        config["source"]["sql_timeout_seconds"] = 11
        resolver = SecretResolver(
            environment={"BCP_SOURCE_SQL_PASSWORD": "segredo-temporario"}
        )
        request = build_odbc_request(
            config["source"], config, resolver, endpoint_name="origem"
        )
        self.assertEqual(request.connection_timeout_seconds, 7)
        self.assertEqual(request.sql_timeout_seconds, 11)
        invocation = build_bcp_invocation(
            config["source"], config, resolver, endpoint_name="origem"
        )
        login_timeout_index = invocation.arguments.index("-l") + 1
        self.assertEqual(invocation.arguments[login_timeout_index], "7")

    def test_integrated_connection_has_tls_and_no_user_or_password(self):
        config = read_config(EXAMPLES / "config.full.json")
        request = build_odbc_request(
            config["source"], config, SecretResolver(), endpoint_name="origem"
        )
        self.assertIn("Trusted_Connection={yes}", request.connection_string)
        self.assertIn("Encrypt={yes}", request.connection_string)
        self.assertIn("TrustServerCertificate={no}", request.connection_string)
        self.assertNotIn("UID=", request.connection_string)
        self.assertNotIn("PWD=", request.connection_string)
        self.assertFalse(request.requires_windows_context)

    def test_sql_connection_escapes_value_and_repr_is_redacted(self):
        password = "s enha;Ü} especial"
        config = read_config(EXAMPLES / "config.auth-sql.json")
        resolver = SecretResolver(environment={"BCP_SOURCE_SQL_PASSWORD": password})
        request = build_odbc_request(
            config["source"], config, resolver, endpoint_name="origem"
        )
        self.assertIn("UID={login_leitura}", request.connection_string)
        self.assertIn("PWD={", request.connection_string)
        self.assertNotIn(password, repr(request))
        self.assertNotIn(password, request.redacted_connection_string)
        self.assertIn(f"PWD={REDACTED}", request.redacted_connection_string)

    def test_sql_builder_requires_resolved_password(self):
        config = read_config(EXAMPLES / "config.auth-sql.json")
        with self.assertRaisesRegex(AuthError, "Senha SQL"):
            build_odbc_connection_string(config["source"], config)

    def test_explicit_windows_credentials_never_become_uid_password(self):
        config = read_config(EXAMPLES / "config.auth-windows-credential.json")
        resolver = SecretResolver(
            prompt=lambda _: "origem secreta",
            credential_reader=lambda _: "destino secreto",
        )
        endpoint = config["source"]
        request = build_odbc_request(endpoint, config, resolver, endpoint_name="origem")
        self.assertTrue(request.requires_windows_context)
        self.assertEqual(request.windows_context.principal, r"DOMINIO_A\svc_leitura")
        self.assertIn("Trusted_Connection={yes}", request.connection_string)
        self.assertNotIn("UID=", request.connection_string)
        self.assertNotIn("PWD=", request.connection_string)
        with self.assertRaises(ExplicitWindowsContextRequired):
            build_odbc_connection_string(endpoint, config, password="não usar")


class BcpAuthenticationTests(unittest.TestCase):
    def test_bcp_invocation_uses_shared_resolved_executable(self):
        config = read_config(EXAMPLES / "config.auth-sql.json")
        resolver = SecretResolver(
            environment={"BCP_SOURCE_SQL_PASSWORD": "segredo-temporario"}
        )
        expected = r"C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\180\Tools\Binn\bcp.exe"
        with patch(
            "bcp_engine.auth.resolve_bcp_executable", return_value=expected
        ) as resolve:
            invocation = build_bcp_invocation(
                config["source"], config, resolver, endpoint_name="origem"
            )

        self.assertEqual(invocation.executable, expected)
        resolve.assert_called_once_with(config["bcp_executable"])

    def test_sql_login_may_equal_password_without_using_password_switch(self):
        secret = "u684"
        invocation = BcpInvocation(
            endpoint_name="origem",
            executable="bcp",
            arguments=("dbo.t", "out", "arquivo.bcp", "-U", "u684"),
            authentication_type="sql",
            password_channel=SecretValue(secret),
        )
        assert_bcp_has_no_password(invocation, secret)
        self.assertNotIn("-P", invocation.arguments)

        leaked = invocation.with_operation("SELECT 'u684'")
        with self.assertRaisesRegex(AuthError, "Senha encontrada"):
            assert_bcp_has_no_password(leaked, secret)

    def test_sql_bcp_uses_private_channel_without_password_argument(self):
        password = "s enha;Unicode-ç}[]"
        config = read_config(EXAMPLES / "config.auth-sql.json")
        resolver = SecretResolver(environment={"BCP_SOURCE_SQL_PASSWORD": password})
        invocation = build_bcp_invocation(
            config["source"], config, resolver, endpoint_name="origem"
        )
        self.assertTrue(invocation.requires_private_password_channel)
        self.assertFalse(invocation.requires_windows_context)
        self.assertIn("-U", invocation.arguments)
        self.assertNotIn("-P", invocation.arguments)
        self.assertTrue(all(password not in argument for argument in invocation.argv))
        self.assertNotIn(password, repr(invocation))
        assert_bcp_has_no_password(invocation, password)

    def test_explicit_windows_bcp_requires_context_adapter(self):
        config = read_config(EXAMPLES / "config.auth-windows-credential.json")
        resolver = SecretResolver(
            prompt=lambda _: "origem secreta",
            credential_reader=lambda _: "destino secreta",
        )
        invocation = build_bcp_invocation(
            config["source"], config, resolver, endpoint_name="origem"
        )
        self.assertTrue(invocation.requires_windows_context)
        self.assertFalse(invocation.requires_private_password_channel)
        self.assertIn("-T", invocation.arguments)
        self.assertNotIn("-U", invocation.arguments)
        self.assertNotIn("-P", invocation.arguments)
        with self.assertRaisesRegex(AuthError, "WindowsContextAdapter"):
            BcpRunner().preflight(invocation)

    def test_authentication_identity_has_no_secret_reference(self):
        config = read_config(EXAMPLES / "config.auth-sql.json")
        identity = authentication_identity(config["source"]["authentication"])
        self.assertEqual(identity["type"], "sql")
        self.assertEqual(identity["configured_principal"], "login_leitura")
        rendered = repr(identity)
        self.assertNotIn("BCP_SOURCE_SQL_PASSWORD", rendered)
        self.assertNotIn("senha", rendered.casefold())

    def test_minimal_environment_drops_secret_and_unrelated_variables(self):
        source = {
            "PATH": r"C:\bin",
            "SystemRoot": r"C:\Windows",
            "TEMP": r"C:\Temp",
            "BCP_SOURCE_SQL_PASSWORD": "não herdar",
            "OUTRA_VARIAVEL": "não herdar",
        }
        result = minimal_subprocess_environment(source)
        self.assertEqual(result["PATH"], r"C:\bin")
        self.assertEqual(result["SystemRoot"], r"C:\Windows")
        self.assertEqual(result["TEMP"], r"C:\Temp")
        self.assertNotIn("BCP_SOURCE_SQL_PASSWORD", result)
        self.assertNotIn("OUTRA_VARIAVEL", result)


if __name__ == "__main__":
    unittest.main()
