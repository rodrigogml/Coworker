import tempfile
import unittest
from pathlib import Path

from scripts.windows_service import (
    WindowsServiceError,
    build_definition,
    service_exception_message,
    validate_service_name,
)


class WindowsServiceContractTests(unittest.TestCase):
    def test_scm_timeout_is_actionable(self) -> None:
        class FakeScmError(Exception):
            winerror = 1053

        message = service_exception_message(FakeScmError(), action="iniciar", name="lavelinha")
        self.assertIn("1053", message)
        self.assertIn("reinstale", message)

    def test_scm_access_denied_requests_elevated_shell(self) -> None:
        class FakeScmError(Exception):
            winerror = 5

        message = service_exception_message(FakeScmError(), action="parar", name="bis")
        self.assertIn("acesso negado", message)
        self.assertIn("Administrador", message)

    def test_remove_service_uses_win32con_delete_permission(self) -> None:
        import win32con
        from unittest.mock import patch

        class FakeService:
            def DeleteService(self, _service):
                return None

        fake_win32 = type(
            "FakeWin32",
            (),
            {
                "SERVICE_STOP": 1,
                "SERVICE_QUERY_STATUS": 2,
                "CloseServiceHandle": staticmethod(lambda _handle: None),
                "DeleteService": staticmethod(lambda _service: None),
            },
        )()
        with (
            patch("scripts.windows_service._win32", return_value=(fake_win32, None, None)),
            patch("scripts.windows_service.service_status", return_value={"installed": True, "state_name": "stopped"}),
            patch("scripts.windows_service._service_handle", return_value=(object(), FakeService())) as handle,
            patch("scripts.windows_service.validate_service_name", return_value="bis"),
        ):
            # The fake service intentionally lacks SCM behavior beyond deletion;
            # this verifies permission construction before the real API call.
            result = __import__("scripts.windows_service", fromlist=["remove_service"]).remove_service("bis")

        self.assertTrue(result["removed"])
        self.assertEqual(1 | win32con.DELETE | 2, handle.call_args.args[2])

    def test_service_name_contract(self):
        self.assertEqual(validate_service_name("ExampleInstance"), "ExampleInstance")
        with self.assertRaises(WindowsServiceError):
            validate_service_name("nome com espaço")
        with self.assertRaises(WindowsServiceError):
            validate_service_name("!")

    def test_definition_uses_instance_name_and_configured_codex_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = root / "interfaces" / "telegram"
            gateway.mkdir(parents=True)
            (gateway / "gateway.py").write_text("", encoding="utf-8")
            config = root / "data" / "config"
            config.mkdir(parents=True)
            (config / "telegram.toml").write_text(
                '[codex]\nhome_dir = "C:/isolated/ExampleInstance/codex"\n',
                encoding="utf-8",
            )
            definition = build_definition(
                root, instance_id="exampleinstance", display_name="ExampleInstance"
            )
            self.assertEqual(definition.name, "exampleinstance")
            self.assertEqual(definition.display_name, "ExampleInstance")
            self.assertEqual(str(definition.codex_home), "C:\\isolated\\ExampleInstance\\codex")
            self.assertIn("exampleinstance", str(definition.gateway_state_dir).casefold())

    def test_invalid_startup_and_timeout_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = root / "interfaces" / "telegram"
            gateway.mkdir(parents=True)
            (gateway / "gateway.py").write_text("", encoding="utf-8")
            with self.assertRaises(WindowsServiceError):
                build_definition(root, instance_id="abc", display_name="ABC", startup="bad")
            with self.assertRaises(WindowsServiceError):
                build_definition(root, instance_id="abc", display_name="ABC", stop_timeout_seconds=1)


if __name__ == "__main__":
    unittest.main()
