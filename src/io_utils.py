"""Utilitaires sûrs pour les fichiers et artefacts runtime."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd


def resolve_within(root: Path, relative_path: str | Path) -> Path:
    """Résout un chemin relatif en refusant toute sortie du répertoire racine."""
    root_resolved = root.resolve(strict=False)
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError("Les chemins absolus ne sont pas autorisés")
    resolved = (root_resolved / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"Chemin hors du répertoire runtime autorisé : {relative_path}") from exc
    return resolved


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Écrit un fichier texte de façon atomique avec permissions privées."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    """Sérialise un objet JSON de façon atomique."""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, default=str))


def sanitize_dataframe_for_excel(frame: pd.DataFrame) -> pd.DataFrame:
    """Neutralise les préfixes interprétables comme formules dans un export Excel."""
    dangerous_prefixes = ("=", "+", "-", "@", "\t", "\r")

    def sanitize(value: object) -> object:
        if isinstance(value, str) and value.startswith(dangerous_prefixes):
            return "'" + value
        return value

    return frame.map(sanitize)
