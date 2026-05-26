"""
Main pipeline orchestrator. Runs fixers in the correct order:

  1. MetadataFixer      — sets Lang, Title, MarkInfo. Must run before
                          structure tagging because tagging engines check
                          for these.
  2. StructureFixer     — adds StructTreeRoot. Can REPLACE the pdf object
                          (Adobe/PDFix return a re-tagged file).
  2c. ArtifactWrapFixer — wraps untagged painting content in /Artifact
                          BMC..EMC so PDF/UA 7.1-3 / WTPDF 8.2.2-1 pass.
  2d. SharedXObjectFixer — demotes Form XObjects with MCIDs that are
                          referenced from more than one page (PDF/UA
                          7.20-2).
  3. ReadingOrderFixer  — sets /Tabs = /S, analyzes reading order.
  4. TableFixer         — detects tables, applies header info.
  5. ImageAltTextFixer  — adds alt text to figures.
  6. FormFieldFixer     — labels form widgets.
  7. ContrastFixer      — reports / remaps colors.
  8. LanguageFixer      — flags non-primary-language spans.
  9. Validator          — runs veraPDF on the final output.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf

from .config import Config
from .fixers import (
    ArtifactWrapFixer,
    ContrastFixer,
    FormFieldFixer,
    ImageAltTextFixer,
    LanguageDetectionFixer,
    MetadataFixer,
    ReadingOrderFixer,
    SharedXObjectFixer,
    StructureFixer,
    TableFixer,
    WTPDFFixer,
)
from .report import RemediationReport
from .validator import Validator


class RemediationPipeline:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.validator = Validator(config)

    def run(self, source: str | Path) -> RemediationReport:
        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(source)

        report = RemediationReport(source_pdf=str(source))

        out_dir = Path(self.cfg.output["directory"])
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = self.cfg.output.get("suffix", "_a11y")
        output_path = out_dir / f"{source.stem}{suffix}.pdf"

        # Backup
        if self.cfg.output.get("backup_originals", True):
            backup = out_dir / f"{source.stem}_original.pdf"
            if not backup.exists():
                shutil.copy2(source, backup)

        # Work on a copy so we don't touch the source
        work_path = out_dir / f".working_{source.name}"
        shutil.copy2(source, work_path)

        try:
            pdf = pikepdf.Pdf.open(work_path, allow_overwriting_input=True)

            metadata_fixer = MetadataFixer(self.cfg)

            # 1. Metadata (pre-tagging) — sets /Lang etc that taggers consult
            print("[1/9] Document metadata (language, title, PDF/UA marker)...",
                  flush=True)
            metadata_fixer.apply(pdf, source, report)

            # 2. Structure — may return a new Pdf object. External taggers
            # (opendataloader, adobe, pdfix) replace the XMP metadata stream
            # wholesale, wiping our pdfuaid:part marker.
            print("[2/9] Tagging document structure "
                  "(this is the longest step)...", flush=True)
            pdf = StructureFixer(self.cfg).apply(pdf, source, report) or pdf

            # 2b. Re-apply metadata so the PDF/UA XMP marker survives the
            # tagger's XMP rewrite (veraPDF 5-1 rule).
            metadata_fixer.apply(pdf, source, report)

            # 2c. Wrap any page content the tagger left outside the
            # structure tree (and outside an Artifact) in /Artifact BMC.
            # Without this veraPDF fails 7.1-3 / WTPDF 8.2.2-1 on every
            # stray painting operator (charts, headers, decorations).
            print("[2c/9] Marking untagged content as Artifact...",
                  flush=True)
            ArtifactWrapFixer(self.cfg).apply(pdf, source, report)

            # 2d. Demote Form XObjects that carry MCIDs and are
            # referenced from more than one page (typically repeating
            # headers/footers) to /Artifact, and prune the matching
            # struct-tree /MCR references. Fixes PDF/UA 7.20-2.
            print("[2d/9] Demoting shared MCID-bearing Form XObjects...",
                  flush=True)
            SharedXObjectFixer(self.cfg).apply(pdf, source, report)

            # 3. Reading order
            print("[3/9] Reading order...", flush=True)
            ReadingOrderFixer(self.cfg).apply(pdf, source, report)

            # 4. Tables
            print("[4/9] Tables (header detection)...", flush=True)
            TableFixer(self.cfg).apply(pdf, source, report)

            # 5. Images / alt text — may return a replacement Pdf object
            # (it reopens from a temp snapshot so pikepdf object numbers
            # match what pymupdf sees as xrefs).
            print("[5/9] Image alt text (calling the vision model)...",
                  flush=True)
            image_fixer = ImageAltTextFixer(self.cfg)
            pdf = image_fixer.apply(pdf, source, report) or pdf

            # 6. Forms
            print("[6/9] Form fields...", flush=True)
            FormFieldFixer(self.cfg).apply(pdf, source, report)

            # 7. Contrast (often report-only)
            print("[7/9] Colour contrast...", flush=True)
            ContrastFixer(self.cfg).apply(pdf, source, report)

            # 8. Per-span language detection
            print("[8/9] Language of parts...", flush=True)
            LanguageDetectionFixer(self.cfg).apply(pdf, source, report)

            # 9. WTPDF accessibility-profile fixes (link Alt/Contents sync,
            # PDF 2.0 namespace on /Document, WTPDF XMP declaration).
            print("[9/9] WTPDF accessibility profile...", flush=True)
            WTPDFFixer(self.cfg).apply(pdf, source, report)

            # Save the remediated PDF
            print("Saving remediated PDF...", flush=True)
            pdf.save(output_path, linearize=False)
            pdf.close()
            report.output_pdf = str(output_path)

            # 9. Validate
            self.validator.validate(output_path, report)
            print(f"Done: {output_path}", flush=True)

        finally:
            work_path.unlink(missing_ok=True)
            # Fixers write working files alongside the source PDF and the
            # output dir. ImageAltTextFixer in particular holds a temp file
            # open via mmap until pdf.close(); clean everything up here,
            # after the save+close above. Without this the tmp files (~20 MB
            # each) leak into the user's source folder.
            for parent in (out_dir, source.parent):
                for pattern in (f".tmp_imgs_{source.name}",
                                f".tmp_in_{source.name}",
                                f".tmp_struct_{source.name}",
                                f".tmp_order_{source.name}",
                                f".tmp_tables_{source.name}",
                                f".tmp_forms_{source.name}",
                                f".tmp_contrast_{source.name}",
                                f".tmp_lang_{source.name}",
                                f".tmp_docling_{source.name}"):
                    for tmp_file in parent.glob(pattern):
                        tmp_file.unlink(missing_ok=True)
                # opendataloader writes a directory of tmp files
                for tmp_dir_path in parent.glob(f".tmp_odl_{source.stem}*"):
                    if tmp_dir_path.is_dir():
                        import shutil as _sh
                        _sh.rmtree(tmp_dir_path, ignore_errors=True)
                    else:
                        tmp_dir_path.unlink(missing_ok=True)
            report.finalize()

            if self.cfg.output.get("write_report", True):
                report_path = out_dir / f"{source.stem}_report.json"
                report.write(report_path)

        return report
