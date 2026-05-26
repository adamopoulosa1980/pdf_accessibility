"""
Contrast fixer — WCAG 1.4.3 Contrast (Minimum) / 1.4.11 Non-text Contrast.

Two modes (config: contrast.apply_remapping):
  - false: REPORT-ONLY. Scans text spans, computes contrast against their
           background, and emits findings for spans below the threshold.
  - true:  Apply explicit color_mappings (hex -> hex) from config to text
           and vector content. Original colors are detected from the
           content stream; replacements use simple operator rewrites.

Why is remapping limited? Contrast is a visual-design decision. The pipeline
detects problems and offers a controlled mapping mechanism — it deliberately
won't guess replacement colors on your behalf.
"""
from __future__ import annotations

from pathlib import Path

import pikepdf

try:
    import fitz  # pymupdf
except ImportError:
    fitz = None

from ..config import Config
from ..report import RemediationReport


def luminance(rgb: tuple[float, float, float]) -> float:
    """WCAG relative luminance. rgb components in 0..1."""
    def chan(c):
        c = c / 1.0 if c > 1 else c
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(c1: tuple[float, float, float], c2: tuple[float, float, float]) -> float:
    l1, l2 = luminance(c1), luminance(c2)
    light, dark = max(l1, l2), min(l1, l2)
    return (light + 0.05) / (dark + 0.05)


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4))


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{int(round(c * 255)):02X}" for c in rgb)


class ContrastFixer:
    name = "contrast"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.apply_remap = cfg.contrast.get("apply_remapping", False)
        self.min_normal = cfg.contrast["min_ratio_normal_text"]
        self.min_large = cfg.contrast["min_ratio_large_text"]
        self.large_pt = cfg.contrast["large_text_threshold_pt"]
        self.large_bold_pt = cfg.contrast["large_text_bold_threshold_pt"]
        self.color_map = cfg.contrast.get("color_mappings") or {}
        # Normalize keys to uppercase hex
        self.color_map = {
            k.upper(): v.upper() for k, v in self.color_map.items()
        }

    def apply(self, pdf: pikepdf.Pdf, source_path: Path, report: RemediationReport) -> None:
        if fitz is None:
            report.add("1.4.3", "error", "pymupdf required for contrast analysis")
            return

        tmp = source_path.parent / f".tmp_contrast_{source_path.name}"
        pdf.save(str(tmp))
        fz_doc = fitz.open(str(tmp))

        violations = 0
        remapped = 0

        # Assume white background unless background detection finds otherwise.
        # Proper background detection would inspect overlapping fill rectangles;
        # for the common case (white pages), this is the safe default.
        bg_color = (1.0, 1.0, 1.0)

        for page_num, page in enumerate(fz_doc):
            blocks = page.get_text("dict")["blocks"]
            for block in blocks:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        color_int = span.get("color", 0)  # sRGB packed int
                        r = ((color_int >> 16) & 0xFF) / 255.0
                        g = ((color_int >> 8) & 0xFF) / 255.0
                        b = (color_int & 0xFF) / 255.0
                        fg = (r, g, b)
                        size = span.get("size", 12.0)
                        font = span.get("font", "")
                        is_bold = "Bold" in font or "bold" in font.lower()

                        threshold = self.min_normal
                        if size >= self.large_pt or (is_bold and size >= self.large_bold_pt):
                            threshold = self.min_large

                        ratio = contrast_ratio(fg, bg_color)
                        if ratio < threshold:
                            violations += 1
                            hex_fg = rgb_to_hex(fg)
                            report.add(
                                "1.4.3", "warning",
                                f"Low contrast text on page {page_num+1}: "
                                f"{hex_fg} on white = {ratio:.2f} (need {threshold})",
                                location={"page": page_num + 1, "text": span.get("text", "")[:60]},
                                details={"foreground": hex_fg, "ratio": round(ratio, 2),
                                         "required": threshold, "size_pt": size},
                            )

                            if self.apply_remap and hex_fg in self.color_map:
                                # Remapping in pymupdf requires content stream edits.
                                # We record the intent here; actual application is
                                # done in a separate pass via pikepdf below.
                                remapped += 1

        fz_doc.close()
        tmp.unlink(missing_ok=True)

        if self.apply_remap and self.color_map:
            actually_remapped = self._apply_color_map(pdf, report)
            report.add(
                "1.4.3", "fixed",
                f"Applied {len(self.color_map)} color mapping(s); "
                f"replaced {actually_remapped} color operators in content streams",
            )

        report.add(
            "1.4.3", "warning" if violations else "fixed",
            f"Contrast scan complete: {violations} potential violations found"
            + (f", {remapped} flagged for remapping" if self.apply_remap else ""),
            details={"violations": violations},
        )

    # --------------------------------------------------------------
    def _apply_color_map(self, pdf: pikepdf.Pdf, report: RemediationReport) -> int:
        """
        Walk every page's content stream and rewrite color-setting operators
        whose RGB matches a key in self.color_map.

        Covered operators (each with 3 numeric operands in 0..1 → RGB):
          - "r g b RG" / "r g b rg"        — DeviceRGB stroke/fill (color)
          - "r g b SC" / "r g b sc"        — set color in current space
          - "r g b SCN" / "r g b scn"      — set color in current space (newer)

        opendataloader-tagged output uses "scn"/"SCN" with an explicit
        DeviceRGB color space ("/DeviceRGB cs"). Earlier versions of this
        fixer only checked "rg"/"RG" and missed every replacement, leaving
        the contrast remap effectively no-op.
        """
        # Build lookup by quantized RGB tuple — match at 3 decimal places so
        # 0.6510 maps to the same key as 0.651.
        rgb_map: dict[tuple[int, int, int], tuple[float, float, float]] = {}
        for k, v in self.color_map.items():
            key = tuple(int(round(c * 1000)) for c in hex_to_rgb(k))
            rgb_map[key] = hex_to_rgb(v)

        rgb_color_ops = {"RG", "rg", "SC", "sc", "SCN", "scn"}
        count = 0

        for page in pdf.pages:
            try:
                instructions = list(pikepdf.parse_content_stream(page))
            except Exception:
                continue
            modified = False
            new_instructions = []
            for operands, operator in instructions:
                op = bytes(operator).decode("latin-1") if hasattr(operator, "__bytes__") else str(operator)
                if op in rgb_color_ops and len(operands) == 3:
                    try:
                        rgb_key = tuple(
                            int(round(float(o) * 1000)) for o in operands
                        )
                        if rgb_key in rgb_map:
                            new_rgb = rgb_map[rgb_key]
                            # pikepdf.Object.parse() requires bytes, not str.
                            # The original code passed str and was silently
                            # swallowed by the surrounding try/except, so
                            # every color replacement was lost.
                            operands = [
                                pikepdf.Object.parse(f"{c:g}".encode())
                                for c in new_rgb
                            ]
                            modified = True
                            count += 1
                    except Exception:
                        pass
                new_instructions.append((operands, operator))

            if modified:
                page.Contents = pdf.make_stream(
                    pikepdf.unparse_content_stream(new_instructions)
                )
        return count
