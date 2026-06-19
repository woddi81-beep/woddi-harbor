# Changelog

## 0.6.5 - 2026-06-19

### Fixed

- OpenStack project-scoped tokens without a Keystone service catalog are now
  accepted instead of being rejected during MCP auth validation.
- OpenStack health and MCP tool responses now expose a catalog warning when the
  token has project context but no service catalog, so endpoint-discovery
  failures can be diagnosed without hiding the valid token scope.
- Tests now cover project-scoped/no-catalog tokens separately from truly
  unscoped tokens.

## 0.6.4 - 2026-06-19

### Added

- Persistent NetBox and OpenStack field catalogs under `data/runtime/field_cache`
- Admin UI field catalog dialog with refresh support for NetBox/OpenStack modules
- `GET /api/modules/{module_id}/fields` and
  `POST /api/modules/{module_id}/fields/refresh`
- `harbor.sh module fields MODULE_ID [--refresh]` for CLI inspection
- Structured OpenStack diagnostics with token scope, service catalog and
  credential-mode details
- Best-effort Git auto-update during `harbor.sh start` and systemd autostart
  (`HARBOR_AUTO_UPDATE=0` disables it)

### Fixed

- OpenStack worker health no longer references a missing `self`
- NetBox HTTP 401/403/404 responses are raised and reported as actionable
  upstream diagnostics
- OpenStack workers no longer inherit shared tokens or project settings from
  environment variables
- Token auth no longer passes separate project fields to the SDK; the project
  must come from the token itself

## 0.6.2 - 2026-06-15

### Fixed

- NetBox discovery and module tests now fail when the worker reports an
  unavailable upstream instead of treating the static tool list as success
- The production check performs NetBox MCP discovery in addition to local
  worker liveness

## 0.6.1 - 2026-06-15

### Fixed

- NetBox worker liveness no longer blocks on DNS, schema discovery or the
  upstream API, so slow or temporarily unavailable NetBox instances do not
  prevent the local worker from starting
- NetBox reachability remains covered by MCP discovery and module tests

## 0.6.0 - 2026-06-15

### Added

- OpenStack User-Tokens can be renewed and removed directly from the chat UI
- Token status identifies the current Harbor user without returning the token
- Per-user OpenStack backend and cache isolation with bounded idle eviction

### Changed

- OpenStack Auth URL, region and timeout remain shared infrastructure settings,
  while every Harbor user has a separate project-scoped token
- OpenStack workers start without cloud credentials and receive the current
  user's token only for that user's internal request
- Discovery, tests, direct calls and chat context use the authenticated user's
  OpenStack credential

### Security

- Legacy shared OpenStack token, password and application-credential secrets are
  removed during startup and configuration changes
- Token rotation invalidates only the affected user's SDK connection and cache

### Fixed

- `harbor.sh` now detects and refreshes a stale installed CLI after `git pull`
  before forwarding commands

## 0.5.3 - 2026-06-15

### Fixed

- Forwarded `harbor.sh` commands automatically install the local CLI when a
  checkout has no prepared virtual environment yet
- Fresh production checkouts no longer fail with a manual-install prerequisite
  for commands such as `./harbor.sh status`
- Fresh Python virtual environments use isolated build dependencies instead of
  failing on a missing `setuptools.build_meta`

## 0.5.2 - 2026-06-15

### Added

- Direct source-version reporting through `./harbor.sh version`,
  `./harbor.sh --version`, `./harbor.sh -V` and
  `./harbor.sh version --short`
- Consistent `harbor.sh` commands in operator documentation and runtime hints

### Fixed

- Removed the obsolete `v0.3.8` checkout from the production how-to
- Installation verification now rejects stale package metadata and no longer
  recommends an obsolete release

## 0.5.1 - 2026-06-15

### Added

- Version reporting in the internal Python CLI
- Runtime version reporting in `/api/health`
- One shared application version for package metadata, CLI, API and MCP clients

## 0.5.0 - 2026-06-15

### Changed

- OpenStack accepts only a project-scoped user token and derives the project
  context from that token
- NetBox uses anonymous access and remains strictly limited to read-only GET
  requests
- Harbor and `harbor.sh` bind to `127.0.0.1` by default
- Readiness and the production gate verify live LLM and integration health
- Response, discovery, dashboard and query caches are bounded and coalesce
  concurrent loads
- MCP instances restore their persisted desired state after a Harbor restart

### Removed

- OpenStack password, application-credential and project-rescoping paths
- NetBox token storage and authorization headers

## 0.4.2 - 2026-06-12

### Added

- Configurable OpenStack timeout from 5 to 600 seconds
- Separate timeout diagnostics for Keystone authentication, project discovery and SDK operations
- OpenStack health reports the active timeout

### Changed

- Project discovery uses the configured timeout instead of a fixed 15 seconds
- SDK requests retry one failed connection

## 0.4.1 - 2026-06-12

### Added

- OpenStack project-ID configuration for reliable Keystone token scoping
- Automatic project discovery for unscoped tokens with an empty service catalog
- Automatic scoping when exactly one project is accessible
- Clear project choices when an unscoped token can access multiple projects

### Changed

- OpenStack health reports the active scope strategy

## 0.4.0 - 2026-06-12

### Changed

- OpenStack MCP uses `openstacksdk` directly instead of spawning the `openstack` CLI
- OpenStack list/show tools use SDK compute, identity, image and network proxies
- Generic read-only OpenStack access is restricted to an SDK resource allowlist

### Added

- `openstacksdk` is a managed Harbor runtime dependency
- Production check reports a missing SDK when an OpenStack module is enabled

### Removed

- OpenStack CLI path lookup, subprocess execution and symlink requirements

## 0.3.14 - 2026-06-12

### Fixed

- `harbor.sh start` always passes an explicit listen address to Uvicorn
- Manual startup no longer falls back to stale loopback configuration
- `HARBOR_HOST` and `HARBOR_PORT` remain explicit runtime overrides

## 0.3.13 - 2026-06-12

### Changed

- Harbor now listens on `0.0.0.0` by default
- Legacy implicit `127.0.0.1` configurations migrate once to the external bind
- Explicit binds configured through `server set` remain unchanged

## 0.3.12 - 2026-06-12

### Changed

- OpenStack project and project domain are optional for project-scoped tokens
- Harbor only applies Keystone project scoping when a project name is configured
- Existing project-scoped tokens use `token` authentication without project variables

## 0.3.11 - 2026-06-12

### Fixed

- OpenStack token authentication uses the Keystone v3 token plugin
- OpenStack tokens can be scoped with project name and project domain
- The admin portal exposes project domain with the conventional `Default` value

## 0.3.10 - 2026-06-12

### Fixed

- `module call` accepts JSON both as a third positional argument and through `--payload`
- Existing NetBox and OpenStack command examples work as documented

## 0.3.9 - 2026-06-12

### Fixed

- OpenStack MCP workers locate `openstack` next to the active virtualenv Python
- OpenStack CLI errors include the exact installation command and expected path
- `OPENSTACK_CLI` can explicitly select a client binary

## 0.3.8 - 2026-06-12

### Added

- OpenStack token authentication with project, Identity URL and region configuration in the admin portal
- Private per-module secret storage with directory mode `0700` and file mode `0600`
- OpenStack token status without exposing the stored token to browsers or module status output

### Fixed

- Module diagnostics now report connection failures as structured results instead of raising `Errno 111`
- Local NetBox, OpenStack and SAP MCP modules can be inspected with `module discover`

## 0.3.7 - 2026-06-12

### Added

- Persistent `server set` and `server show` CLI commands
- Explicit external binding support for protected networks

### Changed

- `harbor.sh start` uses persisted server settings unless environment overrides are set

## 0.3.6 - 2026-06-12

### Fixed

- Workerless Docs and Maildir modules no longer require a network port
- Workerless search modules are excluded from local port-conflict validation
- Document source setup resets stale transport and endpoint fields

## 0.3.5 - 2026-06-12

### Fixed

- Local Docs and Maildir calls execute directly without an HTTP worker
- CLI document search works while Harbor and module processes are stopped
- Chat and administration use the same workerless local search path

## 0.3.4 - 2026-06-12

### Fixed

- Source synchronization reindexes local document modules without an HTTP worker
- Manual operation no longer fails source sync with connection refused
- Sync output reports whether reindexing used direct or transported execution

## 0.3.3 - 2026-06-12

### Added

- HTML documentation is normalized to visible text before indexing
- PNG documentation assets are preserved alongside Markdown and HTML
- Source quality reports distinguish searchable text from binary assets

## 0.3.2 - 2026-06-12

### Fixed

- Removed all hard-coded ASV documentation references from the active product
- Added host-local document source configuration in `config/sources.local.json`
- Added one-command setup for the production operation and customer Markdown repositories
- Document synchronization now excludes non-Markdown repository artifacts

## 0.3.1 - 2026-06-12

### Fixed

- Installation now rejects incomplete source checkouts before starting Harbor
- Post-install verification detects missing modules and foreign top-level `app` packages
- Production and shell installers verify the effective Python import origin

## 0.3.0 - 2026-06-11

### Added

- Real reference-document importer with source manifests and quality verification
- Resilient Ollama/OpenAI-compatible LLM health checks, retries and timeout controls
- Canonical interactive console through `./harbor.sh console`
- Idempotent `runtime stop-all` and `runtime uninstall` commands
- Own process-based MCP example with full discovery and tool-call flow
- Hardware release gate for the 128 GiB / four-socket production target
- Optional local TLS and authenticated Prometheus installers
- Structured admin forms and improved chat module selection and session handling

### Changed

- systemd, TLS and monitoring are explicitly optional runtime components
- Local module configuration is stored in ignored `config/modules.local.json`
- The versioned module default is empty and contains no deployment-specific endpoints
- Configuration locks are stored under `data/runtime/locks`
- Obsolete embedded web applications were removed in favor of packaged web assets

### Fixed

- Read-only systemd workers no longer fail while reading configuration
- Production installer keeps the source checkout editable and finds deployment assets
- CLI module failures no longer print full Python tracebacks
- Tests no longer leave document indexes in the production runtime directory

### Verification

- 56 automated tests, Ruff, compile checks, JavaScript syntax checks and dependency audit
- 10,000 health requests at concurrency 64: zero errors, p95 88.04 ms
- 5,000 readiness requests at concurrency 32: zero errors, p95 38.36 ms
- Controlled LLM and MCP failure/recovery tests

### Known Limitation

- The required 128 GiB / four-socket benchmark remains blocked until that host is
  available. The available host has 15.51 GiB RAM and one CPU socket; its results do
  not count as target-hardware certification.

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
