# Woddi Harbor v0.5.3

A fresh checkout can now use the wrapper immediately. Commands that need the
Python CLI automatically create `.venv` and install Harbor on first use:

```bash
./harbor.sh version
./harbor.sh status
./harbor.sh module list
```

`./harbor.sh version` remains dependency-free and works before the first
installation.

The first installation also handles minimal Python virtual environments that do
not yet contain Setuptools or Wheel.
