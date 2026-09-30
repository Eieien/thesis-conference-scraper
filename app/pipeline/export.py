import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.models import Conference, RawListing
from app.pipeline.fee_kinds import headline_fees
from app.pipeline.fx import convert
from app.pipeline.geo import continent_of
from app.pipeline.quality import organizer_flag

COLUMNS = [
    "acronym",
    "name",
    "start_date",
    "end_date",
    "city",
    "country",
    "description",
    "abstract_deadline",
    "paper_deadline",
    "notification_date",
    "camera_ready_deadline",
    "registration_deadline",
    "continent",
    "organizer_flag",
    "online",
    "history_label",
    "history_years",
    "fee_min",
    "fee_max",
    "currency",
    "fee_min_usd",
    "fee_max_usd",
    "fee_min_php",
    "fee_max_php",
    "standard_fee",
    "standard_fee_usd",
    "standard_fee_php",
    "student_fee",
    "student_fee_usd",
    "student_fee_php",
    "fee_breakdown",
    "registration_url",
    "homepage",
    "sources",
    "status",
    "review_note",
    "field_sources",
]


def conference_rows(
    db: Session,
    settings: Settings,
    *,
    continents: list[str] | None = None,
    source: str | None = None,
    where: list | None = None,
) -> list[dict]:
    """One row per conference. `continents` keeps only those continents (None = all)."""
    stmt = (
        select(Conference)
        .options(selectinload(Conference.fees), selectinload(Conference.listings))
        .order_by(Conference.start_date, Conference.acronym)
    )
    if source:
        stmt = stmt.where(Conference.listings.any(RawListing.source == source))
    if where:
        stmt = stmt.where(*where)
    rows = []
    for c in db.scalars(stmt).all():
        continent = continent_of(c.country)
        if continents is not None and (continent or "Unknown") not in continents:
            continue
        row = {col: getattr(c, col, None) for col in COLUMNS}
        row["continent"] = continent
        row["organizer_flag"] = organizer_flag(c.homepage)
        row["online"] = (c.attendance or {}).get("mode")
        h = c.history
        row["history_label"] = (
            None if h is None else "has history" if h.get("has_history") else "no history found"
        )
        row["history_years"] = ", ".join(str(e["year"]) for e in (h or {}).get("editions", []))
        for side in ("min", "max"):
            amount = getattr(c, f"fee_{side}")
            row[f"fee_{side}_usd"] = convert(amount, c.currency, "USD", settings)
            row[f"fee_{side}_php"] = convert(amount, c.currency, "PHP", settings)
        standard, student = headline_fees(
            c.fees, lambda f: convert(f.amount, f.currency, "USD", settings)
        )
        for name, fee in (("standard_fee", standard), ("student_fee", student)):
            row[name] = (
                f"{fee.amount:g} {fee.currency or ''} ({fee.category})".strip() if fee else None
            )
            row[f"{name}_usd"] = convert(fee.amount, fee.currency, "USD", settings) if fee else None
            row[f"{name}_php"] = convert(fee.amount, fee.currency, "PHP", settings) if fee else None
        row["fee_breakdown"] = json.dumps(
            [
                {
                    "category": f.category,
                    "tier": f.tier,
                    "amount": f.amount,
                    "currency": f.currency,
                    "amount_usd": convert(f.amount, f.currency, "USD", settings),
                    "amount_php": convert(f.amount, f.currency, "PHP", settings),
                    "check": f.check_status,
                }
                for f in c.fees
            ],
            ensure_ascii=False,
        )
        row["sources"] = ", ".join(sorted({f"{row_.source}:{row_.url}" for row_ in c.listings}))
        row["field_sources"] = json.dumps(c.field_sources or {}, ensure_ascii=False)
        rows.append(row)
    return rows


def export_conferences(
    db: Session,
    settings: Settings,
    fmt: str,
    *,
    continents: list[str] | None = None,
    source: str | None = None,
    where: list | None = None,
) -> Path:
    rows = conference_rows(db, settings, continents=continents, source=source, where=where)
    df = pd.DataFrame(rows, columns=COLUMNS)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = settings.export_dir / f"conferences-{stamp}.{fmt}"
    if fmt == "xlsx":
        df.to_excel(path, index=False)
    else:
        df.to_csv(path, index=False, encoding="utf-8-sig")  # BOM so Excel reads UTF-8
    return path
