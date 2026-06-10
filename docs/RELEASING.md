# Release Process

1. Run `woddi-harbor production-check`.
2. Run tests, Ruff, Mypy and `pip-audit`.
3. Update `CHANGELOG.md` and the version in `pyproject.toml`.
4. Create a signed or annotated tag: `git tag -a vX.Y.Z -m "vX.Y.Z"`.
5. Push the tag. GitHub Actions builds the package and publishes the release.

Never publish `config/users.local.json`, `data/secrets/` or runtime databases.
