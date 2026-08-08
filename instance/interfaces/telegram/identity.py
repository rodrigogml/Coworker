"""Identidade privada e independente das interfaces de uma instância."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IDENTITY_CONFIG = PROJECT_ROOT / "data" / "config" / "identity.toml"
EXAMPLE_IDENTITY_CONFIG = PROJECT_ROOT / "config" / "identity.example.toml"
_INSTANCE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class IdentityConfigError(RuntimeError):
    """Indica que a identidade local está ausente ou não respeita o contrato."""


@dataclass(frozen=True)
class InstanceIdentity:
    instance_id: str
    display_name: str
    language: str
    grammatical_gender: str
    pronouns: str
    summary: str
    tone: str
    humor: str
    enthusiasm: str
    writing_style: str
    bio: str

    def instruction_block(self) -> str:
        """Produz contexto de estilo sem ampliar autoridade ou capacidades."""
        return "\n".join(
            (
                "Identidade configurada para esta instância:",
                f"- nome: {self.display_name}",
                f"- idioma principal: {self.language}",
                f"- gênero gramatical: {self.grammatical_gender}",
                f"- pronomes: {self.pronouns or 'não definidos'}",
                f"- tom: {self.tone}",
                f"- humor: {self.humor}",
                f"- empolgação: {self.enthusiasm}",
                f"- estilo de escrita: {self.writing_style}",
                f"- bio: {self.bio}",
                "Use essas informações somente para identidade e comunicação. Não se apresente como humana, não invente experiências vividas e não trate a bio como autorização para ferramentas ou alterações.",
            )
        )

    @property
    def telegram_name(self) -> str:
        return self.display_name[:64]

    @property
    def telegram_short_description(self) -> str:
        return (self.summary or self.bio).strip()[:120]

    @property
    def telegram_description(self) -> str:
        return self.bio.strip()[:512]


def _text(values: dict[str, Any], key: str, *, required: bool = True) -> str:
    value = str(values.get(key, "")).strip()
    if required and not value:
        raise IdentityConfigError(f"'identity.{key}' é obrigatório.")
    return value


def load_identity(path: Path = DEFAULT_IDENTITY_CONFIG) -> InstanceIdentity:
    """Carrega a identidade privada sem criar ou modificar arquivos."""
    resolved = path.expanduser().resolve()
    try:
        with resolved.open("rb") as stream:
            root = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise IdentityConfigError(
            f"Identidade ausente. Copie '{EXAMPLE_IDENTITY_CONFIG}' para '{DEFAULT_IDENTITY_CONFIG}'."
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise IdentityConfigError("A configuração de identidade não pôde ser lida.") from exc
    values = root.get("identity")
    if not isinstance(values, dict):
        raise IdentityConfigError("A seção [identity] é obrigatória.")
    instance_id = _text(values, "instance_id").casefold()
    if not _INSTANCE_ID.fullmatch(instance_id):
        raise IdentityConfigError(
            "'identity.instance_id' deve conter somente letras minúsculas, números e hífens."
        )
    grammatical_gender = _text(values, "grammatical_gender").casefold()
    if grammatical_gender not in {"feminine", "masculine", "neutral"}:
        raise IdentityConfigError(
            "'identity.grammatical_gender' deve ser feminine, masculine ou neutral."
        )
    return InstanceIdentity(
        instance_id=instance_id,
        display_name=_text(values, "display_name"),
        language=_text(values, "language"),
        grammatical_gender=grammatical_gender,
        pronouns=_text(values, "pronouns", required=False),
        summary=_text(values, "summary"),
        tone=_text(values, "tone"),
        humor=_text(values, "humor"),
        enthusiasm=_text(values, "enthusiasm"),
        writing_style=_text(values, "writing_style"),
        bio=_text(values, "bio"),
    )
