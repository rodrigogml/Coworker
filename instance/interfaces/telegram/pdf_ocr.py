"""Rasterização, OCR local e validação conservadora de candidatos de boleto."""

from __future__ import annotations

import csv
import importlib.util
import io
import math
import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence

from interfaces.telegram.pdf_extraction import PdfExtractionError, validate_pdf_file


DEFAULT_DPI = 220
DEFAULT_MAX_PAGES = 5
DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_PIXELS_PER_PAGE = 24_000_000
DEFAULT_MAX_TOTAL_PIXELS = 60_000_000
DEFAULT_MAX_MEMORY_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_TEXT_CHARACTERS = 30_000
DEFAULT_OCR_TIMEOUT_SECONDS = 60
HARD_MAX_DPI = 400
HARD_MAX_PAGES = 20
HARD_MAX_PIXELS_PER_PAGE = 40_000_000
HARD_MAX_TOTAL_PIXELS = 120_000_000
HARD_MAX_MEMORY_BYTES = 512 * 1024 * 1024
HARD_MAX_TEXT_CHARACTERS = 100_000
HARD_MAX_OCR_TIMEOUT_SECONDS = 180
_DIGIT_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ .\-]?){43,47}\d(?!\d)")
_LANGUAGE_PATTERN = re.compile(r"^[a-z]{3}(?:\+[a-z]{3}){0,3}$")


class PdfOcrError(RuntimeError):
    """Indica dependência ausente, limite inseguro ou falha técnica local."""


@dataclass(frozen=True)
class RasterizedPage:
    number: int
    width: int
    height: int
    png: bytes


@dataclass(frozen=True)
class OcrText:
    text: str
    confidence: float | None


@dataclass(frozen=True)
class OcrPageResult:
    page: int
    text: str
    confidence: float | None
    image: str | None
    errors: tuple[str, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "page": self.page,
            "text": self.text,
            "confidence": self.confidence,
            "image": self.image,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class BoletoCandidate:
    page: int
    value: str
    kind: str
    sources: tuple[str, ...]
    confidence: float | None
    validation: dict[str, object]

    def as_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PdfOcrResult:
    needs_ocr: bool
    page_count: int
    pages_processed: int
    pages: tuple[OcrPageResult, ...]
    candidates: tuple[BoletoCandidate, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "needs_ocr": self.needs_ocr,
            "page_count": self.page_count,
            "pages_processed": self.pages_processed,
            "pages": [page.as_json() for page in self.pages],
            "candidates": [candidate.as_json() for candidate in self.candidates],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def _bounded_integer(value: int, label: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise PdfOcrError(f"'{label}' deve ficar entre {minimum} e {maximum}.")
    return value


def _safe_text(value: str) -> str:
    return "".join(
        character
        for character in value
        if character in {"\n", "\t"}
        or not unicodedata.category(character).startswith("C")
    ).strip()


def parse_page_selection(
    raw: str | None,
    *,
    page_count: int,
    max_pages: int,
) -> tuple[int, ...]:
    """Converte páginas 1-based e intervalos em uma seleção ordenada e limitada."""
    if page_count < 0:
        raise PdfOcrError("O PDF informou uma quantidade de páginas inválida.")
    if raw is None or not raw.strip():
        if page_count > max_pages:
            raise PdfOcrError(
                "O PDF excede --max-pages; informe uma seleção explícita em --pages."
            )
        return tuple(range(1, page_count + 1))
    selected: set[int] = set()
    for item in raw.split(","):
        token = item.strip()
        if not token:
            raise PdfOcrError("A seleção de páginas contém um item vazio.")
        if "-" in token:
            pieces = token.split("-", 1)
            if len(pieces) != 2 or not all(piece.isdigit() for piece in pieces):
                raise PdfOcrError("Use páginas no formato 1,3-5.")
            start, end = (int(piece) for piece in pieces)
            if start > end:
                raise PdfOcrError("O início do intervalo de páginas supera o fim.")
            selected.update(range(start, end + 1))
        elif token.isdigit():
            selected.add(int(token))
        else:
            raise PdfOcrError("Use páginas no formato 1,3-5.")
    if not selected or min(selected) < 1 or max(selected) > page_count:
        raise PdfOcrError("A seleção contém uma página inexistente.")
    if len(selected) > max_pages:
        raise PdfOcrError("A seleção excede --max-pages.")
    return tuple(sorted(selected))


def _mod10_digit(value: str) -> int:
    total = 0
    weight = 2
    for character in reversed(value):
        product = int(character) * weight
        total += product if product < 10 else product - 9
        weight = 1 if weight == 2 else 2
    return (10 - (total % 10)) % 10


def _bank_barcode_digit(value_without_digit: str) -> int:
    total = 0
    weight = 2
    for character in reversed(value_without_digit):
        total += int(character) * weight
        weight = 2 if weight == 9 else weight + 1
    digit = 11 - (total % 11)
    return 1 if digit in {0, 10, 11} else digit


def _collection_mod11_digit(value: str) -> int:
    total = 0
    weight = 2
    for character in reversed(value):
        total += int(character) * weight
        weight = 2 if weight == 9 else weight + 1
    digit = 11 - (total % 11)
    return 0 if digit in {10, 11} else digit


def _validation(status: str, checks: dict[str, bool | None]) -> dict[str, object]:
    return {"status": status, "checks": checks}


def validate_boleto_candidate(value: str) -> tuple[str, dict[str, object]]:
    """Valida apenas dígitos verificadores conhecidos, sem certificar autenticidade."""
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) == 47:
        checks: dict[str, bool | None] = {
            "field_1": _mod10_digit(digits[0:9]) == int(digits[9]),
            "field_2": _mod10_digit(digits[10:20]) == int(digits[20]),
            "field_3": _mod10_digit(digits[21:31]) == int(digits[31]),
        }
        barcode = (
            digits[0:4]
            + digits[32]
            + digits[33:47]
            + digits[4:9]
            + digits[10:20]
            + digits[21:31]
        )
        checks["general"] = (
            _bank_barcode_digit(barcode[0:4] + barcode[5:]) == int(barcode[4])
        )
        status = "checksum_valid" if all(checks.values()) else "checksum_invalid"
        return "bank_slip_line_47", _validation(status, checks)
    if len(digits) == 44 and digits[0] != "8":
        valid = _bank_barcode_digit(digits[0:4] + digits[5:]) == int(digits[4])
        return "bank_barcode_44", _validation(
            "checksum_valid" if valid else "checksum_invalid",
            {"general": valid},
        )
    if len(digits) == 48 and digits[0] == "8":
        selector = digits[2]
        algorithm = "mod10" if selector in {"6", "7"} else "mod11" if selector in {"8", "9"} else None
        if algorithm is None:
            return "collection_line_48", _validation(
                "inconclusive", {"algorithm_known": None}
            )
        digit_function = _mod10_digit if algorithm == "mod10" else _collection_mod11_digit
        checks = {
            f"field_{index + 1}": digit_function(digits[index * 12 : index * 12 + 11])
            == int(digits[index * 12 + 11])
            for index in range(4)
        }
        barcode = "".join(digits[index * 12 : index * 12 + 11] for index in range(4))
        checks["general"] = digit_function(barcode[0:3] + barcode[4:]) == int(barcode[3])
        return "collection_line_48", _validation(
            "checksum_valid" if all(checks.values()) else "checksum_invalid",
            checks,
        )
    if len(digits) == 44 and digits[0] == "8":
        selector = digits[2]
        digit_function = (
            _mod10_digit
            if selector in {"6", "7"}
            else _collection_mod11_digit
            if selector in {"8", "9"}
            else None
        )
        if digit_function is None:
            return "collection_barcode_44", _validation(
                "inconclusive", {"algorithm_known": None}
            )
        valid = digit_function(digits[0:3] + digits[4:]) == int(digits[3])
        return "collection_barcode_44", _validation(
            "checksum_valid" if valid else "checksum_invalid",
            {"general": valid},
        )
    return "unknown", _validation("inconclusive", {})


def extract_numeric_candidates(text: str) -> tuple[str, ...]:
    """Normaliza sequências OCR de 44, 47 ou 48 dígitos sem corrigir valores."""
    candidates: list[str] = []
    for line in text.splitlines() or [text]:
        for match in _DIGIT_CANDIDATE.finditer(line):
            digits = "".join(character for character in match.group() if character.isdigit())
            if len(digits) in {44, 47, 48} and digits not in candidates:
                candidates.append(digits)
    return tuple(candidates)


def locate_tesseract() -> Path:
    """Localiza somente o executável conhecido; não aceita comando vindo do job."""
    discovered = shutil.which("tesseract")
    candidates = [
        Path(discovered) if discovered else None,
        Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
        Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate.resolve()
    raise PdfOcrError(
        "Tesseract OCR não foi localizado. Instale-o localmente fora do job e "
        "disponibilize o executável no PATH."
    )


def dependency_status() -> dict[str, bool]:
    """Informa disponibilidade local sem instalar, iniciar ou consultar serviços."""
    try:
        locate_tesseract()
        tesseract = True
    except PdfOcrError:
        tesseract = False
    return {
        "pypdfium2": importlib.util.find_spec("pypdfium2") is not None,
        "pillow": importlib.util.find_spec("PIL") is not None,
        "tesseract": tesseract,
    }


def run_tesseract_ocr(
    png: bytes,
    *,
    dpi: int,
    language: str,
    timeout_seconds: int,
    max_output_bytes: int,
) -> OcrText:
    """Executa Tesseract local sem shell e interpreta somente TSV limitado."""
    if not _LANGUAGE_PATTERN.fullmatch(language):
        raise PdfOcrError("O idioma OCR deve usar códigos como 'por' ou 'por+eng'.")
    executable = locate_tesseract()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [
                str(executable),
                "stdin",
                "stdout",
                "--dpi",
                str(dpi),
                "-l",
                language,
                "--psm",
                "6",
                "tsv",
            ],
            input=png,
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout_seconds,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired as exc:
        raise PdfOcrError("O OCR excedeu o tempo limite da página.") from exc
    except OSError as exc:
        raise PdfOcrError("Não foi possível iniciar o Tesseract local.") from exc
    if completed.returncode != 0:
        raise PdfOcrError(
            "O Tesseract falhou. Verifique a instalação e os pacotes de idioma locais."
        )
    if len(completed.stdout) > max_output_bytes:
        raise PdfOcrError("A saída do OCR excedeu o limite de memória permitido.")
    try:
        decoded = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PdfOcrError("O Tesseract devolveu texto em codificação inválida.") from exc
    lines: dict[tuple[str, str, str], list[str]] = {}
    confidences: list[float] = []
    reader = csv.DictReader(io.StringIO(decoded), delimiter="\t")
    for row in reader:
        word = _safe_text(str(row.get("text") or ""))
        if not word:
            continue
        key = (
            str(row.get("block_num") or ""),
            str(row.get("par_num") or ""),
            str(row.get("line_num") or ""),
        )
        lines.setdefault(key, []).append(word)
        try:
            confidence = float(str(row.get("conf") or "-1"))
        except ValueError:
            confidence = -1
        if confidence >= 0:
            confidences.append(confidence / 100)
    text = "\n".join(" ".join(words) for words in lines.values())
    confidence = (
        round(sum(confidences) / len(confidences), 4) if confidences else None
    )
    return OcrText(_safe_text(text), confidence)


def read_local_barcodes(png: bytes) -> tuple[str, ...]:
    """Lê códigos localmente quando zxing-cpp e Pillow estiverem disponíveis."""
    try:
        from PIL import Image
        import zxingcpp
    except ImportError:
        return ()
    try:
        with Image.open(io.BytesIO(png)) as image:
            results = zxingcpp.read_barcodes(image)
    except Exception:
        return ()
    values: list[str] = []
    for result in results:
        digits = "".join(character for character in str(result.text) if character.isdigit())
        if len(digits) in {44, 47, 48} and digits not in values:
            values.append(digits)
    return tuple(values)


def pdf_page_sizes(path: Path) -> tuple[tuple[float, float], ...]:
    """Lê somente as dimensões das páginas usando PDFium local."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise PdfOcrError(
            "pypdfium2 não está instalado; instale as dependências do projeto fora do job."
        ) from exc
    try:
        document = pdfium.PdfDocument(str(path))
    except Exception as exc:
        raise PdfOcrError("O PDF não pôde ser aberto pelo rasterizador local.") from exc
    sizes: list[tuple[float, float]] = []
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                width, height = page.get_size()
                sizes.append((float(width), float(height)))
            finally:
                page.close()
    finally:
        document.close()
    return tuple(sizes)


def render_pdf_page(path: Path, page_number: int, dpi: int) -> RasterizedPage:
    """Rasteriza uma única página e libera PDFium/Pillow antes da próxima."""
    try:
        import pypdfium2 as pdfium
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise PdfOcrError(
            "pypdfium2 ou Pillow não está instalado; instale as dependências do "
            "projeto fora do job."
        ) from exc
    try:
        document = pdfium.PdfDocument(str(path))
        page = document[page_number - 1]
        bitmap = page.render(scale=dpi / 72)
        image = bitmap.to_pil().convert("RGB")
        stream = io.BytesIO()
        image.save(stream, format="PNG", optimize=True)
        return RasterizedPage(page_number, image.width, image.height, stream.getvalue())
    except Exception as exc:
        raise PdfOcrError(f"A página {page_number} não pôde ser rasterizada.") from exc
    finally:
        for item in (locals().get("image"), locals().get("bitmap"), locals().get("page"), locals().get("document")):
            try:
                if item is not None:
                    item.close()
            except Exception:
                pass


def extract_pdf_ocr(
    path: Path,
    *,
    pages: str | None = None,
    dpi: int = DEFAULT_DPI,
    language: str = "por+eng",
    max_pages: int = DEFAULT_MAX_PAGES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_pixels_per_page: int = DEFAULT_MAX_PIXELS_PER_PAGE,
    max_total_pixels: int = DEFAULT_MAX_TOTAL_PIXELS,
    max_memory_bytes: int = DEFAULT_MAX_MEMORY_BYTES,
    max_text_characters: int = DEFAULT_MAX_TEXT_CHARACTERS,
    ocr_timeout_seconds: int = DEFAULT_OCR_TIMEOUT_SECONDS,
    page_probe: Callable[[Path], Sequence[tuple[float, float]]] = pdf_page_sizes,
    page_renderer: Callable[[Path, int, int], RasterizedPage] = render_pdf_page,
    ocr_engine: Callable[..., OcrText] = run_tesseract_ocr,
    barcode_reader: Callable[[bytes], tuple[str, ...]] = read_local_barcodes,
    image_writer: Callable[[int, bytes], str] | None = None,
) -> PdfOcrResult:
    """Processa páginas sequencialmente e descarta cada imagem após seu uso."""
    dpi = _bounded_integer(dpi, "dpi", 100, HARD_MAX_DPI)
    max_pages = _bounded_integer(max_pages, "max_pages", 1, HARD_MAX_PAGES)
    max_pixels_per_page = _bounded_integer(
        max_pixels_per_page, "max_pixels_per_page", 1, HARD_MAX_PIXELS_PER_PAGE
    )
    max_total_pixels = _bounded_integer(
        max_total_pixels, "max_total_pixels", 1, HARD_MAX_TOTAL_PIXELS
    )
    max_memory_bytes = _bounded_integer(
        max_memory_bytes, "max_memory_bytes", 1, HARD_MAX_MEMORY_BYTES
    )
    max_text_characters = _bounded_integer(
        max_text_characters, "max_text_characters", 1, HARD_MAX_TEXT_CHARACTERS
    )
    ocr_timeout_seconds = _bounded_integer(
        ocr_timeout_seconds,
        "ocr_timeout_seconds",
        1,
        HARD_MAX_OCR_TIMEOUT_SECONDS,
    )
    if not _LANGUAGE_PATTERN.fullmatch(language):
        raise PdfOcrError("O idioma OCR deve usar códigos como 'por' ou 'por+eng'.")
    try:
        resolved, _size = validate_pdf_file(path, max_file_bytes)
    except PdfExtractionError as exc:
        raise PdfOcrError(str(exc)) from exc
    sizes = tuple(page_probe(resolved))
    selected = parse_page_selection(pages, page_count=len(sizes), max_pages=max_pages)
    estimates: dict[int, int] = {}
    for number in selected:
        width, height = sizes[number - 1]
        pixels = math.ceil(width * dpi / 72) * math.ceil(height * dpi / 72)
        if pixels <= 0 or pixels > max_pixels_per_page:
            raise PdfOcrError(f"A página {number} excede o limite de pixels.")
        estimates[number] = pixels
    if sum(estimates.values()) > max_total_pixels:
        raise PdfOcrError("As páginas selecionadas excedem o limite total de pixels.")
    if max(estimates.values(), default=0) * 4 > max_memory_bytes:
        raise PdfOcrError("A rasterização estimada excede o limite de memória.")

    page_results: list[OcrPageResult] = []
    candidates_by_value: dict[tuple[int, str], BoletoCandidate] = {}
    warnings: list[str] = []
    errors: list[str] = []
    remaining_characters = max_text_characters
    for number in selected:
        page_errors: list[str] = []
        try:
            rendered = page_renderer(resolved, number, dpi)
            actual_pixels = rendered.width * rendered.height
            if actual_pixels <= 0 or actual_pixels > max_pixels_per_page:
                raise PdfOcrError(f"A página {number} excedeu o limite real de pixels.")
            if len(rendered.png) > max_memory_bytes:
                raise PdfOcrError(f"A imagem da página {number} excede o limite de memória.")
            ocr = ocr_engine(
                rendered.png,
                dpi=dpi,
                language=language,
                timeout_seconds=ocr_timeout_seconds,
                max_output_bytes=min(max_memory_bytes, max_text_characters * 64),
            )
            text = ocr.text[:remaining_characters]
            if len(ocr.text) > len(text):
                warnings.append(f"page_{number}_text_truncated")
            remaining_characters -= len(text)
            image_name = image_writer(number, rendered.png) if image_writer else None
            source_values: list[tuple[str, str, float | None]] = [
                (value, "ocr", ocr.confidence)
                for value in extract_numeric_candidates(text)
            ]
            source_values.extend(
                (value, "barcode", None) for value in barcode_reader(rendered.png)
            )
            for value, source, confidence in source_values:
                key = (number, value)
                kind, validation = validate_boleto_candidate(value)
                existing = candidates_by_value.get(key)
                sources = tuple(dict.fromkeys((*existing.sources, source))) if existing else (source,)
                candidate_confidence = (
                    max(
                        item
                        for item in (existing.confidence if existing else None, confidence)
                        if item is not None
                    )
                    if any(
                        item is not None
                        for item in (existing.confidence if existing else None, confidence)
                    )
                    else None
                )
                candidates_by_value[key] = BoletoCandidate(
                    number,
                    value,
                    kind,
                    sources,
                    candidate_confidence,
                    validation,
                )
            page_results.append(
                OcrPageResult(number, text, ocr.confidence, image_name, ())
            )
        except (PdfOcrError, OSError) as exc:
            message = str(exc)
            page_errors.append(message)
            errors.append(f"page_{number}: {message}")
            page_results.append(OcrPageResult(number, "", None, None, tuple(page_errors)))
        if remaining_characters <= 0:
            warnings.append("text_limit_reached")
            break
    return PdfOcrResult(
        needs_ocr=bool(errors),
        page_count=len(sizes),
        pages_processed=len(page_results),
        pages=tuple(page_results),
        candidates=tuple(candidates_by_value.values()),
        warnings=tuple(dict.fromkeys(warnings)),
        errors=tuple(errors),
    )
