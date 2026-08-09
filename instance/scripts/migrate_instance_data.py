#!/usr/bin/env python3
"""Migra o estado legado de uma instância para instance/data."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"


class MigrationError(RuntimeError):
    """Indica que a migração não pode prosseguir sem risco de perda."""


def default_legacy_root(instance_id: str) -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise MigrationError("LOCALAPPDATA não está definido; informe --legacy-root.")
    return Path(local_app_data) / "Coworker" / "instances" / instance_id


def _same_file(left: Path, right: Path) -> bool:
    return left.stat().st_size == right.stat().st_size and left.read_bytes() == right.read_bytes()


def _merge(source: Path, target: Path, *, apply: bool, remove_source: bool, moved: list[str]) -> None:
    if not source.exists():
        return
    if source.is_symlink():
        raise MigrationError(f"Não migro links simbólicos: {source}")
    if source.is_dir():
        if target.exists() and not target.is_dir():
            raise MigrationError(f"Conflito de arquivo/diretório em '{target}'.")
        if apply:
            target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            _merge(child, target / child.name, apply=apply, remove_source=remove_source, moved=moved)
        if apply and remove_source and source.exists() and not any(source.iterdir()):
            source.rmdir()
        return
    if target.exists():
        if not target.is_file() or not _same_file(source, target):
            raise MigrationError(f"Conflito de conteúdo em '{target}'.")
        if apply and remove_source:
            source.unlink()
        return
    moved.append(str(target))
    if apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))


def _rewrite_config(path: Path, *, apply: bool) -> bool:
    if not path.is_file():
        return False
    content = path.read_text(encoding="utf-8")
    updated = re.sub(r"(?m)^state_dir\s*=.*$", 'state_dir = "data/telegram/state"', content)
    updated = re.sub(r'(?m)^home_dir\s*=.*$', 'home_dir = "data/codex"', updated)
    updated = re.sub(
        r'(?m)^generated_images_dir\s*=.*$',
        'generated_images_dir = "data/codex/generated_images"',
        updated,
    )
    if updated == content:
        return False
    if apply:
        path.write_text(updated, encoding="utf-8", newline="\n")
    return True


def _rewrite_codex_rollout_paths(legacy_codex: Path, current_codex: Path, *, apply: bool) -> int:
    """Atualiza índices do Codex depois de mover sessões para data/codex."""
    database = current_codex / "state_5.sqlite"
    if not database.is_file():
        return 0
    connection = sqlite3.connect(database)
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'"
        ).fetchone() is None:
            return 0
        rows = connection.execute(
            "SELECT rowid, rollout_path FROM threads WHERE rollout_path IS NOT NULL"
        ).fetchall()
        old_root = str(legacy_codex.resolve())
        updates: list[tuple[str, int]] = []
        for rowid, raw_path in rows:
            value = str(raw_path)
            normalized = value[4:] if value.startswith("\\\\?\\") else value
            if not normalized.casefold().startswith(old_root.casefold()):
                continue
            suffix = normalized[len(old_root):]
            target = current_codex / suffix.lstrip("\\/")
            if not target.is_file():
                raise MigrationError(f"Rollout não encontrado após a migração: {target}")
            updates.append((str(target), int(rowid)))
        if apply:
            with connection:
                connection.executemany(
                    "UPDATE threads SET rollout_path=? WHERE rowid=?", updates
                )
        return len(updates)
    finally:
        connection.close()


def migrate(
    instance_id: str,
    legacy_root: Path,
    *,
    apply: bool,
    remove_source: bool,
) -> dict[str, object]:
    if legacy_root.resolve() == DATA_DIR.resolve() or DATA_DIR.resolve() in legacy_root.resolve().parents:
        raise MigrationError("A origem não pode ser instance/data nem um de seus descendentes.")
    if not legacy_root.exists():
        return {"ok": True, "instance_id": instance_id, "source_exists": False, "moved": [], "config_updated": False}
    moved: list[str] = []
    legacy_codex = legacy_root / "codex"
    current_codex = DATA_DIR / "codex"
    _merge(legacy_codex, current_codex, apply=apply, remove_source=remove_source, moved=moved)
    _merge(legacy_root / "telegram", DATA_DIR / "telegram" / "state", apply=apply, remove_source=remove_source, moved=moved)
    rollout_paths_updated = _rewrite_codex_rollout_paths(
        legacy_codex, current_codex, apply=apply
    )
    config_updated = _rewrite_config(DATA_DIR / "config" / "telegram.toml", apply=apply)
    if apply and remove_source and legacy_root.exists() and not any(legacy_root.iterdir()):
        legacy_root.rmdir()
    return {
        "ok": True,
        "instance_id": instance_id,
        "source_exists": True,
        "moved": moved,
        "config_updated": config_updated,
        "rollout_paths_updated": rollout_paths_updated,
        "source_removed": not legacy_root.exists(),
        "dry_run": not apply,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migra estado legado para instance/data.")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--apply", action="store_true", help="Executa a migração; sem isto, apenas simula.")
    parser.add_argument("--remove-source", action="store_true", help="Remove a origem após migração sem conflitos.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = migrate(
            args.instance_id,
            (args.legacy_root or default_legacy_root(args.instance_id)).resolve(),
            apply=args.apply,
            remove_source=args.remove_source,
        )
    except (MigrationError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
