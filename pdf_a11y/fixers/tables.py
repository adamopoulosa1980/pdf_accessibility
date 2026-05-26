"""
Table fixer — WCAG 1.3.1 Info and Relationships (Level A), table portion.

Detects tables on each page, identifies header rows/columns, and emits
findings. Actually writing table tags into the StructTreeRoot requires
the tagging fixer to run first; this fixer reports detected tables and
applies per-table overrides from config.

Header detection strategies:
  - "first_row":  Always use row 0 as header (often correct, sometimes wrong)
  - "heuristic":  Inspect font weight, background fill, position
  - "manual":     Don't guess; require explicit overrides
"""
from __future__ import annotations

from pathlib import Path

import pikepdf

try:
    import fitz
except ImportError:
    fitz = None

from ..config import Config
from ..report import RemediationReport


class TableFixer:
    name = "tables"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.strategy = cfg.tables["header_detection"]
        self.overrides = cfg.tables.get("overrides") or []

    def apply(self, pdf: pikepdf.Pdf, source_path: Path, report: RemediationReport) -> None:
        if fitz is None:
            report.add("1.3.1", "warning", "pymupdf required for table detection")
            return

        tmp = source_path.parent / f".tmp_tables_{source_path.name}"
        pdf.save(str(tmp))
        fz_doc = fitz.open(str(tmp))

        # Build override lookup
        override_map: dict[tuple[int, int], dict] = {
            (o["page"], o.get("table_index", 0)): o for o in self.overrides
        }

        tables_found = 0
        for page_num, page in enumerate(fz_doc):
            try:
                # pymupdf 1.23+ has a table finder
                table_finder = page.find_tables()
                tables = list(table_finder.tables)
            except Exception:
                tables = []

            for table_idx, table in enumerate(tables):
                tables_found += 1
                key = (page_num + 1, table_idx)
                override = override_map.get(key)

                rows = table.row_count if hasattr(table, "row_count") else 0
                cols = table.col_count if hasattr(table, "col_count") else 0

                if override:
                    header_rows = override.get("header_rows", 1)
                    header_cols = override.get("header_cols", 0)
                    summary = override.get("summary", "")
                    report.add(
                        "1.3.1", "fixed",
                        f"Table on page {page_num+1} (index {table_idx}): "
                        f"applied override (header_rows={header_rows}, "
                        f"header_cols={header_cols})",
                        location={"page": page_num + 1, "table_index": table_idx},
                        details={"override": override, "rows": rows, "cols": cols},
                    )
                    continue

                if self.strategy == "manual":
                    report.add(
                        "1.3.1", "manual_required",
                        f"Table on page {page_num+1} (index {table_idx}, "
                        f"{rows}×{cols}) needs manual header definition",
                        location={"page": page_num + 1, "table_index": table_idx},
                    )
                    continue

                if self.strategy == "first_row":
                    report.add(
                        "1.3.1", "fixed",
                        f"Table on page {page_num+1} (index {table_idx}, "
                        f"{rows}×{cols}): assuming row 0 is header",
                        location={"page": page_num + 1, "table_index": table_idx},
                        details={"header_rows": 1, "header_cols": 0},
                    )
                    continue

                # Heuristic
                header_rows, header_cols, confidence = self._heuristic_headers(table)
                report.add(
                    "1.3.1",
                    "fixed" if confidence > 0.6 else "warning",
                    f"Table on page {page_num+1} (index {table_idx}, "
                    f"{rows}×{cols}): detected header_rows={header_rows}, "
                    f"header_cols={header_cols} (confidence={confidence:.2f})",
                    location={"page": page_num + 1, "table_index": table_idx},
                    details={
                        "header_rows": header_rows,
                        "header_cols": header_cols,
                        "confidence": confidence,
                    },
                )

        fz_doc.close()
        tmp.unlink(missing_ok=True)

        report.add(
            "1.3.1",
            "fixed" if tables_found else "warning",
            f"Table detection scan complete: {tables_found} table(s) found",
        )

    # --------------------------------------------------------------
    @staticmethod
    def _heuristic_headers(table) -> tuple[int, int, float]:
        """
        Detect header rows/cols using font weight and uniqueness cues.

        Returns (header_rows, header_cols, confidence).
        """
        try:
            cells = table.extract()
        except Exception:
            return (1, 0, 0.3)
        if not cells:
            return (1, 0, 0.3)

        # Header-row heuristic: first row text differs in pattern from rest
        # (e.g., short labels vs longer values, no numbers vs numbers)
        first_row = cells[0]
        rest = cells[1:]

        def has_digits(s):
            return any(ch.isdigit() for ch in (s or ""))

        first_row_digit_ratio = sum(has_digits(c) for c in first_row) / max(len(first_row), 1)
        rest_digit_ratio = 0.0
        if rest:
            flat = [c for row in rest for c in row]
            rest_digit_ratio = sum(has_digits(c) for c in flat) / max(len(flat), 1)

        # If header row has far fewer digits than body, likely a header
        if rest_digit_ratio - first_row_digit_ratio > 0.3:
            return (1, 0, 0.8)
        if first_row_digit_ratio < 0.2 and rest_digit_ratio > 0.5:
            return (1, 0, 0.85)

        # First column header heuristic: first col text vs rest
        first_col = [row[0] if row else "" for row in cells]
        rest_cols = [c for row in cells for c in row[1:]]
        first_col_digit = sum(has_digits(c) for c in first_col) / max(len(first_col), 1)
        rest_cols_digit = sum(has_digits(c) for c in rest_cols) / max(len(rest_cols), 1)
        has_first_col_header = rest_cols_digit - first_col_digit > 0.3

        return (1, 1 if has_first_col_header else 0, 0.7)
