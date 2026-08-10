"""Testes do OCR local e restrito para PDFs recebidos pelo Telegram."""

from __future__ import annotations

import io
import json
import base64
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from interfaces.telegram.job_context import JobContextError, write_job_png
from interfaces.telegram.pdf_ocr import (
    BoletoCandidate,
    OcrPageResult,
    OcrText,
    PdfOcrError,
    PdfOcrResult,
    RasterizedPage,
    _bank_barcode_digit,
    _mod10_digit,
    extract_numeric_candidates,
    extract_pdf_ocr,
    parse_page_selection,
    validate_boleto_candidate,
)
from interfaces.telegram.scripts import extract_pdf_images_ocr as pdf_ocr_cli


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _minimal_pdf() -> bytes:
    return b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


def _valid_bank_barcode_and_line() -> tuple[str, str]:
    without_general = "0019" + "123400000000000000000000000000000000000"
    general = str(_bank_barcode_digit(without_general))
    barcode = without_general[:4] + general + without_general[4:]
    field_1 = barcode[0:4] + barcode[19:24]
    field_2 = barcode[24:34]
    field_3 = barcode[34:44]
    line = (
        field_1
        + str(_mod10_digit(field_1))
        + field_2
        + str(_mod10_digit(field_2))
        + field_3
        + str(_mod10_digit(field_3))
        + barcode[4]
        + barcode[5:19]
    )
    return barcode, line


class PdfOcrTests(unittest.TestCase):
    def test_page_selection_is_ordered_and_limited(self) -> None:
        self.assertEqual((1, 3, 4, 5), parse_page_selection("5,3-4,1", page_count=6, max_pages=4))
        with self.assertRaises(PdfOcrError):
            parse_page_selection(None, page_count=6, max_pages=5)
        with self.assertRaises(PdfOcrError):
            parse_page_selection("7", page_count=6, max_pages=5)

    def test_bank_line_normalization_and_checksums(self) -> None:
        barcode, line = _valid_bank_barcode_and_line()
        formatted = f"{line[:5]}.{line[5:10]} {line[10:15]}-{line[15:21]} {line[21:]}"

        self.assertEqual((line,), extract_numeric_candidates(formatted))
        self.assertEqual("checksum_valid", validate_boleto_candidate(line)[1]["status"])
        self.assertEqual("checksum_valid", validate_boleto_candidate(barcode)[1]["status"])

        invalid = line[:-1] + str((int(line[-1]) + 1) % 10)
        self.assertEqual("checksum_invalid", validate_boleto_candidate(invalid)[1]["status"])
        self.assertEqual(
            "inconclusive",
            validate_boleto_candidate("805" + ("0" * 41))[1]["status"],
        )

    def test_ocr_result_keeps_candidate_validation_separate_from_confidence(self) -> None:
        _barcode, line = _valid_bank_barcode_and_line()
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "invoice.pdf"
            pdf.write_bytes(_minimal_pdf())
            result = extract_pdf_ocr(
                pdf,
                dpi=100,
                page_probe=lambda _path: ((72.0, 72.0),),
                page_renderer=lambda _path, page, _dpi: RasterizedPage(page, 100, 100, PNG),
                ocr_engine=lambda *_args, **_kwargs: OcrText(f"Total\n{line}", 0.73),
                barcode_reader=lambda _png: (),
            )

        self.assertFalse(result.needs_ocr)
        self.assertEqual(1, result.pages_processed)
        self.assertEqual(line, result.candidates[0].value)
        self.assertEqual(0.73, result.candidates[0].confidence)
        self.assertEqual("checksum_valid", result.candidates[0].validation["status"])

    def test_blank_page_returns_empty_result_without_false_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "blank.pdf"
            pdf.write_bytes(_minimal_pdf())
            result = extract_pdf_ocr(
                pdf,
                dpi=100,
                page_probe=lambda _path: ((72.0, 72.0),),
                page_renderer=lambda _path, page, _dpi: RasterizedPage(page, 100, 100, PNG),
                ocr_engine=lambda *_args, **_kwargs: OcrText("", None),
                barcode_reader=lambda _png: (),
            )

        self.assertEqual((), result.candidates)
        self.assertEqual((), result.errors)
        self.assertFalse(result.needs_ocr)

    def test_file_page_pixel_and_memory_limits_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "large.pdf"
            pdf.write_bytes(_minimal_pdf() + b"x" * 200)
            with self.assertRaises(PdfOcrError):
                extract_pdf_ocr(pdf, max_file_bytes=20)
            with self.assertRaises(PdfOcrError):
                extract_pdf_ocr(pdf, max_pages=1, page_probe=lambda _path: ((72, 72), (72, 72)))
            with self.assertRaises(PdfOcrError):
                extract_pdf_ocr(pdf, dpi=400, max_pixels_per_page=100, page_probe=lambda _path: ((72, 72),))
            with self.assertRaises(PdfOcrError):
                extract_pdf_ocr(pdf, dpi=100, max_memory_bytes=100, page_probe=lambda _path: ((72, 72),))

    def test_png_writer_is_confined_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            derived = root / "data" / "telegram" / "jobs" / "4" / "derived"
            derived.mkdir(parents=True)
            environment = {"COWORKER_JOB_DERIVED": str(derived)}
            first = write_job_png("pdf-ocr", "page-1", PNG, project_root=root, environment=environment)
            second = write_job_png("pdf-ocr", "page-1", PNG, project_root=root, environment=environment)

            self.assertEqual(first.path, second.path)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(derived, first.path.parent)
            with self.assertRaises(JobContextError):
                write_job_png("pdf-ocr", "bad", b"not-png", project_root=root, environment=environment)

    def test_public_cli_rejects_external_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            input_dir = root / "data" / "telegram" / "jobs" / "4" / "input"
            derived = input_dir.parent / "derived"
            input_dir.mkdir(parents=True)
            derived.mkdir()
            outside = Path(temporary) / "outside.pdf"
            outside.write_bytes(_minimal_pdf())
            output = io.StringIO()
            with redirect_stdout(output):
                status = pdf_ocr_cli.main(
                    ["--input", str(outside)],
                    project_root=root,
                    environment={
                        "COWORKER_JOB_INPUT": str(input_dir),
                        "COWORKER_JOB_DERIVED": str(derived),
                    },
                )
            payload = json.loads(output.getvalue())

        self.assertEqual(1, status)
        self.assertFalse(payload["ok"])
        self.assertEqual([], payload["candidates"])

    def test_public_cli_emits_sanitized_json_and_derived_name_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            input_dir = root / "data" / "telegram" / "jobs" / "4" / "input"
            derived = input_dir.parent / "derived"
            input_dir.mkdir(parents=True)
            derived.mkdir()
            pdf = input_dir / "invoice.pdf"
            pdf.write_bytes(_minimal_pdf())

            def fake_extractor(_source: Path, **kwargs: object) -> PdfOcrResult:
                image_name = kwargs["image_writer"](1, PNG)  # type: ignore[index,operator]
                candidate = BoletoCandidate(1, "1" * 44, "unknown", ("ocr",), 0.51, {"status": "inconclusive", "checks": {}})
                page = OcrPageResult(1, "texto", 0.51, image_name, ())
                return PdfOcrResult(False, 1, 1, (page,), (candidate,), (), ())

            output = io.StringIO()
            with redirect_stdout(output):
                status = pdf_ocr_cli.main(
                    ["--input", str(pdf), "--save-images"],
                    project_root=root,
                    environment={
                        "COWORKER_JOB_INPUT": str(input_dir),
                        "COWORKER_JOB_DERIVED": str(derived),
                    },
                    extractor=fake_extractor,
                )
            payload = json.loads(output.getvalue())

        self.assertEqual(0, status)
        self.assertTrue(payload["ok"])
        self.assertNotIn(str(derived), payload["pages"][0]["image"])
        self.assertTrue(payload["pages"][0]["image"].endswith(".png"))


if __name__ == "__main__":
    unittest.main()
