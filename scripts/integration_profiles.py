"""Resolve perfis de credenciais em configurações TOML de integrações."""

from __future__ import annotations

import re
from typing import Any, Mapping


PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class IntegrationProfileError(ValueError):
    """Configuração de perfil inválida ou ambígua."""


def validate_profile_name(value: str) -> str:
    """Valida um nome curto usado somente como seletor local."""
    normalized = str(value).strip()
    if not PROFILE_PATTERN.fullmatch(normalized):
        raise IntegrationProfileError(
            "O perfil deve usar de 1 a 64 letras, números, '_' ou '-'."
        )
    return normalized


def resolve_credential_ref(
    values: Mapping[str, Any],
    requested_profile: str | None = None,
) -> tuple[str | None, str]:
    """Resolve uma referência nova por perfil ou a configuração legada."""
    legacy_ref = str(values.get("credential_ref", "")).strip()
    profiles = values.get("profiles")
    if profiles is None:
        if requested_profile is not None:
            raise IntegrationProfileError(
                "Esta configuração usa uma credencial única e não define perfis."
            )
        if not legacy_ref:
            raise IntegrationProfileError("'credential_ref' não pode ficar vazio.")
        return None, legacy_ref
    if legacy_ref:
        raise IntegrationProfileError(
            "Use 'credential_ref' na raiz ou '[profiles]', não ambos."
        )
    if not isinstance(profiles, dict) or not profiles:
        raise IntegrationProfileError("'profiles' deve conter ao menos um perfil.")

    profile_name = requested_profile
    if profile_name is None:
        configured_default = values.get("default_profile")
        if configured_default is None:
            raise IntegrationProfileError(
                "'default_profile' é obrigatório quando existem perfis."
            )
        profile_name = str(configured_default)
    profile_name = validate_profile_name(profile_name)
    profile = profiles.get(profile_name)
    if not isinstance(profile, dict):
        available = ", ".join(sorted(str(name) for name in profiles))
        raise IntegrationProfileError(
            f"Perfil '{profile_name}' não encontrado. Disponíveis: {available}."
        )
    credential_ref = str(profile.get("credential_ref", "")).strip()
    if not credential_ref:
        raise IntegrationProfileError(
            f"'profiles.{profile_name}.credential_ref' não pode ficar vazio."
        )
    return profile_name, credential_ref
