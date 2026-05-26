#!/usr/bin/env bash
# Download and install veraPDF 1.30.1 into ./tools/verapdf/ (Linux / macOS).
#
# Required for the local CLI pipeline. Skip this if you only run the web
# app via docker compose -- the container installs veraPDF itself.
#
# Re-run anytime to reinstall over an existing copy.
#
# Overrides:
#   VERAPDF_VERSION=1.30.1
#   VERAPDF_INSTALL_DIR=/abs/or/relative/path   (default: tools/verapdf)

set -euo pipefail

VERSION="${VERAPDF_VERSION:-1.30.1}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -n "${VERAPDF_INSTALL_DIR:-}" ]]; then
    case "$VERAPDF_INSTALL_DIR" in
        /*) INSTALL_DIR="$VERAPDF_INSTALL_DIR" ;;
        *)  INSTALL_DIR="${PROJECT_ROOT}/${VERAPDF_INSTALL_DIR}" ;;
    esac
else
    INSTALL_DIR="${PROJECT_ROOT}/tools/verapdf"
fi

TOOLS_DIR="${PROJECT_ROOT}/tools"
ZIP_PATH="${TOOLS_DIR}/verapdf-installer.zip"
EXTRACT_DIR="${TOOLS_DIR}/verapdf-greenfield-${VERSION}"
AUTO_XML="$(mktemp -t verapdf-auto-install.XXXXXX)"
trap 'rm -f "$AUTO_XML"' EXIT

for cmd in java curl unzip; do
    command -v "$cmd" >/dev/null 2>&1 \
        || { echo "ERROR: $cmd is required but not on PATH." >&2; exit 1; }
done

mkdir -p "$TOOLS_DIR"

echo "Downloading veraPDF $VERSION installer..."
curl -fsSL -o "$ZIP_PATH" \
    "https://software.verapdf.org/releases/1.30/verapdf-greenfield-${VERSION}-installer.zip"

echo "Unpacking installer..."
rm -rf "$EXTRACT_DIR"
unzip -q "$ZIP_PATH" -d "$TOOLS_DIR"

INSTALLER_JAR="${EXTRACT_DIR}/verapdf-izpack-installer-${VERSION}.jar"
if [[ ! -f "$INSTALLER_JAR" ]]; then
    echo "ERROR: installer JAR missing at $INSTALLER_JAR" >&2
    exit 1
fi

# izpack is a JVM process, so on Git Bash/Cygwin it needs a Windows-style
# path. Native Linux/macOS leaves INSTALL_DIR untouched.
JVM_INSTALL_DIR="$INSTALL_DIR"
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        command -v cygpath >/dev/null 2>&1 \
            && JVM_INSTALL_DIR="$(cygpath -w "$INSTALL_DIR")"
        ;;
esac

cat > "$AUTO_XML" <<EOF
<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<AutomatedInstallation langpack="eng">
    <com.izforge.izpack.panels.htmlhello.HTMLHelloPanel id="welcome"/>
    <com.izforge.izpack.panels.htmlinfo.HTMLInfoPanel id="readme"/>
    <com.izforge.izpack.panels.htmllicence.HTMLLicencePanel id="gplv3_license"/>
    <com.izforge.izpack.panels.htmllicence.HTMLLicencePanel id="mpl_license"/>
    <com.izforge.izpack.panels.target.TargetPanel id="install_dir">
        <installpath>${JVM_INSTALL_DIR}</installpath>
    </com.izforge.izpack.panels.target.TargetPanel>
    <com.izforge.izpack.panels.packs.PacksPanel id="sdk_pack_select">
        <pack index="0" name="veraPDF GUI" selected="true"/>
        <pack index="1" name="veraPDF Mac and *nix Scripts" selected="true"/>
        <pack index="2" name="veraPDF Validation model" selected="true"/>
        <pack index="3" name="veraPDF Documentation" selected="false"/>
        <pack index="4" name="veraPDF Sample Plugins" selected="false"/>
    </com.izforge.izpack.panels.packs.PacksPanel>
    <com.izforge.izpack.panels.install.InstallPanel id="install"/>
    <com.izforge.izpack.panels.finish.FinishPanel id="finish"/>
</AutomatedInstallation>
EOF

echo "Running izpack installer into ${INSTALL_DIR}..."
java -Djava.awt.headless=true -jar "$INSTALLER_JAR" "$AUTO_XML"

echo "Cleaning up..."
rm -f "$ZIP_PATH"
rm -rf "$EXTRACT_DIR"

if [[ -f "${INSTALL_DIR}/verapdf" ]]; then
    LAUNCHER="${INSTALL_DIR}/verapdf"
    chmod +x "$LAUNCHER"
elif [[ -f "${INSTALL_DIR}/verapdf.bat" ]]; then
    LAUNCHER="${INSTALL_DIR}/verapdf.bat"
else
    echo "ERROR: install finished but no verapdf launcher in ${INSTALL_DIR}" >&2
    exit 1
fi

echo
echo "veraPDF installed at ${INSTALL_DIR}"
echo "Set validation.verapdf_path in config/remediation_config.yaml to:"
echo "    ${LAUNCHER}"
