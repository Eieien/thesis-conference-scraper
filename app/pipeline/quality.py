"""Warnings about the organizer, derived when read (like continents), never stored.

WASET (World Academy of Science, Engineering and Technology, waset.org) runs hundreds of
look-alike "International Conference on ..." events in every city each month, and is widely
documented as a predatory organizer. Its pages also answer the crawler with HTTP 403. Such
conferences stay in the data but carry a flag, are not recovered with a real browser, and the
visualizer shows the flag.
"""

from urllib.parse import urlsplit

PREDATORY_HOSTS = {
    "waset.org": "WASET, a widely reported predatory conference organizer",
}


def organizer_flag(homepage: str | None) -> str | None:
    """Why this conference's organizer deserves caution, or None."""
    host = (urlsplit(homepage or "").hostname or "").lower().removeprefix("www.")
    for known, why in PREDATORY_HOSTS.items():
        if host == known or host.endswith("." + known):
            return why
    return None
