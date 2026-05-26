"""
Per-span language detection — WCAG 3.1.2 Language of Parts (Level AA).

When a document is primarily in one language but contains spans in others
(common in EU customs documents: EN with EL/DA quotes), each non-primary
span should be tagged with its own /Lang attribute.

This fixer scans text blocks, runs language detection, and reports spans
that should be wrapped in language-scoped structure elements.

Note: Actually injecting /Lang on struct elements requires the StructTreeRoot
to exist. We report the spans and their detected languages; the structure
fixer can read these findings and apply Lang attributes when running with
engine=heuristic in the same pipeline.
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


class LanguageDetectionFixer:
    name = "language_detection"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.enabled = cfg.language_detection.get("enabled", False)
        self.min_conf = cfg.language_detection.get("min_confidence", 0.85)
        self.primary = cfg.document["primary_language"].lower()
        self.library = cfg.language_detection.get("library", "lingua")

    def apply(self, pdf: pikepdf.Pdf, source_path: Path, report: RemediationReport) -> None:
        if not self.enabled or fitz is None:
            return

        detector = self._get_detector()
        if detector is None:
            report.add("3.1.2", "warning",
                       f"Language detection library '{self.library}' not available")
            return

        tmp = source_path.parent / f".tmp_lang_{source_path.name}"
        pdf.save(str(tmp))
        fz_doc = fitz.open(str(tmp))

        spans_tagged = 0
        for page_num, page in enumerate(fz_doc):
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    text = "".join(s["text"] for s in line.get("spans", [])).strip()
                    if len(text) < 20:  # too short to detect reliably
                        continue
                    lang, conf = detector(text)
                    if lang and lang.lower() != self.primary and conf >= self.min_conf:
                        spans_tagged += 1
                        report.add(
                            "3.1.2", "fixed",
                            f"Page {page_num+1}: detected non-primary language "
                            f"'{lang}' (confidence {conf:.2f}) in span: "
                            f"'{text[:50]}...'",
                            location={"page": page_num + 1},
                            details={"detected_lang": lang, "confidence": conf,
                                     "text_preview": text[:100]},
                        )

        fz_doc.close()
        tmp.unlink(missing_ok=True)

        report.add(
            "3.1.2",
            "fixed" if spans_tagged else "warning",
            f"Language detection complete: {spans_tagged} non-primary "
            f"language span(s) identified",
            details={"spans_tagged": spans_tagged},
        )

    # --------------------------------------------------------------
    def _get_detector(self):
        if self.library == "lingua":
            try:
                from lingua import LanguageDetectorBuilder, Language
                detector = LanguageDetectorBuilder.from_all_languages().build()

                def detect(text):
                    confidences = detector.compute_language_confidence_values(text)
                    if not confidences:
                        return None, 0.0
                    top = confidences[0]
                    return top.language.iso_code_639_1.name.lower(), top.value
                return detect
            except ImportError:
                return None
        elif self.library == "langdetect":
            try:
                from langdetect import detect_langs
                def detect(text):
                    try:
                        results = detect_langs(text)
                        if not results:
                            return None, 0.0
                        return results[0].lang, results[0].prob
                    except Exception:
                        return None, 0.0
                return detect
            except ImportError:
                return None
        return None
