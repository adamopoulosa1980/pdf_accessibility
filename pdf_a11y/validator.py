"""
veraPDF wrapper. Runs PDF/UA-1 or WCAG 2.2 validation and parses results.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .config import Config
from .report import RemediationReport


class Validator:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.run = cfg.validation.get("run_verapdf", True)
        self.path = cfg.validation.get("verapdf_path", "verapdf")
        self.profile = cfg.validation.get("profile", "ua1")
        self.fail_on_error = cfg.validation.get("fail_on_error", False)

    def validate(self, pdf_path: Path, report: RemediationReport) -> bool:
        if not self.run:
            return True

        # Resolve the configured path to an absolute path. Windows subprocess
        # cannot launch "./foo.bat" because cmd.exe parses the leading "." as
        # a token. shutil.which returns the input string unchanged for an
        # existing relative path on Windows (which doesn't fix the problem),
        # so we always force absolute via Path.resolve().
        p = Path(self.path)
        if not p.is_absolute():
            candidate = (Path.cwd() / p).resolve()
            if candidate.exists():
                binary = str(candidate)
            else:
                on_path = shutil.which(self.path)
                if on_path:
                    binary = on_path
                else:
                    report.add("validation", "warning",
                               f"veraPDF executable not found at '{self.path}'; "
                               f"install from https://verapdf.org/")
                    return True
        else:
            if not p.exists():
                report.add("validation", "warning",
                           f"veraPDF executable not found at '{self.path}'; "
                           f"install from https://verapdf.org/")
                return True
            binary = str(p)

        profile_flag = "--flavour" if self.profile == "ua1" else "--profile"
        flavour = "ua1" if self.profile == "ua1" else "wcag2.2"

        try:
            result = subprocess.run(
                [binary, "--format", "json", profile_flag, flavour, str(pdf_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                report.add("validation", "warning",
                           f"veraPDF output not parseable: {result.stdout[:300]}")
                return True

            # Parse the validation report
            jobs = data.get("report", {}).get("jobs", [])
            for job in jobs:
                v = job.get("validationResult", [{}])[0] if job.get("validationResult") else {}
                compliant = v.get("compliant", False)
                stmt = v.get("profileName", flavour)
                if compliant:
                    report.add("validation", "fixed",
                               f"veraPDF {stmt}: PASSED")
                else:
                    details = v.get("details", {})
                    failed = details.get("failedRules", 0)
                    report.add("validation", "warning",
                               f"veraPDF {stmt}: FAILED ({failed} rule violation(s))",
                               details={"failed_rules": failed,
                                        "raw": details})
                    # Surface individual rule failures
                    rules = details.get("ruleSummaries", [])
                    for rule in rules[:25]:
                        report.add(
                            "validation", "warning",
                            f"  {rule.get('specification', '')} "
                            f"{rule.get('clause', '')}-{rule.get('testNumber', '')}: "
                            f"{rule.get('description', '')[:120]}",
                            details={"rule": rule},
                        )
                    if self.fail_on_error:
                        return False
            return True
        except subprocess.TimeoutExpired:
            report.add("validation", "warning", "veraPDF timed out after 120s")
            return True
        except Exception as e:
            report.add("validation", "warning", f"veraPDF failed: {e}")
            return True
