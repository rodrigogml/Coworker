from __future__ import annotations

import unittest

from scripts.integration_profiles import (
    IntegrationProfileError,
    resolve_credential_ref,
)


class IntegrationProfilesTests(unittest.TestCase):
    def test_legacy_single_credential_remains_supported(self):
        profile, credential_ref = resolve_credential_ref(
            {"credential_ref": "APIs/Todoist"}
        )

        self.assertIsNone(profile)
        self.assertEqual("APIs/Todoist", credential_ref)

    def test_default_and_requested_profiles_are_resolved(self):
        values = {
            "default_profile": "pessoal",
            "profiles": {
                "pessoal": {"credential_ref": "APIs/Todoist/Pessoal"},
                "empresa": {"credential_ref": "APIs/Todoist/Empresa"},
            },
        }

        self.assertEqual(
            ("pessoal", "APIs/Todoist/Pessoal"),
            resolve_credential_ref(values),
        )
        self.assertEqual(
            ("empresa", "APIs/Todoist/Empresa"),
            resolve_credential_ref(values, "empresa"),
        )

    def test_unknown_profile_lists_available_names_without_secrets(self):
        values = {
            "default_profile": "pessoal",
            "profiles": {
                "pessoal": {"credential_ref": "APIs/Todoist/Pessoal"},
                "empresa": {"credential_ref": "APIs/Todoist/Empresa"},
            },
        }

        with self.assertRaises(IntegrationProfileError) as raised:
            resolve_credential_ref(values, "inexistente")

        self.assertIn("empresa, pessoal", str(raised.exception))
        self.assertNotIn("APIs/Todoist", str(raised.exception))

    def test_legacy_and_profiles_cannot_be_mixed(self):
        with self.assertRaises(IntegrationProfileError):
            resolve_credential_ref(
                {
                    "credential_ref": "APIs/Todoist",
                    "default_profile": "default",
                    "profiles": {
                        "default": {"credential_ref": "APIs/Todoist/Default"}
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
