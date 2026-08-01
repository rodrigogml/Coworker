"""Preparação segura e opcional de arquivos recebidos."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import shutil
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

from interfaces.telegram.config import ProcessorConfig
from interfaces.telegram.contracts import Attachment


class ProcessorError(RuntimeError):
    """Indica que um arquivo não pôde ser preparado dentro dos limites."""


@dataclass(frozen=True)
class PreparedContent:
    processor: str
    text: str | None
    available: bool
    note: str


class ProcessorRegistry:
    def __init__(self, config: ProcessorConfig):
        self.config = config

    def doctor(self) -> dict[str, dict[str, str | bool]]:
        ffprobe_present = shutil.which("ffprobe") is not None
        return {
            "text_json_csv_xml_zip_docx_xlsx": {"available": True, "provider": "stdlib"},
            "pdf": {"available": importlib.util.find_spec("pypdf") is not None, "provider": "pypdf"},
            "ocr": {"available": shutil.which("tesseract") is not None, "provider": "tesseract"},
            "audio_video": {
                "available": False,
                "dependency_present": ffprobe_present,
                "provider": "ffprobe (diagnóstico apenas)",
            },
        }

    def prepare(self, attachment: Attachment) -> PreparedContent:
        path = attachment.local_path
        if path is None or not path.is_file():
            return PreparedContent("none", None, False, "Arquivo local indisponível.")
        mime = attachment.detected_mime or attachment.declared_mime or ""
        suffix = path.suffix.casefold()
        if mime.startswith("image/"):
            return PreparedContent("localImage", None, True, "Imagem enviada como entrada visual nativa.")
        if suffix in {".txt", ".md", ".log"} or mime.startswith("text/"):
            return PreparedContent("text", self._read_text(path), True, "Texto UTF-8 preparado.")
        if suffix == ".json" or mime == "application/json":
            value = json.loads(self._read_text(path))
            return PreparedContent("json", self._limit(json.dumps(value, ensure_ascii=False, indent=2)), True, "JSON validado.")
        if suffix == ".csv" or mime == "text/csv":
            rows = list(csv.reader(io.StringIO(self._read_text(path))))
            return PreparedContent("csv", self._limit("\n".join(" | ".join(row) for row in rows)), True, "CSV preparado.")
        if suffix == ".xml" or mime in {"application/xml", "text/xml"}:
            root = ET.fromstring(self._read_text(path))
            return PreparedContent("xml", self._limit(" ".join(root.itertext())), True, "XML validado e convertido em texto.")
        if suffix == ".docx":
            return PreparedContent("docx", self._docx(path), True, "DOCX extraído por XML interno.")
        if suffix == ".xlsx":
            return PreparedContent("xlsx", self._xlsx(path), True, "XLSX extraído por XML interno.")
        if suffix == ".zip" or mime == "application/zip":
            return PreparedContent("zip", self._zip_inventory(path), True, "Somente inventário seguro; membros não foram executados.")
        if suffix == ".pdf":
            return PreparedContent("pdf", None, False, "Processador PDF opcional não foi aplicado; o original foi preservado.")
        if mime.startswith(("audio/", "video/")):
            return PreparedContent("media", None, False, "Transcrição opcional indisponível; o original foi preservado.")
        return PreparedContent("none", None, False, "Formato sem preparação automática; o original foi preservado.")

    def _read_text(self, path: Path) -> str:
        try:
            return self._limit(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            raise ProcessorError("O arquivo de texto não está em UTF-8.") from exc

    def _limit(self, value: str) -> str:
        return value[: self.config.max_extracted_characters]

    def _safe_zip(self, path: Path) -> zipfile.ZipFile:
        archive = zipfile.ZipFile(path)
        members = archive.infolist()
        if len(members) > self.config.max_archive_members:
            archive.close()
            raise ProcessorError("O arquivo compactado excede o limite de membros.")
        if sum(item.file_size for item in members) > self.config.max_uncompressed_bytes:
            archive.close()
            raise ProcessorError("O arquivo compactado excede o limite descompactado.")
        for item in members:
            member = Path(item.filename)
            if member.is_absolute() or ".." in member.parts:
                archive.close()
                raise ProcessorError("O arquivo compactado contém caminho inseguro.")
        return archive

    def _zip_inventory(self, path: Path) -> str:
        with self._safe_zip(path) as archive:
            return self._limit("\n".join(f"{item.filename}\t{item.file_size}" for item in archive.infolist()))

    def _docx(self, path: Path) -> str:
        with self._safe_zip(path) as archive:
            try:
                root = ET.fromstring(archive.read("word/document.xml"))
            except KeyError as exc:
                raise ProcessorError("O DOCX não contém o documento principal.") from exc
        return self._limit("\n".join(text for node in root.iter() if node.tag.endswith("}p") for text in ["".join(node.itertext())] if text))

    def _xlsx(self, path: Path) -> str:
        lines: list[str] = []
        with self._safe_zip(path) as archive:
            names = set(archive.namelist())
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(node.itertext()) for node in root if node.tag.endswith("}si")]
            for name in sorted(item for item in names if item.startswith("xl/worksheets/sheet") and item.endswith(".xml")):
                lines.append(f"[{Path(name).stem}]")
                root = ET.fromstring(archive.read(name))
                for row in (node for node in root.iter() if node.tag.endswith("}row")):
                    values: list[str] = []
                    for cell in (node for node in row if node.tag.endswith("}c")):
                        raw = next((node.text or "" for node in cell if node.tag.endswith("}v")), "")
                        if cell.attrib.get("t") == "s" and raw.isdigit() and int(raw) < len(shared):
                            raw = shared[int(raw)]
                        values.append(raw)
                    lines.append(" | ".join(values))
        return self._limit("\n".join(lines))
