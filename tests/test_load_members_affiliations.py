from datetime import date

from ingest.load_members import _term_affiliation_periods


def test_term_affiliations_use_caucus_for_committee_party_group() -> None:
    periods = _term_affiliation_periods(
        {
            "start": "2019-01-03",
            "end": "2021-01-03",
            "party": "Independent",
            "caucus": "Democrat",
        }
    )

    assert periods == [
        (date(2019, 1, 3), date(2021, 1, 3), 328, 100)
    ]


def test_term_affiliations_split_party_changes_without_losing_caucus() -> None:
    periods = _term_affiliation_periods(
        {
            "start": "2019-01-03",
            "end": "2021-01-03",
            "party": "Republican",
            "party_affiliations": [
                {
                    "start": "2019-01-03",
                    "end": "2019-12-19",
                    "party": "Democrat",
                },
                {
                    "start": "2019-12-19",
                    "end": "2021-01-03",
                    "party": "Republican",
                },
            ],
        }
    )

    assert periods == [
        (date(2019, 1, 3), date(2019, 12, 19), 100, 100),
        (date(2019, 12, 19), date(2021, 1, 3), 200, 200),
    ]


def test_term_affiliations_close_one_day_upstream_switch_seam() -> None:
    periods = _term_affiliation_periods(
        {
            "start": "2019-01-03",
            "end": "2021-01-03",
            "party": "Republican",
            "party_affiliations": [
                {
                    "start": "2019-01-03",
                    "end": "2019-12-18",
                    "party": "Democrat",
                },
                {
                    "start": "2019-12-19",
                    "end": "2021-01-03",
                    "party": "Republican",
                },
            ],
        }
    )

    assert periods == [
        (date(2019, 1, 3), date(2019, 12, 19), 100, 100),
        (date(2019, 12, 19), date(2021, 1, 3), 200, 200),
    ]
