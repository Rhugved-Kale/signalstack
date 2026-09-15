"""Sanity check that the ORM can round-trip a row against the real database.

This test deliberately does NOT mock the database: it exercises the local
containerized Postgres on port 5433, so a failure here means the connection
settings or the migration state are wrong.

Requires: `docker compose up -d` and `alembic upgrade head`.
"""

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from app.db.models import Campaign
from app.db.session import SessionLocal


@pytest.fixture
def session():
    """A session that is always closed, even if the test fails."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def test_campaign_round_trip(session):
    # A unique external_id keeps repeated runs independent.
    external_id = f"test-camp-{uuid.uuid4()}"

    campaign = Campaign(
        external_id=external_id,
        name="Spring Sale — Search",
        channel="google_ads",
        platform="google_ads",
    )
    session.add(campaign)
    session.commit()

    new_id = campaign.id
    assert new_id is not None, "primary key should be populated after commit"

    # Read it back through a fresh query rather than the identity map.
    session.expire_all()
    fetched = session.scalar(select(Campaign).where(Campaign.external_id == external_id))

    assert fetched is not None
    assert fetched.id == new_id
    assert fetched.external_id == external_id
    assert fetched.name == "Spring Sale — Search"
    assert fetched.channel == "google_ads"
    assert fetched.platform == "google_ads"

    # server_default=now() must have populated a timezone-aware timestamp.
    assert isinstance(fetched.created_at, dt.datetime)
    assert fetched.created_at.tzinfo is not None, "created_at must be timezone-aware"

    # Clean up so the test leaves no residue.
    session.delete(fetched)
    session.commit()

    assert session.scalar(
        select(Campaign).where(Campaign.external_id == external_id)
    ) is None, "campaign should be gone after delete"
