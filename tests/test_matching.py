from datetime import date

import pytest

from app.models import Conference, RawListing
from app.pipeline.fees import fees_from_tables
from app.pipeline.filter import keyword_topic
from app.pipeline.merge import acronym_key, find_match
from app.scrapers.html_clean import html_to_text


@pytest.mark.parametrize(
    "raw,key",
    [
        ("19th IEEE MCSoC-2026", "MCSOC"),
        ("2026 2nd ICAIFI", "ICAIFI"),
        ("ATC'26", "ATC"),
        ("APWiMob 2026", "APWIMOB"),
        ("Springer ICMLSC 2027", "ICMLSC"),
        ("2026 IEEE CAMA", "CAMA"),
    ],
)
def test_acronym_key(raw, key):
    assert acronym_key(raw) == key


def test_keyword_topic():
    assert (
        keyword_topic("International Conference on Machine Learning and Computer Vision")[0] is True
    )
    assert keyword_topic("International Conference on Nursing and Clinical Practice")[0] is False


FEE_HTML = """
<table>
  <tr><th>Category</th><th>Early (USD)</th><th>Late (USD)</th></tr>
  <tr><td>IEEE Member</td><td>450</td><td>550</td></tr>
  <tr><td>Student</td><td>$300</td><td>$350</td></tr>
</table>
"""


def test_fees_from_tables():
    fees = fees_from_tables(FEE_HTML)
    assert [(f.category, f.tier, f.amount, f.currency) for f in fees] == [
        ("IEEE Member", "early", 450.0, "USD"),
        ("IEEE Member", "late", 550.0, "USD"),
        ("Student", "early", 300.0, "USD"),
        ("Student", "late", 350.0, "USD"),
    ]


def test_html_to_text_keeps_table_rows():
    text = html_to_text("<nav>menu</nav><p>Fees</p>" + FEE_HTML)
    assert "menu" not in text
    assert "IEEE Member | 450 | 550" in text


def _listing(acronym, name, start, country):
    return RawListing(
        source="x",
        source_id=acronym or name,
        acronym=acronym,
        name=name,
        start_date=start,
        country=country,
    )


def _conf(acronym, name, start, country):
    return Conference(acronym=acronym, name=name, start_date=start, country=country)


def test_generic_names_with_different_acronyms_do_not_merge():
    # Real false positive from a WikiCFP smoke run.
    conf = _conf(
        "CEI",
        "IEEE 2026 6th International Conference on Computer Science, Electronic Information "
        "Engineering and Intelligent Control Technology",
        date(2026, 11, 20),
        "China",
    )
    listing = _listing(
        "CCSIT",
        "16th International Conference on Computer Science and Information Technology",
        date(2026, 11, 21),
        "UK",
    )
    assert find_match(listing, [conf]) is None


def test_same_event_across_sources_merges():
    conf = _conf(
        "19th IEEE MCSoC-2026",
        "19th IEEE International Symposium on Embedded Multicore/Manycore SoCs (MCSoC-2026)",
        date(2026, 12, 14),
        "China",
    )
    by_acronym = _listing("MCSoC", "Embedded Multicore SoCs", date(2026, 12, 14), "China")
    by_name = _listing(
        None,
        "IEEE International Symposium on Embedded Multicore/Manycore SoCs",
        date(2026, 12, 15),
        None,
    )
    assert find_match(by_acronym, [conf]) is conf
    assert find_match(by_name, [conf]) is conf


def test_merged_table_cells_and_stacked_headers_keep_prices_under_their_column():
    # Shape of the A-SSCC and CSDE fee tables that were misread before.
    html = """<table>
      <tr><td>Category</td><td colspan="2">Non IEEE Member</td><td colspan="2">IEEE Member</td></tr>
      <tr><td>Category</td><td>Onsite</td><td>Virtual</td><td>Onsite</td><td>Virtual</td></tr>
      <tr><td rowspan="2">Student</td><td>$ 660</td><td>$ 600</td><td>$ 546</td><td>-</td></tr>
      <tr><td>$ 700</td><td>$ 640</td><td>$ 580</td><td>$ 600</td></tr>
    </table>"""
    lines = html_to_text(html).splitlines()
    assert lines[0] == (
        "Category | Non IEEE Member / Onsite | Non IEEE Member / Virtual | IEEE Member / Onsite"
        " | IEEE Member / Virtual"
    )
    assert lines[1] == "Student | $ 660 | $ 600 | $ 546 | -"
    assert lines[2] == "Student | $ 700 | $ 640 | $ 580 | $ 600"  # row label repeated
