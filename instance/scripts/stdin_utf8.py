"""Leitura de stdin tolerante ao transporte nativo do Windows PowerShell."""

from __future__ import annotations

import sys


def read_text(max_bytes: int | None = None) -> str:
    """Lê UTF-8 e os encodings Unicode emitidos pelo Windows PowerShell clássico."""
    stream = getattr(sys.stdin, "buffer", None)
    raw = stream.read() if stream is not None else sys.stdin.read().encode("utf-8")
    if max_bytes is not None and len(raw) > max_bytes:
        raise ValueError(f"A entrada excede {max_bytes} bytes.")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    # Windows PowerShell 5.1 may send UTF-16LE without a BOM to stdin.
    if b"\x00" in raw:
        try:
            return raw.decode("utf-16le")
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8")
