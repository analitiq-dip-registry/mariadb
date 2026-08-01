"""MariaDB connector - dialect + connector class for the Analitiq CDK.

Everything MariaDB-specific lives here: backtick quoting, the
stage-then-merge write hooks (``CREATE TEMPORARY TABLE ... LIKE`` for the
stage, ``INSERT ... SELECT ... ON DUPLICATE KEY UPDATE`` for the upsert -
MariaDB has no MERGE and no ON CONFLICT), the aiomysql single-parameter
TLS surface with a post-connect enforcement probe, the UTC session pin,
and the fractional-second CURRENT_TIMESTAMP(6) default. Column types for
the write direction are governed entirely by
``definition/type-map-write.json``; this module ships no Python
type-rendering table.

Transport: async SQLAlchemy ``mariadb+aiomysql``. The ``mariadb`` dialect
name is not cosmetic - SQLAlchemy loads the MariaDB-only variant of the
MySQL dialect, which asserts the server's ``X.Y.Z-MariaDB`` version string
and refuses a MySQL server, so nothing here is ever gated on a MySQL
version probe against a MariaDB build. The DBAPI is aiomysql because the
engine's SQLAlchemy transport is async and MariaDB speaks the MySQL wire
protocol; MariaDB has no ADBC driver (the ADBC driver status page lists
none) and no Arrow Flight SQL endpoint, so the decision order stops at the
SQLAlchemy tier. MariaDB's native bulk load, ``LOAD DATA LOCAL INFILE``,
is deliberately NOT declared in ``sql_capabilities.bulk_load``: it
requires the client connection to be opened with ``local_infile=True``
(aiomysql defaults it to False) and the engine's SQLAlchemy transport
exposes no connect-argument channel other than the TLS hook, so no code
here could make the mechanism run. Batches therefore land via executemany,
which is the contract's documented meaning of an absent bulk declaration.

Registered under connector_id ``mariadb`` via the package entry points
(``analitiq.source_connectors`` / ``analitiq.destination_connectors``).
"""

from __future__ import annotations

import ssl
from collections.abc import Mapping, Sequence
from typing import Any

from cdk.sql.dialects import SqlDialect, TableAddress
from cdk.sql.exceptions import TlsVerificationError
from cdk.sql.generic import GenericSQLConnector
from cdk.transport_factory import ca_ssl_context

#: Modes whose declared meaning is "the session must be encrypted". These
#: are the ones verify_tls_state probes: aiomysql skips the TLS handshake
#: silently when the server does not advertise the capability, so the
#: connect argument alone guarantees nothing.
_ENCRYPTED_MODES = ("require", "verify-ca", "verify-full")

#: Modes that additionally verify the server certificate against the CA
#: bundle supplied through tls.ca_certificate. 'verify-full' is MariaDB's
#: own --ssl-verify-server-cert (certificate AND host name).
_VERIFY_MODES = ("verify-ca", "verify-full")

#: Modes that promise nothing about encryption, so the post-connect probe
#: has nothing to enforce. MariaDB has no opportunistic ("prefer") mode in
#: this connector's declared vocabulary, so 'none' is the only member.
_NO_PROMISE_MODES = ("none",)

#: The connector's declared ssl_mode enum, for error messages. MariaDB has
#: no --ssl-mode option of its own (MDEV-22129 closed unimplemented); this
#: vocabulary expresses its --ssl / --ssl-verify-server-cert pair.
_ALL_MODES = ("none", "require", "verify-ca", "verify-full")

#: MariaDB's first-party probe for the established session: the Ssl_cipher
#: status variable is the cipher in use, empty for an unencrypted session.
_SSL_CIPHER_SQL = "SHOW STATUS LIKE 'Ssl_cipher'"


def _unverified_tls_context() -> ssl.SSLContext:
    """Encrypt-only TLS context (no certificate or host name checking).

    MariaDB's plain ``--ssl`` switch encrypts without verifying anything;
    verification is what ``--ssl-verify-server-cert`` (this connector's
    verify-ca / verify-full) adds. Building the context here rather than
    passing a bare ``True`` keeps 'require' explicit about what it does
    and does not promise.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _canonical_mode(mode: str) -> str:
    """Fold a stored ssl_mode to the connector's declared spelling.

    Both TLS hooks normalize through here, and that shared fold is the
    point: when only one of them recognized a case variant, the mode that
    fails the connect-argument hook would have been waved through the
    post-connect probe as "no promise made" - an uppercase 'REQUIRE'
    silently bypassing TLS enforcement.
    """
    return (mode or "").strip().lower()


def _status_value(row: Any) -> str:
    """Read the Value column out of one ``SHOW STATUS LIKE`` row.

    Stripped: Ssl_cipher is empty for an unencrypted connection, and a
    value that is only whitespace is not a cipher - a truthiness test on
    the raw string would read it as one.
    """
    if row is None:
        return ""
    if isinstance(row, Mapping):
        value = row.get("Value")
    elif len(row) > 1:
        value = row[1]
    else:
        value = None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    return "" if value is None else str(value).strip()


class MariaDBDialect(SqlDialect):
    """MariaDB SQL strategy: backtick quoting, ODKU upserts, aiomysql TLS."""

    name = "mariadb"

    #: MariaDB reads "..." as a string literal, not an identifier (its
    #: default sql_mode does not include ANSI_QUOTES).
    quote_char = "`"

    #: MariaDB ships the sys schema from 10.6 onward; the other three exist
    #: in every supported release.
    system_schemas = ("information_schema", "mysql", "performance_schema", "sys")

    #: MariaDB identifiers are capped at 64 characters - the MariaDB limit,
    #: not the ANSI 63 the CDK base assumes. Declared here and as
    #: sql_capabilities.limits.max_identifier_len; the two channels must
    #: agree, because the CDK composes generated stage names within the
    #: declared budget while the conformance kit asserts the composed name
    #: against this class attribute.
    max_identifier_length = 64

    # ---- stage-then-merge write path ---------------------------------------
    def stage_table_sql(
        self, stage: TableAddress, target: TableAddress, *, temp: bool
    ) -> str:
        """``CREATE [TEMPORARY] TABLE`` *stage* shaped like *target*.

        ``CREATE TABLE ... LIKE`` is MariaDB's only documented column-copy
        form; it copies column definitions, attributes and indexes, so the
        stage binds every landed column exactly as the target would.

        Under the connector's declared ``stage.scope: temp`` the CDK hands
        this hook a schema-less stage address, so ``quote_table`` renders a
        bare identifier: a MariaDB TEMPORARY table lives in the session's
        own namespace, is invisible to other sessions, and is dropped when
        the session closes - which makes a leaked stage impossible even if
        a cycle dies between create and drop. The whole cycle runs on one
        connection, so the temporary table is visible to every step.
        """
        create = "CREATE TEMPORARY TABLE" if temp else "CREATE TABLE"
        return f"{create} {self.quote_table(stage)} LIKE {self.quote_table(target)}"

    def merge_statement_sql(
        self,
        stage: TableAddress,
        target: TableAddress,
        conflict_keys: Sequence[str],
        columns: Sequence[str],
    ) -> str:
        """Render the upsert from *stage* to *target*.

        MariaDB's merge form is ``INSERT ... ON DUPLICATE KEY UPDATE``,
        which fires when a row would duplicate a value in the PRIMARY KEY
        or any UNIQUE index - the statement never names the keys itself, so
        *conflict_keys* selects which columns are excluded from the update
        set rather than composing an ON clause.

        The source is the stage table, referenced once, so no batch value
        is ever rendered into SQL text (values reach the stage as bound
        parameters) and MariaDB's restriction on re-opening a TEMPORARY
        table within one statement cannot arise. The SELECT's FROM clause
        holds only the stage, so its unqualified column names are
        unambiguous; assignment targets on the left of the update are
        resolved against the insert target by definition.

        New values are read through ``VALUES(col)``, which MariaDB
        documents as usable only in an ON DUPLICATE KEY UPDATE clause,
        where it returns the column values from the INSERT portion of the
        statement. MariaDB 10.3.3 added ``VALUE()`` as an unambiguous
        synonym for statements that also carry a table value constructor;
        this statement has none (its source is a SELECT), so the documented
        ``VALUES()`` form is used and stays compatible with every supported
        release.

        When every landed column is a conflict key there is nothing to
        update. An empty ``ON DUPLICATE KEY UPDATE`` clause is a syntax
        error, so the contract's insert-only degradation self-assigns one
        key column: matched rows are left byte-identical (a no-change
        update reports 0 affected rows) and no error is raised. It must be
        a *self*-assignment rather than ``= VALUES(key)`` - a row can match
        on a different unique index, and assigning the incoming value would
        then mutate the stored key.
        """
        target_ref = self.quote_table(target)
        stage_ref = self.quote_table(stage)
        column_list = ", ".join(self.quote_ident(c) for c in columns)
        keys = set(conflict_keys)
        update_columns = [c for c in columns if c not in keys]
        if update_columns:
            set_clause = ", ".join(
                f"{self.quote_ident(c)} = VALUES({self.quote_ident(c)})"
                for c in update_columns
            )
        else:
            key_ref = self.quote_ident(conflict_keys[0])
            set_clause = f"{key_ref} = {key_ref}"
        # Dialect-quoted identifiers only; batch values never enter this
        # text (they reach the stage as bound parameters).
        return (
            f"INSERT INTO {target_ref} ({column_list}) "  # nosec B608
            f"SELECT {column_list} FROM {stage_ref} "
            f"ON DUPLICATE KEY UPDATE {set_clause}"
        )

    # ---- session state -------------------------------------------------------
    def session_init_sql(self) -> list[str]:
        """Pin the session to UTC.

        MariaDB stores TIMESTAMP as seconds since the epoch and converts it
        through the session ``time_zone`` on the way in and out, while the
        wire value carries no offset and no Z. The initial ``time_zone`` is
        'SYSTEM', so without this pin a TIMESTAMP column would carry
        whatever wall clock the server happens to run and the read map's
        ``Timestamp(unit, UTC)`` canonicals would name the wrong instant.
        The numeric offset form needs no populated time-zone tables (a
        stock MariaDB has none until mariadb-tzinfo-to-sql is run).
        """
        return ["SET time_zone = '+00:00'"]

    def current_timestamp_default(self) -> str:
        """DEFAULT expression for server-stamped timestamps.

        The engine's ``_synced_at`` column is ``Timestamp(MICROSECOND, UTC)``,
        which the write map renders ``DATETIME(6)``; the default expression
        has to carry the same fractional-second precision as the column, so
        the bare ``CURRENT_TIMESTAMP`` is not usable here.
        """
        return "CURRENT_TIMESTAMP(6)"

    # ---- TLS ------------------------------------------------------------------
    def build_tls_connect_arg(self, mode: str, ca_pem: str | None) -> Any:
        """Interpret the connector's ssl_mode vocabulary for aiomysql.

        MariaDB has no ``--ssl-mode`` option (MDEV-22129 was closed
        unimplemented); its client TLS surface is ``--ssl`` plus
        ``--ssl-verify-server-cert`` and ``--ssl-ca``. aiomysql in turn
        takes its entire TLS configuration through the single ``ssl``
        connect parameter and documents no mode string, so the singular
        hook is the right one and both vocabularies have to be realized
        here as SSL contexts:

        * ``none`` -> no ssl argument at all (returning ``None`` makes the
          CDK omit the key), i.e. a genuinely unencrypted connection.
        * ``require`` -> an encrypt-only context, MariaDB's plain
          ``--ssl``. A supplied CA bundle is deliberately not used to
          silently upgrade it; verify-ca is the opt-in.
        * ``verify-ca`` / ``verify-full`` -> a context pinned to the
          supplied CA bundle, with host name checking only for
          verify-full (MariaDB's ``--ssl-verify-server-cert`` checks the
          certificate's identity as well). An empty bundle raises rather
          than downgrading.

        The engine performs no vocabulary validation before the dialect
        sees the value (the connector.json enum is control-plane only).
        Case is folded (a stored 'REQUIRE' is the declared require, and
        both hooks fold identically so they can never disagree about a
        given string); anything outside the declared vocabulary fails here
        rather than silently bypassing verification.

        Every encrypting mode differs from ``none`` only post-connect:
        aiomysql skips the handshake when the server does not advertise
        TLS, which :meth:`verify_tls_state` turns into a failure.
        """
        canonical = _canonical_mode(mode)
        if canonical in _VERIFY_MODES:
            if not ca_pem:
                raise ValueError(
                    f"{self.name}: ssl_mode {mode!r} verifies the server "
                    f"certificate and requires the ssl_ca_certificate "
                    f"connection input to be provided"
                )
            return ca_ssl_context(ca_pem, check_hostname=(canonical == "verify-full"))
        if canonical == "require":
            return _unverified_tls_context()
        if canonical == "none":
            return None
        raise ValueError(
            f"{self.name}: unsupported ssl_mode {mode!r}; expected one of "
            f"{', '.join(_ALL_MODES)} (matched case-insensitively)"
        )

    def verify_tls_state(self, dbapi_connection: Any, mode: str) -> None:
        """Fail a connection that promised encryption and did not get it.

        aiomysql sends the TLS capability flag only when the server
        advertises SSL support, and continues in plaintext otherwise - for
        every mode, silently. An active attacker who strips the server's
        advertised capability would therefore downgrade even verify-full to
        cleartext unless the established session is checked. MariaDB's own
        ``Ssl_cipher`` status variable holds the cipher in use and is empty
        for an unencrypted connection, which is exactly that check.

        Only ``none`` promises nothing, so only it passes without probing.

        An unrecognized mode raises rather than returning: this hook fails
        closed, so it can never be the reason a strict mode goes unchecked.
        """
        canonical = _canonical_mode(mode)
        if canonical in _NO_PROMISE_MODES:
            return
        if canonical not in _ENCRYPTED_MODES:
            raise TlsVerificationError(
                f"unrecognized ssl_mode={mode!r}: this dialect cannot decide "
                f"what the established session must satisfy, and treating an "
                f"unknown mode as 'nothing to check' is how a strict mode "
                f"gets silently downgraded to cleartext. Expected one of "
                f"{', '.join(_ALL_MODES)}."
            )
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(_SSL_CIPHER_SQL)
            row = cursor.fetchone()
        finally:
            cursor.close()
        if not _status_value(row):
            raise TlsVerificationError(
                f"ssl_mode={canonical!r} requires an encrypted connection, "
                f"but the MariaDB session reports an empty Ssl_cipher "
                f"(unencrypted). The server either does not support TLS or "
                f"did not negotiate it; the connection is refused rather "
                f"than continuing in cleartext."
            )


class MariaDBConnector(GenericSQLConnector):
    """MariaDB connector: the CDK SQL base wired to the MariaDB dialect."""

    dialect_class = MariaDBDialect
