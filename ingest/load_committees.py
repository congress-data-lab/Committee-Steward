
import yaml
from pathlib import Path
from typing import Dict, List, Any
from core.committees.types import HOUSE_AUTHORIZING_COMMITTEE_IDS, SENATE_AUTHORIZING_COMMITTEE_IDS
from db.connection import get_connection
from ingest.utils import CONGRESS_DATES

# Whitelist all targeted committees
WHITELIST = HOUSE_AUTHORIZING_COMMITTEE_IDS | SENATE_AUTHORIZING_COMMITTEE_IDS

def get_congress_range_string(congresses: list[int]) -> str:
    valid_congresses = [c for c in congresses if c in CONGRESS_DATES]
    if not valid_congresses:
        return ""
    start_date = CONGRESS_DATES[min(valid_congresses)][0]
    end_date = CONGRESS_DATES[max(valid_congresses)][1]
    return f"[{start_date.isoformat()}, {end_date.isoformat()})"

def load_yaml(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def run():
    print("Loading committees from YAML...")
    current_path = Path("data/reference/committees-current.yaml")
    historical_path = Path("data/reference/committees-historical.yaml")

    current_data = load_yaml(current_path)
    historical_data = load_yaml(historical_path)

    # Combined index: committee_id -> { 'type': ..., 'names': { congress: name } }
    committees_info: Dict[str, Dict[str, Any]] = {}

    def process_entry(entry: Dict[str, Any], is_current: bool = False):
        cid = entry.get("thomas_id")
        if not cid or cid not in WHITELIST:
            return

        if cid not in committees_info:
            committees_info[cid] = {
                "type": entry.get("type"),
                "is_joint": entry.get("type") == "joint",
                "congress_names": {},
                "all_congresses": set(),
                "base_name": entry.get("name")
            }
        
        # Handle names
        entry_names = entry.get("names", {})
        entry_congresses = entry.get("congresses", [])
        
        if is_current and not entry_congresses:
            # Current committees in this dataset are usually 113-119 active
            entry_congresses = [113, 114, 115, 116, 117, 118, 119]

        for c in entry_congresses:
            if c in CONGRESS_DATES:
                committees_info[cid]["all_congresses"].add(c)
                # Determine name for this congress: check entry_names dict first, then top-level name
                name = entry_names.get(c) or entry.get("name")
                if name:
                    committees_info[cid]["congress_names"][c] = name

    for entry in current_data:
        process_entry(entry, is_current=True)
    for entry in historical_data:
        process_entry(entry, is_current=False)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Ensure reference data
            cur.execute("INSERT INTO chamber (chamber, name) VALUES ('H', 'House'), ('S', 'Senate') ON CONFLICT DO NOTHING")
            for congress_no, dates in CONGRESS_DATES.items():
                cur.execute(
                    "INSERT INTO congress (congress_no, start_date, end_date) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (congress_no, dates[0], dates[1])
                )

            # Insert committees and their name history
            for cid, info in committees_info.items():
                if not info["all_congresses"]:
                    continue
                
                # Clear existing name history for this committee to avoid duplicates
                cur.execute("DELETE FROM committee_name_history WHERE committee_code = %s", (cid,))

                chamber_code = "H" if info["type"] == "house" else "S"
                total_range = get_congress_range_string(list(info["all_congresses"]))
                
                print(f"Upserting committee {cid} ({total_range})")
                cur.execute("""
                    INSERT INTO committee (committee_code, chamber, is_joint, valid_daterange, parsed_source, parser_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (committee_code) DO UPDATE SET
                        valid_daterange = EXCLUDED.valid_daterange,
                        parsed_at = now()
                """, (cid, chamber_code, info["is_joint"], total_range, "YAML", "load_committees.py"))

                # Process name history
                # Group consecutive congresses with the same name
                sorted_congresses = sorted(list(info["all_congresses"]))
                if not sorted_congresses:
                    continue

                current_name = None
                current_group = []
                
                for c in sorted_congresses:
                    name = info["congress_names"].get(c)
                    if name != current_name:
                        if current_group:
                            # Flush previous group
                            name_range = get_congress_range_string(current_group)
                            cur.execute("""
                                INSERT INTO committee_name_history (committee_code, name, valid_daterange, parsed_source, parser_id)
                                VALUES (%s, %s, %s, %s, %s)
                            """, (cid, current_name, name_range, "YAML", "load_committees.py"))
                        
                        current_name = name
                        current_group = [c]
                    else:
                        # Check if it's consecutive
                        if c == current_group[-1] + 1:
                            current_group.append(c)
                        else:
                            # Not consecutive, flush and start new
                            name_range = get_congress_range_string(current_group)
                            cur.execute("""
                                INSERT INTO committee_name_history (committee_code, name, valid_daterange, parsed_source, parser_id)
                                VALUES (%s, %s, %s, %s, %s)
                            """, (cid, current_name, name_range, "YAML", "load_committees.py"))
                            current_group = [c]

                if current_group:
                    name_range = get_congress_range_string(current_group)
                    cur.execute("""
                        INSERT INTO committee_name_history (committee_code, name, valid_daterange, parsed_source, parser_id)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (cid, current_name, name_range, "YAML", "load_committees.py"))

        conn.commit()
        print("Success.")
    except Exception as e:
        conn.rollback()
        print(f"Error: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    run()
