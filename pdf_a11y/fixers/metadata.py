"""
Metadata & document-level fixer.

Handles:
  - WCAG 3.1.1 Language of Page  -> /Lang in Catalog
  - WCAG 2.4.2 Page Titled       -> /Title in Info, DisplayDocTitle in ViewerPreferences
  - PDF/UA-1 conformance         -> XMP metadata marker
  - MarkInfo /Marked true        -> Required for tagged PDFs
"""
from __future__ import annotations

from pathlib import Path

import pikepdf
from pikepdf import Dictionary, Name, String

from ..config import Config
from ..report import RemediationReport


# Minimal XMP packet declaring PDF/UA-1 conformance.
# Real-world XMP should be merged with existing packet; this is the marker.
PDF_UA_XMP = """<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="pdf_a11y">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:pdfuaid="http://www.aiim.org/pdfua/ns/id/"
        xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:pdf="http://ns.adobe.com/pdf/1.3/">
      <pdfuaid:part>1</pdfuaid:part>
      <dc:title><rdf:Alt><rdf:li xml:lang="x-default">{title}</rdf:li></rdf:Alt></dc:title>
      <dc:language><rdf:Bag><rdf:li>{lang}</rdf:li></rdf:Bag></dc:language>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="r"?>"""


class MetadataFixer:
    """Sets language, title, viewer preferences, and PDF/UA marker."""

    name = "metadata"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def apply(self, pdf: pikepdf.Pdf, source_path: Path, report: RemediationReport) -> None:
        doc_cfg = self.cfg.document

        # --- WCAG 3.1.1: Set document language ---
        lang = doc_cfg["primary_language"]
        pdf.Root.Lang = String(lang)
        report.add("3.1.1", "fixed", f"Set document /Lang = {lang}")

        # --- WCAG 2.4.2: Set document title ---
        title = doc_cfg.get("title")
        if not title:
            # Fallback chain: existing metadata title -> filename stem
            try:
                existing = pdf.docinfo.get("/Title")
                title = str(existing) if existing else source_path.stem
            except Exception:
                title = source_path.stem

        with pdf.open_metadata() as meta:
            meta["dc:title"] = title
            if doc_cfg.get("author"):
                meta["dc:creator"] = [doc_cfg["author"]]
            if doc_cfg.get("subject"):
                meta["dc:description"] = doc_cfg["subject"]
            if doc_cfg.get("keywords"):
                meta["pdf:Keywords"] = ", ".join(doc_cfg["keywords"])
            if doc_cfg.get("declare_pdf_ua", True):
                meta["pdfuaid:part"] = "1"

        # Also update legacy Info dict (some readers still check it)
        pdf.docinfo["/Title"] = String(title)
        if doc_cfg.get("author"):
            pdf.docinfo["/Author"] = String(doc_cfg["author"])

        report.add("2.4.2", "fixed", f"Set document title to '{title}'")

        # --- WCAG 2.4.2 supplement: DisplayDocTitle = true ---
        if doc_cfg.get("display_doc_title", True):
            if "/ViewerPreferences" not in pdf.Root:
                pdf.Root.ViewerPreferences = Dictionary()
            pdf.Root.ViewerPreferences.DisplayDocTitle = True
            report.add("2.4.2", "fixed", "Enabled DisplayDocTitle in ViewerPreferences")

        # --- PDF/UA marker: MarkInfo dict ---
        # Required when the PDF claims to be tagged.
        if "/MarkInfo" not in pdf.Root:
            pdf.Root.MarkInfo = Dictionary()
        pdf.Root.MarkInfo.Marked = True
        report.add("PDF/UA", "fixed", "Set MarkInfo /Marked = true")
