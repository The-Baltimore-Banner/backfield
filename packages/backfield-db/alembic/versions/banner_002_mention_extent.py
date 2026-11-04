"""Record whether a location's geometry IS an article's event, or only contains it.

``substrate_location_mention.extent`` takes ``coextensive`` -- the stored shape is
what the story describes -- or ``within_place`` -- the shape only bounds it, and the
event happened at some real spot inside. ``within_place`` is not a synonym for "a
point inside an area": a crash on a road is ``within_place`` along a line.

It belongs on the mention rather than on the location because one location row serves
many articles. Annapolis is the whole subject of one story and the container of a
single shooting in the next, from the identical stored shape. ``nature`` sits on the
mention for the same reason.

**No backfill, by decision.** Every existing row stays NULL, and NULL is a third state
meaning nothing was recorded -- not ``coextensive`` and not ``within_place``. Old rows
therefore keep today's proximity behaviour instead of acquiring a containment claim
nobody made. Area rows cannot be filled without re-running the extractor, and for a
point geometry NULL and ``coextensive`` behave identically under any containment
predicate, so a backfill would buy nothing it could prove.

Nullable Text with an index, matching ``nature`` on the same table. The valid values
are normalized in Python (``PLACE_MENTION_EXTENT_VALUES``) rather than pinned by a
CHECK constraint, again matching ``nature``.

Banner fork revision. Fork revisions are namespaced ``banner_NNN_`` so they can never
collide with an upstream ``0NN_`` id; an upstream merge that forks the chain is
resolved with an ``alembic merge`` revision, never by re-pointing a ``down_revision``.
Revision ids stay at or under 32 characters, the width of ``alembic_version``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "banner_002_mention_extent"
down_revision: str | None = "banner_001_geocode_layer"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "substrate_location_mention",
        sa.Column("extent", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_substrate_location_mention_extent",
        "substrate_location_mention",
        ["extent"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_substrate_location_mention_extent",
        table_name="substrate_location_mention",
    )
    op.drop_column("substrate_location_mention", "extent")
