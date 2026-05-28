---
name: mariadb
description: >
  MariaDB relational database (open-source MySQL fork) read over the SQLAlchemy mariadb+pymysql dialect
type: database
---

# MariaDB

MariaDB is a community-developed, open-source relational database and a drop-in
fork of MySQL. This connector reads structured data and discovers schemas,
tables, and columns over the MySQL wire protocol using the SQLAlchemy
`mariadb+pymysql` dialect.

## Authentication

### Database credentials (`auth.type: db`)
- Client app required: no
- Credentials: `username` + `password`, supplied per connection
- Transport: SQLAlchemy `mariadb+pymysql` (pure-Python PyMySQL driver, default port `3306`)
- TLS: optional via `ssl_mode` — one of `none`, `require`, `verify-ca`, `verify-full`.
  `verify-ca` / `verify-full` require a PEM-encoded CA certificate in `ssl_ca_certificate`.

## Post-Auth Steps

None required. Once the connection is active, `resource_discovery`
(`information_schema` strategy) enumerates schemas, tables, and columns
automatically and produces connection-scoped endpoints and a type map.

## Available Endpoints

This is a database connector — it ships no static endpoints. Readable resources
(tables and views) are discovered at runtime from `information_schema`; native
column types are mapped to canonical Arrow types via `definition/type-map.json`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| _discovered at runtime_ | read | Tables and views enumerated from `information_schema` |

## Rate Limits

Not applicable. MariaDB imposes no API-style request quota; concurrency is
bounded by the server's `max_connections` and available resources.

## Caveats

- `tinyint(1)` is mapped to `Boolean` (the MySQL/MariaDB convention for a
  one-bit-width tinyint); wider `tinyint(n)` maps to `Int8` / `UInt8`.
- The `mariadb+pymysql` driver is pure Python and needs no system C library.
- Spatial/geometry types (`geometry`, `point`, `polygon`, …) are mapped to
  `Binary` (returned as WKB).
