import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.models import Conference
from app.pipeline.history import editions, history_record, query_url

FIXTURES = Path(__file__).parent / "fixtures"


def _body(name):
    # Real Crossref answers saved on 2026-09-29.
    return json.loads((FIXTURES / f"crossref_{name}.json").read_text(encoding="utf-8"))


def _conf(acronym, name, start=date(2026, 10, 1)):
    return Conference(acronym=acronym, name=name, start_date=start)


def test_ieee_series_has_its_past_editions():
    conf = _conf(
        "TSSA",
        "The 20th International Conference on Telecommunication Systems, Services and Apps",
    )
    found = editions(conf, _body("tssa"))
    years = [e["year"] for e in found]
    assert {2020, 2021, 2023, 2025} <= set(years)
    assert all(e["publisher"] == "IEEE" for e in found)
    assert years == sorted(years, reverse=True)


def test_springer_series_matches_on_the_volume_title():
    conf = _conf("ADMA", "Advanced Data Mining and Applications", date(2026, 11, 13))
    found = editions(conf, _body("adma"))
    assert found and all("Springer" in e["publisher"] for e in found)


def test_shared_acronym_of_a_different_series_is_not_history():
    # The answer is full of "Control Science and Systems Engineering (ICCSSE)".
    body = _body("generic")
    beijing = _conf(
        "ICCSSE", "International Conference on Computer Science and Software Engineering"
    )
    assert editions(beijing, body) == []
    wuhan = _conf(
        "IEEE ICCSSE",
        "2026 IEEE 12th International Conference on Control Science and Systems Engineering",
        date(2026, 10, 23),
    )
    assert editions(wuhan, body)  # the real ICCSSE series does have a history


def test_record_and_query():
    conf = _conf("TSSA", "The 20th International Conference on Telecommunication Systems")
    url = query_url(conf)
    q = parse_qs(urlsplit(url).query)
    assert q["filter"] == ["until-pub-date:2025-12-31"]  # earlier editions only
    assert "20th" not in q["query.bibliographic"][0]
    rec = history_record(conf, {"message": {"items": []}}, url)
    assert rec["has_history"] is False and rec["editions"] == [] and rec["source"] == "crossref"


def _item(name, year, publisher="IEEE"):
    return {
        "type": "proceedings-article",
        "published": {"date-parts": [[year]]},
        "publisher": publisher,
        "event": {"name": name},
        "DOI": "10.1109/x",
    }


def _answer(*items):
    return {"message": {"items": list(items)}}


def test_same_acronym_different_words_is_not_history():
    # Found on the first run: ICB "Bioethics" matched the ICB "Biometrics" series.
    conf = _conf("ICB", "International Conference on Bioethics")
    body = _answer(_item("2019 International Conference on Biometrics (ICB)", 2019))
    assert editions(conf, body) == []


NETCRYPT_2025 = "2025 4th International Conference on Networks and Cryptology (NETCRYPT)"


def test_edition_numbers_must_fit():
    body = _answer(_item(NETCRYPT_2025, 2025))
    first = _conf("NETCRYPT", "2026 1st International Conference on Networks and Cryptology")
    assert editions(first, body) == []
    second = _conf("NETCRYPT", "2026 2nd International Conference on Networks and Cryptology")
    assert editions(second, body) == []  # a past 4th can't precede this 2nd
    fifth = _conf("NETCRYPT", "2026 5th International Conference on Networks and Cryptology")
    assert [e["year"] for e in editions(fifth, body)] == [2025]


CSIT = "International Conference on Computer Science and Information Technology (ICCSIT)"


def test_only_a_recent_history_counts():
    conf = _conf(
        "ICCSIT", "International Conference on Computer Science and Information Technology"
    )
    old = _answer(
        _item(
            "2013 5th " + CSIT,
            2013,
        )
    )
    rec = history_record(conf, old, "q")
    assert rec["has_history"] is False and rec["editions"] and "another series" in rec["note"]
    new = _answer(
        _item(
            "2024 " + CSIT,
            2024,
        )
    )
    assert history_record(conf, new, "q")["has_history"] is True


def test_similar_words_in_another_order_are_not_the_same_series():
    # "Machine Learning and Data Analytics" is not "Data Analysis and Machine Learning".
    conf = _conf("ICMLDA", "International Conference on Machine Learning and Data Analytics")
    body = _answer(_item("International Conference on Data Analysis and Machine Learning", 2024))
    assert editions(conf, body) == []


def test_half_the_words_is_not_enough():
    # Found on the second run: ICSET "Science Engineering and Technology" matched ICSET "System
    # Engineering and Technology".
    conf = _conf("ICSET", "International Conference on Science Engineering and Technology")
    body = _answer(
        _item(
            "2024 14th International Conference on System Engineering and Technology (ICSET)", 2024
        )
    )
    assert editions(conf, body) == []


def test_acm_puts_the_acronym_in_front():
    conf = _conf(
        "ACM ICIMH",
        "ACM--2026 The 7th International Conference on Intelligent Medicine and Health",
    )
    body = _answer(
        _item(
            "ICIMH 2022: 2022 The 4th International Conference on Intelligent Medicine and Health",
            2022,
            "ACM",
        )
    )
    rec = history_record(conf, _answer(), "q", extra=body)  # found by the second query
    assert rec["has_history"] is True and rec["editions"][0]["year"] == 2022


def test_search_name_drops_tags_and_numbers():
    from app.pipeline.history import search_name

    conf = _conf(
        "VSIP",
        "IEEE--2026 The 8th International Conference on Video, Signal and Image Processing (VSIP)",
    )
    assert search_name(conf) == "International Conference on Video, Signal and Image Processing"
