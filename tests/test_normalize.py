from datetime import date

import pytest

from app.pipeline.normalize import parse_date, parse_date_range, parse_money, split_location


@pytest.mark.parametrize(
    "text,start,end",
    [
        # Formats from the EDAS list
        ("December 14-20, 2026", date(2026, 12, 14), date(2026, 12, 20)),
        ("31 October 2026", date(2026, 10, 31), date(2026, 10, 31)),
        ("2-3 December 2026", date(2026, 12, 2), date(2026, 12, 3)),
        ("October 21-23, 2026", date(2026, 10, 21), date(2026, 10, 23)),
        ("14-15 April 2027", date(2027, 4, 14), date(2027, 4, 15)),
        # WikiCFP
        ("Nov 20, 2026 - Nov 22, 2026", date(2026, 11, 20), date(2026, 11, 22)),
        # Cross-month
        ("Nov 30 - Dec 2, 2026", date(2026, 11, 30), date(2026, 12, 2)),
        ("30 Nov - 2 Dec 2026", date(2026, 11, 30), date(2026, 12, 2)),
        ("N/A", None, None),
    ],
)
def test_parse_date_range(text, start, end):
    assert parse_date_range(text) == (start, end)


def test_parse_date_ignores_parenthesised_original_deadline():
    assert parse_date("Sep 23, 2026 (Sep 21, 2026)") == date(2026, 9, 23)


@pytest.mark.parametrize(
    "text,city,country",
    [
        ("Urayasu, Chiba, 〒 , Japan", "Urayasu", "Japan"),
        ("Washington, D.C., DC, USA", "Washington", "USA"),
        ("Valencia", "Valencia", None),
        ("Jember Jawa Timur, Indonesia", "Jember Jawa Timur", "Indonesia"),
    ],
)
def test_split_location(text, city, country):
    assert split_location(text) == (city, country)


@pytest.mark.parametrize(
    "text,amount,currency",
    [
        ("US$ 450", 450.0, "USD"),
        ("€1,200.00", 1200.0, "EUR"),
        ("IDR 2.500.000", 2500000.0, "IDR"),
        ("350 USD", 350.0, "USD"),
    ],
)
def test_parse_money(text, amount, currency):
    assert parse_money(text) == (amount, currency)
