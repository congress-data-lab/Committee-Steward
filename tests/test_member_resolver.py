from core.members.resolver import (
    MemberCandidate,
    MemberResolutionError,
    MemberResolver,
    given_names_equivalent,
    normalize_name_for_match,
)


def test_given_name_nickname_equivalence_is_bidirectional():
    assert given_names_equivalent("Michael", "Mike")
    assert given_names_equivalent("Mike", "Michael")
    assert given_names_equivalent("Thomas", "Tom")
    assert given_names_equivalent("Tom", "Thomas")


def test_given_name_nickname_equivalence_rejects_different_names():
    assert not given_names_equivalent("Michael", "Thomas")
    assert not given_names_equivalent("", "Mike")


def test_name_normalization_repairs_pdf_dotless_i_artifact():
    assert normalize_name_for_match("Garcı́a") == "garcia"


def test_resolver_rejects_punctuation_only_name_without_crashing():
    resolver = MemberResolver(None)
    resolver._get_candidates = lambda *args, **kwargs: []

    try:
        resolver.resolve("31, 2024", 118, "S")
    except MemberResolutionError as exc:
        assert str(exc) == "Could not resolve member name: 31, 2024"
    else:
        raise AssertionError("Expected an unresolved numeric name to be rejected")


def test_resolver_uses_source_nickname_and_official_full_name_aliases():
    candidates = [
        MemberCandidate("B001282", "Garland", "Barr", "KY", 6, nickname="Andy", party="R"),
        MemberCandidate(
            "K000387", "Steve", "Knight", "CA", 25,
            official_full_name="Stephen Knight", party="R",
        ),
    ]
    resolver = MemberResolver(None)
    resolver._get_candidates = lambda *args, **kwargs: candidates

    assert resolver.resolve("Andy Barr", 115, "H", party="R") == "B001282"
    assert resolver.resolve("Stephen Knight", 115, "H", party="R") == "K000387"


def test_resolver_strips_representative_honorific():
    candidates = [
        MemberCandidate(
            "G000565",
            "Paul",
            "Gosar",
            "AZ",
            4,
            official_full_name="Paul A. Gosar",
            party="R",
        )
    ]
    resolver = MemberResolver(None)
    resolver._get_candidates = lambda *args, **kwargs: candidates

    assert resolver.resolve("Representative Paul Gosar", 117, "H") == "G000565"
