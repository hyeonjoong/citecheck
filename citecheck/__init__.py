"""citecheck — verify manuscript citations against Crossref.

Catches broken DOIs, metadata mismatches (title/author/year), and retracted
references before they reach a reviewer.
"""

__version__ = "0.1.0"

from .core import CrossrefClient, check_reference, CheckResult
from .parsers import parse_references, Reference

__all__ = [
    "CrossrefClient",
    "check_reference",
    "CheckResult",
    "parse_references",
    "Reference",
    "__version__",
]
