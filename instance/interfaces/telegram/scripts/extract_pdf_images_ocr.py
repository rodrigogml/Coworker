#!/usr/bin/env python3
"""Rasteriza páginas e executa OCR local em um PDF do trabalho Telegram atual."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from interfaces.telegram.job_context import (  # noqa: E402
    JobContextError,
    resolve_job_input_file,
    write_job_png,
)
from interfaces.telegram.pdf_ocr import (  # noqa: E402
    DEFAULT_DPI,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_MEMORY_BYTES,
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_PIXELS_PER_PAGE,
    DEFAULT_MAX_TEXT_CHARACTERS,
    DEFAULT_MAX_TOTAL_PIXELS,
    DEFAULT_OCR_TIMEOUT_SECONDS,
    PdfOcrError,
    PdfOcrResult,
    extract_pdf_ocr,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Executa rasterização e OCR exclusivamente locais em um PDF do job."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--pages", help="Páginas 1-based, por exemplo: 1,3-5")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--language", default="por+eng")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument(
        "--max-pixels-per-page", type=int, default=DEFAULT_MAX_PIXELS_PER_PAGE
    )
    parser.add_argument("--max-total-pixels", type=int, default=DEFAULT_MAX_TOTAL_PIXELS)
    parser.add_argument("--max-memory-bytes", type=int, default=DEFAULT_MAX_MEMORY_BYTES)
    parser.add_argument(
        "--max-text-characters", type=int, default=DEFAULT_MAX_TEXT_CHARACTERS
    )
    parser.add_argument(
        "--ocr-timeout-seconds", type=int, default=DEFAULT_OCR_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--save-images",
        action="store_true",
        help="Persiste PNGs determinísticos somente no derived/ do job atual.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
    environment: Mapping[str, str] | None = None,
    extractor: Callable[..., PdfOcrResult] = extract_pdf_ocr,
) -> int:
    args = build_parser().parse_args(argv)
    effective_environment = os.environ if environment is None else environment
    try:
        source = resolve_job_input_file(
            args.input,
            project_root=project_root,
            environment=effective_environment,
        )

        def persist_image(page: int, png: bytes) -> str:
            artifact = write_job_png(
                "pdf-ocr",
                f"{source.name}:page:{page}:dpi:{args.dpi}",
                png,
                project_root=project_root,
                environment=effective_environment,
                max_bytes=min(args.max_memory_bytes, 50 * 1024 * 1024),
            )
            return artifact.path.name

        result = extractor(
            source,
            pages=args.pages,
            dpi=args.dpi,
            language=args.language,
            max_pages=args.max_pages,
            max_file_bytes=args.max_file_bytes,
            max_pixels_per_page=args.max_pixels_per_page,
            max_total_pixels=args.max_total_pixels,
            max_memory_bytes=args.max_memory_bytes,
            max_text_characters=args.max_text_characters,
            ocr_timeout_seconds=args.ocr_timeout_seconds,
            image_writer=persist_image if args.save_images else None,
        )
        payload = {"ok": not result.errors, **result.as_json()}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if payload["ok"] else 1
    except (JobContextError, PdfOcrError, OSError) as exc:
        payload = {
            "ok": False,
            "needs_ocr": True,
            "pages_processed": 0,
            "candidates": [],
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
