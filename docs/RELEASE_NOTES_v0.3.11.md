# Woddi Harbor v0.3.11

OpenStack token authentication now scopes unscoped Keystone tokens to the
configured project. The admin dialog includes the project domain, defaulting to
`Default`, and Harbor uses the `v3token` authentication plugin.

After upgrading, open the OpenStack dialog, confirm the project and project
domain, save, and restart the OpenStack module.
