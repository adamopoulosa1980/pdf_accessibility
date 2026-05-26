<#
.SYNOPSIS
  Download and install veraPDF 1.30.1 into ./tools/verapdf/ (Windows).

.DESCRIPTION
  Required for the local CLI pipeline. Skip this if you only run the
  web app via docker compose — the container installs veraPDF itself.

  Re-run anytime to reinstall over an existing copy.

.PARAMETER Version
  veraPDF release to fetch (default 1.30.1).

.PARAMETER InstallDir
  Project-relative install path (default tools/verapdf). Override with
  an absolute path to install elsewhere.

.EXAMPLE
  PS> .\scripts\install-verapdf.ps1
#>
[CmdletBinding()]
param(
    [string]$Version = "1.30.1",
    [string]$InstallDir = "tools/verapdf"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([System.IO.Path]::IsPathRooted($InstallDir)) {
    $AbsInstallDir = $InstallDir
} else {
    $AbsInstallDir = Join-Path $ProjectRoot $InstallDir
}
$ToolsDir   = Join-Path $ProjectRoot "tools"
$ZipPath    = Join-Path $ToolsDir   "verapdf-installer.zip"
$ExtractDir = Join-Path $ToolsDir   "verapdf-greenfield-$Version"
$AutoXml    = Join-Path $env:TEMP   "verapdf-auto-install-$([guid]::NewGuid()).xml"

if (-not (Get-Command java -ErrorAction SilentlyContinue)) {
    throw "Java is required (JRE 8+). Install it before running this script."
}

if (-not (Test-Path $ToolsDir)) {
    New-Item -ItemType Directory -Path $ToolsDir | Out-Null
}

Write-Host "Downloading veraPDF $Version installer..." -ForegroundColor Cyan
$Url = "https://software.verapdf.org/releases/1.30/verapdf-greenfield-$Version-installer.zip"
try {
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -Uri $Url -OutFile $ZipPath -UseBasicParsing
} finally {
    $ProgressPreference = "Continue"
}

Write-Host "Unpacking installer..." -ForegroundColor Cyan
if (Test-Path $ExtractDir) { Remove-Item -Recurse -Force $ExtractDir }
Expand-Archive -Path $ZipPath -DestinationPath $ToolsDir -Force

$InstallerJar = Join-Path $ExtractDir "verapdf-izpack-installer-$Version.jar"
if (-not (Test-Path $InstallerJar)) {
    throw "Expected installer JAR not found at $InstallerJar"
}

$XmlBody = @"
<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<AutomatedInstallation langpack="eng">
    <com.izforge.izpack.panels.htmlhello.HTMLHelloPanel id="welcome"/>
    <com.izforge.izpack.panels.htmlinfo.HTMLInfoPanel id="readme"/>
    <com.izforge.izpack.panels.htmllicence.HTMLLicencePanel id="gplv3_license"/>
    <com.izforge.izpack.panels.htmllicence.HTMLLicencePanel id="mpl_license"/>
    <com.izforge.izpack.panels.target.TargetPanel id="install_dir">
        <installpath>$AbsInstallDir</installpath>
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
"@
Set-Content -Path $AutoXml -Value $XmlBody -Encoding utf8

Write-Host "Running izpack installer into $AbsInstallDir..." -ForegroundColor Cyan
& java "-Djava.awt.headless=true" -jar $InstallerJar $AutoXml
if ($LASTEXITCODE -ne 0) {
    throw "veraPDF installer exited with code $LASTEXITCODE"
}

Write-Host "Cleaning up..." -ForegroundColor Cyan
Remove-Item -Force $ZipPath -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $ExtractDir -ErrorAction SilentlyContinue
Remove-Item -Force $AutoXml -ErrorAction SilentlyContinue

$Launcher = Join-Path $AbsInstallDir "verapdf.bat"
if (-not (Test-Path $Launcher)) {
    throw "Install finished but launcher $Launcher is missing."
}

Write-Host ""
Write-Host "veraPDF installed at $AbsInstallDir" -ForegroundColor Green
Write-Host "config/remediation_config.yaml default (validation.verapdf_path) already points here:"
Write-Host "    ./tools/verapdf/verapdf.bat"
