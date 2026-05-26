"""
Artifact-wrap fixer — PDF/UA-1 7.1-3, WTPDF 8.2.2-1.

After structure tagging, real-world taggers (opendataloader in particular)
often leave page content that is neither inside any marked-content
sequence nor inside an /Artifact BMC/EMC pair. veraPDF flags every
painting operator in such a state:

    "Content shall be marked as Artifact or tagged as real content"

This fixer walks each page's top-level content stream, finds runs of
painting operators (text-showing, XObject draws, path fills/strokes,
shadings) outside any BDC/BMC..EMC wrapper, and encloses them in
``/Artifact BMC ... EMC``.

State-setting operators (``q``, ``Q``, ``cm``, ``gs``, colour selectors,
etc.) are bundled into the same wrapper when they sit between painting
operators in the same untagged run, so the graphics state stays
coherent. They are not wrapped on their own.

The fixer never touches content that is already tagged or already inside
an artifact, so it is idempotent and safe to run after any tagger.
"""
from __future__ import annotations

from pathlib import Path

import pikepdf
from pikepdf import Name

from ..config import Config
from ..report import RemediationReport


# Operators that visibly paint something. If a run of untagged
# instructions contains at least one of these, the run becomes an
# artifact. State-only runs are left alone.
PAINTING_OPS: frozenset[str] = frozenset({
    # Text-showing
    "Tj", "TJ", "'", '"',
    # XObject draw (image or form)
    "Do",
    # Path painting
    "S", "s", "F", "f", "F*", "f*",
    "B", "B*", "b", "b*", "n",
    # Shading paint
    "sh",
    # Inline image (BI ... ID ... EI is parsed as a single InlineImage
    # instruction whose operator is "EI" in pikepdf).
    "EI",
})


def _decode_op(op) -> str:
    return (bytes(op).decode("latin-1")
            if hasattr(op, "__bytes__") else str(op))


def _form_xobject_is_tagged(page_resources, name) -> bool:
    """
    True if ``name`` resolves to a Form XObject that the structure tree
    references (directly via /StructParents, or via MCID-bearing BDCs in
    its own content stream).

    Wrapping such a Form XObject in /Artifact would create a
    tagged-content-inside-Artifact failure (PDF/UA 7.1-2), so the
    artifact-wrap pass treats these Do calls as opaque boundaries.
    """
    if page_resources is None:
        return False
    try:
        xobjects = page_resources.get("/XObject")
    except Exception:
        return False
    if not xobjects:
        return False
    try:
        obj = xobjects.get(name)
    except Exception:
        return False
    if obj is None:
        return False
    try:
        if obj.get("/Subtype") != Name("/Form"):
            return False
    except Exception:
        return False
    # A Form XObject that participates in the struct tree advertises
    # itself via /StructParents (array of struct elems referenced) or
    # /StructParent (single struct elem reference).
    if "/StructParents" in obj or "/StructParent" in obj:
        return True
    # Fallback: parse the XObject's stream and look for MCID-bearing
    # BDCs. Cheap because we only walk it once per (page, name) pair.
    try:
        for ops, op in pikepdf.parse_content_stream(obj):
            if _decode_op(op) != "BDC":
                continue
            if (len(ops) >= 2 and isinstance(ops[1], pikepdf.Dictionary)
                    and ops[1].get("/MCID") is not None):
                return True
    except Exception:
        pass
    return False


class ArtifactWrapFixer:
    """Wrap untagged painting content in ``/Artifact BMC ... EMC``."""

    name = "artifact_wrap"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # Off-switch lives under tagging because the fixer is a follow-up
        # to whatever the tagger produced.
        self.enabled = bool(cfg.tagging.get("wrap_untagged_as_artifact", True))

    def apply(self, pdf: pikepdf.Pdf, source_path: Path,
              report: RemediationReport) -> None:
        if not self.enabled:
            return

        from pikepdf import Operator  # local import: matches structure.py

        pages_modified = 0
        total_wraps = 0

        for page_index, page in enumerate(pdf.pages):
            try:
                instructions = list(pikepdf.parse_content_stream(page))
            except Exception:
                continue
            if not instructions:
                continue

            try:
                page_resources = page.get("/Resources")
            except Exception:
                page_resources = None

            decoded = [_decode_op(op) for _, op in instructions]
            rebuilt: list = []
            untagged: list[int] = []
            depth = 0
            page_wraps = 0

            def flush() -> int:
                """Emit the pending untagged run. Returns 1 if wrapped, 0 if not."""
                nonlocal untagged
                if not untagged:
                    return 0
                has_paint = any(decoded[idx] in PAINTING_OPS for idx in untagged)
                if not has_paint:
                    # Pure state changes — emit as-is; veraPDF doesn't
                    # complain about untagged state ops.
                    for idx in untagged:
                        rebuilt.append(instructions[idx])
                    untagged = []
                    return 0
                rebuilt.append(([Name("/Artifact")], Operator("BMC")))
                for idx in untagged:
                    rebuilt.append(instructions[idx])
                rebuilt.append(([], Operator("EMC")))
                untagged = []
                return 1

            for i, op_str in enumerate(decoded):
                if op_str in ("BMC", "BDC"):
                    page_wraps += flush()
                    rebuilt.append(instructions[i])
                    depth += 1
                elif op_str == "EMC":
                    # An EMC at depth 0 is a malformed stream; leave it
                    # alone rather than rewrite. Otherwise flush any
                    # stragglers (shouldn't happen — well-formed BDC blocks
                    # don't leak painting to the page level).
                    page_wraps += flush()
                    rebuilt.append(instructions[i])
                    if depth > 0:
                        depth -= 1
                elif (op_str == "Do" and depth == 0
                      and len(instructions[i][0]) >= 1
                      and _form_xobject_is_tagged(
                          page_resources, instructions[i][0][0])):
                    # The Do references a Form XObject that already
                    # participates in the structure tree. Folding it
                    # into our /Artifact wrapper would create a
                    # tagged-content-inside-Artifact violation
                    # (PDF/UA 7.1-2). Treat it as a hard boundary.
                    page_wraps += flush()
                    rebuilt.append(instructions[i])
                else:
                    if depth == 0:
                        untagged.append(i)
                    else:
                        rebuilt.append(instructions[i])

            page_wraps += flush()

            if page_wraps:
                page.Contents = pdf.make_stream(
                    pikepdf.unparse_content_stream(rebuilt)
                )
                pages_modified += 1
                total_wraps += page_wraps

        if total_wraps:
            report.add(
                "PDF/UA 7.1-3",
                "fixed",
                f"Wrapped {total_wraps} untagged content run(s) as "
                f"/Artifact across {pages_modified} page(s)",
                details={"pages_modified": pages_modified,
                         "wraps": total_wraps},
            )
