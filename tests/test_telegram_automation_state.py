import tempfile
import unittest
from pathlib import Path

from interfaces.telegram.automation_state import AutomationState, AutomationStateError


class TelegramAutomationStateTests(unittest.TestCase):
    def test_group_gate_task_persistence_idempotent_event_and_one_to_one_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            state = AutomationState(Path(directory) / "automation.sqlite3")
            try:
                state.upsert_group("financeiro", -1001, valid=False)
                with self.assertRaisesRegex(AutomationStateError, "grupo"):
                    state.save_task(
                        {
                            "task_uid": "task_finance",
                            "topic_title": "Fechamento",
                            "script_id": "review_finance",
                            "enabled": True,
                        },
                        group_alias="financeiro",
                    )
                state.save_task(
                    {
                        "task_uid": "task_draft",
                        "topic_title": "Rascunho",
                        "script_id": "review_finance",
                        "enabled": False,
                    },
                    group_alias="financeiro",
                )
                state.upsert_group("financeiro", -1001, valid=True)
                state.save_task(
                    {
                        "task_uid": "task_finance",
                        "topic_title": "Fechamento",
                        "script_id": "review_finance",
                        "enabled": True,
                    },
                    group_alias="financeiro",
                )
                state.create_run("run_001", "task_finance", event_uid="event_001")
                with self.assertRaises(AutomationStateError):
                    state.create_run("run_002", "task_finance", event_uid="event_001")
                state.bind_conversation(
                    "run_001",
                    codex_thread_id="codex-001",
                    telegram_chat_id=-1001,
                    telegram_message_thread_id=42,
                    telegram_root_message_id=99,
                )
                self.assertEqual(state.run_for_topic(-1001, 42)["run_uid"], "run_001")
                self.assertEqual(state.run_for_codex_thread("codex-001")["run_uid"], "run_001")
                with self.assertRaises(AutomationStateError):
                    state.bind_conversation(
                        "run_001",
                        codex_thread_id="codex-002",
                        telegram_chat_id=-1001,
                        telegram_message_thread_id=42,
                        telegram_root_message_id=100,
                    )
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
