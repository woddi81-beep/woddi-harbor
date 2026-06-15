# Woddi Harbor v0.5.2

The production wrapper now reports the checked-out source version directly,
without requiring an activated virtual environment or a global command:

```bash
./harbor.sh version
./harbor.sh --version
./harbor.sh -V
./harbor.sh version --short
```

All internal CLI commands can now be called directly through the wrapper, for
example `./harbor.sh status` or `./harbor.sh module list`. The older
`./harbor.sh cli ...` form remains available for compatibility.

All operator-facing documentation and runtime hints now use `harbor.sh` as the
single production entry point. The installation how-to no longer pins the
obsolete `v0.3.8` release.
