from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
MODULE_PATH = PROJECT_ROOT / "scripts" / "google_accounts.py"
SPEC = importlib.util.spec_from_file_location("google_accounts_tested", MODULE_PATH)
google = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = google
SPEC.loader.exec_module(google)


class FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_args):
        return json.dumps(self.payload).encode("utf-8")


class GoogleAccountsTests(unittest.TestCase):
    def config(self):
        profile = google.GoogleProfile(
            "pessoal",
            "APIs/Google/Accounts/Pessoal",
            (
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.modify",
            ),
        )
        return google.GoogleConfig(
            "https://accounts.google.com/o/oauth2/v2/auth",
            "https://oauth2.googleapis.com/token",
            "https://openidconnect.googleapis.com/v1/userinfo",
            "APIs/Google/OAuthClient",
            30,
            300,
            "pessoal",
            {"pessoal": profile},
        )

    def test_load_config_and_select_profile(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "google.toml"
            path.write_text(
                'authorization_endpoint = '
                '"https://accounts.google.com/o/oauth2/v2/auth"\n'
                'token_endpoint = "https://oauth2.googleapis.com/token"\n'
                'userinfo_endpoint = '
                '"https://openidconnect.googleapis.com/v1/userinfo"\n'
                'client_credential_ref = "APIs/Google/OAuthClient"\n'
                "timeout_seconds = 20\n"
                "authorization_timeout_seconds = 180\n"
                'default_profile = "pessoal"\n'
                "[profiles.pessoal]\n"
                'credential_ref = "APIs/Google/Accounts/Pessoal"\n'
                'scopes = ["openid", "email"]\n',
                encoding="utf-8",
            )
            config = google.load_google_config(path)

        self.assertEqual("pessoal", config.select(None).name)
        self.assertEqual(180, config.authorization_timeout_seconds)

    def test_config_rejects_token_exfiltration_endpoint(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "google.toml"
            path.write_text(
                'authorization_endpoint = '
                '"https://accounts.google.com/o/oauth2/v2/auth"\n'
                'token_endpoint = "https://example.com/token"\n'
                'userinfo_endpoint = '
                '"https://openidconnect.googleapis.com/v1/userinfo"\n'
                'client_credential_ref = "APIs/Google/OAuthClient"\n'
                "timeout_seconds = 20\n"
                "authorization_timeout_seconds = 180\n"
                'default_profile = "pessoal"\n'
                "[profiles.pessoal]\n"
                'credential_ref = "APIs/Google/Accounts/Pessoal"\n'
                'scopes = ["openid", "email"]\n',
                encoding="utf-8",
            )
            with self.assertRaises(google.GoogleAccountError):
                google.load_google_config(path)

    def test_refresh_uses_two_vault_entries_and_returns_ephemeral_access(self):
        requests = []

        def opener(request, *, timeout):
            del timeout
            requests.append(request)
            if request.full_url == "https://oauth2.googleapis.com/token":
                body = request.data.decode("ascii")
                self.assertIn("refresh_token=refresh-secreto", body)
                return FakeResponse(
                    {
                        "access_token": "access-secreto",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": "openid email "
                        "https://www.googleapis.com/auth/gmail.modify",
                    }
                )
            self.assertEqual(
                "Bearer access-secreto",
                request.get_header("Authorization"),
            )
            return FakeResponse({"email": "pessoal@example.com"})

        with patch.object(
            google,
            "read_entry_credentials",
            side_effect=[
                ("client-id", "client-secret"),
                ("pessoal@example.com", "refresh-secreto"),
            ],
        ) as read:
            access = google.refresh_google_access(
                self.config(),
                "pessoal",
                opener=opener,
            )

        self.assertEqual(
            [
                ("APIs/Google/OAuthClient",),
                ("APIs/Google/Accounts/Pessoal",),
            ],
            [call.args for call in read.call_args_list],
        )
        self.assertEqual("pessoal@example.com", access.email)
        self.assertEqual("access-secreto", access.access_token)
        access.close()
        self.assertEqual("", access.access_token)

    def test_launch_enrollment_never_passes_credentials(self):
        with patch.object(google.subprocess, "Popen") as popen:
            popen.return_value.pid = 123
            result = google.launch_enrollment(
                Path("C:/private/google.toml"),
                "pessoal",
            )

        arguments = popen.call_args.args[0]
        serialized = " ".join(arguments)
        self.assertIn("_enroll", serialized)
        self.assertIn("pessoal", serialized)
        self.assertNotIn("token", serialized.casefold())
        self.assertEqual(123, result["process_id"])

    def test_parser_never_accepts_tokens(self):
        help_text = google.build_parser().format_help()
        self.assertNotIn("--token", help_text)
        self.assertNotIn("--refresh-token", help_text)

    def test_required_scopes_fail_before_api_use(self):
        access = google.GoogleAccess(
            "pessoal",
            "pessoal@example.com",
            "access",
            ("openid", "email"),
            3600,
        )
        with self.assertRaises(google.GoogleAccountError):
            google.require_google_scopes(
                access,
                {"https://www.googleapis.com/auth/drive"},
                "Google Drive",
            )


if __name__ == "__main__":
    unittest.main()
