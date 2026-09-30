from app.pipeline.quality import organizer_flag


def test_waset_pages_are_flagged_and_others_are_not():
    url = "https://waset.org/computer-science-and-technology-conference-in-november-2026-in-tokyo"
    assert "WASET" in organizer_flag(url)
    assert organizer_flag("https://www.waset.org/x")
    assert organizer_flag("https://ieee-csde.org/") is None
    assert organizer_flag("https://notwaset.org/") is None
    assert organizer_flag(None) is None
