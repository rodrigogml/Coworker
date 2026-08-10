from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1] / "instance"
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
            ("gmail",),
        )
        return google.GoogleConfig(
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
            userinfo_endpoint="https://openidconnect.googleapis.com/v1/userinfo",
            client_id="client-id-publico.apps.googleusercontent.com",
            timeout_seconds=30,
            authorization_timeout_seconds=300,
            default_profile="pessoal",
            profiles={"pessoal": profile},
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
                'client_id = "client-id-publico.apps.googleusercontent.com"\n'
                "timeout_seconds = 20\n"
                "authorization_timeout_seconds = 180\n"
                'default_profile = "pessoal"\n'
                "[profiles.pessoal]\n"
                'credential_ref = "APIs/Google/Accounts/Pessoal"\n'
                'services = ["gmail"]\n',
                encoding="utf-8",
            )
            config = google.load_google_config(path)

        self.assertEqual("pessoal", config.select(None).name)
        self.assertEqual(("gmail",), config.select(None).services)
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
                'client_id = "client-id-publico.apps.googleusercontent.com"\n'
                "timeout_seconds = 20\n"
                "authorization_timeout_seconds = 180\n"
                'default_profile = "pessoal"\n'
                "[profiles.pessoal]\n"
                'credential_ref = "APIs/Google/Accounts/Pessoal"\n'
                'services = ["gmail"]\n',
                encoding="utf-8",
            )
            with self.assertRaises(google.GoogleAccountError):
                google.load_google_config(path)

    def test_load_config_rejects_legacy_vault_client(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "google.toml"
            path.write_text(
                'authorization_endpoint = '
                '"https://accounts.google.com/o/oauth2/v2/auth"\n'
                'token_endpoint = "https://oauth2.googleapis.com/token"\n'
                'userinfo_endpoint = '
                '"https://openidconnect.googleapis.com/v1/userinfo"\n'
                'client_id = "client-id-publico.apps.googleusercontent.com"\n'
                'client_credential_ref = "APIs/Google/OAuthClient"\n'
                'default_profile = "pessoal"\n'
                '[profiles.pessoal]\n'
                'credential_ref = "APIs/Google/Accounts/Pessoal"\n'
                'services = ["gmail"]\n',
                encoding="utf-8",
            )

            with self.assertRaises(google.GoogleAccountError) as raised:
                google.load_google_config(path)

        self.assertIn("formato OAuth legado", str(raised.exception))

    def test_load_config_rejects_legacy_raw_scopes(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "google.toml"
            path.write_text(
                'authorization_endpoint = '
                '"https://accounts.google.com/o/oauth2/v2/auth"\n'
                'token_endpoint = "https://oauth2.googleapis.com/token"\n'
                'userinfo_endpoint = '
                '"https://openidconnect.googleapis.com/v1/userinfo"\n'
                'client_id = "client-id-publico.apps.googleusercontent.com"\n'
                'default_profile = "pessoal"\n'
                '[profiles.pessoal]\n'
                'credential_ref = "APIs/Google/Accounts/Pessoal"\n'
                'scopes = ["openid", "email"]\n',
                encoding="utf-8",
            )

            with self.assertRaises(google.GoogleAccountError):
                google.load_google_config(path)

    def test_refresh_uses_public_client_without_client_secret(self):
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
            return_value=("pessoal@example.com", "refresh-secreto"),
        ) as read:
            access = google.refresh_google_access(self.config(), "pessoal", opener=opener)

        self.assertEqual(
            [("APIs/Google/Accounts/Pessoal",)],
            [call.args for call in read.call_args_list],
        )
        token_body = requests[0].data.decode("ascii")
        self.assertIn(
            "client_id=client-id-publico.apps.googleusercontent.com",
            token_body,
        )
        self.assertNotIn("client_secret", token_body)
        self.assertEqual("pessoal@example.com", access.email)
        self.assertEqual("access-secreto", access.access_token)
        access.close()
        self.assertEqual("", access.access_token)

    def test_service_catalog_expands_identity_and_service_scopes(self):
        scopes = google.scopes_for_services(("gmail", "contacts"))

        self.assertEqual(("openid", "email"), scopes[:2])
        self.assertIn("https://www.googleapis.com/auth/gmail.modify", scopes)
        self.assertIn("https://www.googleapis.com/auth/contacts", scopes)

    def test_partial_calendar_grant_does_not_enable_calendar(self):
        granted = google.services_for_granted_scopes(
            (
                "openid",
                "email",
                "https://www.googleapis.com/auth/calendar.events",
            )
        )

        self.assertNotIn("calendar", granted)

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

    def test_configure_command_accepts_only_known_services(self):
        args = google.build_parser().parse_args(
            ["configure", "--profile", "pessoal", "--service", "gmail"]
        )

        self.assertEqual(["gmail"], args.service)

    def test_profile_services_are_updated_atomically(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "google.toml"
            path.write_text(
                '[profiles.pessoal]\n'
                'credential_ref = "APIs/Google/Accounts/Pessoal"\n'
                'services = ["gmail"]\n',
                encoding="utf-8",
            )

            google._replace_profile_services(path, "pessoal", ("gmail", "drive"))

            content = path.read_text(encoding="utf-8")
        self.assertIn('services = ["gmail", "drive"]', content)

    def test_enrollment_unions_services_and_never_sends_client_secret(self):
        requests = []

        class FakeServer:
            server_port = 43123
            parameters = {"state": "fixed-state", "code": "authorization-code"}

            def handle_request(self):
                return None

            def server_close(self):
                return None

        def opener(request, *, timeout):
            del timeout
            requests.append(request)
            if request.full_url == "https://oauth2.googleapis.com/token":
                return FakeResponse(
                    {
                        "access_token": "access-secreto",
                        "refresh_token": "refresh-secreto",
                        "token_type": "Bearer",
                        "scope": "openid email "
                        "https://www.googleapis.com/auth/gmail.modify "
                        "https://www.googleapis.com/auth/calendar.calendarlist.readonly "
                        "https://www.googleapis.com/auth/calendar.events "
                        "https://www.googleapis.com/auth/calendar.freebusy",
                    }
                )
            return FakeResponse({"email": "pessoal@example.com"})

        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "google.toml"
            config_path.write_text(
                '[profiles.pessoal]\n'
                'credential_ref = "APIs/Google/Accounts/Pessoal"\n'
                'services = ["gmail"]\n',
                encoding="utf-8",
            )
            with (
                patch.object(google, "_OAuthServer", return_value=FakeServer()),
                patch.object(
                    google.secrets,
                    "token_urlsafe",
                    side_effect=["fixed-state", "fixed-verifier"],
                ),
                patch.object(google.webbrowser, "open", return_value=True),
                patch.object(google, "write_entry_credentials") as write_credentials,
            ):
                result = google.enroll_google_profile(
                    self.config(),
                    "pessoal",
                    requested_services=("calendar",),
                    config_path=config_path,
                    opener=opener,
                )
            updated = config_path.read_text(encoding="utf-8")

        token_body = requests[0].data.decode("ascii")
        self.assertNotIn("client_secret", token_body)
        write_credentials.assert_called_once_with(
            "APIs/Google/Accounts/Pessoal",
            "pessoal@example.com",
            "refresh-secreto",
        )
        self.assertEqual(["gmail", "calendar"], result["services"])
        self.assertIn('services = ["gmail", "calendar"]', updated)

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
