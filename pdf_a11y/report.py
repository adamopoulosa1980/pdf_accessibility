"""Remediation report — accumulates what each fixer did and what needs review."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class Finding:
    """A single accessibility finding or remediation action."""
    wcag: str           # e.g. "1.1.1", "3.1.1"
    severity: str       # "fixed" | "warning" | "manual_required" | "error"
    message: str
    location: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RemediationReport:
    source_pdf: str
    output_pdf: str | None = None
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    finished_at: str | None = None
    findings: list[Finding] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)

    def add(self, wcag: str, severity: str, message: str, **kwargs: Any) -> None:
        self.findings.append(Finding(
            wcag=wcag,
            severity=severity,
            message=message,
            location=kwargs.pop("location", {}),
            details=kwargs,
        ))

    def finalize(self) -> None:
        self.finished_at = datetime.utcnow().isoformat()
        # Build summary counts
        for f in self.findings:
            key = f.severity
            self.summary[key] = self.summary.get(key, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_pdf": self.source_pdf,
            "output_pdf": self.output_pdf,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
            "findings": [
                {
                    "wcag": f.wcag,
                    "severity": f.severity,
                    "message": f.message,
                    "location": f.location,
                    "details": f.details,
                }
                for f in self.findings
            ],
        }

    def write(self, path: str | Path) -> None:
        path = Path(path)
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
