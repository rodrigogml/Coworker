from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    PROJECT_ROOT
    / "skills"
    / "cloudflare-manage"
    / "scripts"
    / "cloudflare.py"
)
SPEC = importlib.util.spec_from_file_location("cloudflare_skill", MODULE_PATH)
cloudflare = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cloudflare
SPEC.loader.exec_module(cloudflare)


ZONE = {
    "id": "a" * 32,
    "name": "example.com",
    "status": "active",
    "account": {"id": "c" * 32, "name": "Test Account"},
}
RECORD = {
    "id": "b" * 32,
    "type": "A",
    "name": "www.example.com",
    "content": "192.0.2.10",
    "ttl": 1,
    "proxied": False,
    "proxiable": True,
}


class FakeClient:
    def __init__(self) -> None:
        self.updated = []
        self.created_zones = []

    def list_zones(self, *, name=None, account_id=None):
        del account_id
        if name is None or name == ZONE["name"]:
            return [dict(ZONE)]
        return []

    def get_zone(self, zone_id):
        if zone_id == ZONE["id"]:
            return dict(ZONE)
        raise AssertionError("zone id inesperado")

    def list_records(
        self,
        zone_id,
        *,
        name=None,
        record_type=None,
        proxied=None,
    ):
        del proxied
        self._assert_zone(zone_id)
        if name not in (None, RECORD["name"]):
            return []
        if record_type not in (None, RECORD["type"]):
            return []
        return [dict(RECORD)]

    def get_record(self, zone_id, record_id):
        self._assert_zone(zone_id)
        if record_id != RECORD["id"]:
            raise AssertionError("record id inesperado")
        return dict(RECORD)

    def update_record(self, zone_id, record_id, body):
        self._assert_zone(zone_id)
        self.updated.append((record_id, body))
        return {**RECORD, **body}

    def create_zone(self, body):
        self.created_zones.append(body)
        return {**ZONE, **body}

    @staticmethod
    def _assert_zone(zone_id):
        if zone_id != ZONE["id"]:
            raise AssertionError("zone id inesperado")


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    @staticmethod
    def read():
        return json.dumps(
            {"success": True, "result": {"status": "active"}}
        ).encode()


class CloudflareSkillTests(unittest.TestCase):
    def test_load_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cloudflare.toml"
            path.write_text(
                'api_base = "https://api.example.test/v4"\n'
                'credential_ref = "APIs/CloudFlare"\n'
                "timeout_seconds = 15\n",
                encoding="utf-8",
            )
            config = cloudflare.load_config(path)
        self.assertEqual("APIs/CloudFlare", config.credential_ref)
        self.assertEqual(15, config.timeout_seconds)

    def test_record_name_accepts_relative_root_and_full_names(self):
        self.assertEqual(
            "www.example.com",
            cloudflare.canonical_record_name("www", "example.com"),
        )
        self.assertEqual(
            "example.com",
            cloudflare.canonical_record_name("@", "example.com"),
        )
        self.assertEqual(
            "api.example.com",
            cloudflare.canonical_record_name("api.example.com.", "example.com"),
        )

    def test_proxy_dry_run_does_not_mutate(self):
        client = FakeClient()
        args = argparse.Namespace(
            zone="example.com",
            record_id=None,
            name="www",
            record_type="A",
            enabled=True,
            dry_run=True,
        )
        result = cloudflare.execute_dns_proxy(client, args)
        self.assertTrue(result["dry_run"])
        self.assertEqual([], client.updated)

    def test_proxy_updates_exact_record(self):
        client = FakeClient()
        args = argparse.Namespace(
            zone="example.com",
            record_id=RECORD["id"],
            name=None,
            record_type=None,
            enabled=True,
            dry_run=False,
        )
        result = cloudflare.execute_dns_proxy(client, args)
        self.assertTrue(result["changed"])
        self.assertEqual(
            [(RECORD["id"], {"proxied": True})],
            client.updated,
        )

    def test_proxy_refuses_non_proxiable_record(self):
        client = FakeClient()

        def get_record(zone_id, record_id):
            client._assert_zone(zone_id)
            return {**RECORD, "proxiable": False}

        client.get_record = get_record
        args = argparse.Namespace(
            zone="example.com",
            record_id=RECORD["id"],
            name=None,
            record_type=None,
            enabled=True,
            dry_run=False,
        )
        with self.assertRaises(cloudflare.CloudflareToolError):
            cloudflare.execute_dns_proxy(client, args)
        self.assertEqual([], client.updated)

    def test_existing_zone_is_idempotent(self):
        client = FakeClient()
        args = argparse.Namespace(
            name="example.com",
            account_id="c" * 32,
            zone_type="full",
            dry_run=False,
        )
        result = cloudflare.execute_zones_create(client, args)
        self.assertFalse(result["changed"])
        self.assertEqual([], client.created_zones)

    def test_zone_creation_rejects_invalid_account_id(self):
        client = FakeClient()
        args = argparse.Namespace(
            name="new-example.com",
            account_id="invalido",
            zone_type="full",
            dry_run=False,
        )
        with self.assertRaises(cloudflare.CloudflareToolError):
            cloudflare.execute_zones_create(client, args)
        self.assertEqual([], client.created_zones)

    def test_http_client_uses_bearer_without_returning_it(self):
        captured = {}

        def opener(request, *, timeout):
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return FakeResponse()

        config = cloudflare.CloudflareConfig(
            "https://api.example.test/v4",
            "APIs/CloudFlare",
            17,
        )
        client = cloudflare.CloudflareClient(
            config,
            "segredo-de-teste",
            opener=opener,
        )
        result = client.verify_token()
        client.close()
        self.assertEqual("Bearer segredo-de-teste", captured["authorization"])
        self.assertEqual(17, captured["timeout"])
        self.assertNotIn("segredo-de-teste", json.dumps(result))
        self.assertEqual("", client._token)

    def test_cli_has_no_token_argument(self):
        help_text = cloudflare.build_parser().format_help()
        self.assertNotIn("--token", help_text)


if __name__ == "__main__":
    unittest.main()
