import importlib.util
from pathlib import Path

import pytest


def _load_cut_release_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "cut-release.py"
    spec = importlib.util.spec_from_file_location("cut_release", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_normalize_release_version_accepts_plain_and_v_prefixed_semver() -> None:
    cut_release = _load_cut_release_module()

    assert cut_release.normalize_release_version("0.0.5") == "0.0.5"
    assert cut_release.normalize_release_version("v0.0.5") == "0.0.5"


def test_normalize_release_version_rejects_missing_or_invalid_versions() -> None:
    cut_release = _load_cut_release_module()

    with pytest.raises(ValueError, match="release version is required"):
        cut_release.normalize_release_version("")
    with pytest.raises(ValueError, match="invalid release version"):
        cut_release.normalize_release_version("latest")


def test_parse_cut_release_args_accepts_optional_push_remote() -> None:
    cut_release = _load_cut_release_module()

    assert cut_release.parse_cut_release_args(["v0.0.5", "--push", "github"]) == (
        False,
        "0.0.5",
        "github",
    )
    assert cut_release.parse_cut_release_args(["minor", "--push=origin"]) == (
        False,
        "minor",
        "origin",
    )


def test_parse_cut_release_args_rejects_unknown_flags_and_extra_args() -> None:
    cut_release = _load_cut_release_module()

    with pytest.raises(ValueError, match="unknown option"):
        cut_release.parse_cut_release_args(["0.0.5", "--nope"])
    with pytest.raises(ValueError, match="unexpected extra argument"):
        cut_release.parse_cut_release_args(["0.0.5", "extra"])


def test_resolve_release_version_increments_requested_semver_part() -> None:
    cut_release = _load_cut_release_module()

    assert cut_release.resolve_release_version("0.3.0", "patch") == "0.3.1"
    assert cut_release.resolve_release_version("0.3.0", "minor") == "0.4.0"
    assert cut_release.resolve_release_version("0.3.0", "major") == "1.0.0"


def test_resolve_release_version_preserves_explicit_semver() -> None:
    cut_release = _load_cut_release_module()

    assert (
        cut_release.resolve_release_version("1.2.3-beta.1+build.7", "patch") == "1.2.4"
    )
    assert cut_release.resolve_release_version("1.2.3", "v1.2.4") == "1.2.4"


def test_is_dirty_worktree_treats_any_porcelain_output_as_dirty() -> None:
    cut_release = _load_cut_release_module()

    assert cut_release.is_dirty_worktree("") is False
    assert cut_release.is_dirty_worktree(" M pyproject.toml\n") is True
    assert cut_release.is_dirty_worktree("?? scripts/cut-release.py\n") is True


def test_rewrite_version_files_updates_duplicate_public_version_markers(
    tmp_path: Path,
) -> None:
    cut_release = _load_cut_release_module()
    project_root = tmp_path
    package_root = project_root / "src" / "codex_telegram"
    package_root.mkdir(parents=True)
    (package_root / "__init__.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8"
    )
    (project_root / "README.md").write_text(
        "[![Version](https://img.shields.io/badge/version-0.1.0-blue)]"
        "(https://github.com/alendit/codex-telegram/releases/tag/v0.1.0)\n",
        encoding="utf-8",
    )

    changed = cut_release.rewrite_version_files(project_root, "0.2.0")

    assert changed == [
        Path("src/codex_telegram/__init__.py"),
        Path("README.md"),
    ]
    assert (package_root / "__init__.py").read_text(encoding="utf-8") == (
        '__version__ = "0.2.0"\n'
    )
    assert "version-0.2.0-blue" in (project_root / "README.md").read_text(
        encoding="utf-8"
    )
    assert "releases/tag/v0.2.0" in (project_root / "README.md").read_text(
        encoding="utf-8"
    )


def test_release_commit_message_includes_codex_trailer_once() -> None:
    cut_release = _load_cut_release_module()

    message = cut_release.release_commit_message("0.2.0")

    assert message == (
        "Cut release v0.2.0\n\n" "Co-authored-by: Codex <noreply@openai.com>"
    )


def test_usage_shows_uv_run_cut_release_script() -> None:
    cut_release = _load_cut_release_module()

    assert "uv run scripts/cut-release.py patch" in cut_release.usage()
