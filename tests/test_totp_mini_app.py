import hashlib
import hmac
import json
import time
import urllib.parse

from interfaces.telegram.totp_mini_app import TotpMiniApp, _password_hash, _password_ok


def _init_data(token: str, user_id: int = 7) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


def test_totp_password_hash_is_verifiable_without_plaintext():
    encoded = _password_hash("correct horse battery")
    assert encoded.startswith("pbkdf2-sha256$")
    assert _password_ok("correct horse battery", encoded)
    assert not _password_ok("wrong", encoded)


def test_mini_app_validates_telegram_init_data_and_owner():
    app = TotpMiniApp("127.0.0.1", 0, "bot-token", lambda user_id: user_id == 7)
    assert app._validate_init(_init_data("bot-token")) == 7


def test_mini_app_rejects_tampered_init_data():
    app = TotpMiniApp("127.0.0.1", 0, "bot-token", lambda _user_id: True)
    value = _init_data("bot-token").replace("auth_date=", "auth_date=1", 1)
    try:
        app._validate_init(value)
    except PermissionError:
        pass
    else:
        raise AssertionError("initData adulterado foi aceito")
