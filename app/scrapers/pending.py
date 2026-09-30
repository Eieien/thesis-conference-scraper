"""Sources that still need their markup inspected before a parser can be written."""

from app.scrapers.base import NotImplementedAdapter


class AcmAdapter(NotImplementedAdapter):
    name = "acm"
    label = "ACM Conferences"
    notes = (
        "www.acm.org answers the bot user agent with a Cloudflare managed challenge (HTTP 403) "
        "on /conferences; the old /conferences/upcoming-conferences URL is 404, and robots.txt "
        "disallows the calendar (/conferences/conference-events, /calendar). Not scraped."
    )


class SpringerAdapter(NotImplementedAdapter):
    name = "springer"
    label = "Springer conferences"
    notes = (
        "link.springer.com and springernature.com answer the bot user agent with HTTP 406 "
        "'Access Blocked' (CDN edge block on IP/TLS fingerprint). The old springer.com "
        "conference and LNCS forthcoming-proceedings pages now redirect to author guidelines. "
        "No public upcoming-conference listing; not scraped."
    )
