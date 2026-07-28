
"""
Committee name → committee_id resolution.

Combines YAML-based resolution (H.Res/S.Res) with hardcoded fallback (CREC, Journal).
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

from .types import CommitteeRec, CommitteeResolutionError


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_committee_name(s: str) -> str:
    """Strip prefixes and compress for lookup. Used by YAML index."""
    s = s.strip()
    s = re.sub(r":\s*$", "", s)
    s = re.sub(r"\s+", " ", s)
    s = s.lower()
    s = re.sub(r"^committee\s+on\s+", "", s)
    s = re.sub(r"^committee\s+of\s+", "", s)
    s = re.sub(r"^the\s+", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


def _normalize_for_matcher(raw: str) -> Optional[str]:
    """Extract key from 'Committee on Agriculture', 'Joint Committee on Printing', etc."""
    if not raw:
        return None
    # Normalize Unicode apostrophes (U+2019, U+2018) to ASCII (U+0027)
    s = raw.strip().lower().replace("\u2019", "'").replace("\u2018", "'")
    s = re.sub(r"\s+", " ", s)
    prefixes = (
        "joint committee of congress on ",
        "joint committee on the ",
        "joint committee on ",
        "select committee on ",
        "special committee on ",
        "committee of the ",
        "committee of ",
        "committee on the ",
        "committee on ",
        "standing committee on the ",
        "standing committee on ",
    )
    for prefix in prefixes:
        if s.startswith(prefix):
            return s.removeprefix(prefix).strip().strip(".:") or None
    return s.strip(".:") or None


def congress_to_year_range(congress: int) -> tuple[int, int]:
    start = 1789 + (congress - 1) * 2
    return start, start + 1


# ---------------------------------------------------------------------------
# Hardcoded fallback (CREC, Journal, PDF artifacts)
# ---------------------------------------------------------------------------

HOUSE_COMMITTEE_NAME_TO_ID: Dict[str, str] = {
    "agriculture": "HSAG",
    "appropriations": "HSAP",
    "armed services": "HSAS",
    "budget": "HSBU",
    "education and the workforce": "HSED",
    "education and labor": "HSED",
    "energy and commerce": "HSIF",
    "ethics": "HSSO",
    "financial services": "HSBA",
    "foreign affairs": "HSFA",
    "homeland security": "HSHM",
    "house administration": "HSHA",
    "intelligence": "HLIG",
    "intelligence (select)": "HLIG",
    "permanent select committee on intelligence": "HLIG",
    "judiciary": "HSJU",
    "natural resources": "HSII",
    "oversight and government reform": "HSGO",
    "oversight and reform": "HSGO",
    "oversight and accountability": "HSGO",
    "rules": "HSRU",
    "science, space, and technology": "HSSY",
    "science and technology": "HSSY",
    "education and the w force": "HSED",
    "transportation and in structure": "HSPW",
    "transportation and infrastructure": "HSPW",
    "small business": "HSSM",
    "veterans' affairs": "HSVR",
    "veterans affairs": "HSVR",
    "ways and means": "HSWM",
    "united states and the chinese communist party": "HSZS",
    "strategic competition between the united states and the chinese communist party": "HSZS",
}

SENATE_COMMITTEE_NAME_TO_ID: Dict[str, str] = {
    "agriculture, nutrition, and forestry": "SSAF",
    "appropriations": "SSAP",
    "armed services": "SSAS",
    "banking, housing, and urban affairs": "SSBK",
    "budget": "SSBU",
    "commerce, science, and transportation": "SSCM",
    "energy and natural resources": "SSEG",
    "environment and public works": "SSEV",
    "finance": "SSFI",
    "foreign relations": "SSFR",
    "homeland security and governmental affairs": "SSGA",
    "health, education, labor, and pensions": "SSHR",
    "judiciary": "SSJU",
    "rules and administration": "SSRA",
    "small business and entrepreneurship": "SSSB",
    "veterans' affairs": "SSVA",
    "veterans affairs": "SSVA",
    "indian affairs": "SLIA",
    "intelligence": "SLIN",
    "aging": "SPAG",
    "ethics": "SLET",
}

JOINT_COMMITTEE_NAME_TO_ID: Dict[str, str] = {
    "printing": "JSPR",
    "library": "JSLC",
    "the library": "JSLC",
    "congress on the library": "JSLC",
    "taxation": "JSTX",
    "economic": "JSEC",
    "joint economic committee": "JSEC",
}

COMMITTEE_NAME_TO_ID_BY_BILL_TYPE: Dict[str, Dict[str, str]] = {
    "hres": HOUSE_COMMITTEE_NAME_TO_ID,
    "sres": SENATE_COMMITTEE_NAME_TO_ID,
}


# ---------------------------------------------------------------------------
# YAML index building
# ---------------------------------------------------------------------------


def _record_from_yaml(rec: dict) -> Optional[CommitteeRec]:
    cid = rec.get("thomas_id") or rec.get("committee_id")
    if not cid:
        return None
    name = rec.get("name", "")
    congresses_raw = rec.get("congresses")
    congresses: Optional[tuple[int, ...]] = None
    if congresses_raw is not None:
        congresses = tuple(sorted(int(c) for c in congresses_raw))
    return CommitteeRec(
        committee_id=cid,
        name=name,
        start_year=rec.get("start_year"),
        end_year=rec.get("end_year"),
        congresses=congresses,
    )


def _keys_for_record(rec: dict) -> List[str]:
    keys: List[str] = []
    name = rec.get("name", "")
    if name:
        k = normalize_committee_name(name)
        if k and k not in keys:
            keys.append(k)
    names = rec.get("names")
    if isinstance(names, dict):
        for v in names.values():
            if isinstance(v, list):
                for w in v:
                    k = normalize_committee_name(str(w))
                    if k and k not in keys:
                        keys.append(k)
            else:
                k = normalize_committee_name(str(v))
                if k and k not in keys:
                    keys.append(k)
    elif isinstance(names, str):
        k = normalize_committee_name(names)
        if k and k not in keys:
            keys.append(k)
    return keys


def build_committee_index(paths: List[Path]) -> Dict[str, List[CommitteeRec]]:
    """Load YAMLs and build normalized name -> [CommitteeRec] index."""
    import yaml

    idx: Dict[str, List[CommitteeRec]] = {}
    for p in paths:
        with p.open("r", encoding="utf-8") as f:
            obj = yaml.safe_load(f)
        if not isinstance(obj, list):
            continue
        for rec in obj:
            crec = _record_from_yaml(rec)
            if crec is None or not crec.name:
                continue
            for k in _keys_for_record(rec):
                if k:
                    idx.setdefault(k, []).append(crec)
    return idx


def is_active_in_congress(rec: CommitteeRec, congress: int) -> bool:
    if rec.congresses is not None:
        return congress in rec.congresses
    y0, y1 = congress_to_year_range(congress)
    if rec.start_year is None and rec.end_year is None:
        return True
    if rec.start_year is not None and rec.end_year is None:
        return rec.start_year <= y1
    if rec.start_year is None and rec.end_year is not None:
        return rec.end_year >= y0
    # Both start_year and end_year are set (all other cases returned above)
    start = rec.start_year
    end = rec.end_year
    if start is None or end is None:
        return True
    return not (end < y0 or start > y1)


# Congress-scoped aliases for header variants the YAML doesn't index under.
_RESOLUTION_ALIASES: Dict[tuple[int, str], str] = {
    (113, "housecommitteeoneducationandtheworkforce"): "educationandtheworkforce",
    (113, "housecommitteeonscienceandtechnology"): "housecommitteeonsciencespaceandtechnology",
    # CREC split-header artifact: "Committees on the Judiciary and Oversight and Government Reform"
    # may emit "Committee on Oversight"/"Committee on Government Reform" as separate fragments.
    (113, "oversight"): "oversightandgovernmentreform",
    (113, "governmentreform"): "oversightandgovernmentreform",
}


# ---------------------------------------------------------------------------
# Resolution entry points
# ---------------------------------------------------------------------------


def resolve_from_index(
    header_text: str,
    congress: int,
    idx: Dict[str, List[CommitteeRec]],
    chamber: str,
) -> str:
    """
    Resolve committee name to committee_id using YAML-built index.
    chamber must be 'house' or 'senate'.
    """
    k = normalize_committee_name(header_text)
    if k not in idx:
        k = _RESOLUTION_ALIASES.get((congress, k), k)
    cands = idx.get(k)
    if not cands:
        raise CommitteeResolutionError(f"no committee match: {header_text}")

    if chamber == "house":
        cands = [c for c in cands if c.committee_id.startswith("H") or c.committee_id.startswith("J")]
    elif chamber == "senate":
        cands = [c for c in cands if c.committee_id.startswith("S") or c.committee_id.startswith("J")]
    if not cands:
        raise CommitteeResolutionError(
            f"no committee for chamber={chamber}: {header_text}"
        )

    valid = [c for c in cands if is_active_in_congress(c, congress)]
    if not valid:
        raise CommitteeResolutionError(
            f"no active match for congress={congress}: {header_text}"
        )
    distinct_ids = list(dict.fromkeys(c.committee_id for c in valid))
    if len(distinct_ids) > 1:
        raise CommitteeResolutionError(
            f"ambiguous for congress={congress}: {header_text} -> {distinct_ids}"
        )
    return distinct_ids[0]


def committee_name_to_id(raw: str, bill_type: str = "hres") -> Optional[str]:
    """
    Resolve committee name to committee_id using hardcoded maps.
    Used by CREC parser, Journal parser when YAML index not available.
    """
    key = _normalize_for_matcher(raw)
    if not key:
        return None
    cmap = COMMITTEE_NAME_TO_ID_BY_BILL_TYPE.get(
        bill_type, HOUSE_COMMITTEE_NAME_TO_ID
    )
    cid = cmap.get(key)
    if cid:
        return cid
    cid = JOINT_COMMITTEE_NAME_TO_ID.get(key)
    if cid:
        return cid
    # Fuzzy fallback for PDF artifacts
    compressed = re.sub(r"[\s',]+", "", key)
    for name, cid in cmap.items():
        if re.sub(r"[\s',]+", "", name) == compressed:
            return cid
    for name, cid in JOINT_COMMITTEE_NAME_TO_ID.items():
        if re.sub(r"[\s',]+", "", name) == compressed:
            return cid
    return None
