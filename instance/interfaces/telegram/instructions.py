"""Carrega instruções privadas e persistentes da instância."""

from __future__ import annotations

from pathlib import Path


INSTRUCTIONS_RELATIVE_PATH = Path("data") / "config" / "INSTRUCTIONS.md"
MAX_INSTRUCTIONS_BYTES = 32 * 1024


class InstanceInstructionsError(RuntimeError):
    """Indica instruções privadas ilegíveis ou excessivamente grandes."""


def instructions_path(project_root: Path) -> Path:
    """Retorna o único caminho privado permitido para instruções da instância."""
    root = project_root.resolve(strict=True)
    return (root / INSTRUCTIONS_RELATIVE_PATH).resolve()


def load_instance_instructions(project_root: Path) -> str:
    """Carrega instruções privadas sem aceitar caminhos fornecidos pela conversa.

    Arquivo ausente significa que a instância ainda não definiu preferências
    operacionais. O limite evita que o arquivo privado consuma todo o contexto.
    """
    path = instructions_path(project_root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise InstanceInstructionsError("As instruções privadas não puderam ser lidas.") from exc
    if len(raw) > MAX_INSTRUCTIONS_BYTES:
        raise InstanceInstructionsError(
            f"As instruções privadas excedem o limite de {MAX_INSTRUCTIONS_BYTES} bytes."
        )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstanceInstructionsError("As instruções privadas devem usar UTF-8.") from exc
    return content.strip()


def instruction_block(project_root: Path) -> str:
    """Formata as instruções privadas como contexto explícito, sem ampliar autoridade."""
    content = load_instance_instructions(project_root)
    if not content:
        return ""
    return (
        "Instruções privadas persistentes desta instância (não substituem regras de "
        "segurança, o pedido atual ou as skills aplicáveis):\n"
        f"{content}"
    )
