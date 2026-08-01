"""Unit tests for MariaDBDialect.

Covers the stage-then-merge write-path renderers (stage_table_sql,
merge_statement_sql — including the conformance-required all-keys no-op
degradation), verify_tls_state (post-connect TLS probe), session_init_sql
(session time_zone pinning), and the declared identifier-length cap, which
the tier-1 conformance test reads off the class and which must equal
sql_capabilities.limits.max_identifier_len in definition/connector.json.

The TLS hook receives a raw DBAPI connection (for async drivers, SQLAlchemy's
asyncio adapter exposing the same cursor surface) and must raise
TlsVerificationError when a TLS-promising mode finds an unencrypted session.
"""

import json
import re
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from cdk.sql.dialects import TableAddress  # stubbed in conftest
from cdk.sql.exceptions import TlsVerificationError  # stubbed in conftest
from connector import MariaDBDialect

_DEFINITION = Path(__file__).resolve().parent.parent / "definition" / "connector.json"


def _make_dbapi_connection(cipher) -> MagicMock:
    """Mock DBAPI connection answering SHOW STATUS LIKE 'Ssl_cipher'."""
    conn = MagicMock()
    cursor = conn.cursor.return_value
    # row[0] = Variable_name, row[1] = Value
    cursor.fetchone.return_value = ("Ssl_cipher", cipher)
    return conn


class TestDeclaredLimits:
    def test_max_identifier_length_matches_the_declared_capability(self):
        """The class attribute and the declared cap are one fact.

        The CDK composes generated stage names within
        sql_capabilities.limits.max_identifier_len while the conformance
        kit asserts the composed name against the class attribute; a drift
        between them produces names the server truncates.
        """
        declared = json.loads(_DEFINITION.read_text())
        assert MariaDBDialect.max_identifier_length == 64
        assert (
            declared["sql_capabilities"]["limits"]["max_identifier_len"]
            == MariaDBDialect.max_identifier_length
        )

    def test_merge_form_matches_the_declared_capability(self):
        declared = json.loads(_DEFINITION.read_text())
        assert (
            declared["sql_capabilities"]["merge_form"] == "insert_on_duplicate_key"
        )


class TestVerifyTlsState:
    def setup_method(self):
        self.dialect = MariaDBDialect()

    # ------------------------------------------------------------------
    # Modes that must never query the server
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("mode", ["none", "NONE", " none "])
    def test_no_op_for_the_non_promising_mode(self, mode):
        conn = MagicMock()
        self.dialect.verify_tls_state(conn, mode)
        conn.cursor.assert_not_called()

    def test_unrecognized_mode_raises_rather_than_skipping_the_probe(self):
        """An unknown mode fails closed — it is never treated as "nothing to check".

        Skipping the probe for an unrecognized mode is how a strict mode
        gets silently downgraded to cleartext: build_tls_connect_arg would
        reject the string while this hook waved it through.
        """
        conn = MagicMock()
        with pytest.raises(TlsVerificationError, match="unrecognized ssl_mode"):
            self.dialect.verify_tls_state(conn, "prefer")
        conn.cursor.assert_not_called()

    # ------------------------------------------------------------------
    # Encrypting modes with an active cipher — must not raise
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
    def test_passes_when_encrypted(self, mode):
        conn = _make_dbapi_connection("AES128-SHA256")
        self.dialect.verify_tls_state(conn, mode)  # should not raise

    @pytest.mark.parametrize("mode", ["REQUIRE", "Verify-CA", "VERIFY-FULL"])
    def test_case_insensitive_strict_modes_still_probe(self, mode):
        """A miscased strict mode is the declared mode — and is enforced.

        Asserting only "does not raise" cannot tell a passed probe from a
        skipped one, which is exactly how a fail-open bypass hides.
        """
        conn = _make_dbapi_connection("TLS_AES_256_GCM_SHA384")
        self.dialect.verify_tls_state(conn, mode)  # should not raise
        conn.cursor.return_value.execute.assert_called_once_with(
            "SHOW STATUS LIKE 'Ssl_cipher'"
        )

    @pytest.mark.parametrize("mode", ["REQUIRE", "Verify-CA", "VERIFY-FULL"])
    def test_miscased_strict_mode_raises_when_not_encrypted(self, mode):
        conn = _make_dbapi_connection("")
        with pytest.raises(TlsVerificationError):
            self.dialect.verify_tls_state(conn, mode)

    # ------------------------------------------------------------------
    # Encrypting modes with no cipher — must raise TlsVerificationError
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
    def test_raises_when_not_encrypted(self, mode):
        conn = _make_dbapi_connection("")
        with pytest.raises(TlsVerificationError, match="ssl_mode="):
            self.dialect.verify_tls_state(conn, mode)

    @pytest.mark.parametrize("cipher", [" ", "  \t  "])
    def test_raises_when_cipher_is_whitespace_only(self, cipher):
        conn = _make_dbapi_connection(cipher)
        with pytest.raises(TlsVerificationError):
            self.dialect.verify_tls_state(conn, "require")

    def test_raises_when_cipher_value_is_null(self):
        """row[1] = None (DB NULL) is treated as unencrypted."""
        conn = _make_dbapi_connection(None)
        with pytest.raises(TlsVerificationError):
            self.dialect.verify_tls_state(conn, "require")

    def test_raises_when_status_row_missing(self):
        """No row from SHOW STATUS at all is treated as unencrypted."""
        conn = MagicMock()
        conn.cursor.return_value.fetchone.return_value = None
        with pytest.raises(TlsVerificationError):
            self.dialect.verify_tls_state(conn, "require")

    def test_error_message_names_the_mode(self):
        conn = _make_dbapi_connection("")
        with pytest.raises(TlsVerificationError, match="'require'"):
            self.dialect.verify_tls_state(conn, "require")

    # ------------------------------------------------------------------
    # DBAPI cursor discipline
    # ------------------------------------------------------------------

    def test_executes_show_status_via_dbapi_cursor(self):
        conn = _make_dbapi_connection("AES128-SHA256")
        self.dialect.verify_tls_state(conn, "require")
        cursor = conn.cursor.return_value
        cursor.execute.assert_called_once_with("SHOW STATUS LIKE 'Ssl_cipher'")
        cursor.close.assert_called_once()

    def test_cursor_closed_even_when_probe_raises(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.execute.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            self.dialect.verify_tls_state(conn, "require")
        cursor.close.assert_called_once()


class TestBuildTlsConnectArg:
    def setup_method(self):
        self.dialect = MariaDBDialect()

    def test_none_mode_omits_the_ssl_connect_argument(self):
        assert self.dialect.build_tls_connect_arg("none", None) is None

    def test_require_encrypts_without_verifying(self):
        context = self.dialect.build_tls_connect_arg("require", None)
        assert context is not None
        assert context.check_hostname is False

    def test_require_ignores_a_supplied_ca_rather_than_upgrading(self):
        """A CA bundle must not silently turn 'require' into 'verify-ca'."""
        context = self.dialect.build_tls_connect_arg("require", "-----BEGIN...")
        assert context.check_hostname is False

    @pytest.mark.parametrize("mode", ["verify-ca", "verify-full"])
    def test_verify_modes_require_a_ca_bundle(self, mode):
        with pytest.raises(ValueError, match="ssl_ca_certificate"):
            self.dialect.build_tls_connect_arg(mode, None)

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="unsupported ssl_mode"):
            self.dialect.build_tls_connect_arg("prefer", None)


class TestSessionInitSql:
    def test_pins_session_time_zone_to_utc(self):
        assert MariaDBDialect().session_init_sql() == ["SET time_zone = '+00:00'"]

    def test_is_deterministic(self):
        dialect = MariaDBDialect()
        assert dialect.session_init_sql() == dialect.session_init_sql()


class TestStageTableSql:
    def setup_method(self):
        self.dialect = MariaDBDialect()
        self.stage = TableAddress(table="_stage_orders", schema="shop")
        self.target = TableAddress(table="orders", schema="shop")

    def test_temp_stage_renders_create_temporary_table_like(self):
        sql = self.dialect.stage_table_sql(self.stage, self.target, temp=True)
        assert sql == (
            "CREATE TEMPORARY TABLE `shop`.`_stage_orders` LIKE `shop`.`orders`"
        )

    def test_non_temp_stage_omits_temporary_keyword(self):
        sql = self.dialect.stage_table_sql(self.stage, self.target, temp=False)
        assert sql.startswith("CREATE TABLE ")
        assert "TEMPORARY" not in sql

    def test_uses_unparenthesized_like(self):
        # MariaDB copies the table shape with bare `LIKE target`, never the
        # Postgres `(LIKE ... INCLUDING ...)` parenthesized form.
        sql = self.dialect.stage_table_sql(self.stage, self.target, temp=True)
        assert " LIKE `shop`.`orders`" in sql
        assert "(LIKE" not in sql


class TestMergeStatementSql:
    def setup_method(self):
        self.dialect = MariaDBDialect()
        self.stage = TableAddress(table="_stage_orders", schema="shop")
        self.target = TableAddress(table="orders", schema="shop")

    def test_renders_insert_select_on_duplicate_key_update(self):
        sql = self.dialect.merge_statement_sql(
            self.stage, self.target,
            conflict_keys=["id"], columns=["id", "total", "status"],
        )
        assert sql == (
            "INSERT INTO `shop`.`orders` (`id`, `total`, `status`) "
            "SELECT `id`, `total`, `status` FROM `shop`.`_stage_orders` "
            "ON DUPLICATE KEY UPDATE `total` = VALUES(`total`), "
            "`status` = VALUES(`status`)"
        )

    def test_update_set_excludes_conflict_keys(self):
        sql = self.dialect.merge_statement_sql(
            self.stage, self.target,
            conflict_keys=["id"], columns=["id", "total"],
        )
        # `id` is a conflict key → never appears in the UPDATE clause.
        update_clause = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
        assert "`id` =" not in update_clause
        assert "`total` = VALUES(`total`)" in update_clause

    def test_all_key_columns_degrades_to_self_assignment_noop(self):
        # Conformance-required: when every landed column is a conflict key,
        # an empty ON DUPLICATE KEY UPDATE clause is invalid SQL. The
        # renderer must emit a self-assignment no-op instead.
        sql = self.dialect.merge_statement_sql(
            self.stage, self.target,
            conflict_keys=["id"], columns=["id"],
        )
        assert not sql.rstrip().endswith("ON DUPLICATE KEY UPDATE")
        assert sql.endswith("ON DUPLICATE KEY UPDATE `id` = `id`")

    def test_composite_all_key_columns_self_assigns_one_key(self):
        sql = self.dialect.merge_statement_sql(
            self.stage, self.target,
            conflict_keys=["a", "b"], columns=["a", "b"],
        )
        assert sql.endswith("ON DUPLICATE KEY UPDATE `a` = `a`")

    def test_no_foreign_merge_tokens_leak_into_sql(self):
        # The rendered SQL must carry ON DUPLICATE KEY UPDATE — the declared
        # merge_form — and never MERGE or ON CONFLICT.
        for cols, keys in ([["id", "v"], ["id"]], [["id"], ["id"]]):
            sql = self.dialect.merge_statement_sql(
                self.stage, self.target, conflict_keys=keys, columns=cols,
            )
            assert re.search(r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", sql, re.I)
            assert not re.search(r"\bMERGE\b", sql, re.I)
            assert not re.search(r"\bON\s+CONFLICT\b", sql, re.I)
