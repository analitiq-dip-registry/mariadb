"""Test bootstrap for the flat-layout connector package.

Two jobs, both required before any test module imports:

1. Stub the engine-private ``cdk`` modules so ``connector.py`` imports
   without the engine installed. Only the surface connector.py touches is
   mirrored; ``TlsVerificationError`` is a real class so tests can
   raise/catch it.

2. Import the package under its real distribution name. The repo root IS
   the wheel package directory (``package-dir`` maps
   ``analitiq_connector_mariadb`` to ``.``), so the root ``__init__.py``
   holds a relative import that only works inside a package. pytest's
   Package collector imports that file as a bare top-level module named
   ``__init__`` and would crash on the relative import — pre-importing the
   real package here and aliasing the collector's module name to it turns
   that import into a cache hit on the correctly-constructed package.
"""

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

# ---- 1. engine-private stubs (before the package import below) ----------


@dataclass(frozen=True)
class TableAddress:
    """Mirror of cdk.sql.dialects.TableAddress (table, schema, catalog)."""

    table: str
    schema: str = ""
    catalog: str = ""


class _SqlDialect:
    """Minimal base so MariaDBDialect inherits correctly in test isolation.

    Provides the identifier-quoting surface the write-path renderers call
    (``quote_ident`` / ``quote_table``), mirroring the real CDK base's
    behavior so the render tests exercise real composition logic.
    """

    name: str = ""
    quote_char: str = '"'

    def quote_ident(self, name: str) -> str:
        return f"{self.quote_char}{name}{self.quote_char}"

    def quote_table(self, address: TableAddress) -> str:
        parts = [address.table]
        if address.schema:
            parts.insert(0, address.schema)
        return ".".join(self.quote_ident(p) for p in parts)


class _GenericSQLConnector:
    pass


class TlsVerificationError(Exception):
    """Mirror of cdk.sql.exceptions.TlsVerificationError for test isolation."""


_transport_factory = MagicMock()
_transport_factory.ca_ssl_context = MagicMock()

_cdk_sql_dialects = MagicMock()
_cdk_sql_dialects.SqlDialect = _SqlDialect
_cdk_sql_dialects.TableAddress = TableAddress

_cdk_sql_exceptions = MagicMock()
_cdk_sql_exceptions.TlsVerificationError = TlsVerificationError

_cdk_sql_generic = MagicMock()
_cdk_sql_generic.GenericSQLConnector = _GenericSQLConnector

sys.modules.setdefault("cdk", MagicMock())
sys.modules.setdefault("cdk.sql", MagicMock())
sys.modules.setdefault("cdk.sql.dialects", _cdk_sql_dialects)
sys.modules.setdefault("cdk.sql.exceptions", _cdk_sql_exceptions)
sys.modules.setdefault("cdk.sql.generic", _cdk_sql_generic)
sys.modules.setdefault("cdk.transport_factory", _transport_factory)

# ---- 2. import the flat-layout package properly -------------------------

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))  # `from connector import ...` in tests

_spec = importlib.util.spec_from_file_location(
    "analitiq_connector_mariadb",
    _root / "__init__.py",
    submodule_search_locations=[str(_root)],
)
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["analitiq_connector_mariadb"] = _pkg
_spec.loader.exec_module(_pkg)
# pytest's Package collector resolves the root __init__.py to a module
# literally named "__init__"; satisfy it from the cache.
sys.modules.setdefault("__init__", _pkg)
