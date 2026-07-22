import re
import unicodedata
from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass


FORMAL_TO_NICKNAMES: dict[str, tuple[str, ...]] = {
    "james": ("jim", "jimmy", "jamie"),
    "william": ("bill", "will", "billy"),
    "robert": ("bob", "rob", "bobby"),
    "richard": ("dick", "rick"),
    "john": ("jack", "johnny"),
    "joseph": ("joe", "joey"),
    "thomas": ("tom", "tommy"),
    "christopher": ("chris",),
    "daniel": ("dan", "danny"),
    "michael": ("mike", "mickey"),
    "matthew": ("matt",),
    "anthony": ("tony",),
    "nicholas": ("nick",),
    "charles": ("charlie", "chuck"),
}

@dataclass
class MemberCandidate:
    bioguide_id: str
    first_name: str
    last_name: str
    state: str
    district: Optional[int]
    nickname: Optional[str] = None
    official_full_name: Optional[str] = None
    valid_daterange: object | None = None
    party: Optional[str] = None  # 'D' or 'R'
    gender: Optional[str] = None  # 'M' or 'F'

class MemberResolutionError(Exception):
    pass

def normalize_name_for_match(name: str) -> str:
    """Basic normalization for fuzzy matching."""
    # NFD decomposes é -> e + combining accent; strip combining chars
    s = unicodedata.normalize("NFD", name.lower().strip().replace("ı", "i"))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def given_names_equivalent(left: str, right: str) -> bool:
    """Compare formal given names and common nicknames in either direction."""
    left_tokens = normalize_name_for_match(left).split()
    right_tokens = normalize_name_for_match(right).split()
    if not left_tokens or not right_tokens:
        return False
    left_normalized = left_tokens[0]
    right_normalized = right_tokens[0]

    def canonical(name: str) -> str:
        for formal, nicknames in FORMAL_TO_NICKNAMES.items():
            if name == formal or name in nicknames:
                return formal
        return name

    return canonical(left_normalized) == canonical(right_normalized)


def _strip_suffixes_for_lookup(name: str) -> str:
    """
    Remove common suffix tokens before member lookup.
    Used only for matching; does not alter canonical stored name.
    """
    s = name.strip()
    # Remove suffix tokens: Jr., Jr, Sr., Sr, II, III, IV, M.D., MD, Ph.D., PhD
    s = re.sub(
        r',?\s*(?:Jr\.?|Sr\.?|II|III|IV|M\.D\.?|MD|Ph\.D\.?|PhD)\s*$',
        '',
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r'[.,;:]+$', '', s)  # Remove trailing punctuation
    s = re.sub(r'\s+', ' ', s).strip()  # Collapse double spaces
    return s


def _party_code_to_char(party_code: Optional[int]) -> Optional[str]:
    """Map member_service.party_code (ICPSR-style) to 'D' or 'R' for disambiguation."""
    if party_code is None:
        return None
    if party_code in (100, 328):  # Democrat, Independent caucusing with D
        return "D"
    if party_code == 200:
        return "R"
    return None


def _normalize_gender(g: Optional[str]) -> Optional[str]:
    """Normalize member.gender to 'M' or 'F'."""
    if not g:
        return None
    g = (g or "").strip().upper()
    if g in ("M", "MALE"):
        return "M"
    if g in ("F", "FEMALE"):
        return "F"
    return None


class MemberResolver:
    def __init__(self, conn):
        self.conn = conn
        self._cache: Dict[Tuple[int, str], List[MemberCandidate]] = {}

    def _get_candidates(self, congress: int, chamber: str, event_date: Optional[str] = None) -> List[MemberCandidate]:
        cache_key = (congress, chamber, event_date or "")
        if cache_key in self._cache:
            return self._cache[cache_key]

        with self.conn.cursor() as cur:
            if event_date:
                cur.execute("""
                    SELECT DISTINCT m.bioguide_id, m.first_name, m.last_name, s.state, s.district,
                           m.nickname, m.official_full_name, s.valid_daterange, s.party_code, m.gender
                    FROM member m
                    JOIN member_service s ON m.bioguide_id = s.bioguide_id
                    WHERE s.congress_no = %s AND s.chamber = %s
                      AND %s::date <@ s.valid_daterange
                """, (congress, chamber, event_date))
            else:
                cur.execute("""
                    SELECT DISTINCT m.bioguide_id, m.first_name, m.last_name, s.state, s.district,
                           m.nickname, m.official_full_name, s.valid_daterange, s.party_code, m.gender
                    FROM member m
                    JOIN member_service s ON m.bioguide_id = s.bioguide_id
                    WHERE s.congress_no = %s AND s.chamber = %s
                """, (congress, chamber))
            rows = cur.fetchall()
            # Dedupe by bioguide_id (same member can have multiple member_service rows, e.g. district change)
            by_bio: Dict[str, MemberCandidate] = {}
            for r in rows:
                bid = r[0]
                if bid not in by_bio:
                    by_bio[bid] = MemberCandidate(
                        bioguide_id=bid,
                        first_name=r[1],
                        last_name=r[2],
                        state=r[3],
                        district=r[4],
                        nickname=r[5],
                        official_full_name=r[6],
                        valid_daterange=r[7],
                        party=_party_code_to_char(r[8]),
                        gender=_normalize_gender(r[9]),
                    )
            candidates = list(by_bio.values())
            self._cache[cache_key] = candidates
            return candidates

    def resolve(
        self,
        raw_name: str,
        congress: int,
        chamber: str,
        party: Optional[str] = None,
        state: Optional[str] = None,
        gender: Optional[str] = None,
        event_date: Optional[str] = None,
    ) -> str:
        """
        Resolves a name string (e.g. 'Mr. Goodlatte') to a bioguide_id.
        chamber is 'H' or 'S'.
        party is 'R' or 'D'. state is 2-letter code or full name (e.g. 'NY' or 'New York').
        gender is 'M' or 'F' for disambiguation. Disambiguation order: state, then party, then gender.
        """
        original = raw_name
        # 1. Clean honorifics and typical prefixes
        name = re.sub(
            r'^(?:Mr\.|Mrs\.|Ms\.|Miss|Dr\.|Representative|Senator|'
            r'the Honorable|Congressman|Congresswoman|the Gentlem[ae]n from|'
            r'the Gentlewoman from)\s*',
            '',
            raw_name,
            flags=re.IGNORECASE,
        )
        name = re.sub(r'^Mr\.(?=[A-Z])', '', name)
        
        # 2. Strip suffixes for lookup (Jr., Sr., II, III, IV, M.D., Ph.D., etc.)
        name = _strip_suffixes_for_lookup(name)

        # 3. Extract state from name: "Rogers of Alabama"; context state param overrides
        state_match = re.search(r'(.+?)\s+of\s+([A-Za-z\s]+)$', name, flags=re.IGNORECASE)
        qual_state = None
        if state_match:
            name = state_match.group(1).strip()
            qual_state = state_match.group(2).strip().lower()
        if state is not None and state.strip():
            # Caller passed state (e.g. from CREC "gentleman from New York")
            qual_state = state.strip().lower() if len(state.strip()) > 2 else state.strip().lower()

        # 4. Get candidates
        candidates = self._get_candidates(congress, chamber, event_date=event_date)
        clean_name = normalize_name_for_match(name)
        if not clean_name:
            raise MemberResolutionError(f"Could not resolve member name: {original}")
        
        # 5. Try Exact Last Name Match
        matches = [c for c in candidates if normalize_name_for_match(c.last_name) == clean_name]

        # 5b. Compound-surname suffix fallback:
        # "Ms. Beutler" should match "Herrera Beutler" when unique in chamber/congress.
        if not matches and " " not in clean_name:
            suffix_matches = [
                c
                for c in candidates
                if normalize_name_for_match(c.last_name).split()[-1] == clean_name
            ]
            if len(suffix_matches) == 1:
                matches = suffix_matches
        
        # 6. Try Full Name Match (if multiple words)
        if not matches and " " in clean_name:
            # Try to find a member where last_name is the last part of the string
            # And first_name is the first part
            for c in candidates:
                c_last = normalize_name_for_match(c.last_name)
                official = normalize_name_for_match(c.official_full_name or "")
                prefix = clean_name[: -len(c_last)].strip() if clean_name.endswith(c_last) else ""
                given_names = [c.first_name, c.nickname or ""]
                if official and clean_name == official:
                    matches.append(c)
                elif prefix and any(given_names_equivalent(value, prefix) for value in given_names if value):
                    matches.append(c)
            
            # If still no matches, try just last word of clean_name vs last_name
            if not matches:
                last_word = clean_name.split()[-1]
                matches = [c for c in candidates if normalize_name_for_match(c.last_name) == last_word]
                # Check prefix for first name (including common nicknames: Jim->James, Bill->William, etc.)
                if matches:
                    prefix = " ".join(clean_name.split()[:-1])
                    prefix_word = prefix.split()[0] if prefix else ""
                    def first_name_matches(c_first: str, p: str) -> bool:
                        cf = normalize_name_for_match(c_first)
                        pp_full = normalize_name_for_match(p)
                        if not pp_full:
                            return True
                        # Use only the first token from the prefix as the given name;
                        # this lets us handle patterns like "Michael D. Bishop" where
                        # the middle initial should be ignored.
                        pp = pp_full.split()[0]
                        if cf in pp or pp in cf:
                            return True
                        return given_names_equivalent(cf, pp)
                    matches = [
                        c for c in matches
                        if not prefix
                        or first_name_matches(c.first_name, prefix)
                        or (c.nickname and first_name_matches(c.nickname, prefix))
                        or normalize_name_for_match(c.official_full_name or "") == clean_name
                    ]

        # 7. Disambiguation: state first, then party, then gender
        state_code = self._resolve_state_to_code(qual_state) if qual_state else None
        if len(matches) > 1:
            # State first
            if state_code:
                state_matches = [m for m in matches if m.state == state_code]
                if len(state_matches) == 1:
                    return state_matches[0].bioguide_id
                if state_matches:
                    matches = state_matches
            # Party second
            if len(matches) > 1 and party:
                party_matches = [m for m in matches if m.party == party]
                if len(party_matches) == 1:
                    return party_matches[0].bioguide_id
                if party_matches:
                    matches = party_matches
            # Gender third
            if len(matches) > 1 and gender:
                gender_matches = [m for m in matches if m.gender == gender]
                if len(gender_matches) == 1:
                    return gender_matches[0].bioguide_id
                if gender_matches:
                    matches = gender_matches

        # If we have 0 matches but a state was provided, try a broader search within state
        if not matches and state_code:
            matches = [c for c in candidates if c.state == state_code and normalize_name_for_match(c.last_name) == clean_name]

        if len(matches) == 1:
            return matches[0].bioguide_id
            
        if len(matches) > 1:
            raise MemberResolutionError(f"Ambiguous member name: {original} (matches: {[m.bioguide_id for m in matches]})")
            
        raise MemberResolutionError(f"Could not resolve member name: {original}")

    def _resolve_state_to_code(self, state_name: str) -> Optional[str]:
        if not state_name or not state_name.strip():
            return None
        s = state_name.strip()
        if len(s) == 2:
            return s.upper()
        # Full name map
        states = {
            'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR', 'california': 'CA',
            'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE', 'florida': 'FL', 'georgia': 'GA',
            'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA',
            'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
            'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS', 'missouri': 'MO',
            'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV', 'new hampshire': 'NH', 'new jersey': 'NJ',
            'new mexico': 'NM', 'new york': 'NY', 'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH',
            'oklahoma': 'OK', 'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
            'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT', 'vermont': 'VT',
            'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
            'district of columbia': 'DC', 'american samoa': 'AS', 'guam': 'GU', 'northern mariana islands': 'MP',
            'puerto rico': 'PR', 'virgin islands': 'VI'
        }
        return states.get(state_name.lower())
