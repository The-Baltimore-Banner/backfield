"""Store the granularity the geocoder reported, beside the formatted address.

Pelias returns a ``layer`` (``address``, ``street``, ``locality``, ``county``,
``region``, ``country`` and others). The pipeline already decides with it -- an
area model scores a wrong-layer candidate out, and ``street_road`` refuses a
candidate that is not ``street`` -- and then drops it before the row is
written. Keeping it makes "what we asked for" versus "what we got" a column
comparison instead of a parse of ``formatted_address``.

Nullable on both tables. Providers that do not report a layer leave it NULL,
which means "not reported" rather than "unknown granularity".

Banner fork revision. Fork revisions are namespaced ``banner_NNN_`` so they can
never collide with an upstream ``0NN_`` id; an upstream merge that forks the
chain is resolved with an ``alembic merge`` revision, never by re-pointing a
``down_revision``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "banner_001_geocode_layer"
down_revision: str | None = "083_article_headline_url_fts"
branch_labels: Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "substrate_location",
        sa.Column("geocode_layer", sa.Text(), nullable=True),
    )
    op.add_column(
        "substrate_location_cache",
        sa.Column("geocode_layer", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("substrate_location_cache", "geocode_layer")
    op.drop_column("substrate_location", "geocode_layer")
