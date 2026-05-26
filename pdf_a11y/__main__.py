"""
Command-line entry point.

Usage:
  python -m pdf_a11y INPUT.pdf [--config config.yaml] [--output OUT.pdf]
  python -m pdf_a11y INPUT_DIR/ --recursive [--config config.yaml]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config
from .pipeline import RemediationPipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdf_a11y",
        description="Programmatic WCAG 2.2 / PDF/UA-1 remediation pipeline.",
    )
    parser.add_argument("input", help="PDF file or directory")
    parser.add_argument(
        "--config", default="config/remediation_config.yaml",
        help="Path to YAML config (default: config/remediation_config.yaml)",
    )
    parser.add_argument(
        "--recursive", "-r", action="store_true",
        help="Process all PDFs in input directory recursively",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress per-finding output (still writes JSON reports)",
    )
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    pipeline = RemediationPipeline(cfg)

    input_path = Path(args.input)
    if input_path.is_dir():
        pdfs = list(input_path.rglob("*.pdf") if args.recursive
                    else input_path.glob("*.pdf"))
    else:
        pdfs = [input_path]

    if not pdfs:
        print(f"No PDFs found at {input_path}", file=sys.stderr)
        return 1

    exit_code = 0
    for pdf in pdfs:
        print(f"\n▶ Remediating {pdf}")
        try:
            report = pipeline.run(pdf)
        except Exception as e:
            print(f"  ✗ FAILED: {e}", file=sys.stderr)
            exit_code = 2
            continue

        summary = report.summary
        print(f"  ✓ Output: {report.output_pdf}")
        print(f"  Summary: "
              f"{summary.get('fixed', 0)} fixed, "
              f"{summary.get('warning', 0)} warnings, "
              f"{summary.get('manual_required', 0)} need review, "
              f"{summary.get('error', 0)} errors")

        if not args.quiet:
            for f in report.findings:
                icon = {"fixed": "✓", "warning": "!",
                        "manual_required": "?", "error": "✗"}.get(f.severity, "·")
                print(f"    {icon} [{f.wcag}] {f.message}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
