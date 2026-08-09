"""Compatibilidade para versões antigas; o scheduler não pertence ao Telegram.

O núcleo público está em :mod:`scheduler`. Este módulo permanece somente para
não quebrar imports de skills e instalações antigas.
"""

from scheduler import (  # noqa: F401
    ScheduledTask,
    SchedulerError,
    SchedulerStore,
    TaskScheduler,
    run_python_script,
    validate_task,
)

__all__ = [
    "ScheduledTask",
    "SchedulerError",
    "SchedulerStore",
    "TaskScheduler",
    "run_python_script",
    "validate_task",
]
