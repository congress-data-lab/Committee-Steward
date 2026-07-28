import hashlib
from pathlib import Path
from datetime import date, timedelta
from typing import List, Dict, Any

import yaml
from psycopg2.extras import Json

from db.connection import get_connection
from ingest.utils import CONGRESS_DATES

PARSER_ID = "ingest/load_members.py"
REFERENCE_REPOSITORY = "unitedstates/congress-legislators"

# Party label from YAML term -> stable integer party code.
PARTY_TO_CODE = {
    "Democrat": 100,
    "Democratic": 100,
    "Republican": 200,
    "Independent": 328,
    "Ind": 328,
    "Libertarian": 326,
    "Conservative": 329,
    "Progressive": 331,
    "Nonpartisan": 327,
    "Unknown": 0,
}


def _term_affiliation_periods(
    term: Dict[str, Any],
) -> list[tuple[date, date, int | None, int | None]]:
    """Return half-open party/caucus periods for one congressional term."""
    try:
        term_start = date.fromisoformat(term["start"])
        term_end = date.fromisoformat(term["end"])
    except (KeyError, TypeError, ValueError):
        return []

    affiliations = term.get("party_affiliations") or [
        {
            "start": term["start"],
            "end": term["end"],
            "party": term.get("party"),
            "caucus": term.get("caucus"),
        }
    ]
    periods: list[tuple[date, date, int | None, int | None]] = []
    for affiliation in affiliations:
        try:
            start = max(term_start, date.fromisoformat(affiliation["start"]))
            end = min(term_end, date.fromisoformat(affiliation["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if start >= end:
            continue
        party_label = affiliation.get("party") or term.get("party")
        caucus_label = affiliation.get("caucus") or party_label
        periods.append(
            (
                start,
                end,
                PARTY_TO_CODE.get(party_label) if party_label else None,
                PARTY_TO_CODE.get(caucus_label) if caucus_label else None,
            )
        )
    periods.sort(key=lambda item: (item[0], item[1]))
    # Upstream switch metadata uses both shared half-open boundaries and, for
    # five modern records, an inclusive prior end followed by the next day.
    # Close only those one-day seams; preserve larger source gaps as unknown.
    for index in range(len(periods) - 1):
        current = periods[index]
        following = periods[index + 1]
        if current[1] + timedelta(days=1) == following[0]:
            periods[index] = (current[0], following[0], current[2], current[3])
    return periods

JURISDICTIONS = [
    ('AL', 'Alabama', 'state'), ('AK', 'Alaska', 'state'), ('AZ', 'Arizona', 'state'),
    ('AR', 'Arkansas', 'state'), ('CA', 'California', 'state'), ('CO', 'Colorado', 'state'),
    ('CT', 'Connecticut', 'state'), ('DE', 'Delaware', 'state'), ('FL', 'Florida', 'state'),
    ('GA', 'Georgia', 'state'), ('HI', 'Hawaii', 'state'), ('ID', 'Idaho', 'state'),
    ('IL', 'Illinois', 'state'), ('IN', 'Indiana', 'state'), ('IA', 'Iowa', 'state'),
    ('KS', 'Kansas', 'state'), ('KY', 'Kentucky', 'state'), ('LA', 'Louisiana', 'state'),
    ('ME', 'Maine', 'state'), ('MD', 'Maryland', 'state'), ('MA', 'Massachusetts', 'state'),
    ('MI', 'Michigan', 'state'), ('MN', 'Minnesota', 'state'), ('MS', 'Mississippi', 'state'),
    ('MO', 'Missouri', 'state'), ('MT', 'Montana', 'state'), ('NE', 'Nebraska', 'state'),
    ('NV', 'Nevada', 'state'), ('NH', 'New Hampshire', 'state'), ('NJ', 'New Jersey', 'state'),
    ('NM', 'New Mexico', 'state'), ('NY', 'New York', 'state'), ('NC', 'North Carolina', 'state'),
    ('ND', 'North Dakota', 'state'), ('OH', 'Ohio', 'state'), ('OK', 'Oklahoma', 'state'),
    ('OR', 'Oregon', 'state'), ('PA', 'Pennsylvania', 'state'), ('RI', 'Rhode Island', 'state'),
    ('SC', 'South Carolina', 'state'), ('SD', 'South Dakota', 'state'), ('TN', 'Tennessee', 'state'),
    ('TX', 'Texas', 'state'), ('UT', 'Utah', 'state'), ('VT', 'Vermont', 'state'),
    ('VA', 'Virginia', 'state'), ('WA', 'Washington', 'state'), ('WV', 'West Virginia', 'state'),
    ('WI', 'Wisconsin', 'state'), ('WY', 'Wyoming', 'state'), ('DC', 'District of Columbia', 'district'),
    ('AS', 'American Samoa', 'territory'), ('GU', 'Guam', 'territory'), ('MP', 'Northern Mariana Islands', 'territory'),
    ('PR', 'Puerto Rico', 'territory'), ('VI', 'Virgin Islands', 'territory')
]

def load_yaml(path: Path) -> List[Dict[str, Any]]:
    print(f"Reading {path}...")
    try:
        from yaml import CSafeLoader as Loader
    except ImportError:
        from yaml import SafeLoader as Loader
        
    with path.open("r", encoding="utf-8") as f:
        return yaml.load(f, Loader=Loader)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_reference_document(cur, path: Path) -> tuple[int, int]:
    """Return stable source and document IDs for one exact YAML file."""
    content_hash = file_sha256(path)
    cur.execute(
        """
        SELECT source_document_id, source_id
        FROM source_document
        WHERE content_hash = %s
        """,
        (content_hash,),
    )
    existing_document = cur.fetchone()
    if existing_document and existing_document[1] is not None:
        return existing_document[1], existing_document[0]

    source_name = f"{REFERENCE_REPOSITORY}/{path.name}"
    version_tag = f"sha256:{content_hash}"
    cur.execute(
        """
        SELECT source_id
        FROM source
        WHERE source_type = 'YAML'
          AND source_name = %s
          AND version_tag = %s
        ORDER BY source_id
        LIMIT 1
        """,
        (source_name, version_tag),
    )
    source_row = cur.fetchone()
    if source_row:
        source_id = source_row[0]
    else:
        cur.execute(
            """
            INSERT INTO source (source_type, source_name, version_tag)
            VALUES ('YAML', %s, %s)
            RETURNING source_id
            """,
            (source_name, version_tag),
        )
        source_id = cur.fetchone()[0]

    metadata = {
        "repository": REFERENCE_REPOSITORY,
        "filename": path.name,
        "sha256": content_hash,
        "byte_size": path.stat().st_size,
    }
    if existing_document:
        cur.execute(
            """
            UPDATE source_document
            SET source_id = %s,
                external_id = COALESCE(external_id, %s),
                url = COALESCE(url, %s),
                raw_json = COALESCE(raw_json, %s)
            WHERE source_document_id = %s
            RETURNING source_document_id
            """,
            (
                source_id,
                path.name,
                f"https://github.com/{REFERENCE_REPOSITORY}/blob/main/{path.name}",
                Json(metadata),
                existing_document[0],
            ),
        )
    else:
        cur.execute(
            """
            INSERT INTO source_document (
                source_id, external_id, url, raw_json, content_hash
            ) VALUES (%s, %s, %s, %s, %s)
            RETURNING source_document_id
            """,
            (
                source_id,
                path.name,
                f"https://github.com/{REFERENCE_REPOSITORY}/blob/main/{path.name}",
                Json(metadata),
                content_hash,
            ),
        )
    return source_id, cur.fetchone()[0]

def run():
    print("Starting...")
    current_path = Path("data/reference/legislators-current.yaml")
    historical_path = Path("data/reference/legislators-historical.yaml")

    datasets = [
        (current_path, load_yaml(current_path)),
        (historical_path, load_yaml(historical_path)),
    ]

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            dataset_sources = {
                path: _ensure_reference_document(cur, path)
                for path, _records in datasets
            }

            # 1. Populating Jurisdictions
            print("Populating jurisdictions...")
            cur.executemany(
                "INSERT INTO jurisdiction (code, name, type) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                JURISDICTIONS
            )

            # 2. Populating Members and Service
            total_legislators = sum(len(records) for _path, records in datasets)
            print(f"Processing {total_legislators} legislators...")
            count = 0
            for dataset_path, legislators in datasets:
              source_id, source_document_id = dataset_sources[dataset_path]
              parsed_source = f"YAML:{dataset_path.name}"
              for leg in legislators:
                # First, determine if they have relevant service
                has_relevant_service = False
                relevant_terms = []
                
                for term in leg.get("terms", []):
                    try:
                        start_date = date.fromisoformat(term.get("start"))
                        end_date = date.fromisoformat(term.get("end"))
                    except (ValueError, TypeError):
                        continue
                        
                    for congress_no, (c_start, c_end) in CONGRESS_DATES.items():
                        # Intersection of [start_date, end_date] and [c_start, c_end]
                        intersect_start = max(start_date, c_start)
                        intersect_end = min(end_date, c_end)

                        if intersect_start < intersect_end:
                            has_relevant_service = True
                            relevant_terms.append((term, start_date, end_date))
                            break
                            
                if not has_relevant_service:
                    continue

                ids = leg.get("id", {})
                bioguide_id = ids.get("bioguide")
                
                if not bioguide_id:
                    continue

                name = leg.get("name", {})
                first_name = name.get("first")
                last_name = name.get("last")
                nickname = name.get("nickname")
                official_full_name = name.get("official_full")
                icpsr_id = ids.get("icpsr")
                govtrack_id = ids.get("govtrack")

                # Insert member
                cur.execute("""
                    INSERT INTO member (
                        bioguide_id, first_name, last_name, nickname, official_full_name,
                        icpsr_id, govtrack_id, parsed_source, parser_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (bioguide_id) DO UPDATE SET
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        nickname = EXCLUDED.nickname,
                        official_full_name = EXCLUDED.official_full_name,
                        icpsr_id = EXCLUDED.icpsr_id,
                        govtrack_id = EXCLUDED.govtrack_id,
                        parsed_source = EXCLUDED.parsed_source,
                        parser_id = EXCLUDED.parser_id,
                        parsed_at = now()
                """, (
                    bioguide_id, first_name, last_name, nickname, official_full_name,
                    icpsr_id, govtrack_id, parsed_source, PARSER_ID,
                ))

                # Process terms and preserve within-term party/caucus changes.
                for term in leg.get("terms", []):
                    chamber = "H" if term.get("type") == "rep" else "S"
                    state = term.get("state")
                    district = term.get("district") # None is fine for Senate
                    for start_date, end_date, party_code, caucus_party_code in _term_affiliation_periods(term):
                        for congress_no, (c_start, c_end) in CONGRESS_DATES.items():
                            # Intersection of affiliation and Congress half-open ranges.
                            intersect_start = max(start_date, c_start)
                            intersect_end = min(end_date, c_end)

                            if intersect_start < intersect_end:
                                range_str = f"[{intersect_start.isoformat()}, {intersect_end.isoformat()})"
                                cur.execute("""
                                    INSERT INTO member_service (
                                        bioguide_id, chamber, state, district, congress_no,
                                        valid_daterange, party_code, caucus_party_code,
                                        source_id, source_document_id, parsed_source, parser_id
                                    )
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                    ON CONFLICT (
                                        bioguide_id, chamber, state,
                                        (COALESCE(district, -1)), congress_no, valid_daterange
                                    ) DO UPDATE SET
                                        party_code = EXCLUDED.party_code,
                                        caucus_party_code = EXCLUDED.caucus_party_code,
                                        source_id = EXCLUDED.source_id,
                                        source_document_id = EXCLUDED.source_document_id,
                                        parsed_source = EXCLUDED.parsed_source,
                                        parser_id = EXCLUDED.parser_id,
                                        parsed_at = now()
                                """, (
                                    bioguide_id, chamber, state, district, congress_no,
                                    range_str, party_code, caucus_party_code,
                                    source_id, source_document_id, parsed_source, PARSER_ID,
                                ))
                
                count += 1
                if count % 500 == 0:
                    print(f"Processed {count} members...")

        conn.commit()
        print(f"Successfully loaded {count} members and their service records.")
    except Exception as e:
        conn.rollback()
        print(f"Error: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    run()
