"""Cloud provider scanners for detecting zombie resources."""

import contextlib

from zombie_hunter.scanners import aws as _aws  # noqa: F401
from zombie_hunter.scanners.base import BaseScanner, ScannerRegistry

# Conditional imports for optional cloud providers
with contextlib.suppress(ImportError):
    from zombie_hunter.scanners import gcp as _gcp  # noqa: F401

with contextlib.suppress(ImportError):
    from zombie_hunter.scanners import azure as _azure  # noqa: F401

__all__ = ["BaseScanner", "ScannerRegistry"]
