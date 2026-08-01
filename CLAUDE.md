---
name: mariadb
description: >
  MariaDB relational database (open-source MySQL fork) read and written over the
  async SQLAlchemy mariadb+aiomysql transport
type: database
---

# MariaDB

MariaDB is a community-developed, open-source relational database and a drop-in
fork of MySQL. This connector reads structured data, discovers schemas, tables,
and columns, and writes data back as a destination, over the MySQL wire protocol
using the async SQLAlchemy `mariadb+aiomysql` transport.

## Authentication

### Database credentials (`auth.type: db`)
- Client app required: no
- Credentials: `username` + `password`, supplied per connection
- Transport: SQLAlchemy `mariadb+aiomysql` (async; aiomysql DBAPI, which builds
  on pure-Python PyMySQL — default port `3306`)
- TLS: optional via `ssl_mode` — one of `none` (default), `require`, `verify-ca`,
  `verify-full`. `verify-ca` / `verify-full` require a PEM-encoded CA certificate
  in `ssl_ca_certificate`.

## Post-Auth Steps

None required. Once the connection is active, `resource_discovery`
(`information_schema` strategy) enumerates schemas, tables, and columns
automatically and produces connection-scoped endpoints and a type map.

## Available Endpoints

This is a database connector — it ships no static endpoints. Resources (tables
and views) are discovered at runtime from `information_schema`. Native column
types map to canonical Arrow types via `definition/type-map-read.json`; the
write path renders canonical Arrow types back into native DDL types via
`definition/type-map-write.json`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| _discovered at runtime_ | read | Tables and views enumerated from `information_schema` |
| _selected at pipeline setup_ | write | Tables created and upserted into by the stage-then-merge write path |

## Rate Limits

Not applicable. MariaDB imposes no API-style request quota; concurrency is
bounded by the server's `max_connections` and available resources.

## Write Path

The connector is registered under both `analitiq.source_connectors` and
`analitiq.destination_connectors`. Writes are stage-then-merge:
`CREATE TEMPORARY TABLE <stage> LIKE <target>` for the stage, batches landed via
`executemany`, then
`INSERT INTO <target> (...) SELECT ... FROM <stage> ON DUPLICATE KEY UPDATE ...`
for the upsert — MariaDB has no `MERGE` and no `ON CONFLICT`, so the upsert fires
on the PRIMARY KEY or any UNIQUE index rather than on named conflict keys. New
values are read through `VALUES(col)`. Server-stamped timestamp defaults use
`CURRENT_TIMESTAMP(6)` to match the `DATETIME(6)` rendering.

## Caveats

- The `mariadb` dialect name is load-bearing: SQLAlchemy loads its MariaDB-only
  variant, asserts the server's `X.Y.Z-MariaDB` version string and refuses a
  plain MySQL server. Use the `mysql` connector for MySQL servers.
- The transport is async (`aiomysql`); aiomysql builds on pure-Python PyMySQL, so
  no system C library is required. `pymysql` is pinned `<1.2` because aiomysql
  still passes the deprecated positional argument to `Connection.ping()`.
  `cryptography` is deliberately not required (MariaDB does not ship MySQL 8's
  `caching_sha2_password` plugin).
- Prefer `127.0.0.1` over `localhost`: the aiomysql driver always connects over
  TCP, while other MariaDB clients special-case `localhost` into a Unix socket.
- `ssl_mode` defaults to `none`. MariaDB has no `--ssl-mode` option of its own
  (MDEV-22129 closed unimplemented), so the four modes express its `--ssl` /
  `--ssl-verify-server-cert` pair. Every mode above `none` is enforced
  post-connect via `SHOW STATUS LIKE 'Ssl_cipher'` and fails the connection when
  the session is not actually encrypted, because the driver falls back to
  plaintext silently when the server does not advertise TLS.
- Port must be an integer. There is no catalog level above the database:
  MariaDB uses "database" and "schema" interchangeably.
- Every new connection is pinned to UTC (`SET time_zone = '+00:00'`), so
  `TIMESTAMP` reads carry a well-defined instant regardless of the server's
  `time_zone`. `DATETIME` values are zoneless and unaffected, but
  `CURRENT_TIMESTAMP`/`NOW()` defaults evaluated on connector connections
  generate UTC wall-clock values.
- `tinyint(1)` is mapped to `Boolean` (the MySQL/MariaDB convention for a
  one-bit-width tinyint); wider `tinyint(n)` maps to `Int8` / `UInt8`.
- `TIME` columns are read as `Duration` canonicals (unit follows the declared
  fsp), not time-of-day types: MariaDB `TIME` is a signed duration
  (`-838:59:59` to `+838:59:59`).
- MariaDB natives with no MySQL counterpart — `UUID`, `INET4`, `INET6` — are read
  as `Utf8`. Spatial/geometry types (`geometry`, `point`, `polygon`, …) are
  mapped to `Binary` (returned as WKB).
- Write-path text columns are capped: the `Utf8` canonical renders
  `VARCHAR(255)`. MariaDB rejects `TEXT`/`BLOB` in a key without a prefix length
  and the engine declares its keyless-stream dedup column as a `Utf8` primary
  key, so one rendering serves both roles. Longer values fail loudly under strict
  mode, and many string columns can exceed the 65,535-byte row limit.
  `LargeUtf8` renders `LONGTEXT` for genuinely long text.
- Bulk load is not used and not declared. `LOAD DATA LOCAL INFILE` needs the
  client connection opened with `local_infile=True`, which aiomysql defaults to
  off and the engine's SQLAlchemy transport exposes no channel to set; batches
  land via `executemany`.
- No ADBC driver and no Arrow Flight SQL endpoint exist for MariaDB, so the
  SQLAlchemy transport is the only supported path.
- Resource discovery excludes the `information_schema`, `mysql`,
  `performance_schema` and `sys` schemas.
