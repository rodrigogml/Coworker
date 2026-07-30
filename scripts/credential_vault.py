#!/usr/bin/env python3
"""Gerencia operações interativas seguras do cofre KeePassXC da BOTina."""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import subprocess
import sys
import tomllib
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "data" / "config" / "secrets.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "secrets.example.toml"
FALLBACK_GUI = Path("KeePassXC.exe")
FALLBACK_CLI = Path("keepassxc-cli.exe")
FALLBACK_VAULT = PROJECT_ROOT / "data" / "secrets" / "botina.kdbx"
FALLBACK_CREDENTIAL_TARGET = "BOTina/KeePassXC/MasterPassword"

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_CREDENTIAL_BLOB_SIZE = 5 * 512


class CredentialW(ctypes.Structure):
    """Representa a estrutura CREDENTIALW usada pela API nativa do Windows."""

    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class VaultToolError(Exception):
    """Erro esperado e seguro para apresentação ao chamador."""


@dataclass(frozen=True)
class VaultConfig:
    """Configuração local e não confidencial do cofre."""

    gui_path: Path
    cli_path: Path
    vault_path: Path
    credential_target: str


def configured_path(raw_value: Any, field: str) -> Path:
    """Resolve um caminho da configuração em relação ao projeto."""
    value = str(raw_value or "").strip()
    if not value:
        raise VaultToolError(f"O campo '{field}' não pode ficar vazio.")
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_vault_config(path: Path = DEFAULT_CONFIG) -> VaultConfig:
    """Carrega a configuração TOML privada da instância."""
    resolved = path.expanduser().resolve()
    try:
        values = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VaultToolError(
            f"Configuração local não encontrada em '{resolved}'. Copie "
            f"'{EXAMPLE_CONFIG}' para '{DEFAULT_CONFIG}' e ajuste os caminhos."
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VaultToolError(
            f"Não foi possível carregar a configuração '{resolved}'."
        ) from exc

    executables = values.get("executables")
    vault = values.get("vault")
    windows_credential = values.get("windows_credential")
    if not isinstance(executables, dict):
        raise VaultToolError("A seção [executables] é obrigatória.")
    if not isinstance(vault, dict):
        raise VaultToolError("A seção [vault] é obrigatória.")
    if not isinstance(windows_credential, dict):
        raise VaultToolError("A seção [windows_credential] é obrigatória.")
    credential_target = str(windows_credential.get("target", "")).strip()
    if not credential_target:
        raise VaultToolError(
            "O campo 'windows_credential.target' não pode ficar vazio."
        )
    return VaultConfig(
        gui_path=configured_path(executables.get("gui"), "executables.gui"),
        cli_path=configured_path(executables.get("cli"), "executables.cli"),
        vault_path=configured_path(vault.get("path"), "vault.path"),
        credential_target=credential_target,
    )


def check_directory(vault_path: Path) -> Path:
    """Mantém resultados não confidenciais junto ao cofre privado."""
    return vault_path.parent / ".checks"


def credential_api() -> Any:
    """Configura e retorna as funções do Gerenciador de Credenciais do Windows."""
    if os.name != "nt":
        raise VaultToolError(
            "O Gerenciador de Credenciais está disponível somente no Windows."
        )
    library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    library.CredWriteW.argtypes = [ctypes.POINTER(CredentialW), wintypes.DWORD]
    library.CredWriteW.restype = wintypes.BOOL
    library.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CredentialW)),
    ]
    library.CredReadW.restype = wintypes.BOOL
    library.CredDeleteW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    library.CredDeleteW.restype = wintypes.BOOL
    library.CredFree.argtypes = [ctypes.c_void_p]
    library.CredFree.restype = None
    return library


def write_windows_credential(target: str, secret: str) -> None:
    """Grava uma credencial genérica persistente somente na máquina atual."""
    if not secret:
        raise VaultToolError("A senha mestra não pode ficar vazia.")
    blob = secret.encode("utf-16-le")
    if len(blob) > MAX_CREDENTIAL_BLOB_SIZE:
        raise VaultToolError("A senha mestra excede o limite aceito pelo Windows.")

    blob_buffer = ctypes.create_string_buffer(blob, len(blob))
    credential = CredentialW()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.Comment = "BOTina - senha mestra do cofre KeePassXC"
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(
        blob_buffer, ctypes.POINTER(ctypes.c_ubyte)
    )
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = getpass.getuser()

    library = credential_api()
    try:
        if not library.CredWriteW(ctypes.byref(credential), 0):
            error_code = ctypes.get_last_error()
            raise VaultToolError(
                f"O Windows recusou o cadastro da credencial (erro {error_code})."
            )
    finally:
        ctypes.memset(blob_buffer, 0, len(blob))


def read_windows_credential(target: str) -> str:
    """Lê a credencial do usuário atual e libera o buffer nativo imediatamente."""
    library = credential_api()
    credential_pointer = ctypes.POINTER(CredentialW)()
    if not library.CredReadW(
        target,
        CRED_TYPE_GENERIC,
        0,
        ctypes.byref(credential_pointer),
    ):
        error_code = ctypes.get_last_error()
        if error_code == ERROR_NOT_FOUND:
            raise VaultToolError(
                "Senha mestra não cadastrada nesta máquina. Execute 'enroll'."
            )
        raise VaultToolError(
            f"O Windows não permitiu ler a credencial (erro {error_code})."
        )

    try:
        credential = credential_pointer.contents
        blob = ctypes.string_at(
            credential.CredentialBlob,
            credential.CredentialBlobSize,
        )
        return blob.decode("utf-16-le")
    finally:
        credential = credential_pointer.contents
        if credential.CredentialBlob and credential.CredentialBlobSize:
            ctypes.memset(
                credential.CredentialBlob,
                0,
                credential.CredentialBlobSize,
            )
        library.CredFree(credential_pointer)


def windows_credential_exists(target: str) -> bool:
    """Verifica a presença da credencial sem devolver seu conteúdo."""
    library = credential_api()
    credential_pointer = ctypes.POINTER(CredentialW)()
    if not library.CredReadW(
        target,
        CRED_TYPE_GENERIC,
        0,
        ctypes.byref(credential_pointer),
    ):
        error_code = ctypes.get_last_error()
        if error_code == ERROR_NOT_FOUND:
            return False
        raise VaultToolError(
            f"O Windows não permitiu verificar a credencial (erro {error_code})."
        )
    try:
        credential = credential_pointer.contents
        if credential.CredentialBlob and credential.CredentialBlobSize:
            ctypes.memset(
                credential.CredentialBlob,
                0,
                credential.CredentialBlobSize,
            )
    finally:
        library.CredFree(credential_pointer)
    return True


def delete_windows_credential(target: str) -> bool:
    """Remove a senha mestra cadastrada para o usuário atual nesta máquina."""
    library = credential_api()
    if library.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
        return True
    error_code = ctypes.get_last_error()
    if error_code == ERROR_NOT_FOUND:
        return False
    raise VaultToolError(
        f"O Windows recusou a remoção da credencial (erro {error_code})."
    )


def print_json(payload: Any, *, stream: Any = sys.stdout) -> None:
    """Imprime uma resposta JSON em UTF-8."""
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


def resolved_path(raw_path: str) -> Path:
    """Resolve um caminho absoluto ou relativo ao diretório atual."""
    return Path(raw_path).expanduser().resolve()


def require_file(path: Path, description: str) -> None:
    """Exige a presença de um arquivo necessário."""
    if not path.is_file():
        raise VaultToolError(f"{description} não encontrado em '{path}'.")


def validate_entry_path(value: str) -> str:
    """Valida o caminho lógico de uma entrada do cofre."""
    normalized = value.strip().strip("/")
    if not normalized:
        raise VaultToolError("O caminho da entrada não pode ficar vazio.")
    if any(character in normalized for character in "\r\n\0"):
        raise VaultToolError("O caminho da entrada contém caracteres inválidos.")
    return normalized


def executable_version(cli_path: Path) -> str:
    """Obtém a versão do KeePassXC CLI sem abrir o cofre."""
    completed = subprocess.run(
        [str(cli_path), "--version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise VaultToolError("Não foi possível executar o KeePassXC CLI.")
    return completed.stdout.strip()


def run_keepassxc(
    cli_path: Path,
    arguments: list[str],
    master_password: str,
) -> subprocess.CompletedProcess[str]:
    """Executa o CLI com a senha mestra pela entrada padrão e captura sua saída."""
    return subprocess.run(
        [str(cli_path), *arguments],
        input=master_password + "\n",
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def verify_master_password(
    cli_path: Path,
    vault_path: Path,
    master_password: str,
) -> bool:
    """Confirma que a senha mestra consegue abrir o cofre configurado."""
    completed = run_keepassxc(
        cli_path,
        ["ls", "--flatten", str(vault_path)],
        master_password,
    )
    return completed.returncode == 0


def read_entry_secret(
    entry: str,
    *,
    cli_path: Path | None = None,
    vault_path: Path | None = None,
    credential_target: str | None = None,
    config_path: Path = DEFAULT_CONFIG,
) -> str:
    """Obtém internamente um segredo para uso por um script de integração."""
    if cli_path is None or vault_path is None or credential_target is None:
        config = load_vault_config(config_path)
        cli_path = cli_path or config.cli_path
        vault_path = vault_path or config.vault_path
        credential_target = credential_target or config.credential_target
    normalized_entry = validate_entry_path(entry)
    require_file(cli_path, "KeePassXC CLI")
    require_file(vault_path, "Cofre")
    master_password = read_windows_credential(credential_target)
    try:
        completed = run_keepassxc(
            cli_path,
            [
                "show",
                "--show-protected",
                "--attributes",
                "Password",
                str(vault_path),
                normalized_entry,
            ],
            master_password,
        )
    finally:
        master_password = ""
    if completed.returncode != 0:
        raise VaultToolError(
            f"Não foi possível acessar a credencial '{normalized_entry}'."
        )
    return completed.stdout.rstrip("\r\n")


def launch_interactive(
    cli_path: Path,
    arguments: list[str],
    title: str,
    *,
    result_path: Path | None = None,
) -> int:
    """Abre um console separado para entrada confidencial pelo usuário."""
    if os.name != "nt":
        raise VaultToolError("A operação interativa está disponível somente no Windows.")

    worker_arguments = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_interactive",
        "--title",
        title,
    ]
    if result_path is not None:
        worker_arguments.extend(["--result", str(result_path)])
    worker_arguments.extend(["--", str(cli_path), *arguments])
    process = subprocess.Popen(
        worker_arguments,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return process.pid


def launch_enrollment(
    cli_path: Path,
    vault_path: Path,
    credential_target: str,
) -> int:
    """Abre um console separado para cadastrar a senha mestra nesta máquina."""
    if os.name != "nt":
        raise VaultToolError("O cadastro está disponível somente no Windows.")
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_enroll",
            "--cli",
            str(cli_path),
            "--vault",
            str(vault_path),
            "--credential-target",
            credential_target,
        ],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return process.pid


def pause_interactive_console() -> None:
    """Mantém o console aberto para o usuário conferir o resultado."""
    print()
    print("Operação concluída. Pressione Enter para fechar esta janela.")
    try:
        input()
    except EOFError:
        pass


def enrollment_worker(raw_arguments: list[str]) -> int:
    """Solicita, valida e grava a senha mestra sem passá-la pela conversa."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--credential-target", required=True)
    arguments = parser.parse_args(raw_arguments)
    cli_path = resolved_path(arguments.cli)
    vault_path = resolved_path(arguments.vault)

    if os.name == "nt":
        ctypes.windll.kernel32.SetConsoleTitleW(
            "BOTina - Cadastrar senha mestra nesta máquina"
        )

    try:
        require_file(cli_path, "KeePassXC CLI")
        require_file(vault_path, "Cofre")
        master_password = getpass.getpass("Senha mestra do KeePassXC: ")
        confirmation = getpass.getpass("Confirme a senha mestra: ")
        if not master_password:
            raise VaultToolError("A senha mestra não pode ficar vazia.")
        if master_password != confirmation:
            raise VaultToolError("As senhas informadas não coincidem.")
        confirmation = ""
        if not verify_master_password(cli_path, vault_path, master_password):
            raise VaultToolError("A senha não conseguiu desbloquear o cofre.")
        write_windows_credential(arguments.credential_target, master_password)
        master_password = ""
        print("Senha mestra cadastrada com sucesso para o usuário atual.")
        result = 0
    except (VaultToolError, OSError) as exc:
        print(f"Falha: {exc}")
        result = 1
    pause_interactive_console()
    return result


def interactive_worker(raw_arguments: list[str]) -> int:
    """Executa o KeePassXC em um console separado sem usar interpretação do shell."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--title", required=True)
    parser.add_argument("--result")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(raw_arguments)
    command = arguments.command[1:] if arguments.command[:1] == ["--"] else arguments.command
    if not command:
        return 2

    if os.name == "nt":
        ctypes.windll.kernel32.SetConsoleTitleW(arguments.title)
    completed = subprocess.run(command, check=False)

    if arguments.result:
        result_path = resolved_path(arguments.result)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "ok": completed.returncode == 0,
                    "exit_code": completed.returncode,
                    "checked_at": datetime.now(UTC)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    pause_interactive_console()
    return completed.returncode


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    """Verifica a ferramenta portátil e a existência do cofre."""
    gui_path = resolved_path(args.gui)
    cli_path = resolved_path(args.cli)
    vault_path = resolved_path(args.vault)
    require_file(gui_path, "KeePassXC")
    require_file(cli_path, "KeePassXC CLI")
    return {
        "ok": True,
        "provider": "keepassxc",
        "version": executable_version(cli_path),
        "gui": str(gui_path),
        "cli": str(cli_path),
        "vault": str(vault_path),
        "vault_exists": vault_path.is_file(),
        "master_password_enrolled": windows_credential_exists(
            args.credential_target
        ),
        "credential_target": args.credential_target,
        "secrets_may_be_printed": False,
    }


def command_create(args: argparse.Namespace) -> dict[str, Any]:
    """Abre uma sessão interativa para criar o cofre com senha mestra."""
    cli_path = resolved_path(args.cli)
    vault_path = resolved_path(args.vault)
    require_file(cli_path, "KeePassXC CLI")
    if vault_path.exists():
        raise VaultToolError(f"O cofre já existe em '{vault_path}'.")
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    process_id = launch_interactive(
        cli_path,
        ["db-create", "--set-password", str(vault_path)],
        "BOTina - Criar cofre",
    )
    return {
        "ok": True,
        "launched": True,
        "process_id": process_id,
        "vault": str(vault_path),
        "instruction": (
            "Digite e confirme a senha mestra somente na janela interativa aberta."
        ),
    }


def command_open(args: argparse.Namespace) -> dict[str, Any]:
    """Abre o cofre existente na interface gráfica do KeePassXC."""
    gui_path = resolved_path(args.gui)
    vault_path = resolved_path(args.vault)
    require_file(gui_path, "KeePassXC")
    require_file(vault_path, "Cofre")
    process = subprocess.Popen(
        [str(gui_path), str(vault_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {
        "ok": True,
        "launched": True,
        "process_id": process.pid,
        "vault": str(vault_path),
    }


def command_add(args: argparse.Namespace) -> dict[str, Any]:
    """Abre uma sessão interativa para incluir uma credencial."""
    cli_path = resolved_path(args.cli)
    vault_path = resolved_path(args.vault)
    require_file(cli_path, "KeePassXC CLI")
    require_file(vault_path, "Cofre")
    entry_path = validate_entry_path(args.entry)

    command_arguments = ["add", "--password-prompt"]
    if args.username:
        command_arguments.extend(["--username", args.username])
    if args.url:
        command_arguments.extend(["--url", args.url])
    command_arguments.extend([str(vault_path), entry_path])

    process_id = launch_interactive(
        cli_path,
        command_arguments,
        "BOTina - Adicionar credencial",
    )
    return {
        "ok": True,
        "launched": True,
        "process_id": process_id,
        "vault": str(vault_path),
        "entry": entry_path,
        "instruction": (
            "Digite a senha mestra e o segredo somente na janela interativa aberta."
        ),
    }


def command_enroll(args: argparse.Namespace) -> dict[str, Any]:
    """Abre o cadastro local e seguro da senha mestra."""
    cli_path = resolved_path(args.cli)
    vault_path = resolved_path(args.vault)
    require_file(cli_path, "KeePassXC CLI")
    require_file(vault_path, "Cofre")
    process_id = launch_enrollment(
        cli_path,
        vault_path,
        args.credential_target,
    )
    return {
        "ok": True,
        "launched": True,
        "process_id": process_id,
        "credential_target": args.credential_target,
        "instruction": (
            "Digite e confirme a senha mestra somente na janela interativa aberta."
        ),
    }


def command_unenroll(args: argparse.Namespace) -> dict[str, Any]:
    """Remove o desbloqueio persistente desta máquina."""
    if not args.confirm:
        raise VaultToolError("A remoção exige a opção '--confirm'.")
    removed = delete_windows_credential(args.credential_target)
    return {
        "ok": True,
        "credential_target": args.credential_target,
        "removed": removed,
        "recoverable": False,
    }


def command_check(args: argparse.Namespace) -> dict[str, Any]:
    """Verifica a existência de uma entrada sem revelar campos protegidos."""
    cli_path = resolved_path(args.cli)
    vault_path = resolved_path(args.vault)
    require_file(cli_path, "KeePassXC CLI")
    require_file(vault_path, "Cofre")
    entry_path = validate_entry_path(args.entry)
    if windows_credential_exists(args.credential_target):
        master_password = read_windows_credential(args.credential_target)
        try:
            completed = run_keepassxc(
                cli_path,
                [
                    "show",
                    "--attributes",
                    "Title",
                    str(vault_path),
                    entry_path,
                ],
                master_password,
            )
        finally:
            master_password = ""
        return {
            "ok": True,
            "interactive": False,
            "entry": entry_path,
            "entry_exists": completed.returncode == 0,
            "check_exit_code": completed.returncode,
        }

    request_id = uuid.uuid4().hex
    result_path = check_directory(vault_path) / f"{request_id}.json"
    process_id = launch_interactive(
        cli_path,
        ["show", "--attributes", "Title", str(vault_path), entry_path],
        "BOTina - Verificar credencial",
        result_path=result_path,
    )
    return {
        "ok": True,
        "launched": True,
        "process_id": process_id,
        "request_id": request_id,
        "entry": entry_path,
        "result_file": str(result_path),
        "instruction": "Digite a senha mestra somente na janela interativa aberta.",
    }


def command_check_result(args: argparse.Namespace) -> dict[str, Any]:
    """Lê o resultado não confidencial de uma verificação interativa."""
    if not re_fullmatch_hex_id(args.request_id):
        raise VaultToolError("Identificador de verificação inválido.")
    vault_path = resolved_path(args.vault)
    result_path = check_directory(vault_path) / f"{args.request_id}.json"
    require_file(result_path, "Resultado da verificação")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise VaultToolError("Resultado da verificação inválido.") from exc
    return {
        "ok": True,
        "request_id": args.request_id,
        "entry_exists": result.get("ok") is True,
        "check_exit_code": result.get("exit_code"),
        "checked_at": result.get("checked_at"),
    }


def re_fullmatch_hex_id(value: str) -> bool:
    """Valida identificadores aleatórios usados nos resultados de verificação."""
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


def build_parser(config: VaultConfig | None = None) -> argparse.ArgumentParser:
    """Constrói a interface de linha de comando."""
    settings = config or VaultConfig(
        FALLBACK_GUI,
        FALLBACK_CLI,
        FALLBACK_VAULT,
        FALLBACK_CREDENTIAL_TARGET,
    )
    parser = argparse.ArgumentParser(
        description="Operações seguras do cofre KeePassXC da BOTina."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--gui", default=str(settings.gui_path))
    parser.add_argument("--cli", default=str(settings.cli_path))
    parser.add_argument("--vault", default=str(settings.vault_path))
    parser.add_argument(
        "--credential-target",
        default=settings.credential_target,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    status_parser = commands.add_parser("status", help="Verifica ferramenta e cofre.")
    status_parser.set_defaults(handler=command_status)

    create_parser = commands.add_parser(
        "create", help="Cria o cofre em um console interativo."
    )
    create_parser.set_defaults(handler=command_create)

    open_parser = commands.add_parser("open", help="Abre o cofre no KeePassXC.")
    open_parser.set_defaults(handler=command_open)

    add_parser = commands.add_parser(
        "add", help="Adiciona uma credencial em um console interativo."
    )
    add_parser.add_argument("entry", help="Caminho da entrada, por exemplo APIs/Gmail.")
    add_parser.add_argument("--username")
    add_parser.add_argument("--url")
    add_parser.set_defaults(handler=command_add)

    enroll_parser = commands.add_parser(
        "enroll", help="Cadastra a senha mestra nesta máquina."
    )
    enroll_parser.set_defaults(handler=command_enroll)

    unenroll_parser = commands.add_parser(
        "unenroll", help="Remove a senha mestra cadastrada nesta máquina."
    )
    unenroll_parser.add_argument("--confirm", action="store_true")
    unenroll_parser.set_defaults(handler=command_unenroll)

    check_parser = commands.add_parser(
        "check", help="Verifica uma entrada sem revelar seu segredo."
    )
    check_parser.add_argument("entry")
    check_parser.set_defaults(handler=command_check)

    check_result_parser = commands.add_parser(
        "check-result", help="Consulta o resultado de uma verificação."
    )
    check_result_parser.add_argument("request_id")
    check_result_parser.set_defaults(handler=command_check_result)

    return parser


def main() -> int:
    """Executa o comando e converte falhas esperadas em JSON."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "_interactive":
        return interactive_worker(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "_enroll":
        return enrollment_worker(sys.argv[2:])
    try:
        config_parser = argparse.ArgumentParser(add_help=False)
        config_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
        config_args, _ = config_parser.parse_known_args()
        config = load_vault_config(Path(config_args.config))
        args = build_parser(config).parse_args()
        result = args.handler(args)
    except (VaultToolError, OSError) as exc:
        print_json(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            stream=sys.stderr,
        )
        return 1
    print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
