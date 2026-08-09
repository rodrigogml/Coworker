#!/usr/bin/env python3
"""Ponto de entrada seguro da skill TOTP."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from skills.totp.core import TotpError, decode_qr, format_codes, parse_input  # noqa: E402
from skills.totp.vault import TotpVaultError, find_records, read, store  # noqa: E402


def output(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gerencia tokens TOTP no cofre local.")
    sub = parser.add_subparsers(dest="operation", required=True)
    enroll = sub.add_parser("enroll")
    enroll.add_argument("--issuer", default="")
    enroll.add_argument("--account", default="")
    enroll.add_argument("--qr", type=Path)
    code = sub.add_parser("code")
    code.add_argument("selector")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    try:
        if args.operation == "enroll":
            raw = sys.stdin.read().strip()
            if args.qr:
                raw = decode_qr(args.qr)
            config = parse_input(raw, issuer=args.issuer, account=args.account)
            record = store(config)
            output({"ok": True, "stored": True, "entry": record.entry, "issuer": config.issuer, "account": config.account})
        elif args.operation == "code":
            matches = find_records(args.selector)
            if len(matches) != 1:
                output({"ok": False, "ambiguous": len(matches) > 1, "matches": matches})
                return 2
            record = read(matches[0]["entry"])
            output({"ok": True, "entry": record.entry, "text": format_codes(record.config)})
        else:
            output({"ok": True, "records": find_records("")})
        return 0
    except (TotpError, TotpVaultError, OSError) as exc:
        output({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
