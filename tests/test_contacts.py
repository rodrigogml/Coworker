from __future__ import annotations

import argparse
import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "skills" / "contacts" / "scripts" / "contacts.py"
SPEC = importlib.util.spec_from_file_location("contacts_skill", MODULE_PATH)
contacts = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = contacts
SPEC.loader.exec_module(contacts)


class FakeClient:
    def __init__(self):
        self.requests = []

    def request(self, method, path, *, query=None, payload=None):
        self.requests.append((method, path, query, payload))
        if method == "GET":
            return {
                "resourceName": "people/abc",
                "etag": "group-etag",
                "metadata": {"sources": [{"etag": "etag-atual", "type": "CONTACT"}]},
            }
        return {"resourceName": "people/abc"}


class ContactsTests(unittest.TestCase):
    def config(self):
        return contacts.GoogleServiceConfig(
            "https://people.googleapis.com/v1",
            PROJECT_ROOT / "data" / "config" / "google.toml",
            30,
            100,
            20,
            5_242_880,
            {
                "person_fields": (
                    "metadata,names,emailAddresses,phoneNumbers,"
                    "organizations,biographies"
                )
            },
        )

    def test_load_config(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "contacts.toml"
            path.write_text(
                'api_base = "https://people.googleapis.com/v1"\n'
                'google_config = "data/config/google.toml"\n'
                "timeout_seconds = 20\npage_size = 100\nmax_pages = 5\n"
                "max_response_bytes = 1000000\n"
                'person_fields = "metadata,names,emailAddresses,phoneNumbers,organizations,biographies"\n',
                encoding="utf-8",
            )
            config = contacts.load_config(path)
        self.assertIn("metadata", config.extras["person_fields"])

    def test_create_requires_identity_field(self):
        args = argparse.Namespace(
            name=None,
            email=[],
            phone=[],
            organization="Empresa",
            title=None,
            note=None,
            dry_run=True,
        )
        with self.assertRaises(contacts.ContactsToolError):
            contacts.contact_create(None, self.config(), args)

    def test_update_reads_current_metadata_before_patch(self):
        client = FakeClient()
        args = argparse.Namespace(
            resource_name="people/abc",
            name=None,
            email=[],
            phone=["+55 11 99999-9999"],
            organization=None,
            title=None,
            note=None,
            clear_name=False,
            clear_emails=False,
            clear_phones=False,
            clear_organization=False,
            clear_note=False,
            dry_run=False,
        )
        contacts.contact_update(client, self.config(), args)
        self.assertEqual("GET", client.requests[0][0])
        self.assertEqual("PATCH", client.requests[1][0])
        self.assertEqual(
            "etag-atual",
            client.requests[1][3]["metadata"]["sources"][0]["etag"],
        )

    def test_delete_dry_run_is_closed_operation(self):
        args = argparse.Namespace(resource_name="people/abc", dry_run=True)
        result = contacts.contact_delete(None, self.config(), args)
        self.assertEqual("DELETE", result["request"]["method"])
        self.assertEqual("/people/abc:deleteContact", result["request"]["path"])

    def test_parser_exposes_no_token_or_arbitrary_path(self):
        help_text = contacts.build_parser().format_help()
        self.assertNotIn("--token", help_text)
        self.assertNotIn("--method", help_text)

    def test_doctor_uses_contacts_scope(self):
        client = FakeClient()
        contacts.doctor(client, self.config(), argparse.Namespace())
        self.assertEqual("GET", client.requests[0][0])
        self.assertEqual("/people/me/connections", client.requests[0][1])
        self.assertEqual(1, client.requests[0][2]["pageSize"])
        self.assertEqual(
            "READ_SOURCE_TYPE_CONTACT",
            client.requests[0][2]["sources"],
        )

    def test_group_members_rejects_same_contact_in_both_sets(self):
        args = argparse.Namespace(
            group_resource="contactGroups/friends",
            add=["people/abc"],
            remove=["people/abc"],
            dry_run=True,
        )
        with self.assertRaises(contacts.ContactsToolError):
            contacts.group_members_modify(None, self.config(), args)

    def test_group_update_reads_etag_before_put(self):
        client = FakeClient()
        args = argparse.Namespace(
            group_resource="contactGroups/friends",
            name="Amigos",
            dry_run=False,
        )
        contacts.group_update(client, self.config(), args)
        self.assertEqual("GET", client.requests[0][0])
        self.assertEqual("PUT", client.requests[1][0])
        self.assertEqual("group-etag", client.requests[1][3]["contactGroup"]["etag"])


if __name__ == "__main__":
    unittest.main()
