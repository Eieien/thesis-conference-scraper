from types import SimpleNamespace

import pytest

from app.pipeline.fee_kinds import classify, headline_fees


# Real categories from the data (2026-09-29).
@pytest.mark.parametrize(
    "category,tier,who,student,member,mode,local",
    [
        ("IEEE Member", "early", "author", False, True, None, None),
        ("Regular - IEEE member", None, "author", False, True, None, None),
        ("Non-IEEE member attend only", None, "attendee", False, False, None, None),
        ("Listener/Attendee", None, "attendee", False, None, None, None),
        ("Student IEEE Member Onsite", None, "author", True, True, "onsite", None),
        ("Online-Only - Student Non-Member", None, "author", True, False, "virtual", None),
        (
            "Offline Student Author Non-IEEE Member Indian",
            None,
            "author",
            True,
            False,
            "onsite",
            True,
        ),
        ("Extra Paper (per paper)", None, "extra", False, None, None, None),
        ("Additional banquet", None, "extra", False, None, None, None),
        ("One Day Tour", None, "extra", False, None, None, None),
        ("Foreign Delegates - IEEE Members", None, "author", False, True, None, False),
        (
            "Attendee/Listener (Without paper presentation)",
            None,
            "attendee",
            False,
            None,
            None,
            None,
        ),
        (
            "Student IEEE International Physical 1 Paper",
            None,
            "author",
            True,
            True,
            "onsite",
            False,
        ),
    ],
)
def test_classify_real_categories(category, tier, who, student, member, mode, local):
    k = classify(category, tier)
    assert (k.who, k.student, k.member, k.mode, k.local) == (who, student, member, mode, local)


def test_periods():
    assert classify("IEEE Member", "early").period == "early"
    assert classify("IEEE Member", "Early Bird").period == "early"
    assert classify("IEEE Member", "regular").period == "regular"
    assert classify("IEEE Member", "late").period == "late"
    assert classify("IEEE Member", "onsite").period == "late"  # the at-the-desk rate
    assert classify("IEEE Member", None, "Before August 30, 2026").period == "early"


def _fee(category, amount, tier=None, currency="USD", status="verified"):
    return SimpleNamespace(
        category=category,
        tier=tier,
        notes=None,
        amount=amount,
        currency=currency,
        check_status=status,
    )


def test_headline_fees_pick_the_standard_and_lowest_student_rate():
    fees = [
        _fee("IEEE Member", 400, "early"),
        _fee("Non-IEEE Member", 450, "early"),
        _fee("Non-IEEE Member", 550, "regular"),
        _fee("Student IEEE Member", 250, "early"),
        _fee("Student Non-IEEE Member", 300, "early"),
        _fee("Local Participants (Indian) Non-IEEE Member", 90, "regular"),
        _fee("Listener", 150),
        _fee("Extra Paper", 200),
        _fee("Non-IEEE Member", 9999, "regular", status="implausible"),  # does not count
    ]
    standard, student = headline_fees(fees, lambda f: f.amount)
    assert standard.amount == 550  # non-member, regular period, not local
    assert student.amount == 250


def test_no_headline_without_suitable_fees():
    standard, student = headline_fees(
        [_fee("Listener", 100), _fee("Banquet ticket", 60)], lambda f: f.amount
    )
    assert standard is None and student is None


def test_society_names_without_the_word_member():
    assert classify("Non IEEE Academician").member is False
    assert classify("Professional (Academician), IEEE").member is True
    assert classify("Student Non-IEEE").member is False


def test_non_author_is_an_attendee():
    assert classify("Non-Author & Non-Presenter participant Online").who == "attendee"
    assert classify("Author (Non-IEEE member)").who == "author"


def test_student_fee_prefers_a_student_author_over_a_student_listener():
    fees = [
        _fee("Student Author", 250),
        _fee("Student Listener", 50),
        _fee("Regular Author", 500),
    ]
    assert headline_fees(fees, lambda f: f.amount)[1].amount == 250
    only_listener = [_fee("Student Listener", 50), _fee("Regular Author", 500)]
    assert headline_fees(only_listener, lambda f: f.amount)[1].amount == 50
