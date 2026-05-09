"""Local filesystem adapters."""

from __future__ import annotations

import asyncio
from pathlib import Path


class LocalDirectoryResolver:
    """Resolve directory command paths against the process filesystem."""

    async def default_base_path(self) -> str:
        return await asyncio.to_thread(_current_working_directory)

    async def resolve_directory(self, raw_path: str, *, base_path: str) -> str:
        return await asyncio.to_thread(_resolve_directory_path, raw_path, base_path)


def _current_working_directory() -> str:
    return str(Path.cwd())


def _resolve_directory_path(raw_path: str, base_path: str) -> str:
    candidate = Path(raw_path.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = Path(base_path) / candidate
    resolved = candidate.resolve()
    if not resolved.exists():
        raise ValueError(f"Directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Not a directory: {resolved}")
    return str(resolved)
