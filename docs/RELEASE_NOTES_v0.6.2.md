# Woddi Harbor v0.6.2

NetBox readiness now reports the real upstream state.

If tool metadata says that NetBox discovery is unavailable, Harbor marks
`module discover`, `module test` and `production-check` as failed instead of
accepting the static MCP tool list as a successful integration test.
