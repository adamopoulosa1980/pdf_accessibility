"""
PDF Accessibility Remediation Pipeline
=======================================
A modular toolkit for programmatically fixing common WCAG 2.2 / PDF/UA-1
issues in PDF documents.

Each fixer targets a specific WCAG checkpoint category. Items requiring
human judgment (colors, alt text overrides, table headers) are externalized
to the YAML config.
"""
from .pipeline import RemediationPipeline
from .config import Config

__version__ = "0.1.0"
__all__ = ["RemediationPipeline", "Config"]
