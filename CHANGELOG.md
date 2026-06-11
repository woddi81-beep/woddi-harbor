# Changelog

## 0.2.0 - 2026-06-10

### Added

- Separate streaming chat and administration web applications
- Persistent chat sessions, audit events and durable SQLite job queue
- User, role, module and tool permission administration
- Managed local and Git document sources with quality gates
- MCP package and instance lifecycle for HTTP, process, systemd and container drivers
- MCP install, start, stop, restart, upgrade and rollback workflows
- Backup/restore, backup timer and production readiness gate
- Prometheus metrics, Grafana dashboard and load-test thresholds
- Nginx and Caddy TLS reverse-proxy templates
- CI for Python 3.10 and 3.12, release packaging, Ruff, Mypy and dependency audit
- Product, SLO, privacy, security, installation, upgrade and operations documentation

### Changed

- API defaults to four workers for the 128 GB / multi-socket target host
- Background work moved from process-local threads to a dedicated persistent worker
- Web assets are packaged into the Python wheel
- SQLite connections are explicitly closed and schema migration version 2 is applied

### Security

- Fail-closed authentication when no active user exists
- PBKDF2 password hashes and role-based authorization
- Per-user module and tool allowlists
- Internal bearer authentication between Harbor and local workers
- Secret redaction, same-origin write protection and hardened systemd units

### Known Limitations

- The checked-in operation and customer document sources are placeholders and do not
  pass the production source-quality gate.
- Harbor 0.2.0 is designed for one Harbor host with multiple API workers. Multi-host
  high availability requires an external state database in a later release.

## 0.1.0

- Initial Harbor control plane, local search workers and MCP integration
