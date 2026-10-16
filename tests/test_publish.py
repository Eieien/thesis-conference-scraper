"""What the public site may show (app/pipeline/publish.py, app/pipeline/privacy.py)."""

from app.pipeline.privacy import AccountScrubber, scrub_deep
from app.pipeline.publish import _without_person, fee_status

EDAS_PAGE = (
    "Register for 2026 2nd ICAIFI [2026 2nd ICAIFI] EDAS (1234567 - someone@example.edu):\n"
    "Regular | $275.00\n"
    "EDAS at golf (Wed, 23 Sep 2026) [User 1234567 at 10.0.0.1 arNvGamsM9EzNrcH2n6c_gAAAB0]"
)


def test_the_edas_account_is_scrubbed_everywhere():
    scrub = AccountScrubber([EDAS_PAGE])
    data = {"a": [EDAS_PAGE, {"url": "https://edas.info/N1234567"}], "n": 5}
    out = scrub_deep(data, scrub)
    text = str(out)
    assert "someone@example.edu" not in text and "1234567" not in text
    assert "10.0.0.1" not in text and "arNvGamsM9" not in text
    assert out["n"] == 5 and "$275.00" in text
    assert scrub.leaks(text) == []


def test_edas_links_lose_the_person_id_only():
    url = "https://edas.info/registerPerson.php?c=35492&p=2616716"
    assert _without_person(url) == "https://edas.info/registerPerson.php?c=35492"
    other = "https://example.org/register?p=2"
    assert _without_person(other) == other


def test_fee_status_in_plain_words():
    assert fee_status("enriched", None) == "Fees found"
    assert fee_status("collected", None) == "Not read yet"
    assert fee_status("needs_review", "fees not published yet") == "No fees posted yet"
    assert fee_status("needs_review", 'prices on the page were not extracted, e.g. "x"') == (
        "Fees on the site, not read yet"
    )
    assert fee_status("needs_review", "no homepage and no source page text") == (
        "Website unreadable"
    )
    assert fee_status("needs_review", "homepage dates 2026-12-11 don't match listing") == (
        "Check the edition"
    )


def test_a_half_written_rates_file_keeps_the_last_good_rates(tmp_path):
    from app.config import Settings
    from app.pipeline import fx

    settings = Settings(data_dir=tmp_path)
    path = tmp_path / "fx_rates.json"
    path.write_text('{"base": "USD", "rates": {"USD": 1, "PHP": 58.0}}', encoding="utf-8")
    fx._memo.update(mtime=None, data=None)
    assert fx.load_rates(settings)["rates"]["PHP"] == 58.0
    path.write_text("", encoding="utf-8")  # cut short mid-write
    import os

    os.utime(path, (1, 1))  # a new mtime, so it is re-read
    assert fx.load_rates(settings)["rates"]["PHP"] == 58.0
    fx._memo.update(mtime=None, data=None)


def test_impossible_end_dates_are_repaired_or_dropped():
    from datetime import date

    from app.pipeline.normalize import sane_end_date

    # WikiCFP's ICDM Applied Track: "12 Nov 2026 - 15 Nov 2025" (last year's year)
    assert sane_end_date(date(2026, 11, 12), date(2025, 11, 15)) == date(2026, 11, 15)
    assert sane_end_date(date(2026, 11, 12), date(2026, 3, 1)) is None  # can't be fixed
    assert sane_end_date(date(2026, 11, 12), date(2026, 11, 14)) == date(2026, 11, 14)
    assert sane_end_date(None, date(2026, 11, 14)) == date(2026, 11, 14)


def test_currency_spellings_from_real_fee_pages():
    from app.pipeline.normalize import normalize_currency

    assert [normalize_currency(c) for c in ("EURO", "TL", "KM", "DA", "₺")] == [
        "EUR", "TRY", "BAM", "DZD", "TRY",
    ]  # fmt: skip
