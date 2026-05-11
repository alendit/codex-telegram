#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

SEMVER_PATTERN = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
SEMVER_CORE_PATTERN = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
RELEASE_TYPES = {"major", "minor", "patch"}
VERSION_FILES = [
    Path("Dockerfile"),
    Path("pyproject.toml"),
    Path("scripts/build-image.sh"),
    Path("uv.lock"),
    Path("src/codex_telegram/__init__.py"),
    Path("README.md"),
]
CODEX_TRAILER = "Co-authored-by: Codex <noreply@openai.com>"


def usage() -> str:
    return "\n".join(
        [
            "Usage: uv run scripts/cut-release.py <major|minor|patch|version> [--push <remote>]",
            "",
            "Examples:",
            "  uv run scripts/cut-release.py patch",
            "  uv run scripts/cut-release.py minor --push origin",
            "  uv run scripts/cut-release.py 0.2.0",
            "  uv run scripts/cut-release.py v0.2.0 --push origin",
        ]
    )


def normalize_release_version(value: str | None) -> str:
    if value is None or value.strip() == "":
        raise ValueError("A release version is required.")

    trimmed = value.strip()
    normalized = trimmed[1:] if trimmed.startswith("v") else trimmed
    if not SEMVER_PATTERN.fullmatch(normalized):
        raise ValueError(f"invalid release version: {value}")

    return normalized


def normalize_release_target(value: str | None) -> str:
    if value is None or value.strip() == "":
        raise ValueError("A release version or bump type is required.")

    trimmed = value.strip()
    if trimmed in RELEASE_TYPES:
        return trimmed

    return normalize_release_version(trimmed)


def resolve_release_version(current_version: str, release_target: str) -> str:
    if release_target not in RELEASE_TYPES:
        return normalize_release_version(release_target)

    match = SEMVER_CORE_PATTERN.fullmatch(normalize_release_version(current_version))
    if match is None:
        raise ValueError(f"Invalid current package version: {current_version}")

    major, minor, patch = (int(part) for part in match.groups())
    if release_target == "major":
        return f"{major + 1}.0.0"
    if release_target == "minor":
        return f"{major}.{minor + 1}.0"
    if release_target == "patch":
        return f"{major}.{minor}.{patch + 1}"

    raise ValueError(f"Unsupported release type: {release_target}")


def parse_cut_release_args(argv: Sequence[str]) -> tuple[bool, str | None, str | None]:
    requested_target: str | None = None
    push_remote: str | None = None
    show_help = False

    index = 0
    while index < len(argv):
        arg = argv[index]

        if arg in {"--help", "-h"}:
            show_help = True
            index += 1
            continue

        if arg == "--push":
            try:
                remote = argv[index + 1]
            except IndexError as exc:
                raise ValueError("Expected a remote name after --push.") from exc
            if remote.startswith("-"):
                raise ValueError("Expected a remote name after --push.")
            push_remote = remote
            index += 2
            continue

        if arg.startswith("--push="):
            push_remote = arg.removeprefix("--push=")
            if push_remote == "":
                raise ValueError("Expected a remote name after --push=.")
            index += 1
            continue

        if arg.startswith("-"):
            raise ValueError(f"unknown option: {arg}")

        if requested_target is not None:
            raise ValueError(f"unexpected extra argument: {arg}")

        requested_target = arg
        index += 1

    if show_help:
        return True, None, push_remote

    return False, normalize_release_target(requested_target), push_remote


def is_dirty_worktree(porcelain_status: str) -> bool:
    return porcelain_status.strip() != ""


def release_commit_message(version: str) -> str:
    return f"Cut release v{version}\n\n{CODEX_TRAILER}"


def _replace_once(text: str, pattern: re.Pattern[str], replacement: str) -> str:
    next_text, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise ValueError(f"Expected exactly one match for {pattern.pattern}")
    return next_text


def rewrite_version_files(project_root: Path, version: str) -> list[Path]:
    changed: list[Path] = []

    dockerfile_path = project_root / "Dockerfile"
    dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
    next_dockerfile_text = _replace_once(
        dockerfile_text,
        re.compile(r"ARG VERSION=[0-9A-Za-z.+-]+"),
        f"ARG VERSION={version}",
    )
    if next_dockerfile_text != dockerfile_text:
        dockerfile_path.write_text(next_dockerfile_text, encoding="utf-8")
        changed.append(Path("Dockerfile"))

    build_image_path = project_root / "scripts" / "build-image.sh"
    build_image_text = build_image_path.read_text(encoding="utf-8")
    next_build_image_text = _replace_once(
        build_image_text,
        re.compile(r'CODEX_TELEGRAM_VERSION:=[0-9A-Za-z.+-]+'),
        f"CODEX_TELEGRAM_VERSION:={version}",
    )
    if next_build_image_text != build_image_text:
        build_image_path.write_text(next_build_image_text, encoding="utf-8")
        changed.append(Path("scripts/build-image.sh"))

    init_path = project_root / "src" / "codex_telegram" / "__init__.py"
    init_text = init_path.read_text(encoding="utf-8")
    next_init_text = _replace_once(
        init_text,
        re.compile(r'__version__ = "[^"]+"'),
        f'__version__ = "{version}"',
    )
    if next_init_text != init_text:
        init_path.write_text(next_init_text, encoding="utf-8")
        changed.append(Path("src/codex_telegram/__init__.py"))

    readme_path = project_root / "README.md"
    readme_text = readme_path.read_text(encoding="utf-8")
    next_readme_text = _replace_once(
        readme_text,
        re.compile(r"version-[0-9A-Za-z.+-]+-blue"),
        f"version-{version}-blue",
    )
    next_readme_text = _replace_once(
        next_readme_text,
        re.compile(r"releases/tag/v[0-9A-Za-z.+-]+"),
        f"releases/tag/v{version}",
    )
    next_readme_text = _replace_once(
        next_readme_text,
        re.compile(r"codex--telegram%3A[0-9A-Za-z.+-]+-blue"),
        f"codex--telegram%3A{version}-blue",
    )
    next_readme_text = _replace_once(
        next_readme_text,
        re.compile(r"docker pull ghcr\.io/alendit/codex-telegram:[0-9A-Za-z.+-]+"),
        f"docker pull ghcr.io/alendit/codex-telegram:{version}",
    )
    if next_readme_text != readme_text:
        readme_path.write_text(next_readme_text, encoding="utf-8")
        changed.append(Path("README.md"))

    return changed


def run(command: Sequence[str], *, cwd: Path, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        encoding="utf-8",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture and result.stdout is not None else ""


def has_staged_changes(project_root: Path) -> bool:
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=project_root,
        check=False,
    )
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    raise RuntimeError("Failed to inspect staged release changes.")


def cut_release(
    project_root: Path, release_target: str, push_remote: str | None
) -> None:
    porcelain_status = run(
        ["git", "status", "--porcelain"], cwd=project_root, capture=True
    )
    if is_dirty_worktree(porcelain_status):
        raise RuntimeError("Worktree must be clean before cutting a release.")

    current_version = run(
        ["uv", "version", "--short"], cwd=project_root, capture=True
    ).strip()
    version = resolve_release_version(current_version, release_target)
    if current_version == version:
        raise RuntimeError(f"pyproject.toml is already at version {version}.")

    existing_tag = run(
        ["git", "tag", "--list", f"v{version}"],
        cwd=project_root,
        capture=True,
    )
    if existing_tag.strip() != "":
        raise RuntimeError(f"Tag v{version} already exists.")

    run(["uv", "version", version], cwd=project_root)
    run(["uv", "lock"], cwd=project_root)
    rewrite_version_files(project_root, version)

    run(["git", "add", *(str(path) for path in VERSION_FILES)], cwd=project_root)
    if not has_staged_changes(project_root):
        raise RuntimeError("No release changes were staged.")

    run(["git", "commit", "-m", release_commit_message(version)], cwd=project_root)
    run(
        ["git", "tag", "-a", f"v{version}", "-m", f"Release v{version}"],
        cwd=project_root,
    )

    if push_remote is not None:
        run(
            ["git", "push", push_remote, "HEAD", f"refs/tags/v{version}"],
            cwd=project_root,
        )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        show_help, release_target, push_remote = parse_cut_release_args(
            sys.argv[1:] if argv is None else argv
        )
        if show_help:
            print(usage())
            return 0
        assert release_target is not None
        cut_release(Path(__file__).resolve().parents[1], release_target, push_remote)
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        print(usage(), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
