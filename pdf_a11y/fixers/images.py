"""
Image / figure alt-text fixer — WCAG 1.1.1 Non-text Content (Level A).

Strategy options (config: images.strategy):
  - "vlm":        Generate alt text via Vision Language Model
  - "prompt":     Skip generation, emit a CSV of images needing review
  - "decorative": Mark all untagged images as artifacts (decorative)

Decorative-detection heuristics catch obvious icons/dividers without
hitting the VLM.

Per-image overrides keyed by SHA-256 hash prefix let you lock in
human-reviewed alt text after the first run.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, String

try:
    import fitz  # pymupdf
    from PIL import Image
except ImportError:
    fitz = None
    Image = None

from ..config import Config
from ..report import RemediationReport


class ImageAltTextFixer:
    name = "images"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.strategy = cfg.images["strategy"]
        self.overrides = cfg.images.get("alt_overrides") or {}
        # Per-image audit trail emitted as <stem>_images_manifest.json so the
        # web app / CI clients can power an alt-text review screen.
        self._manifest: dict[str, dict] = {}    # hash -> entry
        self._thumbs_dir: Path | None = None
        self._stem: str = ""

    def apply(self, pdf: pikepdf.Pdf, source_path: Path, report: RemediationReport) -> pikepdf.Pdf:
        """
        Returns the (possibly replaced) pdf so callers can chain. We replace
        the in-memory Pdf with a fresh one opened from the temp snapshot
        because pikepdf renumbers objects on save: without the reopen,
        pymupdf reports the *post-save* xref while the in-memory pikepdf
        still has the *pre-save* object numbers, so xref-to-objgen matching
        silently fails and /Alt writes are never persisted.
        """
        if fitz is None or Image is None:
            report.add("1.1.1", "error",
                       "pymupdf and Pillow are required for image processing")
            return pdf

        # Prep manifest + thumbnail output dir (used by both this fixer and
        # the web app's review screen).
        out_dir = Path(self.cfg.output["directory"])
        out_dir.mkdir(parents=True, exist_ok=True)
        self._stem = source_path.stem
        self._thumbs_dir = out_dir / f"{self._stem}_thumbs"
        self._thumbs_dir.mkdir(parents=True, exist_ok=True)
        self._manifest = {}

        # Snapshot current state to a temp file, then reopen BOTH pikepdf
        # and pymupdf from the same file. After the save, pikepdf object
        # numbers in the file match what pymupdf will read as xrefs.
        tmp = source_path.parent / f".tmp_imgs_{source_path.name}"
        pdf.save(str(tmp))
        pdf.close()
        pdf = pikepdf.Pdf.open(str(tmp), allow_overwriting_input=True)
        self._tmp_path = tmp  # pipeline cleans this up in its finally block
        fz_doc = fitz.open(str(tmp))

        # If a StructTreeRoot exists (e.g., produced by opendataloader/adobe/
        # pdfix/heuristic upstream), build a map from page index -> list of
        # /Figure StructElem refs in document order. We update those Figure
        # /Alt entries alongside the XObject /Alt writes — PAC reads from the
        # struct tree, not the XObject, so this is what actually moves the
        # 1.1 Text Alternatives number.
        figures_by_page = self._build_figure_index(pdf)
        if figures_by_page:
            total_figs = sum(len(v) for v in figures_by_page.values())
            report.add(
                "1.1.1", "fixed",
                f"Found StructTreeRoot with {total_figs} /Figure elements "
                f"across {len(figures_by_page)} pages; will update their /Alt",
            )

        # Pass 1: classify every image, handle overrides + decoratives synchronously,
        # collect VLM-bound work for parallel dispatch.
        review_rows: list[dict] = []
        vlm_tasks: list[dict] = []  # {hash, page_num, xref, img_bytes, figure}

        for page_num in range(len(fz_doc)):
            page = fz_doc[page_num]
            images = page.get_images(full=True)
            for img_index, img_info in enumerate(images):
                xref = img_info[0]
                try:
                    pix = fitz.Pixmap(fz_doc, xref)
                    if pix.n - pix.alpha >= 4:  # CMYK -> RGB
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    img_bytes = pix.tobytes("png")
                    pil_img = Image.open(io.BytesIO(img_bytes))
                except Exception as e:
                    report.add("1.1.1", "warning",
                               f"Could not extract image (page {page_num+1}, xref {xref}): {e}")
                    continue

                width_pt, height_pt = pil_img.width, pil_img.height
                img_hash = hashlib.sha256(img_bytes).hexdigest()[:8]

                # Consume the next /Figure StructElem on this page (if any).
                # Visual order from pymupdf is expected to match opendataloader's
                # Figure ordering on the same page. May be None if no struct tree.
                figure = self._consume_figure_for_page(figures_by_page, page_num)

                # 1) Overrides
                if img_hash in self.overrides:
                    override = self.overrides[img_hash]
                    if override == "DECORATIVE":
                        self._mark_decorative(pdf, page_num, xref, report, img_hash, figure)
                        self._record_image(img_hash, page_num, xref, width_pt,
                                           height_pt, None, "override_decorative", img_bytes)
                    else:
                        self._set_alt(pdf, page_num, xref, override, report, img_hash, "override", figure)
                        self._record_image(img_hash, page_num, xref, width_pt,
                                           height_pt, override, "override", img_bytes)
                    continue

                # 2) Decorative heuristic
                if self._is_decorative(pil_img, width_pt, height_pt):
                    self._mark_decorative(pdf, page_num, xref, report, img_hash, figure)
                    self._record_image(img_hash, page_num, xref, width_pt,
                                       height_pt, None, "decorative_auto", img_bytes)
                    continue

                # 3) Strategy
                if self.strategy == "decorative":
                    self._mark_decorative(pdf, page_num, xref, report, img_hash, figure)
                    self._record_image(img_hash, page_num, xref, width_pt,
                                       height_pt, None, "decorative_strategy", img_bytes)
                elif self.strategy == "prompt":
                    review_rows.append({
                        "hash": img_hash, "page": page_num + 1, "xref": xref,
                        "width": width_pt, "height": height_pt, "suggested_alt": "",
                    })
                    self._record_image(img_hash, page_num, xref, width_pt,
                                       height_pt, None, "manual_required", img_bytes)
                    report.add("1.1.1", "manual_required",
                               f"Image on page {page_num+1} needs alt text (hash={img_hash})",
                               location={"page": page_num + 1, "image_hash": img_hash})
                elif self.strategy == "vlm":
                    self._record_image(img_hash, page_num, xref, width_pt,
                                       height_pt, None, "pending_vlm", img_bytes)
                    vlm_tasks.append({
                        "hash": img_hash, "page_num": page_num, "xref": xref,
                        "img_bytes": img_bytes, "width": width_pt, "height": height_pt,
                        "figure": figure,
                    })

        fz_doc.close()
        # NB: don't unlink tmp — pikepdf has it mmap'd. Pipeline cleans up.

        # Pass 2: parallel VLM dispatch
        if vlm_tasks:
            self._dispatch_vlm(vlm_tasks, pdf, report, review_rows)

        # Write review CSV (kept for CLI users; the webapp consumes the
        # richer images_manifest.json below).
        if review_rows:
            csv_path = out_dir / f"{source_path.stem}_images_review.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(review_rows[0].keys()))
                writer.writeheader()
                writer.writerows(review_rows)
            report.add("1.1.1", "manual_required",
                       f"{len(review_rows)} images need manual review — see {csv_path.name}",
                       review_csv=str(csv_path))

        # Write the per-image manifest (one entry per unique image hash, with
        # the page+xref occurrences listed). Powers the alt-text review screen.
        self._write_manifest(out_dir)

        return pdf

    # -- Manifest helpers ------------------------------------------------
    def _record_image(self, img_hash, page_num, xref, width, height,
                      alt, source, img_bytes=None):
        """Append an occurrence to the manifest; create the entry + thumb
        the first time we see a given image hash."""
        entry = self._manifest.get(img_hash)
        if entry is None:
            entry = {
                "hash": img_hash,
                "alt": alt,
                "source": source,
                "width": width,
                "height": height,
                "thumb": f"{self._stem}_thumbs/{img_hash}.png",
                "occurrences": [],
            }
            self._manifest[img_hash] = entry
            if img_bytes and self._thumbs_dir is not None:
                self._save_thumb(img_hash, img_bytes)
        else:
            # Later passes can refine the initial pending_vlm entry.
            if alt is not None and entry.get("alt") is None:
                entry["alt"] = alt
            if source and entry.get("source") == "pending_vlm":
                entry["source"] = source
        entry["occurrences"].append({"page": page_num + 1, "xref": xref})

    def _save_thumb(self, img_hash, img_bytes):
        """Resize image bytes to max 300px on the longest side, write as PNG."""
        if Image is None or self._thumbs_dir is None:
            return
        try:
            img = Image.open(io.BytesIO(img_bytes))
            img.thumbnail((300, 300))
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            img.save(self._thumbs_dir / f"{img_hash}.png",
                     format="PNG", optimize=True)
        except Exception:
            # Thumbnails are best-effort — never break a remediation run.
            pass

    def _update_manifest_for_vlm(self, img_hash, alt, source):
        """Called from _dispatch_vlm once the VLM call resolves."""
        entry = self._manifest.get(img_hash)
        if entry is None:
            return
        entry["alt"] = alt
        entry["source"] = source

    def _write_manifest(self, out_dir):
        if not self._manifest:
            return
        entries = list(self._manifest.values())
        entries.sort(key=lambda e: (
            e["occurrences"][0]["page"] if e["occurrences"] else 0, e["hash"]))
        path = out_dir / f"{self._stem}_images_manifest.json"
        path.write_text(
            json.dumps({"version": 1, "images": entries},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # --------------------------------------------------------------
    def _dispatch_vlm(self, tasks, pdf, report, review_rows):
        """
        Run VLM alt-text generation across `tasks` with bounded concurrency.
        Cloud providers (anthropic, openai) default to 8 workers; local
        providers (openai_compatible, ollama) default to 2 to avoid OOMing
        the local model. Config `images.vlm.concurrency` overrides both.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        provider = self.cfg.images["vlm"].get("provider", "anthropic")
        default_concurrency = 2 if provider in ("openai_compatible", "ollama") else 8
        max_workers = self.cfg.images["vlm"].get("concurrency", default_concurrency)
        max_workers = max(1, min(max_workers, len(tasks)))

        verbose = self.cfg.images["vlm"].get("verbose_progress", False)
        total = len(tasks)
        completed = 0

        def _do_one(task):
            return task, self._generate_alt_vlm(task["img_bytes"], report)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_do_one, t) for t in tasks]
            for fut in as_completed(futures):
                try:
                    task, alt = fut.result()
                except Exception as e:
                    report.add("1.1.1", "warning", f"VLM task crashed: {e}")
                    continue

                completed += 1
                if verbose and completed % 10 == 0:
                    print(f"  [VLM] {completed}/{total} images processed")

                page_num = task["page_num"]
                xref = task["xref"]
                img_hash = task["hash"]
                figure = task.get("figure")

                if alt:
                    self._set_alt(pdf, page_num, xref, alt, report, img_hash, "vlm", figure)
                    self._update_manifest_for_vlm(img_hash, alt, "vlm")
                elif alt is False:
                    # Sentinel meaning "VLM said exactly DECORATIVE"
                    self._mark_decorative(pdf, page_num, xref, report, img_hash, figure)
                    self._update_manifest_for_vlm(img_hash, None, "decorative_vlm")
                else:
                    # alt is None or "" -> VLM failed or returned no content
                    # (e.g. text-only model fed an image). Don't silently mark
                    # decorative — that hides real content from screen readers.
                    # Fall back to the manual-review CSV.
                    review_rows.append({
                        "hash": img_hash, "page": page_num + 1, "xref": xref,
                        "width": task["width"], "height": task["height"],
                        "suggested_alt": "",
                    })
                    self._update_manifest_for_vlm(img_hash, None, "manual_required")
                    report.add(
                        "1.1.1", "manual_required",
                        f"VLM returned no usable alt text for image on page "
                        f"{page_num+1} (hash={img_hash}); needs manual review",
                        location={"page": page_num + 1, "image_hash": img_hash},
                    )

    # --------------------------------------------------------------
    def _is_decorative(self, pil_img, w: int, h: int) -> bool:
        h_cfg = self.cfg.images["decorative_heuristics"]
        if w <= h_cfg["max_decorative_width"] and h <= h_cfg["max_decorative_height"]:
            return True
        # Entropy check (very flat = likely decoration/background)
        try:
            entropy = pil_img.convert("L").entropy()
            if entropy < h_cfg["entropy_threshold"]:
                return True
        except Exception:
            pass
        return False

    def _mark_decorative(self, pdf, page_num, xref, report, img_hash, figure=None):
        """
        Mark an image as an artifact (decorative) on the XObject side.

        We deliberately DO NOT clear the matching /Figure /Alt: PDF/UA-1 rule
        7.3-1 requires `Alt != null && Alt != ''`. Writing an empty string
        fails veraPDF. Leaving the tagger-supplied placeholder (e.g. "image N")
        is non-empty and satisfies the rule. For strict PDF/UA artifact marking
        the image's Do operator would need to be wrapped in /Artifact BMC...EMC
        marks in the content stream — invasive, out of scope here.
        """
        try:
            page = pdf.pages[page_num]
            resources = page.get("/Resources", {})
            xobjects = resources.get("/XObject", {})
            for name, ref in xobjects.items():
                if int(ref.objgen[0]) == xref:
                    ref.Alt = String("")
                    ref["/A11Y_Artifact"] = True
            # Note: we intentionally consume the next /Figure for this page
            # (so subsequent VLM-described images on the same page align with
            # later Figures), but we don't overwrite its /Alt. The placeholder
            # text from the structure tagger stands.
            report.add("1.1.1", "fixed",
                       f"Marked image as decorative (page {page_num+1}, hash={img_hash})",
                       location={"page": page_num + 1, "image_hash": img_hash})
        except Exception as e:
            report.add("1.1.1", "warning",
                       f"Could not mark image decorative: {e}")

    def _set_alt(self, pdf, page_num, xref, alt_text, report, img_hash, source, figure=None):
        try:
            page = pdf.pages[page_num]
            resources = page.get("/Resources", {})
            xobjects = resources.get("/XObject", {})
            for name, ref in xobjects.items():
                if int(ref.objgen[0]) == xref:
                    ref.Alt = String(alt_text)
            wrote_to_figure = False
            if figure is not None:
                try:
                    figure.Alt = String(alt_text)
                    wrote_to_figure = True
                except Exception as e:
                    report.add("1.1.1", "warning",
                               f"Could not set /Figure /Alt (page {page_num+1}): {e}")
            preview = alt_text[:60] + ("…" if len(alt_text) > 60 else "")
            tag = "vlm+fig" if (wrote_to_figure and source == "vlm") else (
                  f"{source}+fig" if wrote_to_figure else source)
            report.add("1.1.1", "fixed",
                       f"Set alt text on image (page {page_num+1}, hash={img_hash}, src={tag}): '{preview}'",
                       location={"page": page_num + 1, "image_hash": img_hash},
                       details={"alt_text": alt_text, "source": tag})
        except Exception as e:
            report.add("1.1.1", "warning",
                       f"Could not set alt text: {e}")

    # --------------------------------------------------------------
    def _build_figure_index(self, pdf):
        """
        Walk the StructTreeRoot and return dict[page_index, list[/Figure ref]]
        in document order, so callers can pop the next Figure as they iterate
        the page's images in visual order.
        """
        if "/StructTreeRoot" not in pdf.Root:
            return {}
        # page object id -> page index (for matching /Figure /Pg references)
        page_by_obj = {p.objgen[0]: i for i, p in enumerate(pdf.pages)}
        figures_by_page: dict[int, list] = {}

        def walk(elem, depth=0):
            if depth > 60:
                return
            try:
                s = elem.get("/S")
            except Exception:
                return
            if s is not None and str(s) == "/Figure":
                pg = elem.get("/Pg")
                if pg is not None:
                    idx = page_by_obj.get(pg.objgen[0])
                    if idx is not None:
                        figures_by_page.setdefault(idx, []).append(elem)
                return  # don't recurse into Figure children
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
        return figures_by_page

    @staticmethod
    def _consume_figure_for_page(figures_by_page, page_num):
        lst = figures_by_page.get(page_num)
        if not lst:
            return None
        return lst.pop(0)

    # --------------------------------------------------------------
    def _generate_alt_vlm(self, img_bytes: bytes, report: RemediationReport) -> str | None:
        cfg = self.cfg.images["vlm"]
        provider = cfg.get("provider", "anthropic")
        max_len = cfg.get("max_alt_length", 125)
        lang = cfg.get("output_language", "en")

        prompt = (
            f"Provide concise alt text in {lang} for this image, suitable for a screen reader. "
            f"Maximum {max_len} characters. Describe content and function, not visual style. "
            f"If the image is purely decorative (icon, divider, ornament), respond with exactly: DECORATIVE. "
            f"Do not include phrases like 'image of' or 'picture showing'."
        )

        try:
            if provider == "anthropic":
                return self._vlm_anthropic(img_bytes, prompt, cfg, max_len)
            if provider == "openai":
                return self._vlm_openai(img_bytes, prompt, cfg, max_len)
            if provider == "openai_compatible":
                return self._vlm_openai_compatible(img_bytes, prompt, cfg, max_len)
            if provider == "ollama":
                return self._vlm_ollama(img_bytes, prompt, cfg, max_len)
            report.add("1.1.1", "error", f"Unknown VLM provider: {provider}")
            return None
        except Exception as e:
            report.add("1.1.1", "warning", f"VLM call failed: {e}")
            return None

    @staticmethod
    def _resolve_api_key(cfg, default=None):
        """
        API-key precedence: an explicit `api_key` value in the config
        wins, then the env var named by `api_key_env`, then `default`.
        The explicit value lets a runtime caller (e.g. the web app) pass
        a key without setting a process environment variable.
        """
        explicit = cfg.get("api_key")
        if explicit:
            return str(explicit)
        env_name = cfg.get("api_key_env")
        if env_name:
            val = os.environ.get(env_name)
            if val:
                return val
        return default

    def _vlm_anthropic(self, img_bytes, prompt, cfg, max_len):
        import anthropic
        client = anthropic.Anthropic(api_key=self._resolve_api_key(cfg))
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        msg = client.messages.create(
            model=cfg.get("model", "claude-sonnet-4-5"),
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text = msg.content[0].text.strip()
        if text == "DECORATIVE":
            return False  # explicit decorative classification
        return text[:max_len] if text else None

    def _vlm_openai(self, img_bytes, prompt, cfg, max_len):
        from openai import OpenAI
        client = OpenAI(api_key=self._resolve_api_key(cfg))
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        rsp = client.chat.completions.create(
            model=cfg.get("model", "gpt-4o"),
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{b64}"}},
                ],
            }],
        )
        text = rsp.choices[0].message.content.strip()
        if text == "DECORATIVE":
            return False
        return text[:max_len] if text else None

    def _vlm_openai_compatible(self, img_bytes, prompt, cfg, max_len):
        """
        Talk to any OpenAI-compatible chat completions endpoint — LM Studio,
        vLLM, llama.cpp's HTTP server, Ollama in OpenAI-compat mode, text-
        generation-webui, LiteLLM, etc. The defaults assume an LM Studio
        server at http://localhost:1234/v1 but the `base_url` config field
        retargets it freely.

        Notes for hosting a vision-language model (Qwen 2.5/3.x VL, etc.):
        - Many local servers (e.g. LM Studio with default settings) do not
          require an API key. Set `api_key` or `api_key_env` only if your
          server enforces auth.
        - `model` MUST match the model identifier the server advertises at
          /v1/models (e.g. "qwen/qwen3-vl-30b").
        - Local inference is slower per request but doesn't rate-limit;
          the pipeline batches via `images.vlm.concurrency`.
        - We run a one-time health check on the first call to fail fast if
          the server is unreachable or the requested model isn't loaded.
        """
        from openai import OpenAI
        base_url = cfg.get("base_url", "http://localhost:1234/v1")
        # Local servers usually ignore the key; an explicit api_key (or
        # api_key_env) is used when the server has key auth enabled.
        api_key = self._resolve_api_key(cfg, default="not-needed")

        client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=cfg.get("timeout_seconds", 120),
        )

        # One-time health check (cached on the instance)
        if not getattr(self, "_oai_compat_checked", False):
            self._health_check_openai_compatible(client, cfg)
            self._oai_compat_checked = True

        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        rsp = client.chat.completions.create(
            model=cfg.get("model", "qwen2.5-vl-7b-instruct"),
            max_tokens=cfg.get("max_response_tokens", 300),
            temperature=cfg.get("temperature", 0.2),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{b64}"}},
                ],
            }],
        )
        text = (rsp.choices[0].message.content or "").strip()
        if text == "DECORATIVE":
            return False
        # Qwen sometimes wraps responses in markdown or prefixes; clean up
        text = text.lstrip("*-• ").rstrip("*. ")
        for prefix in ("Alt text: ", "Alt-text: ", "Description: ", "The image shows "):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):]
        # Empty response (e.g. text-only model fed an image) -> let caller
        # send this to manual review rather than mis-marking it decorative.
        return text[:max_len] if text else None

    @staticmethod
    def _health_check_openai_compatible(client, cfg) -> None:
        """
        Verify (a) the OpenAI-compatible server is reachable and (b) the
        requested model is loaded. Raises RuntimeError with a useful message
        so the user knows what to fix rather than getting N bad alt texts.
        """
        try:
            models = client.models.list()
        except Exception as e:
            raise RuntimeError(
                f"Cannot reach the OpenAI-compatible server at "
                f"{client.base_url}. Is it running? Original error: {e}"
            )
        loaded = [m.id for m in models.data] if models.data else []
        wanted = cfg.get("model", "qwen2.5-vl-7b-instruct")
        # Match exact or as substring (some servers prepend repo paths)
        if not any(wanted in m or m in wanted for m in loaded):
            raise RuntimeError(
                f"Model '{wanted}' not loaded on the server at {client.base_url}. "
                f"Loaded models: {loaded}. "
                f"Load the VL model in your inference server, or update "
                f"images.vlm.model in the config."
            )

    def _vlm_ollama(self, img_bytes, prompt, cfg, max_len):
        import requests
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        rsp = requests.post(
            cfg.get("ollama_url", "http://localhost:11434/api/generate"),
            json={
                "model": cfg.get("model", "llava"),
                "prompt": prompt,
                "images": [b64],
                "stream": False,
            },
            timeout=cfg.get("timeout_seconds", 120),
        )
        text = rsp.json().get("response", "").strip()
        if text == "DECORATIVE":
            return False
        return text[:max_len] if text else None
