"""
Background job runner for the PDF Accessibility web app.

Wraps ``RemediationPipeline`` so a single HTTP upload can kick off the
~5-10 minute remediation in a worker thread, expose live progress, then
serve the results. veraPDF report generation (PDF/UA-1 + WTPDF 1.0) is
owned here rather than inside the pipeline so the user gets both reports
as downloadable HTML.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make the pdf_a11y package importable no matter where uvicorn is started.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pdf_a11y.config import Config            # noqa: E402
from pdf_a11y.pipeline import RemediationPipeline  # noqa: E402

# --------------------------------------------------------------------------
# Settings — every one overridable by an environment variable so the same
# image runs unchanged on any host.
# --------------------------------------------------------------------------
BASE_CONFIG = Path(os.environ.get(
    "BASE_CONFIG", str(PROJECT_ROOT / "config" / "remediation_config.yaml")))
JOBS_DIR = Path(os.environ.get(
    "JOBS_DIR", str(PROJECT_ROOT / "webapp" / "jobs")))
VERAPDF_PATH = os.environ.get("VERAPDF_PATH", "/opt/verapdf/verapdf")
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "1"))
JOB_RETENTION_HOURS = float(os.environ.get("JOB_RETENTION_HOURS", "24"))
VERAPDF_TIMEOUT = int(os.environ.get("VERAPDF_TIMEOUT", "1200"))

JOBS_DIR.mkdir(parents=True, exist_ok=True)

# veraPDF profiles we report on. (label, flavour-flag value)
VERAPDF_PROFILES = [
    ("ua1", "PDF/UA-1"),
    ("wt1a", "WTPDF 1.0 Accessibility"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_stem(filename: str) -> str:
    """Reduce an uploaded filename to a filesystem- and JVM-safe stem."""
    stem = Path(filename).stem or "document"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return (stem or "document")[:80]


# --------------------------------------------------------------------------
# Config assembly — base YAML overlaid with the handful of runtime values
# the UI exposes.
# --------------------------------------------------------------------------
def build_config(job_dir: Path, overrides: dict[str, Any]) -> Config:
    """Load the canonical config and apply the UI's runtime overrides."""
    cfg = Config.load(BASE_CONFIG)
    raw = cfg.raw

    # Engine is fixed: opendataloader is free, local, and needs no keys.
    raw["tagging"]["engine"] = "opendataloader"

    vlm = raw["images"].setdefault("vlm", {})
    if overrides.get("vlm_base_url"):
        vlm["base_url"] = overrides["vlm_base_url"].strip()
    if overrides.get("vlm_model"):
        vlm["model"] = overrides["vlm_model"].strip()

    # Vision-model API key: the per-job value wins; otherwise fall back to a
    # deployment-wide VLM_API_KEY env var. Left unset, the pipeline still
    # resolves api_key_env (and most local OpenAI-compatible servers work
    # with no key at all).
    api_key = (overrides.get("vlm_api_key") or "").strip() \
        or os.environ.get("VLM_API_KEY", "")
    if api_key:
        vlm["api_key"] = api_key

    strategy = overrides.get("image_strategy")
    if strategy in ("vlm", "prompt", "decorative"):
        raw["images"]["strategy"] = strategy

    lang = (overrides.get("language") or "").strip()
    if lang:
        raw["document"]["primary_language"] = lang
        vlm["output_language"] = lang

    raw["contrast"]["apply_remapping"] = bool(overrides.get("apply_contrast", True))

    # Per-image alt-text overrides keyed by SHA-256 hash prefix. Carried
    # across refinement chains so each /refine builds on the previous run.
    alt_overrides = overrides.get("alt_overrides") or {}
    if alt_overrides:
        raw["images"]["alt_overrides"] = dict(alt_overrides)

    # The web app owns output location and veraPDF reporting.
    raw["output"]["directory"] = str(job_dir)
    raw["output"]["backup_originals"] = True
    raw["output"]["write_report"] = True
    raw["validation"]["run_verapdf"] = False

    cfg._validate()
    return cfg


def get_config_defaults() -> dict[str, Any]:
    """
    Values used to pre-fill the upload form. Environment variables
    (VLM_BASE_URL / VLM_MODEL) win over the bundled config file so an
    administrator can point a deployment at their own model server
    without rebuilding the image.
    """
    try:
        cfg = Config.load(BASE_CONFIG)
        vlm = cfg.images.get("vlm", {})
        cfg_url, cfg_model = vlm.get("base_url", ""), vlm.get("model", "")
        strategy = cfg.images.get("strategy", "vlm")
        language = cfg.document.get("primary_language", "en")
        contrast = bool(cfg.contrast.get("apply_remapping", True))
    except Exception:
        cfg_url = cfg_model = ""
        strategy, language, contrast = "vlm", "en", True
    return {
        "vlm_base_url": os.environ.get("VLM_BASE_URL", cfg_url),
        "vlm_model": os.environ.get("VLM_MODEL", cfg_model),
        "image_strategy": strategy,
        "language": language,
        "apply_contrast": contrast,
    }


# --------------------------------------------------------------------------
# veraPDF
# --------------------------------------------------------------------------
def _verapdf_available() -> bool:
    p = Path(VERAPDF_PATH)
    return p.exists() or shutil.which(VERAPDF_PATH) is not None


def _run_verapdf(pdf_path: Path, flavour: str, fmt: str, out_file: Path) -> str:
    """Run veraPDF once and write its report to *out_file*. Returns stdout."""
    binary = VERAPDF_PATH
    if not Path(binary).exists():
        resolved = shutil.which(VERAPDF_PATH)
        if resolved:
            binary = resolved
    proc = subprocess.run(
        [binary, "--format", fmt, "--flavour", flavour, str(pdf_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=VERAPDF_TIMEOUT,
    )
    out_file.write_text(proc.stdout or "", encoding="utf-8")
    return proc.stdout or ""


def _parse_verapdf_json(json_text: str) -> dict[str, Any]:
    """Pull a pass/fail + failed-rule count out of veraPDF JSON output."""
    try:
        data = json.loads(json_text)
    except Exception:
        return {"compliant": None, "failed_rules": None}
    for job in data.get("report", {}).get("jobs", []):
        results = job.get("validationResult") or []
        if results:
            v = results[0]
            details = v.get("details", {})
            return {
                "compliant": bool(v.get("compliant", False)),
                "failed_rules": details.get("failedRules"),
            }
    return {"compliant": None, "failed_rules": None}


# --------------------------------------------------------------------------
# Job model
# --------------------------------------------------------------------------
class Job:
    def __init__(self, job_id: str, display_name: str, job_dir: Path,
                 parent_id: str | None = None) -> None:
        self.id = job_id
        self.display_name = display_name
        self.dir = job_dir
        self.status = "queued"          # queued | running | done | failed
        self.phase = "Waiting in queue…"
        self.error: str | None = None
        self.created_at = _now()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.started_monotonic: float | None = None
        self.elapsed_seconds = 0.0
        self.log_path = job_dir / "job.log"
        # Download artefacts: kind -> filename (relative to job dir)
        self.files: dict[str, str] = {}
        self.validation: dict[str, Any] = {}
        # Refinement chain (alt-text review re-runs).
        self.parent_id: str | None = parent_id
        # Carried across a refinement chain so the next /refine starts from
        # the union of previous overrides + the user's new edits.
        self.alt_overrides: dict[str, str] = {}
        # Path to the uploaded PDF (set at submit time) — the source of truth
        # a refinement re-uses.
        self.input_path: Path | None = None
        # Stem of input_path.name (without .pdf) — pipeline names all
        # artefacts using this; thumbnails live in <dir>/<stem>_thumbs/.
        self.stem: str = ""
        # Snapshot of the overrides this job was submitted with; a
        # refinement child inherits VLM/contrast/language settings from here.
        self._submitted_overrides: dict[str, Any] = {}

    # -- progress helpers --------------------------------------------------
    def _log_tail(self, lines: int = 60) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def _refresh_phase_from_log(self) -> None:
        """Derive the current phase from the pipeline's '[n/9] ...' banners."""
        if self.status != "running":
            return
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        # Last matching line wins: pipeline step banners ('[n/9] ...'),
        # the save step, then the veraPDF validation lines this runner
        # prints once the pipeline is done.
        banner = None
        for line in text.splitlines():
            line = line.strip()
            if (re.match(r"^\[\d+/\d+\]", line)
                    or line.startswith("Saving ")
                    or line.startswith("Validating with veraPDF")):
                banner = line
        if banner:
            self.phase = banner

    def public(self) -> dict[str, Any]:
        if self.status == "running" and self.started_monotonic is not None:
            self.elapsed_seconds = time.monotonic() - self.started_monotonic
        self._refresh_phase_from_log()
        return {
            "id": self.id,
            "display_name": self.display_name,
            "status": self.status,
            "phase": self.phase,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "log_tail": self._log_tail(),
            "files": sorted(self.files.keys()),
            "validation": self.validation,
            "parent_id": self.parent_id,
            "alt_overrides_count": len(self.alt_overrides),
        }


# --------------------------------------------------------------------------
# Job manager
# --------------------------------------------------------------------------
class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="pdfa11y")
        self._purge_old_jobs()

    # -- public API --------------------------------------------------------
    def submit(self, filename: str, data: bytes,
               overrides: dict[str, Any],
               parent_id: str | None = None) -> str:
        self._purge_old_jobs()
        job_id = uuid.uuid4().hex[:12]
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        stem = _safe_stem(filename)
        input_path = job_dir / f"{stem}.pdf"
        input_path.write_bytes(data)

        job = Job(job_id, Path(filename).name, job_dir, parent_id=parent_id)
        job.input_path = input_path
        job.stem = stem
        job.alt_overrides = dict(overrides.get("alt_overrides") or {})
        job._submitted_overrides = dict(overrides)
        with self.lock:
            self.jobs[job_id] = job
        self.executor.submit(self._run, job, input_path, overrides)
        return job_id

    def submit_refinement(self, parent_id: str,
                          new_alt_overrides: dict[str, str]) -> str:
        """
        Spawn a refinement job from a finished parent: same original PDF,
        same VLM settings, with the parent's alt overrides plus the user's
        new edits merged in. Returns the new job_id.
        """
        parent = self.get(parent_id)
        if parent is None:
            raise ValueError(f"Parent job {parent_id} not found.")
        if parent.status != "done":
            raise ValueError(
                f"Parent job is '{parent.status}' — can only refine a "
                f"completed job.")
        if parent.input_path is None or not parent.input_path.exists():
            raise ValueError("Parent job's original PDF is no longer on disk.")

        # Merge: new edits win over the parent's existing overrides.
        merged = dict(parent.alt_overrides)
        merged.update(new_alt_overrides or {})

        # Carry the parent's VLM/contrast/language settings forward so the
        # refinement is consistent with the original run.
        overrides = {
            "vlm_base_url": parent._submitted_overrides.get("vlm_base_url", ""),
            "vlm_model": parent._submitted_overrides.get("vlm_model", ""),
            "vlm_api_key": parent._submitted_overrides.get("vlm_api_key", ""),
            "image_strategy": parent._submitted_overrides.get(
                "image_strategy", "vlm"),
            "language": parent._submitted_overrides.get("language", "en"),
            "apply_contrast": parent._submitted_overrides.get(
                "apply_contrast", True),
            "alt_overrides": merged,
        }
        data = parent.input_path.read_bytes()
        return self.submit(parent.display_name, data, overrides,
                           parent_id=parent_id)

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def queue_depth(self) -> int:
        with self.lock:
            return sum(1 for j in self.jobs.values()
                       if j.status in ("queued", "running"))

    # -- worker ------------------------------------------------------------
    def _run(self, job: Job, input_path: Path,
             overrides: dict[str, Any]) -> None:
        job.status = "running"
        job.started_at = _now()
        job.started_monotonic = time.monotonic()
        job.phase = "Starting…"

        logf = open(job.log_path, "w", encoding="utf-8", buffering=1)
        try:
            # Everything inside the redirect so the UI log shows the full
            # run — pipeline steps and veraPDF validation alike.
            with redirect_stdout(logf), redirect_stderr(logf):
                print(f"=== Remediating {job.display_name} ===", flush=True)
                cfg = build_config(job.dir, overrides)
                report = RemediationPipeline(cfg).run(input_path)

                out_pdf = Path(report.output_pdf) if report.output_pdf else None
                if not out_pdf or not out_pdf.exists():
                    raise RuntimeError(
                        "Pipeline finished but produced no output PDF.")
                job.files["remediated_pdf"] = out_pdf.name

                # Remediation summary JSON, if written.
                summary_json = job.dir / f"{input_path.stem}_report.json"
                if summary_json.exists():
                    job.files["summary_json"] = summary_json.name

                # Per-image manifest (powers the alt-text review screen).
                manifest = job.dir / f"{input_path.stem}_images_manifest.json"
                if manifest.exists():
                    job.files["images_manifest"] = manifest.name

                # veraPDF reports — both profiles, HTML for humans + JSON
                # for the pass/fail badge on the results page.
                self._generate_verapdf_reports(job, out_pdf)
                print("=== Done ===", flush=True)

            job.status = "done"
            job.phase = "Completed"
        except Exception as exc:                       # noqa: BLE001
            job.status = "failed"
            job.error = str(exc) or exc.__class__.__name__
            job.phase = "Failed"
            logf.write("\n=== ERROR ===\n")
            logf.write(traceback.format_exc())
        finally:
            job.finished_at = _now()
            if job.started_monotonic is not None:
                job.elapsed_seconds = time.monotonic() - job.started_monotonic
            logf.close()

    def _generate_verapdf_reports(self, job: Job, out_pdf: Path) -> None:
        if not _verapdf_available():
            print("veraPDF not found — skipping validation reports.",
                  flush=True)
            job.validation["available"] = False
            return
        job.validation["available"] = True
        for flavour, label in VERAPDF_PROFILES:
            job.phase = f"Validating ({label})…"
            print(f"Validating with veraPDF: {label}…", flush=True)
            stem = out_pdf.stem
            html_path = job.dir / f"{stem}_verapdf_{flavour}.html"
            json_path = job.dir / f"{stem}_verapdf_{flavour}.json"
            try:
                _run_verapdf(out_pdf, flavour, "html", html_path)
                json_text = _run_verapdf(out_pdf, flavour, "json", json_path)
                summary = _parse_verapdf_json(json_text)
                job.validation[flavour] = {"label": label, **summary}
                job.files[f"verapdf_{flavour}_html"] = html_path.name
                state = ("PASS" if summary["compliant"]
                         else "FAIL" if summary["compliant"] is False
                         else "?")
                print(f"  {label}: {state}", flush=True)
            except subprocess.TimeoutExpired:
                job.validation[flavour] = {
                    "label": label, "compliant": None,
                    "error": "veraPDF timed out"}
                print(f"  {label}: timed out", flush=True)
            except Exception as exc:                   # noqa: BLE001
                job.validation[flavour] = {
                    "label": label, "compliant": None, "error": str(exc)}
                print(f"  {label}: error — {exc}", flush=True)

    # -- housekeeping ------------------------------------------------------
    def _purge_old_jobs(self) -> None:
        """Delete job directories older than the retention window."""
        cutoff = time.time() - JOB_RETENTION_HOURS * 3600
        for entry in JOBS_DIR.iterdir() if JOBS_DIR.exists() else []:
            if not entry.is_dir():
                continue
            active = self.jobs.get(entry.name)
            if active and active.status in ("queued", "running"):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    with self.lock:
                        self.jobs.pop(entry.name, None)
            except OSError:
                pass
