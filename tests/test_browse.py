"""GET /browse/{source}: conference names straight from listing links (network faked)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app
from app.scrapers.edas import EdasAdapter, parse_register_list
from app.scrapers.http import Fetcher, FetchError, FetchResult

FIXTURES = Path(__file__).parent / "fixtures"

# Mirrors the "Conferences open for registration" table in the EDAS screenshot.
EDAS_LIST_HTML = """
<html><body>
<h2>Conferences open for registration</h2>
<table id="conferences">
  <thead><tr>
    <th>Conference</th><th>Name</th><th>Home page</th><th>Where &amp; When (program)</th>
    <th>Register self</th><th>Register others</th>
  </tr></thead>
  <tbody>
    <tr>
      <td><a href="/web/mcsoc2026/">19th IEEE MCSoC-2026</a></td>
      <td>19th IEEE International Symposium on Embedded Multicore/Manycore SoCs (MCSoC-2026)</td>
      <td><a href="https://mcsoc-forum.org/">home</a></td>
      <td>Shanghai, China<br>December 14-20, 2026</td>
      <td><a href="/register.php?c=34001">self</a></td>
      <td><a href="/registerOthers.php?c=34001">others</a></td>
    </tr>
    <tr>
      <td><a href="/web/icoiact2026/">2026 9th ICOIACT</a></td>
      <td>2026 9th International Conference on Information and Communications Technology
        (ICOIACT)</td>
      <td><a href="https://icoiact.org/">home</a></td>
      <td>Tokyo, 〒 , Japan<br>2-3 December 2026</td>
      <td><a href="/register.php?c=34002">self</a></td>
      <td><a href="/registerOthers.php?c=34002">others</a></td>
    </tr>
  </tbody>
</table>
</body></html>
"""


def _result(url: str, html: str) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=url,
        status_code=200,
        content_type="text/html",
        content=html.encode("utf-8"),
        from_cache=False,
    )


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def requested(monkeypatch, tmp_path):
    """Serve fixture pages instead of the network; record which URLs were requested."""
    urls: list[str] = []

    async def fake_get(self, url, **_kwargs):
        urls.append(url)
        if "edas.info" in url:
            return _result(url, EDAS_LIST_HTML)
        if "page=1" in url:
            return _result(url, _fixture("wikicfp_category_open.html"))
        if "page=" in url:
            # Page 2 onwards: WikiCFP's expired section, which repeats once it runs out.
            return _result(url, _fixture("wikicfp_category_expired.html"))
        raise FetchError(f"unexpected url {url}")

    monkeypatch.setattr(Fetcher, "get", fake_get)
    monkeypatch.setattr(EdasAdapter, "make_fetcher", lambda self: Fetcher(self.settings))
    app.dependency_overrides[get_settings] = lambda: Settings(
        edas_state_path=tmp_path / "missing_state.json"
    )
    yield urls
    app.dependency_overrides.clear()


client = TestClient(app)


def test_wikicfp_first_page_names(requested):
    r = client.get("/browse/wikicfp")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "wikicfp"
    assert body["category"] == "computer science"
    assert body["next_page"] == 2
    assert body["pages"] == [
        {
            "page": 1,
            "link": "http://www.wikicfp.com/cfp/call?conference=computer%20science&page=1",
            "count": body["count"],
        }
    ]
    names = {c["acronym"]: c for c in body["conferences"]}
    assert names["CEI"]["name"].startswith("IEEE 2026 6th International Conference")
    assert names["CEI"]["link"].endswith("eventid=202993&copyownerid=163220")
    # Journals (When = N/A) are not conferences and are left out.
    assert "HIIJ" not in names
    # Only the listing page is fetched, never per-conference detail pages.
    assert all("event.showcfp" not in u for u in requested)


def test_wikicfp_category_is_passed_through(requested):
    r = client.get("/browse/wikicfp", params={"category": "machine learning"})
    assert r.json()["category"] == "machine learning"
    assert requested == ["http://www.wikicfp.com/cfp/call?conference=machine%20learning&page=1"]


def test_wikicfp_multiple_pages_stop_when_names_repeat(requested):
    body = client.get("/browse/wikicfp", params={"pages": 5}).json()
    # Page 1 = open CFPs, page 2 = expired section, page 3 repeats page 2 -> stop.
    assert [p["page"] for p in body["pages"]] == [1, 2, 3]
    assert body["pages"][2]["count"] == 0
    assert body["next_page"] is None
    ids = [c["link"] for c in body["conferences"]]
    assert len(ids) == len(set(ids)) == body["count"]


def test_wikicfp_start_page(requested):
    body = client.get("/browse/wikicfp", params={"page": 2}).json()
    assert body["pages"][0]["page"] == 2
    assert requested[0].endswith("page=2")


def test_edas_names(requested):
    body = client.get("/browse/edas").json()
    assert body["count"] == 2
    assert body["next_page"] is None
    assert [c["name"] for c in body["conferences"]] == [
        "19th IEEE International Symposium on Embedded Multicore/Manycore SoCs (MCSoC-2026)",
        "2026 9th International Conference on Information and Communications Technology (ICOIACT)",
    ]
    assert body["conferences"][0]["link"] == "https://edas.info/web/mcsoc2026/"


def test_edas_without_saved_session_is_400(monkeypatch, tmp_path):
    app.dependency_overrides[get_settings] = lambda: Settings(
        edas_state_path=tmp_path / "missing_state.json"
    )
    try:
        r = client.get("/browse/edas")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 400
    assert "edas_login" in r.json()["detail"]


def test_unknown_source_is_404():
    assert client.get("/browse/nope").status_code == 404


def test_unbuilt_source_is_501():
    assert client.get("/browse/acm").status_code == 501


@pytest.mark.parametrize("params", [{"page": 0}, {"pages": 0}, {"pages": 11}])
def test_invalid_paging_is_422(params):
    assert client.get("/browse/wikicfp", params=params).status_code == 422


def test_parse_register_list_fields():
    rows = parse_register_list(EDAS_LIST_HTML, "https://edas.info")
    first, second = rows
    assert first.source_id == "34001"  # from the register link's ?c=
    assert first.acronym == "19th IEEE MCSoC"
    assert (first.city, first.country) == ("Shanghai", "China")
    assert str(first.start_date) == "2026-12-14"
    assert first.homepage == "https://mcsoc-forum.org/"
    assert first.extra["register_self_url"] == "https://edas.info/register.php?c=34001"
    assert (second.city, second.country) == ("Tokyo", "Japan")
    assert str(second.end_date) == "2026-12-03"


def test_edas_expired_session_is_401(requested, monkeypatch):
    login_page = "<form><input type='password' name='pw'></form>"

    async def logged_out(self, url, **_kwargs):
        return _result(url, login_page)

    monkeypatch.setattr(Fetcher, "get", logged_out)
    r = client.get("/browse/edas")
    assert r.status_code == 401
    assert "edas_login" in r.json()["detail"]
