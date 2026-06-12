# Woddi Harbor v0.3.13

Harbor now binds to `0.0.0.0:9680` by default. Existing configurations created
with the previous implicit `127.0.0.1` default are migrated automatically on the
first start.

An address explicitly configured with `woddi-harbor server set` remains
unchanged, including an intentional loopback-only bind.
