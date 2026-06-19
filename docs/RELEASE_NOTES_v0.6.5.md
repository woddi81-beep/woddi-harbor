# Woddi Harbor v0.6.5

Patch release for OpenStack token handling.

## Fixed

- Project-scoped OpenStack tokens without a Keystone service catalog no longer
  fail MCP authentication validation.
- Truly unscoped tokens are still rejected with structured diagnostics.
- Health and tool results include a warning when the active token has no service
  catalog, because SDK endpoint discovery may still fail for individual
  OpenStack services.

## Verification

- `python -m pytest tests/test_mcp_backends.py tests/test_modules.py`
- `python -m pytest`
