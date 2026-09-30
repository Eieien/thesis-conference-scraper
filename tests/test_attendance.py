from types import SimpleNamespace

from app.pipeline.attendance import IN_PERSON, ONLINE_ONLY, ONLINE_OPTION, decide


def _fee(category, tier=None):
    return SimpleNamespace(category=category, tier=tier, notes=None, amount=100, currency="USD")


def test_a_virtual_fee_means_an_online_option():
    rec = decide("", [_fee("Online-Only - Student Non-Member"), _fee("IEEE Member")], [])
    assert rec["mode"] == ONLINE_OPTION and rec["evidence"][0]["source"] == "fee"


def test_page_wording():
    assert (
        decide("Authors may choose online presentation via Zoom.", [], [])["mode"] == ONLINE_OPTION
    )
    assert decide("The conference will be held in a hybrid mode.", [], [])["mode"] == ONLINE_OPTION
    assert decide("This year the event will be held online.", [], [])["mode"] == ONLINE_ONLY


def test_an_explicit_in_person_rule_wins():
    text = "In-person presentation is mandatory. Online registration opens in May."
    rec = decide(text, [_fee("Online participation")], ["hybrid"])
    assert rec["mode"] == IN_PERSON
    assert decide("No online presentation will be allowed.", [], [])["mode"] == IN_PERSON


def test_ieee_format():
    assert decide("", [], ["hybrid"])["mode"] == ONLINE_OPTION
    assert decide("", [], ["virtual"])["mode"] == ONLINE_ONLY
    assert decide("", [], ["inperson"])["mode"] == IN_PERSON


def test_nothing_either_way():
    # "Online registration" and "virtual machines" are not about attending online.
    text = "Online registration is open. Topics: virtual machines, cloud computing."
    assert decide(text, [], []) is None


def test_false_alarms_from_the_first_run():
    # INMIC: both are allowed.
    text = "Accepted papers should be presented in person/Via Online by Authors."
    assert decide(text, [], [])["mode"] == ONLINE_OPTION
    # Past editions and side events are not this year's format.
    assert (
        decide("The ICSED 2023 was held successfully online and offline in Singapore.", [], [])
        is None
    )
    assert decide("The hackathon returns with IICT 2026, fully online.", [], []) is None
    # The real thing still counts.
    assert decide("WSIS 2026 workshop will be held online.", [], [])["mode"] == ONLINE_ONLY
    assert decide("This is a fully virtual conference.", [], [])["mode"] == ONLINE_ONLY
