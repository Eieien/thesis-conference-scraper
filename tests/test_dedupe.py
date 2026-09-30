from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Conference, Fee, RawListing
from app.pipeline.merge import acronym_key, find_duplicates, run_dedupe


def _conf(id, acronym, name, start, city="Taipei", country="Taiwan", **kw):
    return Conference(
        id=id,
        acronym=acronym,
        name=name,
        start_date=start,
        city=city,
        country=country,
        **{"field_sources": {}, **kw},
    )


@pytest.mark.parametrize(
    "raw,key",
    [("Ei/Scopus-AI2A", "AI2A"), ("CIIS--EI", "CIIS"), ("IEEE WECE", "WECE"), ("ICEI", "ICEI")],
)
def test_indexing_claims_are_not_part_of_the_acronym(raw, key):
    assert acronym_key(raw) == key


def test_real_duplicates_are_paired():
    # Pairs found in the data on 2026-09-29.
    d = date(2026, 10, 1)
    confs = [
        _conf(1, "TSSA", "The 20th International Conference on Telecommunication Systems", d),
        _conf(2, "TSSA", "2026 20th International Conference on Telecommunication Systems", d),
        _conf(
            3,
            "AI2A",
            "2026 6th International Conference on Artificial Intelligence",
            date(2026, 10, 23),
        ),
        _conf(
            4,
            "Ei/Scopus-AI2A",
            "2026 IEEE 6th International Conference on Artificial Intelligence",
            date(2026, 10, 23),
        ),
        _conf(
            5,
            "ICETC",
            "The 7th IEICE-CS International Conference on Emerging Technologies for Comms",
            date(2026, 11, 23),
        ),
        _conf(
            6,
            "ICETCCOM",
            "2026 7th IEICE-CS International Conference on Emerging Technologies for Comms",
            date(2026, 11, 23),
        ),
    ]
    pairs = {frozenset((k.id, d.id)) for k, d in find_duplicates(confs)}
    assert pairs == {frozenset((1, 2)), frozenset((3, 4)), frozenset((5, 6))}


def test_different_events_are_not_paired():
    d = date(2026, 10, 23)
    confs = [
        # Same acronym, other city and dates: two different conferences.
        _conf(
            1,
            "ICCSSE",
            "Computer Science and Software Engineering",
            date(2026, 10, 1),
            "Beijing",
            "China",
        ),
        _conf(2, "IEEE ICCSSE", "Control Science and Systems Engineering", d, "Wuhan", "China"),
        # Co-located at one venue on one day, different acronyms: separate events.
        _conf(
            3,
            "AIVRCH",
            "International Conference on AI, VR and Cultural Heritage",
            d,
            "Wuhan",
            "China",
        ),
        _conf(
            4,
            "EDAI",
            "International Conference on Economic Data Analytics and AI",
            d,
            "Wuhan",
            "China",
        ),
    ]
    assert find_duplicates(confs) == []


def test_combine_keeps_the_better_one_and_moves_everything():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        d = date(2026, 10, 1)
        with_fees = _conf(
            1,
            "TSSA",
            "The 20th TSSA",
            d,
            field_sources={"homepage": {"source": "homepage", "url": "x"}},
        )
        with_fees.homepage = "https://tssa.example"
        with_fees.fee_min, with_fees.fee_max, with_fees.currency = 350.0, 600.0, "USD"
        bare = _conf(2, "TSSA", "2026 20th TSSA", d, registration_url="https://reg.example")
        db.add_all([with_fees, bare])
        db.add(
            Fee(
                conference_id=1,
                category="Author",
                source="homepage",
                amount=350,
                currency="USD",
                check_status="verified",
            )
        )
        db.add(RawListing(source="easychair", source_id="a", name="TSSA", conference_id=1))
        db.add(RawListing(source="ieee", source_id="b", name="TSSA", conference_id=2))
        db.commit()

        stats = run_dedupe(db)
        assert stats["pairs"] == 1
        left = db.scalars(select(Conference)).all()
        assert [c.id for c in left] == [1]  # the one with fees and homepage values
        kept = left[0]
        assert sorted(r.source for r in kept.listings) == ["easychair", "ieee"]
        assert kept.registration_url == "https://reg.example"  # filled from the other
        assert kept.fee_min == 350.0
