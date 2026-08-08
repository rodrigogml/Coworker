"""Extração local e limitada de texto de PDFs não confiáveis."""

from __future__ import annotations

import importlib.util
import threading
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_MAX_PAGES = 200
DEFAULT_MAX_CHARACTERS = 200_000
DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_CONTENT_BYTES = 10 * 1024 * 1024
HARD_MAX_PAGES = 500
HARD_MAX_CHARACTERS = 500_000
HARD_MAX_FILE_BYTES = 50 * 1024 * 1024
HARD_MAX_CONTENT_BYTES = 25 * 1024 * 1024
_PYPDF_LIMIT_LOCK = threading.Lock()
_SAFE_METADATA_FIELDS = {
    "/Author": "author",
    "/CreationDate": "creation_date",
    "/Creator": "creator",
    "/ModDate": "modification_date",
    "/Producer": "producer",
    "/Subject": "subject",
    "/Title": "title",
}


class PdfExtractionError(RuntimeError):
    """Indica dependência ausente, PDF inválido ou limite inseguro."""


@dataclass(frozen=True)
class PdfPageSummary:
    """Metadados não sensíveis sobre o texto encontrado em uma página."""

    number: int
    characters: int
    has_text: bool


@dataclass(frozen=True)
class PdfExtractionResult:
    """Resultado limitado da extração, adequado para JSON e para o prompt."""

    text: str
    pages: tuple[PdfPageSummary, ...]
    page_count: int
    pages_processed: int
    warnings: tuple[str, ...]
    metadata_safe: dict[str, Any]
    extraction_method: str
    needs_ocr: bool
    truncated: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "pages": [asdict(page) for page in self.pages],
            "page_count": self.page_count,
            "pages_processed": self.pages_processed,
            "warnings": list(self.warnings),
            "metadata_safe": self.metadata_safe,
            "extraction_method": self.extraction_method,
            "needs_ocr": self.needs_ocr,
            "truncated": self.truncated,
        }


def dependency_available() -> bool:
    """Informa se o extrator local obrigatório está importável."""
    return importlib.util.find_spec("pypdf") is not None


def _positive_limit(value: int, label: str, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise PdfExtractionError(f"'{label}' deve ficar entre 1 e {maximum}.")
    return value


def _safe_text(value: str) -> str:
    return "".join(
        character
        for character in value
        if character in {"\n", "\t"}
        or not unicodedata.category(character).startswith("C")
    ).strip()


def _validated_pdf(path: Path, max_file_bytes: int) -> tuple[Path, int]:
    candidate = path.expanduser()
    try:
        if candidate.is_symlink():
            raise PdfExtractionError("O PDF não é um arquivo regular seguro.")
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PdfExtractionError("O PDF informado não existe.") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise PdfExtractionError("O PDF não é um arquivo regular seguro.")
    if resolved.suffix.casefold() != ".pdf":
        raise PdfExtractionError("O arquivo informado não possui extensão PDF.")
    size = resolved.stat().st_size
    if size <= 0 or size > max_file_bytes:
        raise PdfExtractionError("O PDF está vazio ou excede o limite permitido.")
    try:
        with resolved.open("rb") as stream:
            header = stream.read(1024)
    except OSError as exc:
        raise PdfExtractionError("O PDF não pôde ser lido.") from exc
    if b"%PDF-" not in header:
        raise PdfExtractionError("O arquivo não possui uma assinatura PDF válida.")
    return resolved, size


def _append_limited(current: str, block: str, maximum: int) -> tuple[str, bool]:
    separator = "\n\n" if current else ""
    addition = separator + block
    remaining = maximum - len(current)
    if len(addition) <= remaining:
        return current + addition, False
    return current + addition[: max(0, remaining)], True


def extract_pdf_text(
    path: Path,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_characters: int = DEFAULT_MAX_CHARACTERS,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
) -> PdfExtractionResult:
    """Extrai somente texto pesquisável, sem executar ações ou anexos do documento."""
    max_pages = _positive_limit(max_pages, "max_pages", HARD_MAX_PAGES)
    max_characters = _positive_limit(
        max_characters, "max_characters", HARD_MAX_CHARACTERS
    )
    max_file_bytes = _positive_limit(
        max_file_bytes, "max_file_bytes", HARD_MAX_FILE_BYTES
    )
    max_content_bytes = _positive_limit(
        max_content_bytes, "max_content_bytes", HARD_MAX_CONTENT_BYTES
    )
    resolved, size = _validated_pdf(path, max_file_bytes)
    try:
        from pypdf import PdfReader, filters
    except ImportError as exc:
        raise PdfExtractionError(
            "A dependência pypdf não está instalada; execute "
            "python -m pip install -r requirements.txt."
        ) from exc

    warnings: list[str] = []
    summaries: list[PdfPageSummary] = []
    text = ""
    truncated = False
    incomplete = False
    with _PYPDF_LIMIT_LOCK:
        previous_limit = filters.ZLIB_MAX_OUTPUT_LENGTH
        filters.ZLIB_MAX_OUTPUT_LENGTH = min(previous_limit, max_content_bytes)
        try:
            try:
                reader = PdfReader(resolved, strict=False, root_object_recovery_limit=1_000)
            except Exception as exc:
                raise PdfExtractionError("O PDF é inválido ou não pôde ser lido.") from exc
            encrypted = bool(reader.is_encrypted)
            if encrypted:
                try:
                    unlocked = bool(reader.decrypt(""))
                except Exception as exc:
                    raise PdfExtractionError(
                        "O PDF é protegido por senha e não pode ser extraído."
                    ) from exc
                if not unlocked:
                    raise PdfExtractionError(
                        "O PDF é protegido por senha e não pode ser extraído."
                    )
            page_count = len(reader.pages)
            pages_to_process = min(page_count, max_pages)
            if page_count > max_pages:
                warnings.append("page_limit_reached")
                truncated = True
            for index in range(pages_to_process):
                page = reader.pages[index]
                try:
                    contents = page.get_contents()
                    if contents is not None and len(contents.get_data()) > max_content_bytes:
                        warnings.append(f"page_{index + 1}_content_limit_reached")
                        incomplete = True
                        summaries.append(PdfPageSummary(index + 1, 0, False))
                        continue
                    extracted = _safe_text(page.extract_text() or "")
                except Exception:
                    warnings.append(f"page_{index + 1}_extraction_failed")
                    incomplete = True
                    summaries.append(PdfPageSummary(index + 1, 0, False))
                    continue
                summaries.append(
                    PdfPageSummary(index + 1, len(extracted), bool(extracted))
                )
                if extracted:
                    text, reached = _append_limited(
                        text, f"[Página {index + 1}]\n{extracted}", max_characters
                    )
                    if reached:
                        warnings.append("character_limit_reached")
                        truncated = True
                        break
            fields_present: list[str] = []
            try:
                metadata = reader.metadata
                if metadata:
                    fields_present = sorted(
                        public_name
                        for key, public_name in _SAFE_METADATA_FIELDS.items()
                        if key in metadata
                    )
            except Exception:
                warnings.append("metadata_unavailable")
        finally:
            filters.ZLIB_MAX_OUTPUT_LENGTH = previous_limit

    if not text.strip() and not incomplete:
        warnings.append("no_searchable_text")
    return PdfExtractionResult(
        text=text,
        pages=tuple(summaries),
        page_count=page_count,
        pages_processed=len(summaries),
        warnings=tuple(dict.fromkeys(warnings)),
        metadata_safe={
            "encrypted": encrypted,
            "file_size_bytes": size,
            "fields_present": fields_present,
        },
        extraction_method="pypdf",
        needs_ocr=not text.strip() and not incomplete,
        truncated=truncated or incomplete,
    )
