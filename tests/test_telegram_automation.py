import unittest

from interfaces.telegram.automation import (
    AutomationContractError,
    fallback_notification,
    topic_title_for_run,
    validate_task_definition,
    validate_topic_title,
)


class TelegramAutomationContractTests(unittest.TestCase):
    def test_topic_policy_preserves_task_title_or_adds_run_variation(self):
        self.assertEqual(
            topic_title_for_run("Revisar e-mails", "task", "run_01jxyz"),
            "Revisar e-mails",
        )
        self.assertEqual(
            topic_title_for_run("Revisar e-mails", "case", "run_01jxyz"),
            "Revisar e-mails",
        )
        self.assertEqual(
            topic_title_for_run("Revisar e-mails", "run", "run_01jxyz"),
            "Revisar e-mails: run_01jxyz",
        )

    def test_topic_title_rejects_empty_newlines_and_utf8_overflow(self):
        with self.assertRaises(AutomationContractError):
            validate_topic_title(" ")
        with self.assertRaises(AutomationContractError):
            validate_topic_title("linha 1\nlinha 2")
        with self.assertRaises(AutomationContractError):
            validate_topic_title("á" * 129)

    def test_fallback_has_required_header_and_blank_line(self):
        message = fallback_notification("Tarefa financeira", "grupo inválido", "Resumo")
        self.assertTrue(
            message.startswith(
                "Falha ao enviar a mensagem para o tópico Tarefa financeira:\r\n\r\n"
            )
        )
        self.assertIn("Motivo: grupo inválido", message)
        self.assertTrue(message.endswith("Resumo"))

    def test_enabled_task_requires_valid_group_and_exactly_one_entrypoint(self):
        base = {
            "task_uid": "task_email",
            "topic_title": "Novos e-mails",
            "topic_policy": "run",
            "thread_policy": "new",
            "trigger": "event",
            "script_id": "email_filter",
            "enabled": True,
        }
        with self.assertRaisesRegex(AutomationContractError, "grupo Telegram"):
            validate_task_definition(base, group_valid=False)
        task = validate_task_definition(base, group_valid=True)
        self.assertEqual(task.task_uid, "task_email")
        self.assertEqual(task.script_id, "email_filter")

        with self.assertRaises(AutomationContractError):
            validate_task_definition(
                {**base, "enabled": False, "prompt": "também"},
                group_valid=False,
            )

    def test_resume_requires_explicitly_resumable_task(self):
        value = {
            "task_uid": "task_case",
            "topic_title": "Caso financeiro",
            "thread_policy": "resume",
            "script_id": "case_monitor",
        }
        with self.assertRaisesRegex(AutomationContractError, "resumable"):
            validate_task_definition(value, group_valid=True)
        value["resumable"] = True
        task = validate_task_definition(value, group_valid=True)
        self.assertTrue(task.resumable)


if __name__ == "__main__":
    unittest.main()
