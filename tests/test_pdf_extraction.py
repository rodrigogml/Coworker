"""Testes da extração segura de PDFs recebidos pelo Telegram."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from interfaces.telegram.config import ProcessorConfig
from interfaces.telegram.contracts import Attachment
from interfaces.telegram.job_context import JobContextError, resolve_job_input_file
from interfaces.telegram.pdf_extraction import PdfExtractionError, extract_pdf_text
from interfaces.telegram.processors import ProcessorRegistry
from interfaces.telegram.scripts import extract_pdf_text as extract_pdf_cli


def _pdf_bytes(page_texts: list[str]) -> bytes:
    """Monta um PDF mínimo e pesquisável sem depender de gerador externo."""
    font_id = 3 + (2 * len(page_texts))
    page_ids = [3 + (2 * index) for index in range(len(page_texts))]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            f"<< /Type /Pages /Kids [{' '.join(f'{value} 0 R' for value in page_ids)}] "
            f"/Count {len(page_ids)} >>"
        ).encode("ascii"),
        font_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, text in enumerate(page_texts):
        page_id = page_ids[index]
        content_id = page_id + 1
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )

    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id in range(1, font_id + 1):
        offsets.append(len(payload))
        payload.extend(f"{object_id} 0 obj\n".encode("ascii"))
        payload.extend(objects[object_id])
        payload.extend(b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {font_id + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size {font_id + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(payload)


class PdfExtractionTests(unittest.TestCase):
    def test_searchable_pdf_extracts_text_and_honors_page_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.pdf"
            path.write_bytes(_pdf_bytes(["Comprovante PIX valor 150,00", "Página dois"]))

            result = extract_pdf_text(
                path,
                max_pages=1,
                max_characters=1_000,
                max_file_bytes=100_000,
                max_content_bytes=100_000,
            )

        self.assertIn("Comprovante PIX valor 150,00", result.text)
        self.assertNotIn("Página dois", result.text)
        self.assertEqual(2, result.page_count)
        self.assertEqual(1, result.pages_processed)
        self.assertTrue(result.truncated)
        self.assertFalse(result.needs_ocr)
        self.assertIn("page_limit_reached", result.warnings)
        self.assertEqual("pypdf", result.extraction_method)

    def test_pdf_without_searchable_text_requests_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scan.pdf"
            path.write_bytes(_pdf_bytes([""]))

            result = extract_pdf_text(
                path,
                max_pages=10,
                max_characters=1_000,
                max_file_bytes=100_000,
                max_content_bytes=100_000,
            )

        self.assertEqual("", result.text)
        self.assertTrue(result.needs_ocr)
        self.assertIn("no_searchable_text", result.warnings)

    def test_gateway_processor_applies_pdf_extraction_automatically(self) -> None:
        registry = ProcessorRegistry(ProcessorConfig(1_000, 10, 1_000, 10, 60, 5))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.pdf"
            path.write_bytes(_pdf_bytes(["Favorecido ACME valor 150,00"]))

            prepared = registry.prepare(
                Attachment(
                    "current",
                    "file-1",
                    original_name="receipt.pdf",
                    detected_mime="application/pdf",
                    local_path=path,
                )
            )

        self.assertTrue(prepared.available)
        self.assertEqual("pypdf", prepared.processor)
        self.assertIn("Favorecido ACME valor 150,00", prepared.text or "")
        self.assertIn("conteúdo não confiável", prepared.note)

    def test_character_limit_is_reported_without_excess_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "long.pdf"
            path.write_bytes(_pdf_bytes(["ABCDEFGHIJKLMNOPQRSTUVWXYZ"]))

            result = extract_pdf_text(
                path,
                max_pages=10,
                max_characters=12,
                max_file_bytes=100_000,
                max_content_bytes=100_000,
            )

        self.assertLessEqual(len(result.text), 12)
        self.assertTrue(result.truncated)
        self.assertIn("character_limit_reached", result.warnings)

    def test_missing_non_pdf_and_oversized_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.pdf"
            invalid = root / "invalid.pdf"
            invalid.write_bytes(b"not a pdf")
            oversized = root / "large.pdf"
            oversized.write_bytes(_pdf_bytes(["x"]) + (b"x" * 1_000))

            with self.assertRaises(PdfExtractionError):
                extract_pdf_text(missing)
            with self.assertRaises(PdfExtractionError):
                extract_pdf_text(invalid)
            with self.assertRaises(PdfExtractionError):
                extract_pdf_text(oversized, max_file_bytes=100)

    def test_job_input_resolver_rejects_external_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            input_dir = root / "data" / "telegram" / "jobs" / "7" / "input"
            input_dir.mkdir(parents=True)
            inside = input_dir / "receipt.pdf"
            inside.write_bytes(_pdf_bytes(["ok"]))
            outside = Path(temporary) / "outside.pdf"
            outside.write_bytes(_pdf_bytes(["fora"]))
            environment = {"COWORKER_JOB_INPUT": str(input_dir)}

            resolved = resolve_job_input_file(
                inside, project_root=root, environment=environment
            )
            with self.assertRaises(JobContextError):
                resolve_job_input_file(
                    outside, project_root=root, environment=environment
                )

        self.assertEqual(inside, resolved)

    def test_public_cli_returns_bounded_structured_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            input_dir = root / "data" / "telegram" / "jobs" / "7" / "input"
            input_dir.mkdir(parents=True)
            path = input_dir / "receipt.pdf"
            path.write_bytes(_pdf_bytes(["Comprovante pesquisável"]))
            output = io.StringIO()
            with redirect_stdout(output):
                status = extract_pdf_cli.main(
                    ["--input", str(path), "--max-characters", "1000"],
                    project_root=root,
                    environment={"COWORKER_JOB_INPUT": str(input_dir)},
                )
            payload = json.loads(output.getvalue())

        self.assertEqual(0, status)
        self.assertTrue(payload["ok"])
        self.assertIn("Comprovante pesquisável", payload["text"])
        self.assertEqual("pypdf", payload["extraction_method"])
        self.assertIn("metadata_safe", payload)


if __name__ == "__main__":
    unittest.main()
