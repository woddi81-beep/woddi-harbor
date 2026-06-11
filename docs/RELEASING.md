# Release Process

1. Run `woddi-harbor production-check`.
2. Run tests, Ruff, Mypy and `pip-audit`.
3. Update `CHANGELOG.md` and the version in `pyproject.toml`.
4. Add `docs/RELEASE_NOTES_vX.Y.Z.md` and reference it from the release workflow.
5. Create a signed or annotated tag: `git tag -a vX.Y.Z -m "vX.Y.Z"`.
6. Push the tag. GitHub Actions builds the package and publishes the release with
   wheel, source archive and the maintained release notes.

Never publish `config/users.local.json`, `data/secrets/` or runtime databases.
