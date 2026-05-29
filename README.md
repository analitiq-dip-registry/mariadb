# MariaDB

[![Status: unverified](https://img.shields.io/badge/status-unverified-orange)](https://github.com/analitiq-dip-registry)
[![Latest release](https://img.shields.io/github/v/release/analitiq-dip-registry/mariadb)](https://github.com/analitiq-dip-registry/mariadb/releases)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

Connects to a MariaDB relational database (an open-source fork of MySQL) to read structured data and discover its schemas, tables, and columns.

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
- A database user and password with privileges to read the target tables and `information_schema`
- *(Optional, for TLS)* A PEM-encoded CA certificate when using `verify-ca` or `verify-full` SSL modes

## Authentication

MariaDB uses standard database credentials. You supply the **host**, **port**, **database**, **username**, and **password**; the connector opens a connection over the MySQL wire protocol using the SQLAlchemy `mariadb+pymysql` driver.

TLS is configurable through the **SSL Mode** setting:

- `none` — plaintext, no encryption
- `require` — encrypt the connection without verifying the server certificate
- `verify-ca` — encrypt and verify the server certificate against the supplied CA
- `verify-full` — also verify the server hostname

For `verify-ca` and `verify-full`, paste a PEM-encoded CA certificate into the **SSL CA Certificate** field.

### How to get your credentials

1. Connect to your MariaDB server as an administrator (e.g. with `mysql` or `mariadb` CLI).
2. Create a read-only user, for example:
   ```sql
   CREATE USER 'analitiq'@'%' IDENTIFIED BY 'a-strong-password';
   GRANT SELECT ON your_database.* TO 'analitiq'@'%';
   FLUSH PRIVILEGES;
   ```
3. Use that username and password, along with your server host, port, and database name, when configuring the connection.

## Available Endpoints

This is a database connector, so it does not ship a fixed list of endpoints. After the connection is activated, the connector discovers readable resources (tables and views) directly from `information_schema`, and maps each column's native MariaDB type to a canonical Arrow type using `definition/type-map.json`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| _discovered at runtime_ | read | Tables and views enumerated from `information_schema` |

## Limitations

- **Rate limits** — none imposed by the connector; concurrency is bounded by the server's `max_connections` and available resources.
- **Read-oriented** — discovery and reads target tables, views, and `information_schema`. Grant the connection user only the privileges it needs.
- **TLS certificates** — `verify-ca` / `verify-full` require a valid PEM-encoded CA certificate; otherwise the connection will fail.

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