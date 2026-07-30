from __future__ import annotations

import json
import urllib.request
import unittest

from scripts.google_api import GoogleApiClient, paginate, validate_api_base


class FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_args):
        return json.dumps(self.payload).encode("utf-8")


class GoogleApiTests(unittest.TestCase):
    def test_validate_api_base_rejects_exfiltration_host(self):
        with self.assertRaises(ValueError):
            validate_api_base(
                "https://people.googleapis.com.attacker.example/v1",
                host="people.googleapis.com",
                path="/v1",
                field="api_base",
            )

    def test_client_uses_bearer_and_redacts_token(self):
        captured = {}

        def opener(request: urllib.request.Request, *, timeout: int):
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return FakeResponse({"value": "secret-access", "access_token": "bad"})

        client = GoogleApiClient(
            "https://people.googleapis.com/v1",
            "secret-access",
            "Contacts",
            timeout_seconds=20,
            max_response_bytes=10_000,
            opener=opener,
        )
        result = client.request("GET", "/people/me")

        self.assertEqual("Bearer secret-access", captured["authorization"])
        self.assertEqual("[REDACTED]", result["value"])
        self.assertNotIn("access_token", result)
        client.close()
        self.assertEqual("", client._access_token)

    def test_pagination_honors_max_pages(self):
        responses = iter(
            [
                {"files": [{"id": "a"}], "nextPageToken": "next"},
                {"files": [{"id": "b"}], "nextPageToken": "still-next"},
            ]
        )

        def opener(_request, *, timeout):
            del timeout
            return FakeResponse(next(responses))

        client = GoogleApiClient(
            "https://www.googleapis.com/drive/v3",
            "access",
            "Drive",
            timeout_seconds=20,
            max_response_bytes=10_000,
            opener=opener,
        )
        result = paginate(
            client,
            "/files",
            "files",
            {"pageSize": 10},
            all_pages=True,
            max_pages=2,
        )

        self.assertEqual(["a", "b"], [item["id"] for item in result["files"]])
        self.assertTrue(result["pagination"]["truncated"])

    def test_closed_client_cannot_make_another_request(self):
        client = GoogleApiClient(
            "https://people.googleapis.com/v1",
            "access",
            "Contacts",
            timeout_seconds=20,
            max_response_bytes=10_000,
            opener=lambda *_args, **_kwargs: None,
        )
        client.close()
        with self.assertRaises(ValueError):
            client.request("GET", "/people/me")

    def test_redirect_handler_never_reissues_bearer_request(self):
        from scripts.google_api import _NoRedirectHandler

        handler = _NoRedirectHandler()
        redirected = handler.redirect_request(
            None, None, 302, "Found", {}, "https://attacker.example", {}
        )
        self.assertIsNone(redirected)


if __name__ == "__main__":
    unittest.main()
