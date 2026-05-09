from __future__ import annotations

import os
import subprocess
from pathlib import Path

ENTRYPOINT = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "codex-app-server"
    / "scripts"
    / "docker-entrypoint.sh"
)


def _run_entrypoint(
    tmp_path: Path,
    *,
    template_config: str | None = None,
    existing_config: str | None = None,
) -> subprocess.CompletedProcess[str]:
    template_dir = tmp_path / "template"
    workspace_dir = tmp_path / "agent"
    codex_home_dir = tmp_path / "codex-home"
    (template_dir / ".codex").mkdir(parents=True)
    if template_config is not None:
        (template_dir / ".codex" / "config.toml").write_text(template_config)
    if existing_config is not None:
        codex_home_dir.mkdir(parents=True)
        (codex_home_dir / "config.toml").write_text(existing_config)

    env = {
        **os.environ,
        "TEMPLATE_DIR": str(template_dir),
        "WORKSPACE_DIR": str(workspace_dir),
        "CODEX_HOME_DIR": str(codex_home_dir),
    }
    return subprocess.run(
        ["sh", str(ENTRYPOINT), "true"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_entrypoint_keeps_runtime_state_lean(tmp_path: Path) -> None:
    result = _run_entrypoint(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "agent").is_dir()
    assert (tmp_path / "codex-home" / "log").is_dir()
    assert not (tmp_path / "codex-home" / "mem0").exists()
    assert not (tmp_path / "codex-home" / "rkllama").exists()


def test_entrypoint_adds_realtime_feature_default_when_missing(tmp_path: Path) -> None:
    result = _run_entrypoint(
        tmp_path,
        template_config='model_provider = "openai"\n\n[features]\nrealtime_conversation = true\n',
        existing_config='model_provider = "openai"\n',
    )

    assert result.returncode == 0, result.stderr
    config_text = (tmp_path / "codex-home" / "config.toml").read_text()
    assert "[features]" in config_text
    assert "realtime_conversation = true" in config_text


def test_entrypoint_preserves_explicit_realtime_false(tmp_path: Path) -> None:
    result = _run_entrypoint(
        tmp_path,
        template_config='model_provider = "openai"\n\n[features]\nrealtime_conversation = true\n',
        existing_config=(
            'model_provider = "openai"\n\n'
            "[features]\n"
            "realtime_conversation = false\n"
        ),
    )

    assert result.returncode == 0, result.stderr
    config_text = (tmp_path / "codex-home" / "config.toml").read_text()
    assert "realtime_conversation = false" in config_text
    assert "realtime_conversation = true" not in config_text
