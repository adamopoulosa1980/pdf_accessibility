"""
Shared-Form-XObject demoter — PDF/UA-1 7.20-2.

PDF/UA-1 7.20-2 (and the underlying ISO 32000-1 §14.7.2 rule) forbids a
Form XObject that carries marked-content MCIDs from being referenced
more than once in a document::

    "The content of Form XObjects shall be incorporated into structure
     elements according to ISO 32000-1:2008, 14.7.2"

The rule exists because each MCID belongs to one structure element on
one page, but a re-used XObject would project the same MCID onto every
page that draws it — an impossible many-to-one mapping for the struct
tree.

Real-world PDFs trip this all the time with header/footer templates:
one Form XObject containing the page number text, the running title and
some branding gets drawn on every body page. The tagger (opendataloader)
wraps the page-number text in a BDC with an MCID, and veraPDF then
flags every occurrence.

The correct semantic for repeating headers/footers is **artifact** —
WCAG/PDF-UA explicitly recommend not exposing running page numbers to
screen readers. So this fixer:

  1. Counts how many times each Form XObject is referenced via `Do`
     across all pages.
  2. Picks Form XObjects that are (a) referenced more than once **and**
     (b) contain at least one BDC with an `/MCID`.
  3. Rewrites the XObject's content stream: every MCID-bearing BDC..EMC
     pair is converted to a plain `/Artifact BMC..EMC` wrapper. State
     and painting operators inside are preserved.
  4. Prunes the struct tree of every `/MCR` reference whose `/Stm`
     points at one of the rewritten XObjects, so no struct element ends
     up pointing at a marked-content sequence that no longer exists.

The pass is engine-agnostic (runs regardless of which tagger produced
the structure) and idempotent (re-running on already-fixed XObjects is
a no-op because no MCID-bearing BDCs remain).
"""
from __future__ import annotations

from pathlib import Path

import pikepdf
from pikepdf import Name

from ..config import Config
from ..report import RemediationReport


def _decode_op(op) -> str:
    return (bytes(op).decode("latin-1")
            if hasattr(op, "__bytes__") else str(op))


def _is_form_xobject(obj) -> bool:
    try:
        return obj.get("/Subtype") == Name("/Form")
    except Exception:
        return False


def _xobject_has_mcid(obj) -> bool:
    """True if the Form XObject's content stream contains a BDC with /MCID."""
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


def _count_xobject_references(pdf: pikepdf.Pdf) -> dict[tuple[int, int], int]:
    """
    Count how often each XObject (keyed by ``objgen``) is referenced via
    `Do` in any page content stream.
    """
    counts: dict[tuple[int, int], int] = {}
    for page in pdf.pages:
        resources = None
        try:
            resources = page.get("/Resources")
        except Exception:
            pass
        xobj_dict = None
        if resources is not None:
            try:
                xobj_dict = resources.get("/XObject")
            except Exception:
                xobj_dict = None
        if not xobj_dict:
            continue
        try:
            instructions = list(pikepdf.parse_content_stream(page))
        except Exception:
            continue
        for ops, op in instructions:
            if _decode_op(op) != "Do" or not ops:
                continue
            try:
                target = xobj_dict.get(ops[0])
            except Exception:
                continue
            if target is None:
                continue
            try:
                key = target.objgen
            except Exception:
                continue
            counts[key] = counts.get(key, 0) + 1
    return counts


def _resolve_xobjects_by_objgen(pdf: pikepdf.Pdf,
                                 targets: set[tuple[int, int]]
                                 ) -> dict[tuple[int, int], pikepdf.Object]:
    """Look up the indirect-object handle for each target objgen."""
    found: dict[tuple[int, int], pikepdf.Object] = {}
    for page in pdf.pages:
        try:
            xobj_dict = page.Resources.get("/XObject")
        except Exception:
            continue
        if not xobj_dict:
            continue
        for name in xobj_dict.keys():
            obj = xobj_dict[name]
            try:
                key = obj.objgen
            except Exception:
                continue
            if key in targets and key not in found:
                found[key] = obj
    return found


def _demote_xobject_content(xobj: pikepdf.Object) -> int:
    """
    Rewrite the XObject's content stream, converting every MCID-bearing
    BDC..EMC pair to a plain ``/Artifact BMC..EMC``. Returns the number
    of conversions made.
    """
    from pikepdf import Operator  # mirror structure.py's local import

    try:
        instructions = list(pikepdf.parse_content_stream(xobj))
    except Exception:
        return 0

    decoded = [_decode_op(op) for _, op in instructions]
    rebuilt: list = []
    converted = 0
    i = 0
    n = len(instructions)
    while i < n:
        ops, op = instructions[i]
        if decoded[i] != "BDC":
            rebuilt.append(instructions[i])
            i += 1
            continue
        # Identify the matching EMC at the same depth
        depth = 1
        emc = None
        for j in range(i + 1, n):
            if decoded[j] in ("BMC", "BDC"):
                depth += 1
            elif decoded[j] == "EMC":
                depth -= 1
                if depth == 0:
                    emc = j
                    break
        if emc is None:
            rebuilt.append(instructions[i])
            i += 1
            continue
        # If this BDC carries a /MCID, demote it
        carries_mcid = (len(ops) >= 2
                        and isinstance(ops[1], pikepdf.Dictionary)
                        and ops[1].get("/MCID") is not None)
        if not carries_mcid:
            rebuilt.append(instructions[i])
            i += 1
            continue
        # Already an /Artifact? Leave alone.
        try:
            tag = str(ops[0])
        except Exception:
            tag = ""
        if tag == "/Artifact":
            rebuilt.append(instructions[i])
            i += 1
            continue
        # Emit /Artifact BMC, then the inner ops, then EMC
        rebuilt.append(([Name("/Artifact")], Operator("BMC")))
        for k in range(i + 1, emc):
            rebuilt.append(instructions[k])
        rebuilt.append(([], Operator("EMC")))
        converted += 1
        i = emc + 1

    if converted:
        xobj.write(pikepdf.unparse_content_stream(rebuilt))
        # The XObject no longer participates in the structure tree, so
        # remove the metadata that points at the (now-gone) MCIDs.
        # veraPDF's 7.20-2 test (isUniqueSemanticParent) considers an
        # XObject "semantic" if it carries /StructParents or
        # /StructParent, regardless of whether the content stream still
        # has MCIDs. Without this both keys would leave the rule
        # failing.
        for key in ("/StructParents", "/StructParent"):
            if key in xobj:
                del xobj[key]
    return converted


def _prune_mcr_refs(pdf: pikepdf.Pdf,
                    targets: set[tuple[int, int]]) -> int:
    """
    Remove every ``/MCR`` entry from the struct tree whose ``/Stm``
    resolves to one of the rewritten XObjects. Returns total refs
    removed.
    """
    try:
        root = pdf.Root.StructTreeRoot
    except Exception:
        return 0

    removed = 0
    seen: set[tuple[int, int]] = set()

    def visit(elem: pikepdf.Object) -> None:
        nonlocal removed
        try:
            key = elem.objgen
        except Exception:
            key = None
        if key is not None:
            if key in seen:
                return
            seen.add(key)

        try:
            k_obj = elem.get("/K")
        except Exception:
            return
        if k_obj is None:
            return

        if isinstance(k_obj, pikepdf.Array):
            indices_to_drop: list[int] = []
            for idx in range(len(k_obj)):
                item = k_obj[idx]
                if isinstance(item, pikepdf.Dictionary):
                    if item.get("/Type") == Name("/MCR"):
                        stm = item.get("/Stm")
                        if stm is not None:
                            try:
                                if stm.objgen in targets:
                                    indices_to_drop.append(idx)
                                    continue
                            except Exception:
                                pass
                    else:
                        # Nested struct element
                        visit(item)
            for idx in reversed(indices_to_drop):
                del k_obj[idx]
                removed += 1
        elif isinstance(k_obj, pikepdf.Dictionary):
            if k_obj.get("/Type") == Name("/MCR"):
                stm = k_obj.get("/Stm")
                if stm is not None:
                    try:
                        if stm.objgen in targets:
                            # Whole /K is the offending ref. Set /K to
                            # an empty array so the parent element
                            # survives but no longer points at the
                            # gone-now MCID.
                            elem["/K"] = pikepdf.Array([])
                            removed += 1
                            return
                    except Exception:
                        pass
            else:
                visit(k_obj)

    visit(root)
    return removed


class SharedXObjectFixer:
    """Fix PDF/UA 7.20-2 — Form XObjects with MCIDs referenced >1 time."""

    name = "shared_xobject"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.enabled = bool(
            cfg.tagging.get("demote_shared_mcid_xobjects", True)
        )

    def apply(self, pdf: pikepdf.Pdf, source_path: Path,
              report: RemediationReport) -> None:
        if not self.enabled:
            return

        counts = _count_xobject_references(pdf)
        if not counts:
            return

        # Candidate set: referenced >1 time
        repeats = {key for key, c in counts.items() if c > 1}
        if not repeats:
            return

        resolved = _resolve_xobjects_by_objgen(pdf, repeats)
        # Filter to Form XObjects that actually carry MCIDs
        targets: dict[tuple[int, int], pikepdf.Object] = {}
        for key, obj in resolved.items():
            if _is_form_xobject(obj) and _xobject_has_mcid(obj):
                targets[key] = obj
        if not targets:
            return

        total_converted = 0
        for obj in targets.values():
            total_converted += _demote_xobject_content(obj)

        if total_converted == 0:
            return

        pruned = _prune_mcr_refs(pdf, set(targets.keys()))

        report.add(
            "PDF/UA 7.20-2",
            "fixed",
            f"Demoted {len(targets)} shared Form XObject(s) to /Artifact "
            f"(converted {total_converted} MCID block(s), pruned "
            f"{pruned} struct-tree reference(s))",
            details={
                "xobjects_demoted": len(targets),
                "mcid_blocks_converted": total_converted,
                "struct_tree_refs_pruned": pruned,
            },
        )
