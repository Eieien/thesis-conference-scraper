"""Audit the data for things that can't be right. Read-only: it reports, it changes nothing.

    uv run python -m scripts.audit_data            # the public continents (PUBLIC_CONTINENTS)
    uv run python -m scripts.audit_data --all      # every conference

Each check prints how many rows fail and a few examples. Exits 1 if a check marked "must be 0"
fails; the others are worth a look but can be legitimately non-zero (a conference that really
moved its dates, a fee page that lists the same price twice).
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Conference, RawListing  # noqa: E402
from app.pipeline.fees import summarize_fees  # noqa: E402
from app.pipeline.fx import convert  # noqa: E402
from app.pipeline.geo import continent_of  # noqa: E402
from app.pipeline.merge import acronym_key  # noqa: E402
from app.pipeline.publish import public_data  # noqa: E402
from app.pipeline.quality import organizer_flag  # noqa: E402

COUNTED = (None, "verified")
URL = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.I)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="every continent, not just the public ones")
    args = ap.parse_args()
    settings = get_settings()

    results: list[tuple[str, bool, int, list[str]]] = []

    def check(name: str, rows: list, must_be_zero: bool = True, show=str) -> None:
        results.append((name, must_be_zero, len(rows), [show(r) for r in rows[:4]]))

    with SessionLocal() as db:
        confs = db.scalars(
            select(Conference).options(
                selectinload(Conference.fees), selectinload(Conference.listings)
            )
        ).all()
        if not args.all:
            confs = [
                c
                for c in confs
                if (continent_of(c.country) or "Unknown") in settings.public_continents
            ]
        label = lambda c: f"#{c.id} {c.acronym or c.name[:40]}"  # noqa: E731
        ws, we = settings.window_start, settings.window_end

        # ---- dates ----
        check(
            "ends before it starts",
            [c for c in confs if c.start_date and c.end_date and c.end_date < c.start_date],
            show=label,
        )
        check(
            "starts outside the window (kept in the database, not published)",
            [c for c in confs if c.start_date and not (ws <= c.start_date <= we)],
            must_be_zero=False,
            show=label,
        )
        check("no start date", [c for c in confs if not c.start_date], show=label)
        check(
            "lasts more than 3 weeks",
            [
                c
                for c in confs
                if c.start_date and c.end_date and (c.end_date - c.start_date).days > 21
            ],
            must_be_zero=False,
            show=label,
        )
        late = []
        for c in confs:
            for f in ("abstract_deadline", "paper_deadline"):
                d = getattr(c, f)
                if d and c.start_date and d > c.start_date:
                    late.append(f"{label(c)} {f} {d} > start {c.start_date}")
        check("submission deadline after the conference starts", late, must_be_zero=False)
        old = [
            f"{label(c)} paper {c.paper_deadline}"
            for c in confs
            if c.paper_deadline and c.paper_deadline.year < 2025
        ]
        check("paper deadline before 2025 (last edition's?)", old, must_be_zero=False)

        # ---- fees ----
        bad_amount, no_currency, out_of_range, dupes = [], [], [], []
        for c in confs:
            seen = set()
            for f in c.fees:
                if f.check_status not in COUNTED:
                    continue
                if f.amount is None or f.amount <= 0:
                    bad_amount.append(f"{label(c)} {f.category}: {f.amount}")
                if not f.currency:
                    no_currency.append(f"{label(c)} {f.category}: {f.amount}")
                usd = convert(f.amount, f.currency, "USD", settings)
                if usd is not None and not (3 <= usd <= 6000):
                    out_of_range.append(
                        f"{label(c)} {f.category}: {f.amount} {f.currency} = US${usd:.0f}"
                    )
                k = (f.category, f.tier, f.amount, f.currency)
                if k in seen:
                    dupes.append(f"{label(c)} {f.category} {f.amount} {f.currency}")
                seen.add(k)
        check("counted fee with zero or negative amount", bad_amount)
        check("counted fee without a currency", no_currency)
        # The validator judges size with rough built-in rates; today's rates can differ a little.
        check("counted fee outside US$3-6,000 at today's rate", out_of_range, must_be_zero=False)
        check("the same fee twice", dupes, must_be_zero=False)

        # fee range stored on the conference = the counted fees, in its main currency
        mismatch = []
        for c in confs:
            want = summarize_fees(
                [(f.amount, f.currency) for f in c.fees if f.check_status in COUNTED]
            )
            if (c.fee_min, c.fee_max, c.currency) != want:
                got = (c.fee_min, c.fee_max, c.currency)
                mismatch.append(f"{label(c)} stored {got}, fees give {want}")
        check("fee range disagrees with the counted fees", mismatch)

        # status agrees with the fees
        check(
            "marked 'enriched' with no counted fee",
            [
                c
                for c in confs
                if c.status == "enriched" and not any(f.check_status in COUNTED for f in c.fees)
            ],
            show=label,
        )
        check(
            "says 'fees not published' but has counted fees",
            [
                c
                for c in confs
                if (c.review_note or "").startswith("fees not published")
                and any(f.check_status in COUNTED for f in c.fees)
            ],
            show=label,
        )
        check(
            "never read (status 'collected')",
            [c for c in confs if c.status == "collected" and not organizer_flag(c.homepage)],
            show=label,
        )

        # ---- duplicates the merge should have caught ----
        groups = defaultdict(list)
        for c in confs:
            if c.start_date and acronym_key(c.acronym):
                groups[(acronym_key(c.acronym), (continent_of(c.country) or "?"))].append(c)
        pairs = []
        for same in groups.values():
            same.sort(key=lambda c: c.start_date)
            for a, b in zip(same, same[1:], strict=False):
                if (b.start_date - a.start_date) <= timedelta(days=7) and (
                    a.country or ""
                ).lower() == (b.country or "").lower():
                    pairs.append(f"{label(a)} / {label(b)} ({a.start_date}, {a.country})")
        check("likely duplicates (same acronym, country, within 7 days)", pairs, must_be_zero=False)

        # ---- links ----
        check(
            "homepage that isn't a web address",
            [
                f"{label(c)} {c.homepage!r}"
                for c in confs
                if c.homepage and not URL.match(c.homepage)
            ],
        )

        # ---- listings the merge left behind ----
        orphans = db.scalars(
            select(RawListing).where(
                RawListing.in_window.is_(True),
                RawListing.is_cs.is_(True),
                RawListing.conference_id.is_(None),
            )
        ).all()
        check(
            "in-window CS listing not merged into a conference",
            orphans,
            show=lambda r: f"{r.source} {r.source_id}",
        )

        # ---- what the public site would publish ----
        site = public_data(db, settings, settings.public_continents)
        published = json.dumps(site, default=str)
        edas = [
            x for (x,) in db.execute(select(RawListing.extra).where(RawListing.source == "edas"))
        ]
        from app.pipeline.privacy import AccountScrubber

        scrub = AccountScrubber(t for e in edas for t in (e or {}).values() if isinstance(t, str))
        check("EDAS account details in the published data", scrub.leaks(published))
        check(
            "'p=' person id in a published EDAS link",
            re.findall(r"edas\.info[^\"]*[?&]p=\d+", published),
        )
        outside = [
            f"#{c['id']} {c['start_date']}"
            for c in site["conferences"]
            if c["start_date"] and not (ws <= c["start_date"] <= we)
        ]
        check("published conference starting outside the window", outside)
        no_status = [c["id"] for c in site["conferences"] if not c["fee_status"]]
        check("published conference without a fee status", no_status)
        std_bad = []
        counted = defaultdict(set)
        for f in site["fees"]:
            counted[f["conference_id"]].add((f["amount"], f["currency"]))
        for c in site["conferences"]:
            s = c["standard_fee"]
            if s and (s["amount"], s["currency"]) not in counted[c["id"]]:
                std_bad.append(f"#{c['id']} {s['amount']} {s['currency']}")
        check("headline fee that isn't one of its published fees", std_bad)
        no_usd = [
            f"#{f['conference_id']} {f['amount']} {f['currency']}"
            for f in site["fees"]
            if f["currency"] and f["amount_usd"] is None
        ]
        check("published fee with a currency the rates can't convert", no_usd, must_be_zero=False)
        week_total = sum(w["total"] for w in site["meta"]["weeks"])
        visible = sum(
            1
            for c in site["conferences"]
            if not c["organizer_flag"] and c["start_date"] and ws <= c["start_date"] <= we
        )
        check(
            "terrain weeks don't add up to the conferences shown",
            [] if week_total == visible else [f"weeks {week_total} vs {visible}"],
        )

    failed = 0
    scope = "all continents" if args.all else ", ".join(settings.public_continents)
    print(f"Audit of {len(confs)} conferences ({scope})\n")
    for name, must, n, examples in results:
        mark = "ok  " if n == 0 else ("FAIL" if must else "note")
        failed += n > 0 and must
        print(f"{mark} {n:5d}  {name}")
        for e in examples:
            print(f"              {e}")
    print(f"\n{'ALL MUST-BE-ZERO CHECKS PASS' if not failed else f'{failed} CHECKS FAILED'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
