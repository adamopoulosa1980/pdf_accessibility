"""
Structure tagging fixer — WCAG 1.3.1 Info and Relationships.

Adds a StructTreeRoot and tags content (headings, paragraphs, lists,
figures, tables) using one of these engines:

  - "adobe":          Adobe PDF Services API (best quality, paid)
  - "pdfix":          PDFix SDK (commercial)
  - "opendataloader": opendataloader-pdf (Apache 2.0, free, Java-backed)
  - "heuristic":      Built-in pymupdf-based tagger (free, basic)
  - "skip":           No-op (if PDF is already tagged)

Heuristic engine inspects font sizes and weights to infer headings vs.
body text, then writes a minimal but valid StructTreeRoot.

opendataloader-pdf wraps a JVM engine that performs layout analysis +
MCID-linked StructTreeRoot generation in one shot. Requires a Java 11+
runtime on PATH (https://adoptium.net). Slower than Adobe but free and
local.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, String

try:
    import fitz  # pymupdf
except ImportError:
    fitz = None

from ..config import Config
from ..report import RemediationReport


class StructureFixer:
    name = "structure"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.engine = cfg.tagging["engine"]

    def apply(self, pdf: pikepdf.Pdf, source_path: Path, report: RemediationReport) -> pikepdf.Pdf:
        """
        Returns a (possibly new) Pdf object. Some engines (Adobe) re-tag
        externally and return a different file.
        """
        if self.engine == "skip":
            report.add("1.3.1", "warning", "Tagging skipped per config (engine=skip)")
            return pdf

        if self.engine == "adobe":
            return self._tag_with_adobe(pdf, source_path, report)

        if self.engine == "pdfix":
            return self._tag_with_pdfix(pdf, source_path, report)

        if self.engine == "opendataloader":
            return self._tag_with_opendataloader(pdf, source_path, report)

        # Default: heuristic
        return self._tag_heuristic(pdf, source_path, report)

    # --------------------------------------------------------------
    # Adobe PDF Services
    # --------------------------------------------------------------
    @staticmethod
    def _load_adobe_credentials(adobe_cfg):
        """
        Resolve Adobe client_id / client_secret. Priority:
          1. tagging.adobe.credentials_file — a text file with lines like
             'client ID : ...' / 'client secret : ...' (also tolerates an
             optional 'access token : ...' line, which is ignored — the SDK
             mints its own token from id+secret).
          2. environment variables named by client_id_env / client_secret_env.
        Returns (client_id, client_secret) or (None, None).
        """
        cred_file = adobe_cfg.get("credentials_file")
        if cred_file:
            from pathlib import Path
            p = Path(cred_file)
            if not p.is_absolute():
                p = (Path.cwd() / p)
            if p.exists():
                cid = csecret = None
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    if ":" not in line:
                        continue
                    key, _, val = line.partition(":")
                    k = key.strip().lower().replace("_", " ")
                    v = val.strip()
                    if not v:
                        continue
                    if k in ("client id", "clientid"):
                        cid = v
                    elif k in ("client secret", "clientsecret"):
                        csecret = v
                if cid and csecret:
                    return cid, csecret
        cid = os.environ.get(adobe_cfg.get("client_id_env", "ADOBE_CLIENT_ID"))
        csecret = os.environ.get(adobe_cfg.get("client_secret_env", "ADOBE_CLIENT_SECRET"))
        return cid, csecret

    def _tag_with_adobe(self, pdf, source_path, report):
        client_id, client_secret = self._load_adobe_credentials(
            self.cfg.tagging.get("adobe", {})
        )
        if not (client_id and client_secret):
            report.add("1.3.1", "error",
                       "Adobe credentials missing; falling back to heuristic tagger")
            return self._tag_heuristic(pdf, source_path, report)

        try:
            # Adobe PDF Services SDK is heavy; import lazily.
            from adobe.pdfservices.operation.auth.service_principal_credentials import (
                ServicePrincipalCredentials,
            )
            from adobe.pdfservices.operation.pdf_services import PDFServices
            from adobe.pdfservices.operation.pdfjobs.jobs.autotag_pdf_job import AutotagPDFJob
            from adobe.pdfservices.operation.pdfjobs.params.autotag_pdf.autotag_pdf_params import (
                AutotagPDFParams,
            )
            from adobe.pdfservices.operation.pdfjobs.result.autotag_pdf_result import (
                AutotagPDFResult,
            )
            from adobe.pdfservices.operation.io.cloud_asset import CloudAsset
            from adobe.pdfservices.operation.io.stream_asset import StreamAsset
            from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
        except ImportError:
            report.add("1.3.1", "error",
                       "adobe-pdfservices-sdk not installed; falling back to heuristic")
            return self._tag_heuristic(pdf, source_path, report)

        try:
            creds = ServicePrincipalCredentials(client_id=client_id, client_secret=client_secret)
            services = PDFServices(credentials=creds)

            # Save current pdf to a temp buffer to send to Adobe
            tmp_in = source_path.parent / f".tmp_in_{source_path.name}"
            pdf.save(str(tmp_in))

            with open(tmp_in, "rb") as f:
                input_asset = services.upload(
                    input_stream=f.read(),
                    mime_type=PDFServicesMediaType.PDF,
                )

            params = AutotagPDFParams(
                generate_report=self.cfg.tagging["adobe"].get("generate_report", True),
                shift_headings=True,
            )
            job = AutotagPDFJob(input_asset=input_asset, autotag_pdf_params=params)
            location = services.submit(job)
            result = services.get_job_result(location, AutotagPDFResult)

            tagged_asset = result.get_result().get_tagged_pdf()
            tagged_bytes = services.get_content(tagged_asset).get_input_stream()

            tmp_out = source_path.parent / f".tmp_tagged_{source_path.name}"
            with open(tmp_out, "wb") as f:
                f.write(tagged_bytes)

            tmp_in.unlink(missing_ok=True)
            report.add("1.3.1", "fixed", "Document auto-tagged via Adobe PDF Services")
            return pikepdf.Pdf.open(tmp_out, allow_overwriting_input=True)
        except Exception as e:
            report.add("1.3.1", "error",
                       f"Adobe tagging failed ({e}); falling back to heuristic")
            return self._tag_heuristic(pdf, source_path, report)

    # --------------------------------------------------------------
    # PDFix SDK (commercial)
    # --------------------------------------------------------------
    def _tag_with_pdfix(self, pdf, source_path, report):
        try:
            from pdfixsdk import Pdfix, GetPdfix, kSaveFull
        except ImportError:
            report.add("1.3.1", "error",
                       "pdfixsdk not installed; falling back to heuristic")
            return self._tag_heuristic(pdf, source_path, report)

        try:
            pdfix = GetPdfix()
            tmp_in = source_path.parent / f".tmp_in_{source_path.name}"
            tmp_out = source_path.parent / f".tmp_tagged_{source_path.name}"
            pdf.save(str(tmp_in))

            doc = pdfix.OpenDoc(str(tmp_in), "")
            # AutoTag entire document
            doc.AutoTag(None, None)
            doc.Save(str(tmp_out), kSaveFull)
            doc.Close()
            tmp_in.unlink(missing_ok=True)
            report.add("1.3.1", "fixed", "Document auto-tagged via PDFix SDK")
            return pikepdf.Pdf.open(tmp_out, allow_overwriting_input=True)
        except Exception as e:
            report.add("1.3.1", "error",
                       f"PDFix tagging failed ({e}); falling back to heuristic")
            return self._tag_heuristic(pdf, source_path, report)

    # --------------------------------------------------------------
    # opendataloader-pdf (Apache 2.0, JVM-backed)
    # --------------------------------------------------------------
    def _tag_with_opendataloader(self, pdf, source_path, report):
        """
        Auto-tag via opendataloader-pdf. Wraps a JVM engine that performs
        layout analysis + MCID-linked StructTreeRoot generation locally
        with no external API calls. Requires Java 11+ on PATH.
        """
        try:
            import opendataloader_pdf
        except ImportError:
            report.add(
                "1.3.1", "error",
                "opendataloader-pdf not installed; "
                "run `pip install opendataloader-pdf` (needs Java 11+). "
                "Falling back to heuristic.",
            )
            return self._tag_heuristic(pdf, source_path, report)

        cfg = self.cfg.tagging.get("opendataloader") or {}
        tmp_dir = source_path.parent / f".tmp_odl_{source_path.stem}"
        tmp_in = source_path.parent / f".tmp_in_{source_path.name}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Snapshot the in-memory pdf so prior fixers' mutations (e.g.,
            # /Lang from MetadataFixer) are carried into the tagging step.
            pdf.save(str(tmp_in))

            convert_kwargs = {
                "input_path": [str(tmp_in)],
                "output_dir": str(tmp_dir),
                "format": "tagged-pdf",
            }
            # Pass-through optional config (e.g., language, hybrid mode)
            for k in ("language", "ocr", "use_hybrid", "hybrid_url"):
                if cfg.get(k) is not None:
                    convert_kwargs[k] = cfg[k]

            opendataloader_pdf.convert(**convert_kwargs)

            # opendataloader (v2.4.x) sometimes emits BMC/BDC inside a
            # path-construction scope (e.g. between `re` and the matching
            # path-painting op). PDF 2.0 ISO 32000-2 §8.5 forbids
            # marked-content operators in path-object state — PAC's
            # 4.1.1 Parsing check flags ~5,000 such occurrences per doc.
            # Move offending BMC/BDC to before the path starts.
            self._fix_invalid_marked_content_in_path_state(tmp_dir, tmp_in.stem)

            # opendataloader also emits 'BT ... BDC <tag> ... TJ ... EMC ... ET'
            # where the BDC opens after BT. This is legal per ISO 32000-2 but
            # PAC's 1.3.1 Tagged-content check flags every such text object as
            # "Text object not tagged" (~17,000 per doc). Move BDC to before
            # BT and EMC to after ET, splitting BT/ET if it contains multiple
            # BDC blocks.
            self._wrap_text_objects_with_marked_content(tmp_dir, tmp_in.stem)

            # opendataloader tags TOC dot-leaders ("....") as /P or /LBody
            # content. Matterhorn 01-001 (the rule behind PAC's "Text object
            # not tagged" warnings) wants such purely-decorative text marked
            # as /Artifact. Convert dot-leader-only marked-content blocks.
            self._demote_decorative_blocks_to_artifact(tmp_dir, tmp_in.stem)

            # opendataloader emits bare "/Artifact BMC" (no type). PDF/UA
            # best practice (ISO 14289-1, Matterhorn) is to classify each
            # artifact: running headers/footers as /Pagination, decorative
            # graphics/leaders as /Layout. Type every untyped artifact.
            self._classify_artifacts(tmp_dir, tmp_in.stem)

            # opendataloader writes the ParentTree /Nums array with keys
            # OUT OF ORDER (page StructParents 708.. before annotation
            # StructParents 0..). ISO 32000-1 §7.9.7 requires number-tree
            # keys in ascending order. veraPDF scans linearly so it never
            # notices, but PAC does a spec-compliant binary search — with
            # unsorted keys it fails to locate ~235 page entries, and then
            # cannot resolve any of those pages' MCID content, surfacing as
            # ~16,800 "Content not tagged" + ~235 "Structural parent tree"
            # errors. Sorting /Nums fixes both at once.
            self._sort_parent_tree_nums(tmp_dir, tmp_in.stem)

            # opendataloader writes "<stem>_tagged.pdf" into output_dir
            tagged_path = tmp_dir / f"{tmp_in.stem}_tagged.pdf"
            if not tagged_path.exists():
                # Belt-and-braces in case the suffix convention changes
                candidates = list(tmp_dir.glob("*.pdf"))
                if not candidates:
                    raise FileNotFoundError(
                        f"opendataloader produced no PDF in {tmp_dir}"
                    )
                tagged_path = candidates[0]

            tmp_in.unlink(missing_ok=True)
            report.add(
                "1.3.1", "fixed",
                "Document auto-tagged via opendataloader-pdf",
                details={"engine": "opendataloader", "output": str(tagged_path)},
            )
            return pikepdf.Pdf.open(str(tagged_path), allow_overwriting_input=True)
        except Exception as e:
            report.add(
                "1.3.1", "error",
                f"opendataloader tagging failed ({e}); falling back to heuristic",
            )
            tmp_in.unlink(missing_ok=True)
            return self._tag_heuristic(pdf, source_path, report)

    # --------------------------------------------------------------
    @staticmethod
    def _fix_invalid_marked_content_in_path_state(tmp_dir, stem):
        """
        Walk every page of `<stem>_tagged.pdf` in `tmp_dir`. For each
        BMC/BDC operator that appears while the parser is in
        path-construction state (i.e. between a path operator like `re`
        or `l` and the path-painting op like `f` or `S`), move it to the
        position right before the first path operator of that path.

        Leaves the matching EMC where it is, so the marked-content
        sequence now wraps the entire path (setup + painting) instead
        of being illegally embedded inside it.
        """
        from pathlib import Path
        tagged_path = Path(tmp_dir) / f"{stem}_tagged.pdf"
        if not tagged_path.exists():
            return

        PATH_START = {"m", "l", "c", "v", "y", "re", "h"}
        PATH_END = {"S", "s", "f", "F", "B", "b", "n", "f*", "B*", "b*",
                    "W", "W*"}

        pdf = pikepdf.Pdf.open(str(tagged_path), allow_overwriting_input=True)
        try:
            for page in pdf.pages:
                try:
                    instructions = list(pikepdf.parse_content_stream(page))
                except Exception:
                    continue

                # Decode operator names once
                ops_decoded = []
                for ops, op in instructions:
                    op_str = (bytes(op).decode("latin-1")
                              if hasattr(op, "__bytes__") else str(op))
                    ops_decoded.append(op_str)

                # First pass: identify (BMC/BDC index → new target index)
                state = "page"
                path_start_idx = None
                # text-object nesting (BMC inside BT/ET is allowed; we
                # only care about BMC inside path-object state).
                moves = []  # list of indices to move (in original order)
                move_set = set()
                for i, op_str in enumerate(ops_decoded):
                    if op_str == "BT":
                        if state == "page":
                            state = "text"
                    elif op_str == "ET":
                        if state == "text":
                            state = "page"
                    elif op_str in PATH_START:
                        if state == "page":
                            state = "path"
                            path_start_idx = i
                    elif op_str in PATH_END:
                        if state == "path":
                            state = "page"
                            path_start_idx = None
                    elif op_str in ("BMC", "BDC") and state == "path":
                        if path_start_idx is not None:
                            moves.append((i, path_start_idx))
                            move_set.add(i)

                if not moves:
                    continue

                # Second pass: rebuild instructions list with each
                # marked move applied. Preserves original ordering of
                # multiple BMC moving to the same target.
                pre_inserts: dict[int, list] = {}
                for old, new in moves:
                    pre_inserts.setdefault(new, []).append(instructions[old])
                # Each list is in original-order because we iterated forward.

                rebuilt = []
                for i, instr in enumerate(instructions):
                    if i in pre_inserts:
                        for ins in pre_inserts[i]:
                            rebuilt.append(ins)
                    if i in move_set:
                        continue
                    rebuilt.append(instr)

                page.Contents = pdf.make_stream(
                    pikepdf.unparse_content_stream(rebuilt)
                )
            pdf.save(str(tagged_path), linearize=False)
        finally:
            pdf.close()

    # --------------------------------------------------------------
    @staticmethod
    def _wrap_text_objects_with_marked_content(tmp_dir, stem):
        """
        Rewrite every BT...ET block so that any BMC/BDC...EMC sequence
        inside it is moved to outside, fully wrapping the text object.

        opendataloader emits:

            BT
              Td  Tf
              /Caption <</MCID 2>> BDC
                TJ
              EMC
            ET

        which is legal but PAC interprets as "Text object not tagged"
        (BT/ET starts outside the marked-content sequence). The fix
        produces:

            /Caption <</MCID 2>> BDC
              BT
                Td  Tf
                TJ
              ET
            EMC

        Three cases per BT/ET:
          (A) Exactly one BDC/EMC pair inside → move it outside.
          (B) Multiple BDC/EMC pairs inside   → split BT/ET into one
              text object per pair, wrap each with its own BDC/EMC.
          (C) No BDC inside (state-setup only or stray text) → wrap the
              whole BT/ET in `/Artifact BMC ... EMC` (decorative — won't
              show up in the struct tree, which is the right place for
              orphan text per PDF/UA).
        """
        from pathlib import Path
        from pikepdf import Operator, Name
        tagged_path = Path(tmp_dir) / f"{stem}_tagged.pdf"
        if not tagged_path.exists():
            return

        def decode(op):
            return (bytes(op).decode("latin-1")
                    if hasattr(op, "__bytes__") else str(op))

        pdf = pikepdf.Pdf.open(str(tagged_path), allow_overwriting_input=True)
        try:
            for page in pdf.pages:
                try:
                    instructions = list(pikepdf.parse_content_stream(page))
                except Exception:
                    continue

                # Decode operator names once
                ops_decoded = [decode(op) for _, op in instructions]

                rebuilt = []
                i = 0
                n = len(instructions)
                modified = False
                while i < n:
                    if ops_decoded[i] != "BT":
                        rebuilt.append(instructions[i])
                        i += 1
                        continue

                    # Find the matching ET (handle nested BT just in case)
                    depth = 1
                    et = None
                    for j in range(i + 1, n):
                        if ops_decoded[j] == "BT":
                            depth += 1
                        elif ops_decoded[j] == "ET":
                            depth -= 1
                            if depth == 0:
                                et = j
                                break
                    if et is None:
                        rebuilt.append(instructions[i])
                        i += 1
                        continue

                    # Identify BDC/BMC...EMC pairs inside this BT/ET
                    block = instructions[i:et + 1]
                    block_ops = ops_decoded[i:et + 1]
                    bdc_pairs = []  # list of (bdc_idx, emc_idx)
                    stack = []
                    for k in range(1, len(block) - 1):
                        s = block_ops[k]
                        if s in ("BMC", "BDC"):
                            stack.append(k)
                        elif s == "EMC":
                            if stack:
                                start = stack.pop()
                                if not stack:  # only outermost pairs
                                    bdc_pairs.append((start, k))

                    if len(bdc_pairs) == 0:
                        # Case (C): wrap whole BT/ET in /Artifact BMC
                        rebuilt.append(([Name("/Artifact")], Operator("BMC")))
                        rebuilt.extend(block)
                        rebuilt.append(([], Operator("EMC")))
                        modified = True
                        i = et + 1
                        continue

                    if len(bdc_pairs) == 1:
                        # Case (A): single BDC inside — move it outside
                        bdc_k, emc_k = bdc_pairs[0]
                        rebuilt.append(block[bdc_k])  # the BDC
                        for k in range(len(block)):
                            if k == bdc_k or k == emc_k:
                                continue
                            rebuilt.append(block[k])
                        rebuilt.append(([], Operator("EMC")))
                        modified = True
                        i = et + 1
                        continue

                    # Case (B): multiple BDC/EMC pairs — DO NOT SPLIT.
                    #
                    # Splitting BT/ET is unsafe: Td/TD are RELATIVE text
                    # positioning operators, and each new BT resets the
                    # text matrix to origin. Copying state-setup ops as a
                    # "prefix" to each new text object causes glyphs to
                    # render at wrong absolute coordinates — observed in
                    # the wild as overlapping/garbled text in TOC pages
                    # and body paragraphs. Visual integrity wins over PAC
                    # compliance for these blocks.
                    #
                    # Leave the BT/ET block unchanged. PAC will still flag
                    # these specific text objects, but the document renders
                    # correctly.
                    rebuilt.extend(block)
                    i = et + 1

                if modified:
                    page.Contents = pdf.make_stream(
                        pikepdf.unparse_content_stream(rebuilt)
                    )
            pdf.save(str(tagged_path), linearize=False)
        finally:
            pdf.close()

    # --------------------------------------------------------------
    @staticmethod
    def _demote_decorative_blocks_to_artifact(tmp_dir, stem):
        """
        Convert marked-content blocks containing only decorative text
        (dot-leaders, whitespace, separators) from a tagged role like
        `/P` or `/LBody` into `/Artifact`.

        Why: opendataloader leaves TOC dot-leaders ("...........") inside
        `/P` or `/LBody` BDC blocks with their own MCIDs. Matterhorn
        Protocol Checkpoint 01-001 — the rule that drives PAC's
        "Text object not tagged" findings — requires that decorative
        text be explicitly marked as `/Artifact` (since it carries no
        semantic content and should be skipped by screen readers).

        We also prune the corresponding struct elements from the
        StructTreeRoot, replacing the ParentTree.Nums slot with `null`
        so that no orphan MCID references remain.
        """
        from pathlib import Path
        import re
        from pikepdf import Operator, Name
        tagged_path = Path(tmp_dir) / f"{stem}_tagged.pdf"
        if not tagged_path.exists():
            return

        DECORATIVE_RE = re.compile(r"^[.\s\xa0–—]+$")

        def is_decorative(text: str) -> bool:
            return bool(text) and len(text.strip()) >= 3 and bool(DECORATIVE_RE.match(text))

        def decode(op):
            return (bytes(op).decode("latin-1")
                    if hasattr(op, "__bytes__") else str(op))

        pdf = pikepdf.Pdf.open(str(tagged_path), allow_overwriting_input=True)
        # Map (page_obj_id, mcid) -> True for blocks we artifact-ify, so we
        # can prune the struct tree afterwards.
        demoted: set[tuple[int, int]] = set()
        try:
            for page in pdf.pages:
                try:
                    instructions = list(pikepdf.parse_content_stream(page))
                except Exception:
                    continue
                decoded = [decode(op) for _, op in instructions]
                page_id = page.objgen[0]
                rebuilt = []
                i = 0
                n = len(instructions)
                modified = False
                while i < n:
                    ops, op = instructions[i]
                    op_str = decoded[i]
                    if op_str not in ("BMC", "BDC"):
                        rebuilt.append((ops, op))
                        i += 1
                        continue
                    # Find matching EMC at the same depth
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
                        rebuilt.append((ops, op))
                        i += 1
                        continue
                    # Collect text inside this BDC block
                    text = ""
                    for k in range(i + 1, emc):
                        kops, _ = instructions[k]
                        kstr = decoded[k]
                        if kstr in ("Tj", "TJ", "'", '"'):
                            try:
                                if kstr == "TJ":
                                    for item in kops[0]:
                                        if isinstance(item, pikepdf.String):
                                            text += str(item)
                                else:
                                    text += str(kops[0])
                            except Exception:
                                pass

                    # Don't demote /Artifact-already blocks
                    try:
                        current_tag = str(ops[0])
                    except Exception:
                        current_tag = ""
                    if current_tag == "/Artifact" or not is_decorative(text):
                        rebuilt.append((ops, op))
                        i += 1
                        continue

                    # Demote: emit /Artifact BMC, then the inner ops, then EMC
                    rebuilt.append(([Name("/Artifact")], Operator("BMC")))
                    for k in range(i + 1, emc):
                        rebuilt.append(instructions[k])
                    rebuilt.append(([], Operator("EMC")))

                    # Track the MCID we're killing so we can prune the
                    # struct elem afterwards.
                    if op_str == "BDC" and len(ops) >= 2 and \
                       isinstance(ops[1], pikepdf.Dictionary):
                        mc = ops[1].get("/MCID")
                        if mc is not None:
                            try:
                                demoted.add((page_id, int(mc)))
                            except Exception:
                                pass
                    modified = True
                    i = emc + 1

                if modified:
                    page.Contents = pdf.make_stream(
                        pikepdf.unparse_content_stream(rebuilt)
                    )

            # Phase 2: prune struct tree of the demoted (page, mcid) pairs.
            if demoted and "/StructTreeRoot" in pdf.Root:
                str_root = pdf.Root.StructTreeRoot
                ptree = str_root.get("/ParentTree")
                if ptree is not None:
                    nums = ptree.get("/Nums")
                    if nums is not None:
                        # Build page_id -> StructParents index map
                        page_sp = {}
                        for page in pdf.pages:
                            sp = page.get("/StructParents")
                            if sp is not None:
                                try:
                                    page_sp[page.objgen[0]] = int(sp)
                                except Exception:
                                    pass
                        # Build sp_index -> array reference
                        sp_to_arr = {}
                        for i in range(0, len(nums), 2):
                            try:
                                sp_to_arr[int(nums[i])] = nums[i + 1]
                            except Exception:
                                pass

                        # For each demoted (page, mcid), the owning struct
                        # element is ParentTree[sp][mcid]. That element's /K
                        # still lists the now-defunct marked-content id.
                        #
                        # CRITICAL: that owning element is frequently a
                        # SHARED container (e.g. an /LBody holding
                        # [text-mcid, dotleader-mcid, /Link]). Removing the
                        # whole container would orphan its real siblings.
                        # So we remove ONLY the integer `mcid` from the
                        # owning element's own /K — never the element. If
                        # that leaves the element's /K empty (it was a
                        # dedicated dot-leader wrapper) we then unhook the
                        # now-empty element from its parent, which is safe
                        # because an empty element has no children to orphan.
                        def remove_mcid_from_k(elem, mcid_val):
                            """Delete a plain-integer MCID (or its MCR dict)
                            from elem's /K, in place. Returns True if /K is
                            empty afterwards."""
                            k = elem.get("/K")
                            if k is None:
                                return True
                            if isinstance(k, pikepdf.Array):
                                for idx in range(len(k) - 1, -1, -1):
                                    item = k[idx]
                                    try:
                                        if isinstance(item, pikepdf.Dictionary):
                                            if str(item.get("/Type", "")) == "/MCR" \
                                               and item.get("/MCID") is not None \
                                               and int(item.get("/MCID")) == mcid_val:
                                                del k[idx]
                                        elif int(item) == mcid_val:
                                            del k[idx]
                                    except Exception:
                                        pass
                                return len(k) == 0
                            # /K is a single value
                            try:
                                if not isinstance(k, pikepdf.Dictionary) \
                                   and int(k) == mcid_val:
                                    del elem["/K"]
                                    return True
                            except Exception:
                                pass
                            return False

                        emptied = set()
                        for page_id, mcid in demoted:
                            sp = page_sp.get(page_id)
                            if sp is None:
                                continue
                            arr = sp_to_arr.get(sp)
                            if arr is None or not hasattr(arr, "__len__"):
                                continue
                            if not (0 <= mcid < len(arr)):
                                continue
                            elem = arr[mcid]
                            if not isinstance(elem, pikepdf.Dictionary):
                                continue
                            if remove_mcid_from_k(elem, mcid):
                                emptied.add(elem.objgen)

                        # Unhook genuinely-empty wrapper elements from their
                        # parent's /K (in place — preserves sibling refs).
                        for elem_objgen in emptied:
                            try:
                                elem = pdf.get_object(elem_objgen)
                            except Exception:
                                continue
                            # Skip if it gained content some other way
                            k = elem.get("/K")
                            if k is not None and (
                                (isinstance(k, pikepdf.Array) and len(k) > 0)
                                or not isinstance(k, (pikepdf.Array,))
                            ):
                                if isinstance(k, pikepdf.Array) and len(k) == 0:
                                    pass
                                else:
                                    continue
                            parent = elem.get("/P")
                            if parent is None or "/K" not in parent:
                                continue
                            pk = parent.K
                            if not isinstance(pk, pikepdf.Array):
                                continue
                            for idx in range(len(pk) - 1, -1, -1):
                                item = pk[idx]
                                try:
                                    if isinstance(item, pikepdf.Dictionary) \
                                       and item.objgen == elem_objgen:
                                        del pk[idx]
                                except Exception:
                                    pass

            pdf.save(str(tagged_path), linearize=False)
        finally:
            pdf.close()

    # --------------------------------------------------------------
    @staticmethod
    def _classify_artifacts(tmp_dir, stem):
        """
        Give every bare `/Artifact BMC` an explicit type, per PDF/UA best
        practice (ISO 14289-1 §7.1, Matterhorn checkpoint 01):

          /Artifact <</Type /Pagination /Subtype /Header>> BDC
          /Artifact <</Type /Pagination /Subtype /Footer>> BDC
          /Artifact <</Type /Layout>> BDC

        Classification heuristic per artifact block:
          - text in the top Y-band, or doc-title/release text   → Pagination/Header
          - text in the bottom Y-band, or copyright/page-number  → Pagination/Footer
          - everything else (dot-leaders, rules, icons, graphics)→ Layout

        Artifacts that already carry a /Type are left untouched.
        """
        from pathlib import Path
        import re
        from pikepdf import Operator, Name, Dictionary
        tagged_path = Path(tmp_dir) / f"{stem}_tagged.pdf"
        if not tagged_path.exists():
            return

        # Text that identifies a running header/footer in this document
        # class (doc title, release marker, classification, copyright,
        # "page N / M").
        PAGINATION_RE = re.compile(
            r"(©|\(c\)|copyright|\d+\s*/\s*\d+|in-confidence|confiden"
            r"|restricted|user guide|release|version)", re.IGNORECASE)

        def decode(op):
            return (bytes(op).decode("latin-1")
                    if hasattr(op, "__bytes__") else str(op))

        pdf = pikepdf.Pdf.open(str(tagged_path), allow_overwriting_input=True)
        try:
            for page in pdf.pages:
                try:
                    media = page.MediaBox
                    page_h = float(media[3]) - float(media[1])
                except Exception:
                    page_h = 842.0
                header_band = page_h - 75.0   # text Y above this → header
                footer_band = 75.0            # text Y below this → footer

                try:
                    instructions = list(pikepdf.parse_content_stream(page))
                except Exception:
                    continue
                decoded = [decode(op) for _, op in instructions]

                rebuilt = []
                i = 0
                n = len(instructions)
                modified = False
                while i < n:
                    ops, op = instructions[i]
                    op_str = decoded[i]
                    if op_str not in ("BMC", "BDC"):
                        rebuilt.append((ops, op))
                        i += 1
                        continue
                    try:
                        tag = str(ops[0])
                    except Exception:
                        tag = ""
                    if tag != "/Artifact":
                        rebuilt.append((ops, op))
                        i += 1
                        continue
                    # Find matching EMC
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
                        rebuilt.append((ops, op))
                        i += 1
                        continue

                    # Already typed? leave it.
                    if op_str == "BDC" and len(ops) >= 2 and \
                       isinstance(ops[1], pikepdf.Dictionary) and \
                       "/Type" in ops[1]:
                        rebuilt.append((ops, op))
                        i += 1
                        continue

                    # Scan block for text and first text Y-position
                    text = ""
                    block_y = None
                    for k in range(i + 1, emc):
                        kops, _ = instructions[k]
                        kstr = decoded[k]
                        if kstr in ("Td", "TD") and block_y is None \
                           and len(kops) >= 2:
                            try:
                                block_y = float(kops[1])
                            except Exception:
                                pass
                        elif kstr == "Tm" and block_y is None \
                                and len(kops) >= 6:
                            try:
                                block_y = float(kops[5])
                            except Exception:
                                pass
                        elif kstr in ("Tj", "TJ", "'", '"'):
                            try:
                                if kstr == "TJ":
                                    for it in kops[0]:
                                        if isinstance(it, pikepdf.String):
                                            text += str(it)
                                else:
                                    text += str(kops[0])
                            except Exception:
                                pass

                    t = text.strip()
                    if t:
                        is_pag = bool(PAGINATION_RE.search(t))
                        if (block_y is not None and block_y >= header_band):
                            props = Dictionary(Type=Name.Pagination,
                                               Subtype=Name.Header)
                        elif (block_y is not None and block_y <= footer_band):
                            props = Dictionary(Type=Name.Pagination,
                                               Subtype=Name.Footer)
                        elif is_pag:
                            # Pagination-looking text in the mid-band:
                            # classify by which half of the page it sits in.
                            if block_y is not None and block_y > page_h / 2:
                                props = Dictionary(Type=Name.Pagination,
                                                   Subtype=Name.Header)
                            else:
                                props = Dictionary(Type=Name.Pagination,
                                                   Subtype=Name.Footer)
                        else:
                            props = Dictionary(Type=Name.Layout)
                    else:
                        # No text → graphic/rule/icon decoration
                        props = Dictionary(Type=Name.Layout)

                    rebuilt.append(([Name("/Artifact"), props],
                                    Operator("BDC")))
                    for k in range(i + 1, emc):
                        rebuilt.append(instructions[k])
                    rebuilt.append(([], Operator("EMC")))
                    modified = True
                    i = emc + 1

                if modified:
                    page.Contents = pdf.make_stream(
                        pikepdf.unparse_content_stream(rebuilt)
                    )
            pdf.save(str(tagged_path), linearize=False)
        finally:
            pdf.close()

    # --------------------------------------------------------------
    @staticmethod
    def _sort_parent_tree_nums(tmp_dir, stem):
        """
        Sort the StructTreeRoot ParentTree `/Nums` array by key, ascending.

        A number tree's `/Nums` is a flat array [k0 v0 k1 v1 ...] and
        ISO 32000-1 §7.9.7 requires the keys to be sorted in ascending
        order. opendataloader emits them out of order (page-level
        StructParents keys before annotation-level keys). veraPDF scans
        the array linearly and is unaffected, but PAC performs a
        spec-compliant binary search and cannot find the misordered
        entries — which is the root cause of the bulk of its
        "Content not tagged" and "Structural parent tree" findings.

        The reorder is done in place (slot-by-slot reassignment of the
        existing array) so every value stays the same indirect object —
        no struct elements get inlined or orphaned.
        """
        from pathlib import Path
        tagged_path = Path(tmp_dir) / f"{stem}_tagged.pdf"
        if not tagged_path.exists():
            return

        pdf = pikepdf.Pdf.open(str(tagged_path), allow_overwriting_input=True)
        try:
            str_root = pdf.Root.get("/StructTreeRoot")
            if str_root is None:
                return
            ptree = str_root.get("/ParentTree")
            if ptree is None:
                return
            nums = ptree.get("/Nums")
            if nums is None or len(nums) < 4:
                return

            pair_count = len(nums) // 2
            # Snapshot current (key, value) pairs. Values are indirect
            # references (arrays of struct elems, or single struct elems);
            # reading them yields handles that retain their objgen.
            keys = []
            vals = []
            for i in range(pair_count):
                try:
                    keys.append(int(nums[2 * i]))
                except Exception:
                    keys.append(0)
                vals.append(nums[2 * i + 1])

            order = sorted(range(pair_count), key=lambda x: keys[x])
            if order == list(range(pair_count)):
                return  # already sorted, nothing to do

            # Reassign each slot in place. Writing back an indirect-object
            # handle keeps it as a reference (no inlining).
            for new_pos, old_pos in enumerate(order):
                nums[2 * new_pos] = keys[old_pos]
                nums[2 * new_pos + 1] = vals[old_pos]

            pdf.save(str(tagged_path), linearize=False)
        finally:
            pdf.close()

    # --------------------------------------------------------------
    # Heuristic tagger (built-in, free)
    # --------------------------------------------------------------
    def _tag_heuristic(self, pdf, source_path, report):
        """
        Build a minimal StructTreeRoot by walking the page text with pymupdf
        and classifying spans by font size into headings vs paragraphs.

        This is intentionally conservative: it produces a valid but flat
        structure. It WILL NOT match the quality of Adobe Autotag for
        complex layouts. Use it as a baseline.
        """
        if fitz is None:
            report.add("1.3.1", "error",
                       "pymupdf not installed; cannot run heuristic tagger")
            return pdf

        thresholds = self.cfg.tagging["heuristic"]["heading_thresholds"]
        body_min = self.cfg.tagging["heuristic"]["body_min_size"]

        # Open the same file with pymupdf for layout analysis
        tmp_path = source_path.parent / f".tmp_struct_{source_path.name}"
        pdf.save(str(tmp_path))
        fz_doc = fitz.open(str(tmp_path))

        # Build the StructTreeRoot
        struct_root = Dictionary(
            Type=Name.StructTreeRoot,
            K=Array(),
            ParentTree=Dictionary(Nums=Array()),
            RoleMap=Dictionary(),
        )

        document_elem = Dictionary(
            Type=Name.StructElem,
            S=Name.Document,
            K=Array(),
        )
        # Will be linked after we add to pdf

        tagged_blocks = 0
        for page_num, page in enumerate(fz_doc):
            blocks = page.get_text("dict")["blocks"]
            for block in blocks:
                if block.get("type") != 0:  # skip non-text
                    continue
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    if not spans:
                        continue
                    text = "".join(s["text"] for s in spans).strip()
                    if not text:
                        continue
                    max_size = max(s["size"] for s in spans)
                    is_bold = any("Bold" in s["font"] or "bold" in s["font"].lower()
                                  for s in spans)

                    role = self._classify(max_size, is_bold, thresholds, body_min)
                    elem = Dictionary(
                        Type=Name.StructElem,
                        S=Name("/" + role),
                        Pg=pdf.pages[page_num].obj,
                        K=Array(),
                    )
                    # Use ActualText for the span content (a simplification —
                    # real tagging would use MCIDs tied to content stream marks)
                    elem.ActualText = String(text)
                    document_elem.K.append(elem)
                    tagged_blocks += 1

        fz_doc.close()
        tmp_path.unlink(missing_ok=True)

        # Attach
        struct_root.K.append(document_elem)
        pdf.Root.StructTreeRoot = struct_root

        report.add(
            "1.3.1", "fixed",
            f"Heuristic tagger added StructTreeRoot with {tagged_blocks} elements",
            details={
                "engine": "heuristic",
                "note": "Heuristic tagging is approximate. For production "
                        "documents, use engine=adobe or engine=pdfix.",
            },
        )
        return pdf

    @staticmethod
    def _classify(size: float, bold: bool, thresholds: dict, body_min: float) -> str:
        """Map font size + weight to a PDF structure role."""
        if size >= thresholds["h1"]:
            return "H1"
        if size >= thresholds["h2"]:
            return "H2"
        if size >= thresholds["h3"]:
            return "H3"
        if size >= thresholds["h4"] and bold:
            return "H4"
        if size < body_min:
            return "Caption"
        return "P"
