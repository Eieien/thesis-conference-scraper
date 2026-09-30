from datetime import date
from pathlib import Path

from app.pipeline.fees import fees_from_tables
from app.scrapers.edas import _looks_logged_out, parse_register_list

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://edas.info"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def test_register_list_page():
    html = _read("edas_register_list.html")
    assert not _looks_logged_out(html)
    rows = parse_register_list(html, BASE)
    assert len(rows) == 70
    assert all(r.start_date and r.source_id.isdigit() for r in rows)
    mcsoc = next(r for r in rows if r.source_id == "34632")
    assert mcsoc.name.startswith("19th IEEE International Symposium on Embedded Multicore")
    assert mcsoc.start_date == date(2026, 12, 14)
    assert mcsoc.end_date == date(2026, 12, 20)
    assert (mcsoc.city, mcsoc.country) == ("Shanghai", "China")
    assert mcsoc.homepage == "https://mcsoc-forum.org/"
    assert mcsoc.url is None  # the conference cell has no link; the ID comes from "Register self"
    assert mcsoc.extra["register_self_url"].startswith(f"{BASE}/registerPerson.php?c=34632")


def test_register_person_fees():
    html = _read("edas_register_person.html")
    assert not _looks_logged_out(html)
    fees = {f.category: (f.amount, f.currency) for f in fees_from_tables(html)}
    assert fees == {
        "RP: Att": (100.0, "USD"),
        "RP: Extra manuscript page": (50.0, "USD"),
        "RP: Pro": (350.0, "USD"),
        "RP: Pro-IEEE": (325.0, "USD"),
        "RP: Xtra": (275.0, "USD"),
    }


def test_register_fees_skip_non_price_columns():
    # UEMCON's table also has Description, Cancellation fee and "Covers how many papers?" columns.
    fees = fees_from_tables(_read("edas_register_uemcon.html"))
    assert fees
    assert all(f.notes and "UEMCON 2026" in f.notes for f in fees)  # Description becomes notes
    assert {f.amount for f in fees} >= {955.0, 950.0, 850.0}
    assert min(f.amount for f in fees) > 2  # "covers 2 papers" is not a $2 fee
    assert 2026.0 not in {f.amount for f in fees}  # the year inside a description
    # One fee per row: cancellation fees are not read as a second price.
    assert len(fees) == len({f.category for f in fees})


def test_login_redirect_counts_as_logged_out():
    # What EDAS sends when the saved session no longer matches the network location.
    html = (
        '<script nonce="0" src="js/redirect.js" ></script>\n<script nonce=\'0\' defer>'
        "redirect('https://edas.info/login.php?rurl=x&error=First+login+from+network+location.', 0)"
        "</script>"
    )
    assert _looks_logged_out(html)
