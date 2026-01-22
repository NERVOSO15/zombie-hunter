"""Cloud provider scanners for detecting zombie resources."""

from zombie_hunter.scanners.base import BaseScanner, ScannerRegistry

# Import scanners to register them
from zombie_hunter.scanners import aws

# Conditional imports for optional cloud providers
try:
    from zombie_hunter.scanners import gcp
except ImportError:
    pass  # GCP SDK not installed

try:
    from zombie_hunter.scanners import azure
except ImportError:
    pass  # Azure SDK not installed

__all__ = ["BaseScanner", "ScannerRegistry"]
