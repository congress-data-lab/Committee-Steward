"""
Committee-related types.
"""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class CommitteeRec:
    """Single committee record from YAML (current or historical)."""

    committee_id: str
    name: str
    start_year: Optional[int] = None
    end_year: Optional[int] = None
    congresses: Optional[Tuple[int, ...]] = None  # when set, "active" = congress in this list


class CommitteeResolutionError(Exception):
    """Raised when committee name cannot be resolved to a committee_id."""

    pass


# Whitelist of committees we extract events for (authorizing + appropriations + budget).
# Used by filter_authorizing_committees, extract_resolutions_docling, etc.
HOUSE_AUTHORIZING_COMMITTEE_IDS: frozenset[str] = frozenset({
    "HSAG", "HSAS", "HSED", "HSIF", "HSSO", "HSBA", "HSFA", "HSHM", "HSHA",
    "HSJU", "HSII", "HSGO", "HSRU", "HSSY", "HSSM", "HSPW", "HSVR", "HSWM",
    "HLIG", "HSAP", "HSBU",
})

SENATE_AUTHORIZING_COMMITTEE_IDS: frozenset[str] = frozenset({
    "SSAF", "SSAS", "SSBK", "SSCM", "SSEG", "SSEV", "SSFI", "SSFR", "SSHR",
    "SSGA", "SSJU", "SSRA", "SSSB", "SSVA", "SLIN", "SLIA", "SSAP", "SSBU",
})
