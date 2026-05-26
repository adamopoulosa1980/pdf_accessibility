"""Config loader. Reads YAML, validates, exposes typed access."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Config:
    """Wrapper around the YAML config with typed accessors."""

    raw: dict[str, Any]
    config_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        cfg = cls(raw=raw, config_path=path)
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        """Sanity checks. Raises ValueError on misconfiguration."""
        required_sections = [
            "document", "tagging", "images", "contrast",
            "reading_order", "tables", "forms", "validation", "output",
        ]
        missing = [s for s in required_sections if s not in self.raw]
        if missing:
            raise ValueError(f"Config missing sections: {missing}")

        lang = self.raw["document"].get("primary_language")
        if not lang or not isinstance(lang, str) or len(lang) < 2:
            raise ValueError("document.primary_language must be a valid ISO 639-1 code")

        engine = self.raw["tagging"].get("engine")
        valid_engines = {"adobe", "pdfix", "opendataloader", "heuristic", "skip"}
        if engine not in valid_engines:
            raise ValueError(f"tagging.engine must be one of {valid_engines}")

        img_strategy = self.raw["images"].get("strategy")
        if img_strategy not in {"vlm", "prompt", "decorative"}:
            raise ValueError("images.strategy must be vlm | prompt | decorative")

    # --- convenience accessors ---
    @property
    def document(self) -> dict[str, Any]:
        return self.raw["document"]

    @property
    def tagging(self) -> dict[str, Any]:
        return self.raw["tagging"]

    @property
    def images(self) -> dict[str, Any]:
        return self.raw["images"]

    @property
    def contrast(self) -> dict[str, Any]:
        return self.raw["contrast"]

    @property
    def reading_order(self) -> dict[str, Any]:
        return self.raw["reading_order"]

    @property
    def tables(self) -> dict[str, Any]:
        return self.raw["tables"]

    @property
    def forms(self) -> dict[str, Any]:
        return self.raw["forms"]

    @property
    def language_detection(self) -> dict[str, Any]:
        return self.raw.get("language_detection", {"enabled": False})

    @property
    def validation(self) -> dict[str, Any]:
        return self.raw["validation"]

    @property
    def output(self) -> dict[str, Any]:
        return self.raw["output"]

    def env(self, key: str) -> str | None:
        """Resolve an env-var name from config to its value."""
        return os.environ.get(key)
