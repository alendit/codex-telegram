"""Profile resolution and compatibility aliases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Protocol

from codex_telegram.domain import ProfileDefinition, SessionOverrides

PROFILE_ALIASES = {
}

VERBOSITY_VALUES = {"none", "verbose"}
COMMAND_VERBOSITY_VALUES = {"none", "approval_only", "always", "verbose", "errors"}
FOLLOWUP_MODE_VALUES = {"steer", "queue"}


class ProfileSource(Protocol):
    """Configuration shape required to resolve runtime profiles."""

    model: str
    model_provider: str
    approval_policy: str
    sandbox_type: str
    network_access: bool
    writable_roots: list[str]
    effort: str
    fast_mode: bool
    summary: str
    verbosity: str
    command_verbosity: str
    followup_mode: str
    developer_instructions: str | None


def build_profiles(
    profile_sources: Mapping[str, ProfileSource],
) -> dict[str, ProfileDefinition]:
    """Build runtime profiles including compatibility aliases."""
    profiles: dict[str, ProfileDefinition] = {}
    for name, value in profile_sources.items():
        profiles[name] = ProfileDefinition(
            name=name,
            model=value.model,
            model_provider=value.model_provider,
            approval_policy=value.approval_policy,
            sandbox_type=value.sandbox_type,
            network_access=value.network_access,
            writable_roots=tuple(value.writable_roots),
            effort=value.effort,
            fast_mode=value.fast_mode,
            summary=value.summary,
            verbosity=value.verbosity,
            command_verbosity=value.command_verbosity,
            followup_mode=value.followup_mode,
            developer_instructions=value.developer_instructions,
        )

    for alias, canonical in PROFILE_ALIASES.items():
        if canonical in profiles:
            profiles[alias] = replace(profiles[canonical], name=alias)

    return profiles


def canonical_profile_name(profile_name: str) -> str:
    """Resolve compatibility aliases back to the canonical name."""
    return PROFILE_ALIASES.get(profile_name, profile_name)


def default_profile_for_chat(
    client_defaults: dict[str, str],
    chat_key: str,
    fallback: str,
) -> str:
    """Return the configured default profile for one chat key."""
    return canonical_profile_name(client_defaults.get(chat_key, fallback))


def resolve_profile(
    profiles: Mapping[str, ProfileDefinition],
    default_profile: str,
    chat_default_profile: str,
    overrides: SessionOverrides,
) -> ProfileDefinition:
    """Resolve the effective profile definition."""
    selected = canonical_profile_name(
        overrides.profile or chat_default_profile or default_profile
    )
    profile = profiles.get(selected)
    if profile is None:
        profile = profiles[canonical_profile_name(default_profile)]
    return profile
