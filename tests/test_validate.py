from pathlib import Path

from app.pipeline.validate import (
    DUPLICATE,
    IMPLAUSIBLE,
    NOT_FOUND,
    VERIFIED,
    check_fee,
    check_fees,
    fee_mentions,
    summary_note,
)
from app.scrapers.html_clean import html_to_text

UEMCON = html_to_text(
    (Path(__file__).parent / "fixtures" / "edas_register_uemcon.html").read_text(encoding="utf-8")
)


def test_real_fee_on_the_page_is_verified_with_evidence():
    c = check_fee(955.0, "USD", UEMCON)
    assert c.status == VERIFIED
    assert "955" in c.evidence


def test_invented_amount_is_not_found():
    assert check_fee(1234.0, "USD", UEMCON).status == NOT_FOUND


def test_the_two_real_mistakes_we_saw_are_caught():
    # "Covers how many papers? 2" once became a USD 2 fee; a description's year became USD 2026.
    assert check_fee(2.0, "USD", UEMCON).status == IMPLAUSIBLE
    assert check_fee(2026.0, "USD", "Conference on 12 Oct 2026 in Tokyo").status == IMPLAUSIBLE


def test_number_formats():
    assert check_fee(1250.0, "EUR", "Regular: 1.250,00 € per author").status == VERIFIED
    assert check_fee(1250.0, "USD", "Regular US$ 1,250").status == VERIFIED
    assert check_fee(2_500_000.0, "IDR", "Peserta umum Rp 2.500.000").status == VERIFIED
    assert check_fee(99.5, "USD", "Students: $99.50").status == VERIFIED
    # 250 must not match inside 1250 or 2500.
    assert check_fee(250.0, "USD", "Fee $1250, late $2500").status == NOT_FOUND


def test_implausible_ranges_by_currency():
    assert check_fee(2.0, "USD", "$2").status == IMPLAUSIBLE  # a "covers 2 papers" column
    assert check_fee(40000.0, "USD", "$40,000").status == IMPLAUSIBLE
    assert check_fee(40000.0, "JPY", "40,000円").status == VERIFIED  # about US$270


def test_duplicates_and_summary():
    fees = [
        {"category": "Author", "tier": "early", "amount": 955.0, "currency": "USD"},
        {"category": "author", "tier": "early", "amount": 955.0, "currency": "USD"},
        {"category": "Listener", "tier": None, "amount": 1234.0, "currency": "USD"},
    ]
    checks = check_fees(fees, UEMCON)
    assert [c.status for c in checks] == [VERIFIED, DUPLICATE, NOT_FOUND]
    assert summary_note(checks) == "1 of 2 fees not found on the page"


def test_a_fee_without_currency_or_from_a_split_year_is_not_verified():
    # Real case (GTSE): "6" read from a year split over two lines, with no currency.
    text = "The conference will be held in November 202\n6 in Padang."
    assert check_fee(6, None, text).status == IMPLAUSIBLE
    assert check_fee(6, "USD", text).status in (IMPLAUSIBLE, NOT_FOUND)


def test_fee_mentions_finds_prices_the_extraction_missed():
    text = (
        "Participant Categories | Non IEEE Member / Onsite | IEEE Member / Onsite\n"
        "Student | ৳ 12,500 | ৳ 10,500\n"
    )
    assert fee_mentions(text)
    assert fee_mentions("Registration fee: RM 1,200 for authors.")
    # Not fees: hotel prices, exchange rates, plain years, pages without prices.
    assert not fee_mentions("Hotel room rate: USD 120 per night")
    assert not fee_mentions("Exchange rate: USD 1 = TWD 30")
    assert not fee_mentions("Submission deadline: 12 October 2026. Registration opens soon.")


def test_fee_mentions_ignores_the_false_alarms_seen_on_real_pages():
    # Each of these was flagged as "prices on the page" in the first run (2026-09-25).
    assert not fee_mentions("She has secured over S$20 million in external research funding.")
    assert not fee_mentions("Ph.D Fees : INR 49,500/- per semester")
    assert not fee_mentions("Extra charges apply for >5 pages (70 USD per additional page).")
    assert not fee_mentions("additional fees ($15/page or Rp200,000/page) if your paper is longer")
    assert not fee_mentions("Proses daftar cepat, deposit mulai Rp 10.000, slot gacor")
    # The real one stays: ICDSG's fee table.
    assert fee_mentions("Author (Non-Member) | $350 (5500K IDR) | Author (IEEE Member) | $250")
