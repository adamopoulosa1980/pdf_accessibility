#!/usr/bin/env python3
"""
Headless client for the PDF Accessibility Remediator API.

Designed for CI/CD: upload a PDF to a running server, wait for the job
to finish, download the remediated PDF and veraPDF reports, and exit with
a status code a build pipeline can gate on.

Usage:
  python client.py --server http://HOST:8000 input.pdf --out ./results

Common CI invocation (fail the build unless the result is compliant):
  python client.py --server http://HOST:8000 doc.pdf --out out --require-compliant

Exit codes:
  0  job finished (and, with --require-compliant, all profiles compliant)
  1  the remediation job failed
  2  job finished but a veraPDF profile was not compliant (--require-compliant)
  3  usage / connection error
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("This client needs the 'requests' package: pip install requests",
          file=sys.stderr)
    sys.exit(3)


def main() -> int:
    ap = argparse.ArgumentParser(description="Headless PDF accessibility remediation client.")
    ap.add_argument("input", help="PDF file to remediate")
    ap.add_argument("--server", required=True, help="Base URL of the running server, e.g. http://host:8000")
    ap.add_argument("--out", default="./remediation-output", help="Directory to write results into")
    ap.add_argument("--vlm-url", default=None, help="Override the vision model server URL")
    ap.add_argument("--vlm-model", default=None, help="Override the vision model name")
    ap.add_argument("--vlm-api-key", default=None,
                    help="Vision model API key (omit if the server needs none)")
    ap.add_argument("--image-strategy", default=None,
                    choices=["vlm", "decorative", "prompt"], help="How to handle images")
    ap.add_argument("--language", default=None, help="Primary document language (ISO 639-1)")
    ap.add_argument("--no-contrast", action="store_true", help="Disable colour-contrast remapping")
    ap.add_argument("--require-compliant", action="store_true",
                    help="Exit non-zero unless every veraPDF profile is compliant")
    ap.add_argument("--timeout", type=int, default=2400, help="Max seconds to wait (default 2400)")
    ap.add_argument("--poll", type=float, default=5.0, help="Seconds between status polls")
    args = ap.parse_args()

    server = args.server.rstrip("/")
    src = Path(args.input)
    if not src.is_file():
        print(f"Input file not found: {src}", file=sys.stderr)
        return 3

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Submit -----------------------------------------------------------
    form = {"image_strategy": args.image_strategy or "vlm"}
    if args.vlm_url:
        form["vlm_base_url"] = args.vlm_url
    if args.vlm_model:
        form["vlm_model"] = args.vlm_model
    if args.vlm_api_key:
        form["vlm_api_key"] = args.vlm_api_key
    if args.language:
        form["language"] = args.language
    form["apply_contrast"] = "false" if args.no_contrast else "true"

    print(f"Uploading {src.name} to {server} ...")
    try:
        with src.open("rb") as fh:
            resp = requests.post(
                f"{server}/api/jobs",
                files={"file": (src.name, fh, "application/pdf")},
                data=form, timeout=120,
            )
    except requests.RequestException as exc:
        print(f"Could not reach the server: {exc}", file=sys.stderr)
        return 3
    if resp.status_code != 200:
        detail = resp.json().get("detail", resp.text) if resp.content else resp.text
        print(f"Upload rejected ({resp.status_code}): {detail}", file=sys.stderr)
        return 3
    job_id = resp.json()["job_id"]
    print(f"Job {job_id} accepted. Waiting for completion...")

    # --- Poll -------------------------------------------------------------
    deadline = time.time() + args.timeout
    last_phase = None
    state = {}
    while True:
        if time.time() > deadline:
            print("Timed out waiting for the job to finish.", file=sys.stderr)
            return 3
        try:
            r = requests.get(f"{server}/api/jobs/{job_id}", timeout=30)
        except requests.RequestException as exc:
            print(f"Status poll failed: {exc}", file=sys.stderr)
            time.sleep(args.poll)
            continue
        if r.status_code != 200:
            print(f"Job lookup failed ({r.status_code}).", file=sys.stderr)
            return 3
        state = r.json()
        phase = f"[{state['status']}] {state.get('phase', '')}"
        if phase != last_phase:
            print(f"  {phase}  ({int(state.get('elapsed_seconds', 0))}s)")
            last_phase = phase
        if state["status"] in ("done", "failed"):
            break
        time.sleep(args.poll)

    if state["status"] == "failed":
        print(f"\nJob FAILED: {state.get('error')}", file=sys.stderr)
        if state.get("log_tail"):
            print("--- log tail ---", file=sys.stderr)
            print(state["log_tail"], file=sys.stderr)
        return 1

    # --- Download ---------------------------------------------------------
    print("\nDownloading results...")
    for kind, url in (state.get("downloads") or {}).items():
        try:
            dl = requests.get(f"{server}{url}", timeout=120)
            dl.raise_for_status()
        except requests.RequestException as exc:
            print(f"  ! could not download {kind}: {exc}", file=sys.stderr)
            continue
        name = _filename_from(dl, kind)
        dest = out_dir / name
        dest.write_bytes(dl.content)
        print(f"  saved {dest}")

    # --- Verdict ----------------------------------------------------------
    validation = state.get("validation") or {}
    print("\nValidation:")
    not_compliant = False
    if validation.get("available") is False:
        print("  veraPDF was not available on the server — reports skipped.")
    else:
        for flavour in ("ua1", "wt1a"):
            res = validation.get(flavour)
            if not res:
                continue
            c = res.get("compliant")
            verdict = "PASS" if c else "FAIL" if c is False else "UNKNOWN"
            extra = f" ({res.get('failed_rules')} failed rule(s))" if c is False else ""
            print(f"  {res.get('label', flavour)}: {verdict}{extra}")
            if c is not True:
                not_compliant = True

    if args.require_compliant and not_compliant:
        print("\nResult: NOT COMPLIANT (build gate failed).", file=sys.stderr)
        return 2
    print("\nResult: done.")
    return 0


def _filename_from(resp: "requests.Response", kind: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    if "filename=" in cd:
        return cd.split("filename=")[-1].strip().strip('"') or kind
    return kind


if __name__ == "__main__":
    sys.exit(main())
