"""Contratos seguros para tarefas agendadas e roteamento de tópicos Telegram.

Este módulo não executa scripts nem chama a API do Telegram. Ele centraliza as
validações que serão compartilhadas pelo scheduler, pelo configurador e pelo
gateway, impedindo que cada camada crie suas próprias regras de identificação.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


class AutomationContractError(ValueError):
    """Indica tarefa, evento ou política de tópico fora do contrato."""


UID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,79}$")
TOPIC_POLICIES = {"task", "run", "case"}
THREAD_POLICIES = {"new", "new_with_state", "resume"}
TRIGGERS = {"interval", "cron", "once", "event"}
MAX_TOPIC_TITLE_LENGTH = 128


@dataclass(frozen=True)
class AutomationTask:
    """Tarefa validada, sem executar seu script ou prompt."""

    task_uid: str
    topic_title: str
    topic_policy: str
    thread_policy: str
    trigger: str
    resumable: bool
    script_id: str | None
    prompt: str | None
    enabled: bool


def _uid(value: Any, label: str) -> str:
    if not isinstance(value, str) or not UID_PATTERN.fullmatch(value):
        raise AutomationContractError(
            f"'{label}' deve usar letras minúsculas, números, '_' ou '-', "
            "começando por letra e contendo ao menos 3 caracteres."
        )
    return value


def validate_topic_title(value: Any) -> str:
    """Valida título Telegram obrigatório e limitado a 128 caracteres UTF-8."""
    if not isinstance(value, str) or not value.strip():
        raise AutomationContractError("'topic_title' é obrigatório e não pode ser vazio.")
    title = value.strip()
    if len(title.encode("utf-8")) > MAX_TOPIC_TITLE_LENGTH:
        raise AutomationContractError(
            "'topic_title' deve ter no máximo 128 bytes em UTF-8."
        )
    if "\r" in title or "\n" in title:
        raise AutomationContractError("'topic_title' não pode conter quebras de linha.")
    return title


def topic_title_for_run(
    topic_title: Any,
    policy: Any,
    run_uid: Any,
) -> str:
    """Deriva o título da execução conforme a política declarada."""
    title = validate_topic_title(topic_title)
    if policy not in TOPIC_POLICIES:
        raise AutomationContractError(
            "'topic_policy' deve ser 'task', 'run' ou 'case'."
        )
    run_id = _uid(run_uid, "run_uid")
    if policy in {"task", "case"}:
        return title
    variation = run_id[:12]
    suffix = f": {variation}"
    available = MAX_TOPIC_TITLE_LENGTH - len(suffix.encode("utf-8"))
    encoded = title.encode("utf-8")[:available]
    while True:
        try:
            base = encoded.decode("utf-8")
            break
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return f"{base}{suffix}"


def fallback_notification(topic_title: Any, reason: Any, body: Any) -> str:
    """Monta fallback ao owner sem misturar a mensagem com a conversa privada."""
    title = validate_topic_title(topic_title)
    if not isinstance(reason, str) or not reason.strip():
        raise AutomationContractError("'reason' do fallback é obrigatório.")
    if not isinstance(body, str):
        raise AutomationContractError("'body' do fallback deve ser texto.")
    return (
        f"Falha ao enviar a mensagem para o tópico {title}:\r\n\r\n"
        f"Motivo: {reason.strip()}\r\n\r\n{body}"
    )


def validate_task_definition(
    value: Mapping[str, Any],
    *,
    group_valid: bool,
) -> AutomationTask:
    """Valida uma tarefa antes de registrá-la ou habilitá-la."""
    if not isinstance(value, Mapping):
        raise AutomationContractError("A definição da tarefa deve ser um objeto.")
    task_uid = _uid(value.get("task_uid"), "task_uid")
    topic_title = validate_topic_title(value.get("topic_title"))
    topic_policy = value.get("topic_policy", "run")
    if topic_policy not in TOPIC_POLICIES:
        raise AutomationContractError("'topic_policy' inválida.")
    thread_policy = value.get("thread_policy", "new")
    if thread_policy not in THREAD_POLICIES:
        raise AutomationContractError("'thread_policy' inválida.")
    trigger = value.get("trigger", "interval")
    if trigger not in TRIGGERS:
        raise AutomationContractError("'trigger' inválido.")
    resumable = value.get("resumable", False)
    if not isinstance(resumable, bool):
        raise AutomationContractError("'resumable' deve ser booleano.")
    if thread_policy == "resume" and not resumable:
        raise AutomationContractError(
            "'thread_policy=resume' exige 'resumable=true'."
        )
    script_id = value.get("script_id")
    prompt = value.get("prompt")
    if (script_id is None) == (prompt is None):
        raise AutomationContractError(
            "A tarefa deve informar exatamente um entre 'script_id' e 'prompt'."
        )
    if script_id is not None:
        script_id = _uid(script_id, "script_id")
    if prompt is not None:
        if not isinstance(prompt, str) or not prompt.strip():
            raise AutomationContractError("'prompt' não pode ser vazio.")
        prompt = prompt.strip()
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise AutomationContractError("'enabled' deve ser booleano.")
    if enabled and not group_valid:
        raise AutomationContractError(
            "Não é permitido habilitar tarefa enquanto o grupo Telegram estiver inválido."
        )
    return AutomationTask(
        task_uid=task_uid,
        topic_title=topic_title,
        topic_policy=topic_policy,
        thread_policy=thread_policy,
        trigger=trigger,
        resumable=resumable,
        script_id=script_id,
        prompt=prompt,
        enabled=enabled,
    )
