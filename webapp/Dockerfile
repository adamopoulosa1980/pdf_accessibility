# ============================================================================
# PDF Accessibility Remediator — web app container
#
# Bundles everything the pipeline needs to run with no external setup:
#   - the Python remediation pipeline (pdf_a11y)
#   - a Java runtime (opendataloader-pdf tags structure via a JVM engine)
#   - veraPDF 1.30.1 (PDF/UA-1 + WTPDF 1.0 validation)
#
# Build from the PROJECT ROOT (the build context must include pdf_a11y/):
#   docker build -f webapp/Dockerfile -t pdf-a11y-remediator:1.0 .
# ============================================================================
# Base: pinned to python:3.11-slim (Debian Bookworm). Trixie-based tags
# (3.13-slim, 3.14-slim) were tested in v2.5 and increased the total CVE
# count rather than reduced it — the newer pip in those tags has its own
# CVEs, and Trixie's `tar`/`glibc` Lows are not yet backported. Revisit
# in 3-6 months once Trixie stabilises.
FROM python:3.11-slim

# --- OS packages ------------------------------------------------------------
# default-jre-headless : Java for opendataloader-pdf and veraPDF
# tini                 : init process that reaps Java/veraPDF subprocesses
# ca-certificates      : TLS roots
# curl / unzip         : build-time only, used to fetch + unpack veraPDF
#                        and PURGED before the image is committed (keeps
#                        libcurl / libunzip CVEs out of the runtime).
#
# `apt-get upgrade` is opt-in: pulls every Debian security patch published
# since the base image was built. Slim images go weeks between rebuilds, so
# this catches what the upstream tag hasn't refreshed yet.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        default-jre-headless \
        ca-certificates \
        tini \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# --- veraPDF 1.30.1 ---------------------------------------------------------
# Headless install via the izpack automated installer into /opt/verapdf.
# Same RUN finishes by purging curl + unzip so they aren't in the final
# image. (Healthcheck uses Python's stdlib instead — see HEALTHCHECK below.)
COPY webapp/verapdf-auto-install.xml /tmp/verapdf-auto-install.xml
RUN cd /tmp \
    && curl -fsSL -o verapdf.zip \
        https://software.verapdf.org/releases/1.30/verapdf-greenfield-1.30.1-installer.zip \
    && unzip -q verapdf.zip \
    && java -Djava.awt.headless=true \
        -jar verapdf-greenfield-1.30.1/verapdf-izpack-installer-1.30.1.jar \
        /tmp/verapdf-auto-install.xml \
    && chmod +x /opt/verapdf/verapdf \
    && test -x /opt/verapdf/verapdf \
    && rm -rf /tmp/verapdf.zip /tmp/verapdf-greenfield-1.30.1 \
              /tmp/verapdf-auto-install.xml \
    && apt-get purge -y --auto-remove curl unzip \
    && rm -rf /var/lib/apt/lists/*

# --- Python dependencies (own layer so code edits don't re-install) ---------
WORKDIR /app
COPY requirements.txt ./requirements.txt
COPY webapp/requirements.txt ./webapp-requirements.txt
# Upgrade pip, setuptools, AND wheel first. Base images bundle all three
# pre-installed, and each carries CVEs from time to time:
#   pip      - CVE-2026-3219, CVE-2026-6357 (3.13-slim's pip 26.0.1)
#   wheel    - CVE-2026-24049 (3.11-slim's wheel 0.45.x)
#   setuptools - has historically had its own (covered for completeness)
# Refreshing all three to current upstream patches the bundled-tooling
# layer regardless of which base image we sit on.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt -r webapp-requirements.txt

# --- Application code -------------------------------------------------------
COPY pdf_a11y/  ./pdf_a11y/
COPY config/    ./config/
COPY webapp/    ./webapp/
COPY examples/  ./examples/
COPY README.md  ./README.md


# --- Runtime configuration --------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    BASE_CONFIG=/app/config/remediation_config.yaml \
    VERAPDF_PATH=/opt/verapdf/verapdf \
    JOBS_DIR=/data/jobs \
    MAX_CONCURRENT_JOBS=1 \
    JOB_RETENTION_HOURS=24 \
    MAX_UPLOAD_MB=200

# Drop root: run as a dedicated unprivileged user, with ownership of the
# writable paths (the jobs volume, the app dir for __pycache__ creation).
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin pdfa11y \
    && mkdir -p /data/jobs \
    && chown -R pdfa11y:pdfa11y /data /app
VOLUME ["/data/jobs"]
EXPOSE 8000
WORKDIR /app/webapp
USER pdfa11y

# Healthcheck via Python's urllib so the image doesn't need to keep curl
# (and its libcurl CVE surface) just for liveness probes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys;\
sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=4).status==200 else 1)" \
        || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
