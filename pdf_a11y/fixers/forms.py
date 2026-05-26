"""
Form field fixer — WCAG 4.1.2 Name, Role, Value (Level A).

Every interactive form field needs a tooltip (TU entry) so screen readers
can announce its purpose.

Strategies:
  - "nearby_text": Read text immediately to the left/above the field
  - "field_name":  Clean up the internal field name (underscores -> spaces, etc.)
  - "manual":      Use only form_field_labels from config
"""
from __future__ import annotations

import re
from pathlib import Path

import pikepdf
from pikepdf import Dictionary, Name, String

try:
    import fitz
except ImportError:
    fitz = None

from ..config import Config
from ..report import RemediationReport


class FormFieldFixer:
    name = "forms"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.strategy = cfg.forms["label_strategy"]
        self.explicit_labels = cfg.forms.get("form_field_labels") or {}

    def apply(self, pdf: pikepdf.Pdf, source_path: Path, report: RemediationReport) -> None:
        if "/AcroForm" not in pdf.Root:
            return  # no form, nothing to do

        # Set NeedAppearances so labels render correctly
        pdf.Root.AcroForm.NeedAppearances = True

        fields = pdf.Root.AcroForm.get("/Fields", [])
        if not fields:
            return

        # Pre-build nearby-text index if needed
        nearby_index = None
        if self.strategy == "nearby_text" and fitz is not None:
            nearby_index = self._build_nearby_index(pdf, source_path)

        fixed = 0
        manual = 0
        for field_ref in fields:
            try:
                field = field_ref
                fixed += self._fix_field(field, pdf, nearby_index, report)
                manual += 1 if not field.get("/TU") else 0
            except Exception as e:
                report.add("4.1.2", "warning", f"Could not process form field: {e}")

        report.add(
            "4.1.2",
            "fixed" if fixed else "warning",
            f"Form field labels: {fixed} field(s) given tooltips",
        )

    # --------------------------------------------------------------
    def _fix_field(self, field, pdf, nearby_index, report) -> int:
        # Already has tooltip?
        if "/TU" in field and str(field["/TU"]).strip():
            return 0

        field_name = str(field.get("/T", "")).strip()

        # 1) Explicit override
        if field_name in self.explicit_labels:
            field.TU = String(self.explicit_labels[field_name])
            report.add("4.1.2", "fixed",
                       f"Form field '{field_name}': label from config",
                       details={"label": self.explicit_labels[field_name]})
            return 1

        # 2) Strategy-specific
        label = None
        if self.strategy == "nearby_text" and nearby_index is not None:
            label = self._find_nearby_label(field, nearby_index)
        if not label and self.strategy in ("field_name", "nearby_text"):
            label = self._clean_field_name(field_name)
        if self.strategy == "manual":
            report.add("4.1.2", "manual_required",
                       f"Form field '{field_name}' needs a label "
                       f"(add to forms.form_field_labels in config)",
                       details={"field_name": field_name})
            return 0

        if label:
            field.TU = String(label)
            report.add("4.1.2", "fixed",
                       f"Form field '{field_name}': label '{label}' "
                       f"(via {self.strategy})",
                       details={"label": label, "source": self.strategy})
            return 1

        report.add("4.1.2", "manual_required",
                   f"Form field '{field_name}': no label derivable; please add manually")
        return 0

    # --------------------------------------------------------------
    def _build_nearby_index(self, pdf, source_path):
        """Index page text spans so we can find labels near each field."""
        tmp = source_path.parent / f".tmp_forms_{source_path.name}"
        pdf.save(str(tmp))
        fz_doc = fitz.open(str(tmp))
        index = []  # list of (page_num, bbox, text)
        for page_num, page in enumerate(fz_doc):
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    text = "".join(s["text"] for s in line.get("spans", [])).strip()
                    if not text:
                        continue
                    index.append((page_num, line["bbox"], text))
        fz_doc.close()
        tmp.unlink(missing_ok=True)
        return index

    def _find_nearby_label(self, field, nearby_index):
        """
        Look for text immediately to the left of or above the field's widget
        rectangle. Returns the closest plausible label, or None.
        """
        rect = field.get("/Rect")
        if not rect or len(rect) != 4:
            return None
        try:
            x0, y0, x1, y1 = (float(c) for c in rect)
        except Exception:
            return None

        # Page lookup: walk widget kids? For simplicity, search all pages
        # within tolerance.
        best = None
        best_dist = float("inf")
        for page_num, bbox, text in nearby_index:
            bx0, by0, bx1, by1 = bbox
            # Candidate: left of field on same line
            if bx1 <= x0 and abs((by0 + by1) / 2 - (y0 + y1) / 2) < 8:
                dist = x0 - bx1
                if dist < best_dist and dist < 100:
                    best_dist = dist
                    best = text
            # Candidate: directly above
            elif by1 <= y0 and abs((bx0 + bx1) / 2 - (x0 + x1) / 2) < 80:
                dist = y0 - by1
                if dist < best_dist and dist < 30:
                    best_dist = dist
                    best = text

        if best:
            # Trim trailing colons and asterisks
            return re.sub(r"[\s:*]+$", "", best)
        return None

    @staticmethod
    def _clean_field_name(name: str) -> str:
        """Turn 'txt_first_name' / 'firstName' into 'First name'."""
        if not name:
            return ""
        # Remove common prefixes
        name = re.sub(r"^(txt_|chk_|rdo_|cbo_|btn_|lst_)", "", name)
        # snake_case -> space
        name = name.replace("_", " ").replace("-", " ")
        # camelCase -> space
        name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
        return name.strip().capitalize()
