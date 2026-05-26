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
FROM python:3.11-slim

# --- OS packages ------------------------------------------------------------
# default-jre-headless : Java for opendataloader-pdf and veraPDF
# curl / unzip         : fetch + unpack the veraPDF installer
# tini                 : init process that reaps Java/veraPDF subprocesses
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-jre-headless \
        curl \
        unzip \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

# --- veraPDF 1.30.1 ---------------------------------------------------------
# Headless install via the izpack automated installer into /opt/verapdf.
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
              /tmp/verapdf-auto-install.xml

# --- Python dependencies (own layer so code edits don't re-install) ---------
WORKDIR /app
COPY requirements.txt ./requirements.txt
COPY webapp/requirements.txt ./webapp-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r webapp-requirements.txt

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

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
