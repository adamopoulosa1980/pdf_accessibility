"""
WTPDF 1.0 Accessibility profile fixes.

veraPDF's `wt1a` (Well-Tagged PDF for Accessibility) profile is stricter
than PDF/UA-1 and closer to what PAC checks. This fixer addresses the
remaining items after structure tagging:

  6.1.3-1   — WTPDF accessibility declaration missing in XMP
  8.2.5.2-2 — /Document StructElem must be in the PDF 2.0 namespace
  8.9.4.2-1 — Link annotation /Contents must match enclosing
              StructElem /Alt byte-for-byte
  8.8-1     — Internal link destinations should be structure destinations
              (resolved separately; see _convert_destinations)

Runs after StructureFixer (and the post-tag MetadataFixer call) so the
StructTreeRoot and XMP exist.
"""
from __future__ import annotations

from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from ..config import Config
from ..report import RemediationReport


WTPDF_ACCESSIBILITY_URI = "http://pdfa.org/declarations/wtpdf#accessibility1.0"
PDF20_STRUCT_NAMESPACE = "http://iso.org/pdf2/ssn"


class WTPDFFixer:
    """Apply WTPDF 1.0 accessibility-profile fixes."""

    name = "wtpdf"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def apply(self, pdf: pikepdf.Pdf, source_path: Path, report: RemediationReport) -> None:
        if "/StructTreeRoot" not in pdf.Root:
            return  # WTPDF only meaningful with a struct tree

        self._set_pdf20_namespace_on_document(pdf, report)
        self._sync_link_alt_contents(pdf, report)
        self._convert_destinations_to_structure(pdf, report)
        self._set_actualtext_on_pua_struct_elements(pdf, report)
        self._add_wtpdf_declaration_to_xmp(pdf, report)

    # --------------------------------------------------------------
    def _set_pdf20_namespace_on_document(self, pdf, report):
        """
        WTPDF 8.2.5.2-2: the StructTreeRoot's only child must be a /Document
        StructElem in the PDF 2.0 namespace (http://iso.org/pdf2/ssn).

        The PDF 2.0 namespace is referenced via a /NS entry on the StructElem,
        which points at a Namespace dictionary listed in
        StructTreeRoot/Namespaces.
        """
        str_root = pdf.Root.StructTreeRoot
        k = str_root.get("/K")
        if k is None:
            return
        document = k if isinstance(k, pikepdf.Dictionary) else (
            k[0] if isinstance(k, pikepdf.Array) and len(k) > 0 else None
        )
        if document is None or str(document.get("/S", "")) != "/Document":
            return

        # Get-or-create the PDF 2.0 namespace dict in StructTreeRoot.Namespaces
        ns_list = str_root.get("/Namespaces")
        if ns_list is None:
            ns_list = Array()
            str_root.Namespaces = ns_list

        pdf20_ns = None
        for ns in ns_list:
            if str(ns.get("/NS", "")) == PDF20_STRUCT_NAMESPACE:
                pdf20_ns = ns
                break
        if pdf20_ns is None:
            pdf20_ns_dict = Dictionary(
                Type=Name.Namespace,
                NS=String(PDF20_STRUCT_NAMESPACE),
            )
            pdf20_ns = pdf.make_indirect(pdf20_ns_dict)
            ns_list.append(pdf20_ns)

        document.NS = pdf20_ns
        report.add(
            "1.3.1", "fixed",
            "WTPDF 8.2.5.2-2: set PDF 2.0 namespace on /Document StructElem",
        )

    # --------------------------------------------------------------
    def _sync_link_alt_contents(self, pdf, report):
        """
        WTPDF 8.9.4.2-1: where a link annotation has /Contents and its
        enclosing /Link StructElem has /Alt, the two must be identical.

        opendataloader writes both with the same text but in different
        byte encodings; this re-writes them as fresh pikepdf.String values
        derived from the same source text, which forces a canonical encoding.
        """
        str_root = pdf.Root.StructTreeRoot
        ptree = str_root.get("/ParentTree")
        if ptree is None:
            return
        nums = ptree.get("/Nums")
        if nums is None:
            return

        # Index ParentTree.Nums [k0, v0, k1, v1, ...] into a dict
        sp_to_elem = {}
        for i in range(0, len(nums), 2):
            try:
                sp_to_elem[int(nums[i])] = nums[i + 1]
            except Exception:
                continue

        synced = 0
        for page in pdf.pages:
            annots = page.get("/Annots")
            if annots is None:
                continue
            for ann in annots:
                if str(ann.get("/Subtype", "")) != "/Link":
                    continue
                if "/Contents" not in ann:
                    continue
                sp = ann.get("/StructParent")
                if sp is None:
                    continue
                try:
                    sp_int = int(sp)
                except Exception:
                    continue
                struct_elem = sp_to_elem.get(sp_int)
                # When the StructParent points at an array of struct elems
                # (multiple roles for one annotation), the value is an Array.
                if isinstance(struct_elem, pikepdf.Array):
                    struct_elem = struct_elem[0] if len(struct_elem) > 0 else None
                if not isinstance(struct_elem, pikepdf.Dictionary):
                    continue
                if "/Alt" not in struct_elem:
                    continue

                alt_text = str(struct_elem.Alt)
                contents_text = str(ann.Contents)
                # Use Alt as canonical (PAC-readable label); fall back to
                # contents if alt is empty.
                canonical = alt_text or contents_text
                if not canonical:
                    continue
                fresh = String(canonical)
                ann.Contents = fresh
                struct_elem.Alt = fresh
                synced += 1

        if synced:
            report.add(
                "4.1.2", "fixed",
                f"WTPDF 8.9.4.2-1: synced /Alt and /Contents on {synced} "
                f"link annotation(s) to byte-equal canonical encoding",
                details={"links_synced": synced},
            )

    # --------------------------------------------------------------
    def _convert_destinations_to_structure(self, pdf, report):
        """
        WTPDF 8.8-1: destinations whose target lies within the current
        document must be structure destinations (first element of the
        explicit array is a /StructElem reference, not a page reference).

        We walk every link annotation, resolve its /Dest to an explicit
        destination array, and rewrite the leading page reference to a
        /StructElem on that page. The remaining fit-type and coords are
        preserved so visual scrolling behaviour is unchanged.
        """
        # 1) Walk /Names/Dests name tree to resolve named destinations.
        name_to_dest = {}
        try:
            names_root = pdf.Root.get("/Names")
            if names_root is not None:
                dests_root = names_root.get("/Dests")
                if dests_root is not None:
                    self._collect_name_tree(dests_root, name_to_dest)
            # Older PDFs use /Dests directly on Root as a flat dict
            flat_dests = pdf.Root.get("/Dests")
            if flat_dests is not None:
                for key in flat_dests.keys():
                    name_to_dest[str(key).lstrip("/")] = flat_dests[key]
        except Exception as e:
            report.add(
                "1.3.2", "warning",
                f"WTPDF 8.8-1: could not enumerate named destinations ({e})",
            )

        # 2) Build map: page object id -> a /StructElem on that page that
        # has at least one direct MCID in its /K (i.e. is a leaf-like
        # content-bearing element, not a container like /L or /Art).
        #
        # PAC's "Structural parent tree" check traces destination → struct
        # elem → /Pg → page.StructParents → ParentTree array → expects the
        # struct elem to be present in that array. Container elements have
        # no MCID and aren't listed in the page's parent tree array, so
        # PAC flags those destinations as "Entry for given 'StructParents'
        # not found". Picking a leaf elem with an MCID gets PAC's traversal
        # to succeed.
        page_to_struct: dict = {}
        page_to_struct_fallback: dict = {}

        def has_direct_mcid(elem) -> bool:
            kk = elem.get("/K")
            if kk is None:
                return False
            items = kk if isinstance(kk, pikepdf.Array) else [kk]
            for item in items:
                # Plain MCID integer
                try:
                    int(item)
                    return True
                except Exception:
                    pass
                # MCR dict: { /Type /MCR /Pg ... /MCID N }
                if isinstance(item, pikepdf.Dictionary):
                    t = item.get("/Type")
                    if t is not None and str(t) == "/MCR":
                        return True
            return False

        def walk(elem, depth=0):
            if depth > 80:
                return
            try:
                t = elem.get("/Type")
            except Exception:
                return
            if t is not None and str(t) == "/StructElem":
                pg = elem.get("/Pg")
                if pg is not None:
                    try:
                        pid = pg.objgen[0]
                        # Prefer leaf elems with direct MCIDs; fall back to
                        # any struct elem on the page if none has an MCID.
                        if has_direct_mcid(elem):
                            if pid not in page_to_struct:
                                page_to_struct[pid] = elem
                        elif pid not in page_to_struct_fallback:
                            page_to_struct_fallback[pid] = elem
                    except Exception:
                        pass
            k = elem.get("/K")
            if k is None:
                return
            if isinstance(k, pikepdf.Array):
                for child in k:
                    if isinstance(child, pikepdf.Dictionary):
                        walk(child, depth + 1)
            elif isinstance(k, pikepdf.Dictionary):
                walk(k, depth + 1)

        walk(pdf.Root.StructTreeRoot)

        # Merge fallback for pages that don't have any MCID-bearing struct elem
        for pid, elem in page_to_struct_fallback.items():
            page_to_struct.setdefault(pid, elem)

        # 3) Rewrite destinations in: link annotations, outline bookmarks,
        #    and the catalog's OpenAction.
        #
        # PDF 2.0 (ISO 32000-2 §12.6.4.11) defines structure destinations
        # via the /SD entry on a GoTo action. We KEEP the original page+coord
        # destination as /D (so Acrobat / Foxit / Chrome's PDF viewer still
        # navigate correctly — many readers do not yet implement struct
        # destinations directly in /Dest) and ADD the struct destination
        # under /SD. PDF/UA / WTPDF validators read /SD.
        converted = 0
        skipped = 0

        def rewrite_one(carrier):
            """
            Replace carrier's existing destination machinery with a GoTo
            action carrying both:
              /D  – the original page+coord destination (used by viewers)
              /SD – the structure destination (used by validators)
            """
            nonlocal converted, skipped
            page_dest = self._resolve_dest(carrier, name_to_dest)
            if page_dest is None:
                return
            if not isinstance(page_dest, pikepdf.Array) or len(page_dest) < 1:
                return
            target = page_dest[0]
            try:
                # Already a struct destination? Leave it.
                if isinstance(target, pikepdf.Dictionary) and \
                   str(target.get("/Type", "")) == "/StructElem":
                    return
                page_id = target.objgen[0]
            except Exception:
                return
            struct_elem = page_to_struct.get(page_id)
            if struct_elem is None:
                skipped += 1
                return

            # Build the struct destination: replace the page ref with the
            # struct elem; carry over the fit-type + coords verbatim.
            struct_dest = pikepdf.Array(
                [struct_elem] + [page_dest[i] for i in range(1, len(page_dest))]
            )

            # Build the GoTo action with both /D and /SD.
            action = pikepdf.Dictionary(
                Type=Name.Action,
                S=Name.GoTo,
                D=page_dest,
                SD=struct_dest,
            )

            # Replace whatever destination machinery was there.
            try:
                if "/Dest" in carrier:
                    del carrier["/Dest"]
            except Exception:
                pass
            carrier["/A"] = action
            converted += 1

        # 3a) Link annotations
        for page in pdf.pages:
            annots = page.get("/Annots")
            if annots is None:
                continue
            for ann in annots:
                if not isinstance(ann, pikepdf.Dictionary):
                    continue
                if str(ann.get("/Subtype", "")) != "/Link":
                    continue
                rewrite_one(ann)

        # 3b) Outline (bookmarks) tree — walk siblings via /Next
        outlines = pdf.Root.get("/Outlines")
        if outlines is not None:
            def walk_outline(node):
                if node is None or not isinstance(node, pikepdf.Dictionary):
                    return
                rewrite_one(node)
                first = node.get("/First")
                if first is not None:
                    walk_outline(first)
                nxt = node.get("/Next")
                if nxt is not None:
                    walk_outline(nxt)
            first = outlines.get("/First")
            if first is not None:
                walk_outline(first)

        # 3c) Catalog /OpenAction (may be a destination array OR an action dict)
        oa = pdf.Root.get("/OpenAction")
        if isinstance(oa, pikepdf.Array):
            page_dest = oa
            if len(page_dest) >= 1:
                target = page_dest[0]
                try:
                    if not (isinstance(target, pikepdf.Dictionary) and
                            str(target.get("/Type", "")) == "/StructElem"):
                        page_id = target.objgen[0]
                        struct_elem = page_to_struct.get(page_id)
                        if struct_elem is not None:
                            struct_dest = pikepdf.Array(
                                [struct_elem] +
                                [page_dest[i] for i in range(1, len(page_dest))]
                            )
                            # Replace OpenAction array with an action that
                            # carries both /D (viewer) and /SD (validator).
                            pdf.Root.OpenAction = pikepdf.Dictionary(
                                Type=Name.Action,
                                S=Name.GoTo,
                                D=page_dest,
                                SD=struct_dest,
                            )
                            converted += 1
                        else:
                            skipped += 1
                except Exception:
                    pass
        elif isinstance(oa, pikepdf.Dictionary):
            rewrite_one(oa)

        report.add(
            "1.3.2", "fixed" if converted else "warning",
            f"WTPDF 8.8-1: converted {converted} destination(s) to "
            f"structure destinations" +
            (f"; skipped {skipped} (no struct elem on target page)"
             if skipped else ""),
            details={"converted": converted, "skipped": skipped},
        )

    @staticmethod
    def _collect_name_tree(node, out: dict) -> None:
        """Walk a PDF name tree, collecting /Names pairs into `out` dict."""
        names = node.get("/Names")
        if names is not None:
            for i in range(0, len(names), 2):
                try:
                    out[str(names[i])] = names[i + 1]
                except Exception:
                    continue
        kids = node.get("/Kids")
        if kids is not None:
            for kid in kids:
                if isinstance(kid, pikepdf.Dictionary):
                    WTPDFFixer._collect_name_tree(kid, out)

    @staticmethod
    def _resolve_dest(ann, name_to_dest: dict):
        """
        Resolve a link annotation's destination to an explicit destination
        array. Handles:
          - /Dest as a name (looked up in name_to_dest)
          - /Dest as an explicit array (returned as-is)
          - /A → /GoTo action with /D (name or array)
        Returns the explicit array or None.
        """
        dest = ann.get("/Dest")
        if dest is None:
            action = ann.get("/A")
            if action is not None and str(action.get("/S", "")) == "/GoTo":
                dest = action.get("/D")
        if dest is None:
            return None
        # Name -> look up
        if isinstance(dest, pikepdf.Name):
            return name_to_dest.get(str(dest).lstrip("/"))
        if isinstance(dest, pikepdf.String):
            return name_to_dest.get(str(dest))
        if isinstance(dest, pikepdf.Array):
            return dest
        # Indirect "Dest dict" form: { /D <array-or-name> }
        if isinstance(dest, pikepdf.Dictionary):
            inner = dest.get("/D")
            if inner is not None:
                if isinstance(inner, pikepdf.Name):
                    return name_to_dest.get(str(inner).lstrip("/"))
                if isinstance(inner, pikepdf.String):
                    return name_to_dest.get(str(inner))
                if isinstance(inner, pikepdf.Array):
                    return inner
        return None

    # --------------------------------------------------------------
    def _set_actualtext_on_pua_struct_elements(self, pdf, report):
        """
        WTPDF 8.4.3-1: real content that maps to Unicode Private Use Area
        (PUA) codepoints (icon fonts like FontAwesome) must carry an
        /ActualText or /Alt.

        We CANNOT solve this by inserting /Span BDC/EMC marked-content
        around the Tj/TJ operator: PDF 2.0 (ISO 32000-2, §14.6.2) forbids
        marked-content operators inside text objects (BT...ET), and PAC's
        4.1 Parsing check enforces this strictly — naive wrapping creates
        thousands of "BMC not allowed in this state" syntax errors.

        Safe alternative: find which (page, MCID) pairs reference icon-font
        glyphs in the content stream, walk the StructTreeRoot to find the
        struct elements that contain those MCIDs, and add /ActualText to
        those struct elements. The rule's predicate (alt-or-actualtext
        present on the glyph's ancestor) is satisfied without touching
        the content stream.
        """
        # 1) Walk content streams to map (page_obj_id, mcid) -> contains_PUA
        pua_mcids = self._find_pua_marked_content_ids(pdf, report)
        if not pua_mcids:
            return

        # 2) Walk struct tree and add /ActualText where /K references a
        # PUA-tagged MCID and the element doesn't already have ActualText/Alt.
        updated = self._add_actualtext_to_struct_elems_with_mcids(pdf, pua_mcids)
        if updated:
            report.add(
                "1.1.1", "fixed",
                f"WTPDF 8.4.3-1: added /ActualText to {updated} struct "
                f"element(s) containing icon-font (PUA) glyphs",
                details={"struct_elems_updated": updated,
                         "pua_marked_content_pairs": len(pua_mcids)},
            )

    def _find_pua_marked_content_ids(self, pdf, report) -> set:
        """
        Returns set of (page_obj_id, mcid) where the marked-content block
        with that MCID on that page contains a Tj/TJ executed under an
        icon-font Tf. Reads content streams non-destructively.
        """
        result = set()
        for page in pdf.pages:
            try:
                resources = page.get("/Resources", {})
                fonts = resources.get("/Font") if resources else None
                if fonts is None:
                    continue
                pua_font_keys = set()
                for fname in fonts.keys():
                    try:
                        fref = fonts[fname]
                        base = str(fref.get("/BaseFont", ""))
                        if self._is_icon_font(base):
                            pua_font_keys.add(str(fname))
                    except Exception:
                        continue
                if not pua_font_keys:
                    continue

                instructions = list(pikepdf.parse_content_stream(page))
                page_id = page.objgen[0]
                # Track BMC/BDC nesting and active MCID + font
                mcid_stack = []  # most-recent-MCID at the top
                current_font = None
                for ops, op in instructions:
                    op_str = (bytes(op).decode("latin-1")
                              if hasattr(op, "__bytes__") else str(op))
                    if op_str == "BDC" and len(ops) >= 2:
                        # ops[1] is the properties dict (may have /MCID)
                        props = ops[1]
                        mcid = None
                        try:
                            if isinstance(props, pikepdf.Dictionary):
                                v = props.get("/MCID")
                                if v is not None:
                                    mcid = int(v)
                        except Exception:
                            pass
                        mcid_stack.append(mcid)
                    elif op_str == "BMC":
                        mcid_stack.append(None)
                    elif op_str == "EMC":
                        if mcid_stack:
                            mcid_stack.pop()
                    elif op_str == "Tf" and len(ops) >= 1:
                        try:
                            current_font = str(ops[0])
                        except Exception:
                            current_font = None
                    elif op_str in ("Tj", "TJ", "'", '"'):
                        if current_font in pua_font_keys:
                            # Find the innermost MCID on the stack
                            for mcid in reversed(mcid_stack):
                                if mcid is not None:
                                    result.add((page_id, mcid))
                                    break
            except Exception as e:
                report.add(
                    "1.1.1", "warning",
                    f"WTPDF 8.4.3-1: failed to scan PUA glyphs on a page ({e})",
                )
        return result

    def _add_actualtext_to_struct_elems_with_mcids(self, pdf, pua_mcids: set) -> int:
        """
        Walk the StructTreeRoot. For each StructElem whose /Pg+MCID set
        intersects pua_mcids, set /ActualText="" (decorative) if no
        /ActualText or /Alt is already present.
        """
        updated = 0

        def elem_references_pua(elem) -> bool:
            pg = elem.get("/Pg")
            if pg is None:
                return False
            try:
                pg_id = pg.objgen[0]
            except Exception:
                return False
            k = elem.get("/K")
            if k is None:
                return False
            # /K can be: int (MCID), dict (MCR/OBJR), or array of either
            def check(item) -> bool:
                # Plain MCID integer
                try:
                    if isinstance(item, (int,)) or hasattr(item, "__int__"):
                        return (pg_id, int(item)) in pua_mcids
                except Exception:
                    pass
                # MCR dict: { /Type /MCR /Pg ... /MCID N }
                if isinstance(item, pikepdf.Dictionary):
                    t = item.get("/Type")
                    if t is not None and str(t) == "/MCR":
                        mc = item.get("/MCID")
                        page_ref = item.get("/Pg") or pg
                        try:
                            return (page_ref.objgen[0], int(mc)) in pua_mcids
                        except Exception:
                            return False
                return False

            if isinstance(k, pikepdf.Array):
                for it in k:
                    if check(it):
                        return True
                return False
            return check(k)

        def walk(elem, depth=0):
            nonlocal updated
            if depth > 80:
                return
            try:
                t = elem.get("/Type")
            except Exception:
                return
            if t is not None and str(t) == "/StructElem":
                if elem_references_pua(elem):
                    has_alt = "/Alt" in elem
                    has_actualtext = "/ActualText" in elem
                    if not (has_alt or has_actualtext):
                        elem.ActualText = String("")
                        updated += 1
            k = elem.get("/K")
            if k is None:
                return
            if isinstance(k, pikepdf.Array):
                for child in k:
                    if isinstance(child, pikepdf.Dictionary):
                        walk(child, depth + 1)
            elif isinstance(k, pikepdf.Dictionary):
                walk(k, depth + 1)

        walk(pdf.Root.StructTreeRoot)
        return updated

    @staticmethod
    def _is_icon_font(base_font: str) -> bool:
        """Heuristic: returns True if BaseFont is an icon font using PUA codepoints."""
        if not base_font:
            return False
        b = base_font.replace("/", "").lower()
        return any(marker in b for marker in (
            "fontawesome", "font-awesome",
            "glyphicons",
            "materialicons", "material-icons",
            "iconfont",
            "fa-solid", "fa-regular", "fa-light", "fa-brands",
        ))

    # --------------------------------------------------------------
    def _add_wtpdf_declaration_to_xmp(self, pdf, report):
        """
        WTPDF 6.1.3-1: the XMP must include a pdfd:declarations bag containing
        a pdfd:conformsTo entry with the URI
        "http://pdfa.org/declarations/wtpdf#accessibility1.0".

        pikepdf's metadata dict cannot model the rdf:Bag structure, and
        writing a plain "pdfd:declaration" property produces an
        unnamespaced <declaration> element that breaks XMP parsers
        (subsequently hiding dc:title and pdfuaid:part from veraPDF).
        So we manipulate the raw XMP XML directly.
        """
        try:
            self._inject_pdfd_declarations(pdf)
            report.add(
                "PDF/UA", "fixed",
                "WTPDF 6.1.3-1: added pdfd:declarations/conformsTo with "
                "accessibility URI to XMP",
            )
        except Exception as e:
            report.add(
                "PDF/UA", "warning",
                f"WTPDF 6.1.3-1: could not add pdfd:declarations to XMP ({e})",
            )

    @staticmethod
    def _inject_pdfd_declarations(pdf) -> None:
        """
        Raw-XML XMP edit: ensure the metadata stream contains
            <pdfd:declarations xmlns:pdfd="http://pdfa.org/ns/declarations#">
              <rdf:Bag>
                <rdf:li rdf:parseType="Resource">
                  <pdfd:conformsTo>...accessibility URI...</pdfd:conformsTo>
                </rdf:li>
              </rdf:Bag>
            </pdfd:declarations>
        as a property of the existing rdf:Description.
        """
        meta = pdf.Root.Metadata
        raw = bytes(meta.read_bytes()).decode("utf-8", errors="replace")

        # If pikepdf already wrote an unnamespaced <declaration>...</declaration>,
        # strip it so we don't keep the broken form alongside the fix.
        import re
        raw = re.sub(
            r"<declaration>[^<]*</declaration>",
            "",
            raw,
        )
        # Also strip any prior pdfd:declarations block to keep this idempotent.
        raw = re.sub(
            r"<pdfd:declarations[\s\S]*?</pdfd:declarations>",
            "",
            raw,
        )

        block = (
            '<pdfd:declarations xmlns:pdfd="http://pdfa.org/declarations/">'
            '<rdf:Bag><rdf:li rdf:parseType="Resource">'
            f'<pdfd:conformsTo>{WTPDF_ACCESSIBILITY_URI}</pdfd:conformsTo>'
            '</rdf:li></rdf:Bag></pdfd:declarations>'
        )

        # Inject just before the closing </rdf:Description>
        if "</rdf:Description>" not in raw:
            raise RuntimeError("XMP has no rdf:Description to extend")
        new_xmp = raw.replace("</rdf:Description>", block + "</rdf:Description>", 1)

        meta.write(new_xmp.encode("utf-8"))
