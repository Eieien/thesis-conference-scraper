"""Removes the EDAS account holder's details from anything that leaves this machine.

EDAS pages were read while logged in, so their text carries the account's email and EDAS person
ID ("EDAS (2616716 - name@example.org)"), a footer with the IP address and a session token, and
EDAS links carry the person ID as `p=`. Used by the MariaDB export and the public site's sync.
"""

import re
from collections.abc import Callable, Iterable

_HEADER = re.compile(r"EDAS \((\d+) - ([^)\s]+@[^)\s]+)\)")
_FOOTER = re.compile(r"\[User \d+ at [^\]]*\]")  # IP address and session token
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


class AccountScrubber:
    def __init__(self, edas_texts: Iterable[str | None]):
        self.ids: set[str] = set()
        self.emails: set[str] = set()
        for text in edas_texts:
            for pid, email in _HEADER.findall(text or ""):
                self.ids.add(pid)
                self.emails.add(email)
        self._needles = (*self.ids, *self.emails)

    def __call__(self, text: str) -> str:
        # Fast path: account details only appear in EDAS text, as the ID, the email or the footer.
        # Plain substring checks let almost every string skip the regular expressions.
        if (
            "EDAS" not in text
            and "[User " not in text
            and not any(n in text for n in self._needles)
        ):
            return text
        text = _FOOTER.sub("[user details removed]", text)
        for email in self.emails:
            text = text.replace(email, "[account email]")
        for pid in self.ids:
            text = re.sub(rf"(?<!\d){pid}(?!\d)", "0", text)
        return _IP.sub("[ip]", text) if "EDAS" in text else text

    def leaks(self, text: str) -> list[str]:
        """The account details still present in `text` (should be none)."""
        return [s for s in [*self.ids, *self.emails] if s in text]


def scrub_deep(value, scrub: Callable[[str], str]):
    """Apply `scrub` to every string inside nested dicts and lists."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: scrub_deep(v, scrub) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_deep(v, scrub) for v in value]
    return value
