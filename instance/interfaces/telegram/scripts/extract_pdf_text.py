#!/usr/bin/env python3
"""Extrai texto limitado de um PDF pertencente ao trabalho Telegram atual."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from interfaces.telegram.job_context import (  # noqa: E402
    JobContextError,
    resolve_job_input_file,
)
from interfaces.telegram.pdf_extraction import (  # noqa: E402
    DEFAULT_MAX_CHARACTERS,
    DEFAULT_MAX_CONTENT_BYTES,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_PAGES,
    PdfExtractionError,
    extract_pdf_text,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extrai texto pesquisável de um PDF da entrada do trabalho atual."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument(
        "--max-characters", type=int, default=DEFAULT_MAX_CHARACTERS
    )
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument(
        "--max-content-bytes", type=int, default=DEFAULT_MAX_CONTENT_BYTES
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
    environment: Mapping[str, str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    effective_environment = os.environ if environment is None else environment
    try:
        source = resolve_job_input_file(
            args.input,
            project_root=project_root,
            environment=effective_environment,
        )
        result = extract_pdf_text(
            source,
            max_pages=args.max_pages,
            max_characters=args.max_characters,
            max_file_bytes=args.max_file_bytes,
            max_content_bytes=args.max_content_bytes,
        )
        payload = {"ok": True, **result.as_json()}
    except (JobContextError, PdfExtractionError, OSError) as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
