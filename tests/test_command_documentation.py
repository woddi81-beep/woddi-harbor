from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_operator_docs_use_harbor_wrapper() -> None:
    docs = [
        ROOT / "README.md",
        *(path for path in (ROOT / "docs").glob("*.md") if not path.name.startswith("RELEASE_NOTES_")),
    ]

    for path in docs:
        content = path.read_text(encoding="utf-8")
        assert ".venv/bin/woddi-harbor" not in content, path
        assert "\nwoddi-harbor " not in content, path
        assert "`woddi-harbor " not in content, path
