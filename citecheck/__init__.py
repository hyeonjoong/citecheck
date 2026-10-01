"""citecheck — verify manuscript citations against Crossref.

Catches broken DOIs, metadata mismatches (title/author/year), and retracted
references before they reach a reviewer.
"""

from ._version import __version__
from .core import CrossrefClient, PubMedClient, check_reference, CheckResult
from .parsers import parse_references, Reference

__all__ = [
    "CrossrefClient",
    "PubMedClient",
    "check_reference",
    "CheckResult",
    "parse_references",
    "Reference",
    "__version__",
]
