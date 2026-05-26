"""
PDF Accessibility Remediator — web front end.

A deliberately small FastAPI app: upload a PDF, tweak a few runtime
settings, run the remediation pipeline, download the fixed PDF together
with veraPDF PDF/UA-1 and WTPDF reports. Designed to be self-explanatory
for non-technical users on a trusted internal network.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from runner import (
    PROJECT_ROOT,
    JobManager,
    VERAPDF_PROFILES,
    _verapdf_available,
    get_config_defaults,
)

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

API_DESCRIPTION = """
Remediate PDFs for WCAG 2.2 / PDF/UA-1 / WTPDF 1.0 accessibility.

**Interactive use:** open `/` in a browser.

**Headless / CI-CD use:** the same job API is fully scriptable —

1. `POST /api/jobs` (multipart: `file` + optional settings) → `{ "job_id": "..." }`
2. poll `GET /api/jobs/{job_id}` until `status` is `done` or `failed`
3. `GET /api/jobs/{job_id}/download/{kind}` for each entry in `downloads`

The poll response carries `validation.ua1.compliant` /
`validation.wt1a.compliant` (`true`/`false`/`null`) so a pipeline can
assert compliance and gate a build.
"""

app = FastAPI(
    title="PDF Accessibility Remediator",
    version="1.0",
    description=API_DESCRIPTION,
)
manager = JobManager()


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


_HELP_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Help &amp; Documentation — PDF Accessibility Remediator</title>
<style>
  body {{ margin:0; background:#f4f6f9; color:#1f2733;
    font:16px/1.6 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  .topbar {{ background:#1f5c99; color:#fff; padding:14px 20px; }}
  .topbar a {{ color:#fff; text-decoration:none; font-weight:600; }}
  .doc {{ max-width:820px; margin:0 auto; padding:28px 22px 64px; }}
  .doc h1 {{ font-size:26px; margin:.6em 0 .3em; }}
  .doc h2 {{ font-size:21px; margin:1.4em 0 .4em;
    border-bottom:1px solid #dfe4ea; padding-bottom:4px; }}
  .doc h3 {{ font-size:17px; margin:1.2em 0 .3em; }}
  .doc code {{ background:#eef1f4; padding:1px 5px; border-radius:4px;
    font-family:Consolas,"Liberation Mono",monospace; font-size:.9em; }}
  .doc pre {{ background:#11202f; color:#cfe3f5; padding:14px 16px;
    border-radius:8px; overflow-x:auto; }}
  .doc pre code {{ background:none; color:inherit; padding:0; }}
  .doc table {{ border-collapse:collapse; width:100%; margin:1em 0; }}
  .doc th, .doc td {{ border:1px solid #dfe4ea; padding:7px 10px;
    text-align:left; font-size:14.5px; }}
  .doc th {{ background:#eef4fb; }}
  .doc blockquote {{ margin:1em 0; padding:8px 14px; color:#5d6b7e;
    border-left:4px solid #1f5c99; background:#fbfcfd; }}
  .doc a {{ color:#1f5c99; }}
  .doc img {{ max-width:100%; }}
</style></head><body>
<div class="topbar"><a href="/">&#8592; Back to the app</a></div>
<div class="doc">{body}</div>
</body></html>"""


# Markdown links to repository files ([text](path)) — strip to plain text on
# the help page; keep absolute URLs and in-page #anchors clickable.
_REL_LINK = re.compile(r"\[([^\]]+)\]\((?!https?://|#)[^)]*\)")


@app.get("/help", response_class=HTMLResponse)
def help_page() -> HTMLResponse:
    """Render the project README as a styled, in-app help page."""
    readme = PROJECT_ROOT / "README.md"
    text = (readme.read_text(encoding="utf-8") if readme.exists()
            else "# Documentation\n\nREADME.md was not found.")
    text = _REL_LINK.sub(r"\1", text)
    try:
        import markdown as _md
        body = _md.markdown(
            text, extensions=["tables", "fenced_code", "sane_lists", "toc"])
    except Exception:
        import html as _html
        body = "<pre>" + _html.escape(text) + "</pre>"
    return HTMLResponse(_HELP_PAGE.format(body=body))


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "verapdf": _verapdf_available()}


@app.get("/api/defaults")
def defaults() -> dict:
    return {
        "config": get_config_defaults(),
        "verapdf_available": _verapdf_available(),
        "max_upload_mb": MAX_UPLOAD_MB,
        "queue_depth": manager.queue_depth(),
        "profiles": [{"flavour": f, "label": l} for f, l in VERAPDF_PROFILES],
    }


# --------------------------------------------------------------------------
# VLM connectivity test
# --------------------------------------------------------------------------
@app.post("/api/test-vlm")
def test_vlm(payload: dict) -> JSONResponse:
    base_url = (payload.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return JSONResponse({"ok": False, "message": "No URL provided."})
    api_key = (payload.get("api_key") or "").strip()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = f"{base_url}/models"
    try:
        resp = httpx.get(url, timeout=8.0, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        models = [m.get("id") for m in data.get("data", []) if m.get("id")]
        msg = (f"Connected — {len(models)} model(s) available."
               if models else "Connected.")
        return JSONResponse({"ok": True, "message": msg, "models": models})
    except httpx.HTTPStatusError as exc:
        return JSONResponse({"ok": False,
                             "message": f"Server replied {exc.response.status_code}."})
    except Exception as exc:                            # noqa: BLE001
        return JSONResponse({"ok": False,
                             "message": f"Could not reach the model server: {exc}"})


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
@app.post("/api/jobs")
async def create_job(
    file: UploadFile,
    vlm_base_url: str = Form(""),
    vlm_model: str = Form(""),
    vlm_api_key: str = Form(""),
    image_strategy: str = Form("vlm"),
    language: str = Form("en"),
    apply_contrast: str = Form("true"),
) -> dict:
    data = await file.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(
            413, f"File is larger than the {MAX_UPLOAD_MB} MB limit.")
    if not data[:5].startswith(b"%PDF"):
        raise HTTPException(400, "That file is not a PDF.")

    overrides = {
        "vlm_base_url": vlm_base_url,
        "vlm_model": vlm_model,
        "vlm_api_key": vlm_api_key,
        "image_strategy": image_strategy,
        "language": language,
        "apply_contrast": str(apply_contrast).lower() in ("true", "1", "on", "yes"),
    }
    job_id = manager.submit(file.filename or "document.pdf", data, overrides)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found (it may have expired).")
    state = job.public()
    # Absolute-ish download paths so a CI script needs no path knowledge.
    state["downloads"] = {
        kind: f"/api/jobs/{job_id}/download/{kind}" for kind in state["files"]
    }
    return state


# Human-readable labels + how each artefact should be served.
_DOWNLOADS = {
    "remediated_pdf":   ("Remediated PDF",            "application/pdf",  True),
    "verapdf_ua1_html": ("veraPDF PDF/UA-1 report",   "text/html",        False),
    "verapdf_wt1a_html": ("veraPDF WTPDF report",     "text/html",        False),
    "summary_json":     ("Remediation summary (JSON)", "application/json", True),
    "images_manifest":  ("Per-image manifest (JSON)",  "application/json", True),
}

# Hash format used as a SHA-256 prefix throughout the pipeline.
_HASH_RE = re.compile(r"^[a-f0-9]{1,64}$")
# Defensive caps on the refinement payload.
_MAX_OVERRIDES = 5000
_MAX_ALT_LEN = 500


@app.get("/api/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str) -> FileResponse:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found (it may have expired).")
    if kind not in job.files:
        raise HTTPException(404, "That file is not available for this job.")
    path = job.dir / job.files[kind]
    if not path.exists():
        raise HTTPException(404, "File missing on disk.")
    label, media_type, as_attachment = _DOWNLOADS.get(
        kind, (kind, "application/octet-stream", True))
    disposition = "attachment" if as_attachment else "inline"
    return FileResponse(
        path, media_type=media_type, filename=path.name,
        headers={"Content-Disposition": f'{disposition}; filename="{path.name}"'},
    )


# --------------------------------------------------------------------------
# Alt-text review (Human-in-the-loop): list images, fetch thumbnails, and
# submit edits as a refinement re-run. Same endpoints back the in-browser UI
# and a CI/CD script that posts unresolved items to a review queue.
# --------------------------------------------------------------------------
@app.get("/api/jobs/{job_id}/images")
def list_images(job_id: str) -> dict:
    """Return the per-image manifest (rewritten with API thumbnail URLs)."""
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found (it may have expired).")
    manifest_name = job.files.get("images_manifest")
    if not manifest_name:
        raise HTTPException(
            404, "No image manifest for this job — it may still be running, "
                 "or the document had no images to process.")
    import json
    raw = (job.dir / manifest_name).read_text(encoding="utf-8")
    data = json.loads(raw)
    for img in data.get("images", []):
        img["thumb_url"] = f"/api/jobs/{job_id}/images/{img['hash']}/thumb"
    # Counts the UI shows as filter tabs.
    counts = {"vlm": 0, "override": 0, "decorative": 0, "manual_required": 0,
              "pending_vlm": 0, "total": 0}
    for img in data.get("images", []):
        counts["total"] += 1
        src = img.get("source", "")
        if src.startswith("decorative") or src == "override_decorative":
            counts["decorative"] += 1
        elif src == "override":
            counts["override"] += 1
        elif src == "vlm":
            counts["vlm"] += 1
        elif src == "manual_required":
            counts["manual_required"] += 1
        elif src == "pending_vlm":
            counts["pending_vlm"] += 1
    data["counts"] = counts
    data["alt_overrides_carried"] = job.alt_overrides
    return data


@app.get("/api/jobs/{job_id}/images/{img_hash}/thumb")
def image_thumb(job_id: str, img_hash: str) -> FileResponse:
    """Serve the PNG thumbnail for one image."""
    if not _HASH_RE.match(img_hash):
        raise HTTPException(400, "Invalid image hash.")
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if not job.stem:
        raise HTTPException(404, "Job is missing image data.")
    path = job.dir / f"{job.stem}_thumbs" / f"{img_hash}.png"
    if not path.exists():
        raise HTTPException(404, "Thumbnail not found.")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=3600"})


@app.post("/api/jobs/{job_id}/refine")
def refine(job_id: str, payload: dict) -> dict:
    """
    Re-run the pipeline on the same original PDF with the supplied alt-text
    overrides merged into the parent job's existing overrides.

    Body: { "overrides": { "<hash>": "<alt text>" | "DECORATIVE", ... } }

    Returns { "job_id": "..." } — poll the new id for progress.
    """
    raw = payload.get("overrides") or {}
    if not isinstance(raw, dict):
        raise HTTPException(400, "`overrides` must be an object of hash -> text.")
    if len(raw) > _MAX_OVERRIDES:
        raise HTTPException(
            413, f"Too many overrides ({len(raw)} > {_MAX_OVERRIDES}).")

    clean: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not _HASH_RE.match(k):
            raise HTTPException(400, f"Invalid hash key: {k!r}.")
        if not isinstance(v, str):
            raise HTTPException(400, f"Override value for {k} must be a string.")
        v = v.strip()
        if not v:
            continue                # skip blanks rather than store empties
        if len(v) > _MAX_ALT_LEN:
            raise HTTPException(
                413, f"Alt text for {k} is over {_MAX_ALT_LEN} chars.")
        clean[k] = v

    try:
        new_id = manager.submit_refinement(job_id, clean)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"job_id": new_id, "parent_id": job_id,
            "overrides_applied": len(clean)}
