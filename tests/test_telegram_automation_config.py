import unittest

from interfaces.telegram.config import (
    TelegramConfigError,
    _group_configs,
    _retention_config,
)


class TelegramAutomationConfigTests(unittest.TestCase):
    def test_retention_defaults_to_180_days_and_rejects_shorter_values(self):
        retention = _retention_config({})
        self.assertEqual(retention.raw_messages_days, 180)
        self.assertEqual(retention.attachments_days, 180)
        with self.assertRaises(TelegramConfigError):
            _retention_config({"retention": {"raw_messages_days": 179}})

    def test_group_config_validates_multiple_groups_and_retention(self):
        retention = _retention_config({})
        groups = _group_configs(
            {
                "groups": {
                    "financeiro": {"chat_id": -1001},
                    "projetos": {
                        "chat_id": -1002,
                        "default_topic_policy": "task",
                        "capture_mode": "full_topic",
                        "retention_days": 365,
                    },
                }
            },
            retention,
        )
        self.assertEqual([group.alias for group in groups], ["financeiro", "projetos"])
        self.assertEqual(groups[1].retention_days, 365)
        with self.assertRaises(TelegramConfigError):
            _group_configs({"groups": {"invalid": {"chat_id": 1}}}, retention)


if __name__ == "__main__":
    unittest.main()
