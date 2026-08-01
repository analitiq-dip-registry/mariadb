# MariaDB

[![Status: unverified](https://img.shields.io/badge/status-unverified-orange)](https://github.com/analitiq-dip-registry)
[![Latest release](https://img.shields.io/github/v/release/analitiq-dip-registry/mariadb)](https://github.com/analitiq-dip-registry/mariadb/releases)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

Connects to a MariaDB relational database (an open-source fork of MySQL) to read structured data, discover its schemas, tables, and columns, and write data back as a pipeline destination.

## What is this?

This is a **connector** — a configuration that defines how to authenticate with MariaDB and what data endpoints are available for reading and writing. It does not move data by itself. Instead, it is used by the [Analitiq](https://analitiq-app.com) data integration platform or the open-source `analitiq-dip-registry` engine to set up data pipelines.

## How to use this connector

There are two ways to use this connector:

### Option 1 — Analitiq Cloud (no setup required)

All connectors from this registry are automatically available on [analitiq-app.com](https://analitiq-app.com). Simply log in, select the connector, and follow the on-screen instructions to connect your account.

### Option 2 — Open Source (self-hosted)

All connectors are open source and free to use. To get started:

1. Clone the [analitiq-dip-registry](https://github.com/analitiq-dip-registry) repository
2. Install the Claude plugin `analitiq-plugin-dataflow`
3. Launch Claude in the root directory of `analitiq-dip-registry`
4. Tell it: *"I need to move data from X to Y"*

The `analitiq-plugin-dataflow` plugin will automatically fetch the required connectors from the [Analitiq DIP Registry](https://github.com/analitiq-dip-registry) and set up the data flow pipeline for you.

## Prerequisites

Before you can connect, you need:

- A running MariaDB server reachable from Analitiq (host and port — default `3306`)
- The name of the database (schema) you want to read from
- A database user and password with privileges to read the target tables and `information_schema` (plus `CREATE`, `CREATE TEMPORARY TABLES`, `INSERT` and `UPDATE` on the target database if you use MariaDB as a destination)
- *(Optional, for TLS)* A PEM-encoded CA certificate when using `verify-ca` or `verify-full` SSL modes

## Authentication

MariaDB uses standard database credentials. You supply the **host**, **port**, **database**, **username**, and **password**; the connector opens a connection over the MySQL wire protocol using the async SQLAlchemy `mariadb+aiomysql` driver (the `mariadb` dialect name puts SQLAlchemy in MariaDB-only mode, and aiomysql is the async DBAPI it runs on).

TLS is configurable through the **SSL Mode** setting:

- `none` — plaintext, no encryption
- `require` — encrypt the connection without verifying the server certificate
- `verify-ca` — encrypt and verify the server certificate against the supplied CA
- `verify-full` — also verify the server hostname

For `verify-ca` and `verify-full`, paste a PEM-encoded CA certificate into the **SSL CA Certificate** field.

### How to get your credentials

1. Connect to your MariaDB server as an administrator (e.g. with `mysql` or `mariadb` CLI).
2. Create a dedicated user — read-only is enough when MariaDB is only a source:
   ```sql
   CREATE USER 'analitiq'@'%' IDENTIFIED BY 'a-strong-password';
   GRANT SELECT ON your_database.* TO 'analitiq'@'%';
   FLUSH PRIVILEGES;
   ```
3. Use that username and password, along with your server host, port, and database name, when configuring the connection.

   If you plan to use MariaDB as a **destination**, the user also needs write privileges on the target database:
   ```sql
   GRANT SELECT, INSERT, UPDATE, CREATE, CREATE TEMPORARY TABLES ON your_database.* TO 'analitiq'@'%';
   FLUSH PRIVILEGES;
   ```

## Available Endpoints

This is a database connector, so it does not ship a fixed list of endpoints. After the connection is activated, the connector discovers resources (tables and views) directly from `information_schema`. Native MariaDB column types are mapped to canonical Arrow types on the read path via `definition/type-map-read.json`; on the write path the reverse mapping — canonical Arrow type to the native DDL type used when the connector creates a table — lives in `definition/type-map-write.json`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| _discovered at runtime_ | read | Tables and views enumerated from `information_schema` |
| _selected at pipeline setup_ | write | Tables created and upserted into by the stage-then-merge write path |

## Writing data (destination)

The connector is registered as both a source and a destination. Batches are staged and merged rather than written directly:

1. A stage table is created with `CREATE TEMPORARY TABLE <stage> LIKE <target>`, so it binds every column exactly as the target does. Being `TEMPORARY`, it lives only in the connector's own session and cannot leak.
2. The batch lands in the stage via `executemany`.
3. The stage is merged into the target with `INSERT INTO <target> (...) SELECT ... FROM <stage> ON DUPLICATE KEY UPDATE ...` — MariaDB has no `MERGE` and no `ON CONFLICT`, so the upsert fires on the PRIMARY KEY or any UNIQUE index rather than on named keys.

## Limitations

- **Rate limits** — none imposed by the connector; concurrency is bounded by the server's `max_connections` and available resources.
- **MariaDB servers only** — the `mariadb` dialect name puts SQLAlchemy in MariaDB-only mode: it asserts the server's `X.Y.Z-MariaDB` version string and refuses a plain MySQL server. Use the `mysql` connector for those.
- **TLS certificates** — `verify-ca` / `verify-full` require a valid PEM-encoded CA certificate; otherwise the connection will fail.
- **SSL mode defaults to `none`** — MariaDB has no `--ssl-mode` client option of its own (MDEV-22129 was closed unimplemented), so the four modes here express its `--ssl` / `--ssl-verify-server-cert` pair. Every mode above `none` is enforced *post-connect* with a `SHOW STATUS LIKE 'Ssl_cipher'` probe and fails loudly if the session turned out unencrypted, because the driver falls back to plaintext silently when the server does not advertise TLS.
- **Sessions are pinned to UTC** — MariaDB converts `TIMESTAMP` through the session `time_zone`, so the connector issues `SET time_zone = '+00:00'` on every new connection. Retrieved instants are then correct regardless of the server's setting, but `CURRENT_TIMESTAMP`/`NOW()` defaults evaluated on connector connections generate UTC wall-clock values.
- **`TIME` maps to `Duration`, not time-of-day** — MariaDB `TIME` is a signed duration (`-838:59:59` to `+838:59:59`). Columns are read as `Duration` canonicals (unit follows the declared fsp) so negative and >24 h values survive intact.
- **Written text columns are capped at 255 characters** — on the write path the `Utf8` canonical renders `VARCHAR(255)`. MariaDB rejects `TEXT`/`BLOB` columns in a key without a prefix length, and the engine declares its keyless-stream dedup column as a `Utf8` primary key, so one rendering has to serve both roles. Longer values fail loudly under strict mode, and a table with many string columns can exceed MariaDB's 65,535-byte row limit. `LargeUtf8` renders `LONGTEXT` and is the escape hatch for genuinely long text.
- **Bulk load is not used** — MariaDB's native `LOAD DATA LOCAL INFILE` requires the client connection to be opened with `local_infile=True`, which aiomysql defaults to off and the engine's SQLAlchemy transport exposes no channel to set. Batches land via `executemany` instead.
- **MariaDB-only natives are read as text** — `UUID`, `INET4` and `INET6` (which have no MySQL counterpart) map to `Utf8`; spatial/geometry types map to `Binary` (WKB).
- **System schemas are hidden from discovery** — `information_schema`, `mysql`, `performance_schema` and `sys` are excluded from resource discovery.

## For AI agents

This connector includes `CLAUDE.md` and `AGENTS.md` files — machine-readable references used by AI agents and agentic frameworks. They document authentication types, available endpoints, post-auth steps, and any caveats for programmatic use. Both files are kept identical — `CLAUDE.md` is for Claude Code, `AGENTS.md` is for other agent frameworks.

## Create a connector to any system

You can create a new connector to any API or database using Claude and the Analitiq connector builder plugin:

1. Install [Claude Code](https://claude.ai/code)
2. Install the connector builder plugin:
   ```
   claude plugin add analitiq-dip-registry/analitiq-plugin-connector-builder
   ```
3. Launch Claude and say: *"I want to create a connector for [system name]"*
4. The plugin will interview you about the system, research its API documentation, and generate the full connector with all required files

No coding required — the plugin handles authentication research, endpoint schema generation, and file creation automatically.

![Example of Claude building a connector](media/example_1.png)

## Contributing

All connectors in this registry are community-maintained and live at [github.com/analitiq-dip-registry](https://github.com/analitiq-dip-registry). To add new endpoints or improve an existing connector, install the [connector builder plugin](https://github.com/analitiq-dip-registry/analitiq-plugin-connector-builder) and follow its instructions.

## Links

- [MariaDB Documentation](https://mariadb.com/kb/en/)
- [Analitiq Cloud](https://analitiq-app.com)
- [Analitiq Engine (open source)](https://github.com/analitiq-ai/analitiq-engine)
- [Analitiq DIP Registry (open source)](https://github.com/analitiq-dip-registry)