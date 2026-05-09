from __future__ import annotations

from pathlib import Path

import pytest

from codex_telegram.adapters.filesystem import LocalDirectoryResolver


@pytest.mark.asyncio
async def test_local_directory_resolver_resolves_relative_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    resolver = LocalDirectoryResolver()

    assert await resolver.default_base_path() == str(tmp_path.resolve())
    assert await resolver.resolve_directory(
        "workspace/nested",
        base_path=str(tmp_path),
    ) == str(nested.resolve())


@pytest.mark.asyncio
async def test_local_directory_resolver_rejects_missing_and_non_directory(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("not a directory")
    resolver = LocalDirectoryResolver()

    with pytest.raises(ValueError, match="Directory does not exist"):
        await resolver.resolve_directory("missing", base_path=str(tmp_path))

    with pytest.raises(ValueError, match="Not a directory"):
        await resolver.resolve_directory(str(file_path), base_path=str(tmp_path))
