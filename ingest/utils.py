from datetime import date
from typing import Dict, Tuple

# Targeted Congresses
CONGRESS_DATES: Dict[int, Tuple[date, date]] = {
    113: (date(2013, 1, 3), date(2015, 1, 3)),
    114: (date(2015, 1, 3), date(2017, 1, 3)),
    115: (date(2017, 1, 3), date(2019, 1, 3)),
    116: (date(2019, 1, 3), date(2021, 1, 3)),
    117: (date(2021, 1, 3), date(2023, 1, 3)),
    118: (date(2023, 1, 3), date(2025, 1, 3)),
    119: (date(2025, 1, 3), date(2027, 1, 3)),
}

def get_congress_for_date(d: date) -> int | None:
    for congress_no, (start, end) in CONGRESS_DATES.items():
        if start <= d < end:
            return congress_no
    return None

def get_date_range_for_congress(congress_no: int) -> str | None:
    if congress_no not in CONGRESS_DATES:
        return None
    start, end = CONGRESS_DATES[congress_no]
    return f"[{start.isoformat()}, {end.isoformat()})"
