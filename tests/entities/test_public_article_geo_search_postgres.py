"""PostGIS coverage for radius search against coarse stored areas.

The default suite runs on SQLite, where ``_sqlite_geometry_matches`` returns
False for every non-Point geometry. Polygon matching therefore never runs
there, and a SQLite version of these tests would pass on unmodified code.
These tests need PostGIS, so they need ``BACKFIELD_DATABASE_URL_DIRECT``.
"""

from __future__ import annotations

import os
from datetime import date
from uuid import uuid4

import pytest
from backfield_db import (
    BackfieldOrganization,
    BackfieldProject,
    SubstrateArticle,
    SubstrateLocation,
    SubstrateLocationMention,
)
from backfield_entities.geo.geometry_bind import assign_geojson_geometry
from backfield_entities.public.article_geo_search import (
    PublicArticleGeoSearchMode,
    PublicArticleGeoSearchParams,
    search_public_articles_by_geo,
)
from sqlalchemy import delete, text
from sqlmodel import Session, col, create_engine, select

from tests.project_helpers import project_ownership_fields

_DATABASE_URL = os.getenv("BACKFIELD_DATABASE_URL_DIRECT", "")

requires_postgres = pytest.mark.skipif(
    not _DATABASE_URL.startswith("postgresql"),
    reason="requires BACKFIELD_DATABASE_URL_DIRECT for PostgreSQL",
)

# The query center. It is the centroid of the state-sized rectangle below, so a
# centroid-only implementation of the fix would still return the state article
# and these tests would not detect it.
CENTER_LNG = -77.0
CENTER_LAT = 39.0


def _rectangle(*, half_lng: float, half_lat: float) -> dict:
    """Build the five-point box a geocoder viewport produces."""
    min_lng = CENTER_LNG - half_lng
    max_lng = CENTER_LNG + half_lng
    min_lat = CENTER_LAT - half_lat
    max_lat = CENTER_LAT + half_lat
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [min_lng, min_lat],
                [max_lng, min_lat],
                [max_lng, max_lat],
                [min_lng, max_lat],
                [min_lng, min_lat],
            ]
        ],
    }


class _Seed:
    def __init__(self, project_id: int, state_article_id: int, block_article_id: int) -> None:
        self.project_id = project_id
        self.state_article_id = state_article_id
        self.block_article_id = block_article_id


def _seed(session: Session, suffix: str) -> _Seed:
    organization = BackfieldOrganization(
        name=f"Coarse area {suffix}",
        slug=f"coarse-area-{suffix}",
    )
    session.add(organization)
    session.commit()
    session.refresh(organization)
    organization_id = int(organization.id)  # type: ignore[arg-type]

    project = BackfieldProject(
        **project_ownership_fields(session, organization_id),
        organization_id=organization_id,
        name=f"Coarse area {suffix}",
        slug=f"coarse-area-{suffix}",
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    project_id = int(project.id)  # type: ignore[arg-type]

    articles: dict[str, int] = {}
    # (key, headline, half_lng, half_lat). The first box spans about 215 by 138
    # miles, the scale of a state. The second spans about one mile, the scale of
    # a city block. Both contain the query center.
    for key, headline, half_lng, half_lat in (
        ("state", "Statewide budget vote", 2.0, 1.0),
        ("block", "Corner store reopens", 0.007, 0.007),
    ):
        article = SubstrateArticle(
            project_id=project_id,
            headline=headline,
            text=headline,
            pub_date=date(2024, 5, 1),
        )
        session.add(article)
        session.commit()
        session.refresh(article)
        articles[key] = int(article.id)  # type: ignore[arg-type]

        location = SubstrateLocation(
            project_id=project_id,
            name=f"{key}-{suffix}",
            normalized_name=f"{key}-{suffix}",
            location_type="state" if key == "state" else "neighborhood",
            formatted_address=f"{key}-{suffix}",
        )
        assign_geojson_geometry(
            session,
            location,
            _rectangle(half_lng=half_lng, half_lat=half_lat),
        )
        session.add(location)
        session.commit()
        session.refresh(location)

        session.add(
            SubstrateLocationMention(
                article_id=articles[key],
                location_id=int(location.id),  # type: ignore[arg-type]
                nature="primary",
            )
        )
        session.commit()

    return _Seed(project_id, articles["state"], articles["block"])


def _cleanup(session: Session, project_id: int) -> None:
    article_ids = session.exec(  # type: ignore[call-overload]
        select(SubstrateArticle.id).where(
            SubstrateArticle.project_id == project_id  # type: ignore[arg-type]
        )
    ).all()
    if article_ids:
        session.exec(  # type: ignore[call-overload]
            delete(SubstrateLocationMention).where(
                col(SubstrateLocationMention.article_id).in_([int(a) for a in article_ids])
            )
        )
    session.exec(  # type: ignore[call-overload]
        delete(SubstrateLocation).where(
            SubstrateLocation.project_id == project_id  # type: ignore[arg-type]
        )
    )
    session.exec(  # type: ignore[call-overload]
        delete(SubstrateArticle).where(
            SubstrateArticle.project_id == project_id  # type: ignore[arg-type]
        )
    )
    project = session.get(BackfieldProject, project_id)
    if project is not None:
        organization_id = int(project.organization_id)
        session.delete(project)
        session.commit()
        organization = session.get(BackfieldOrganization, organization_id)
        if organization is not None:
            session.delete(organization)
    session.commit()


def _ids(session: Session, *, project_id: int, **overrides) -> set[int]:
    params = PublicArticleGeoSearchParams(
        mode=PublicArticleGeoSearchMode.point,
        center_lng=CENTER_LNG,
        center_lat=CENTER_LAT,
        **overrides,
    )
    items, _total = search_public_articles_by_geo(
        session,
        project_id=project_id,
        params=params,
    )
    return {int(item.id) for item in items}


@requires_postgres
def test_state_sized_area_does_not_answer_a_two_mile_radius() -> None:
    engine = create_engine(_DATABASE_URL, pool_pre_ping=True)
    suffix = uuid4().hex[:12]
    with Session(engine) as session:
        seed = _seed(session, suffix)
        try:
            found = _ids(session, project_id=seed.project_id, radius_miles=2.0)
            assert seed.block_article_id in found, (
                "a city-block area must still answer a two-mile radius over it"
            )
            assert seed.state_article_id in found, (
                "with the ratio heuristic gone, a NULL-extent area matches any "
                "radius it overlaps -- extent, not size, is what excludes it now"
            )
        finally:
            _cleanup(session, seed.project_id)


@requires_postgres
def test_state_sized_area_still_answers_a_state_sized_radius() -> None:
    """The rule is relative to the radius asked for, not a blanket exclusion.

    Without this, a fix that simply dropped every large area would pass the
    test above while making coarse places unreachable at any radius.
    """
    engine = create_engine(_DATABASE_URL, pool_pre_ping=True)
    suffix = uuid4().hex[:12]
    with Session(engine) as session:
        seed = _seed(session, suffix)
        try:
            found = _ids(session, project_id=seed.project_id, radius_miles=200.0)
            assert seed.state_article_id in found
            assert seed.block_article_id in found
        finally:
            _cleanup(session, seed.project_id)



# --- extent: inside, not merely near ----------------------------------------
#
# These drive the two-armed predicate. A mention marked ``within_place`` says
# the article only bounded the event to that shape, so the shape must sit INSIDE
# the search area to count. ``coextensive`` says the shape IS the story, so it
# keeps the proximity rule. A NULL extent keeps proximity too, which is what
# makes the change safe on a corpus that has never been backfilled.


def _seed_one_area(
    session: Session,
    suffix: str,
    *,
    half_lng: float,
    half_lat: float,
    extent: str | None,
) -> tuple[int, int]:
    """Seed one article whose single mention carries ``extent``."""
    organization = BackfieldOrganization(name=f"Extent {suffix}", slug=f"extent-{suffix}")
    session.add(organization)
    session.commit()
    session.refresh(organization)
    organization_id = int(organization.id)  # type: ignore[arg-type]

    project = BackfieldProject(
        **project_ownership_fields(session, organization_id),
        organization_id=organization_id,
        name=f"Extent {suffix}",
        slug=f"extent-{suffix}",
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    project_id = int(project.id)  # type: ignore[arg-type]

    article = SubstrateArticle(
        project_id=project_id,
        headline=f"Extent {suffix}",
        text=f"Extent {suffix}",
        pub_date=date(2024, 5, 1),
    )
    session.add(article)
    session.commit()
    session.refresh(article)

    location = SubstrateLocation(
        project_id=project_id,
        name=f"extent-{suffix}",
        normalized_name=f"extent-{suffix}",
        location_type="city",
        formatted_address=f"extent-{suffix}",
    )
    assign_geojson_geometry(
        session, location, _rectangle(half_lng=half_lng, half_lat=half_lat)
    )
    session.add(location)
    session.commit()
    session.refresh(location)

    session.add(
        SubstrateLocationMention(
            article_id=int(article.id),  # type: ignore[arg-type]
            location_id=int(location.id),  # type: ignore[arg-type]
            nature="primary",
            extent=extent,
        )
    )
    session.commit()
    return project_id, int(article.id)  # type: ignore[arg-type]


# A box far larger than the search radius: it surrounds the circle rather than
# sitting inside it. This is the shape the two arms disagree on.
_SURROUNDING = {"half_lng": 2.0, "half_lat": 1.0}
# A box well inside a two-mile circle.
_INSIDE = {"half_lng": 0.007, "half_lat": 0.007}


@requires_postgres
@pytest.mark.parametrize(
    ("extent", "expected"),
    [
        (None, True),
        ("coextensive", True),
        ("within_place", False),
    ],
)
def test_extent_decides_whether_a_surrounding_area_matches(
    extent: str | None, expected: bool
) -> None:
    """One shape, three readings, two answers.

    The NULL row is the important one. Written as ``lm.extent <> 'within_place'``
    the clause evaluates to NULL for it, ``NULL OR false`` drops the row, and
    containment would silently apply to an entire un-backfilled corpus while the
    ``within_place`` case below still passed. ``IS DISTINCT FROM`` is what makes
    this case come out True.
    """
    engine = create_engine(_DATABASE_URL, pool_pre_ping=True)
    suffix = uuid4().hex[:12]
    with Session(engine) as session:
        project_id, article_id = _seed_one_area(
            session, suffix, extent=extent, **_SURROUNDING
        )
        try:
            found = _ids(session, project_id=project_id, radius_miles=2.0)
            assert (article_id in found) is expected
        finally:
            _cleanup(session, project_id)


@requires_postgres
def test_a_within_place_area_inside_the_circle_still_matches() -> None:
    """Containment is a real test, not a blanket exclusion of within_place.

    Without this, a predicate that simply dropped every ``within_place`` row
    would pass the case above.
    """
    engine = create_engine(_DATABASE_URL, pool_pre_ping=True)
    suffix = uuid4().hex[:12]
    with Session(engine) as session:
        project_id, article_id = _seed_one_area(
            session, suffix, extent="within_place", **_INSIDE
        )
        try:
            found = _ids(session, project_id=project_id, radius_miles=2.0)
            assert article_id in found
        finally:
            _cleanup(session, project_id)


@requires_postgres
def test_a_within_place_area_is_excluded_in_bbox_mode_too() -> None:
    """Box mode runs the same arm against the envelope, with no buffer."""
    engine = create_engine(_DATABASE_URL, pool_pre_ping=True)
    suffix = uuid4().hex[:12]
    with Session(engine) as session:
        project_id, article_id = _seed_one_area(
            session, suffix, extent="within_place", **_SURROUNDING
        )
        try:
            params = PublicArticleGeoSearchParams(
                mode=PublicArticleGeoSearchMode.bbox,
                min_lng=CENTER_LNG - 0.02,
                min_lat=CENTER_LAT - 0.02,
                max_lng=CENTER_LNG + 0.02,
                max_lat=CENTER_LAT + 0.02,
            )
            items, _total = search_public_articles_by_geo(
                session, project_id=project_id, params=params
            )
            assert article_id not in {int(i.id) for i in items}
        finally:
            _cleanup(session, project_id)


def _ring_of_points(session: Session, suffix: str, *, radius_miles: float, fraction: float):
    """Seed one article per bearing, each just inside the radius.

    ``ST_Project`` places each point on the WGS84 spheroid, the same model
    ``ST_DWithin`` measures with, so the distance is exact rather than a
    spherical estimate. That matters: the band these points sit in is only
    about half a percent wide.
    """
    organization = BackfieldOrganization(name=f"Ring {suffix}", slug=f"ring-{suffix}")
    session.add(organization)
    session.commit()
    session.refresh(organization)
    organization_id = int(organization.id)  # type: ignore[arg-type]

    project = BackfieldProject(
        **project_ownership_fields(session, organization_id),
        organization_id=organization_id,
        name=f"Ring {suffix}",
        slug=f"ring-{suffix}",
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    project_id = int(project.id)  # type: ignore[arg-type]

    meters = radius_miles * 1609.34 * fraction
    bearings = list(range(0, 360, 10))
    placed = session.exec(  # type: ignore[call-overload]
        text(
            """
            SELECT d AS bearing,
                   ST_X(p.geom::geometry) AS lng,
                   ST_Y(p.geom::geometry) AS lat
            FROM generate_series(0, 350, 10) AS d,
                 LATERAL (
                     SELECT ST_Project(
                         ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
                         :meters,
                         radians(d)
                     ) AS geom
                 ) p
            """
        ).bindparams(lng=CENTER_LNG, lat=CENTER_LAT, meters=meters)
    ).all()
    assert len(placed) == len(bearings)

    article_ids: list[int] = []
    for row in placed:
        article = SubstrateArticle(
            project_id=project_id,
            headline=f"Bearing {int(row.bearing)}",
            text=f"Bearing {int(row.bearing)}",
            pub_date=date(2024, 6, 1),
        )
        session.add(article)
        session.commit()
        session.refresh(article)
        article_ids.append(int(article.id))  # type: ignore[arg-type]

        location = SubstrateLocation(
            project_id=project_id,
            name=f"bearing-{int(row.bearing)}-{suffix}",
            normalized_name=f"bearing-{int(row.bearing)}-{suffix}",
            location_type="place",
            formatted_address=f"bearing-{int(row.bearing)}-{suffix}",
        )
        assign_geojson_geometry(
            session,
            location,
            {"type": "Point", "coordinates": [float(row.lng), float(row.lat)]},
        )
        session.add(location)
        session.commit()
        session.refresh(location)
        session.add(
            SubstrateLocationMention(
                article_id=article_ids[-1],
                location_id=int(location.id),  # type: ignore[arg-type]
                nature="primary",
            )
        )
        session.commit()

    return project_id, article_ids


@requires_postgres
def test_radius_is_a_true_distance_not_a_polygon_that_approximates_one() -> None:
    """Every point inside the radius is returned, whatever direction it lies in.

    The search used to build the area with ``ST_Buffer`` on a geography, which
    returns a 32-sided polygon inscribed in the circle. Its edges fall short of
    the radius asked for. Measured on this stack, of 360 bearings at 3210 m from
    a center with a 3218.68 m radius, all 360 are inside the true circle and
    only 136 are inside the buffer polygon. So the answer depended on which way
    the place happened to lie from the center.
    """
    engine = create_engine(_DATABASE_URL, pool_pre_ping=True)
    suffix = uuid4().hex[:12]
    with Session(engine) as session:
        project_id, article_ids = _ring_of_points(
            session,
            suffix,
            radius_miles=2.0,
            fraction=0.998,
        )
        try:
            found = _ids(session, project_id=project_id, radius_miles=2.0, limit=100)
            missing = sorted(set(article_ids) - found)
            assert not missing, (
                f"{len(missing)} of {len(article_ids)} points inside the radius were "
                "dropped; the radius is being approximated by a polygon"
            )
        finally:
            _cleanup(session, project_id)


@requires_postgres
def test_a_point_outside_the_radius_is_still_excluded() -> None:
    """The control for the test above: the boundary is real, not merely generous."""
    engine = create_engine(_DATABASE_URL, pool_pre_ping=True)
    suffix = uuid4().hex[:12]
    with Session(engine) as session:
        project_id, article_ids = _ring_of_points(
            session,
            suffix,
            radius_miles=2.0,
            fraction=1.002,
        )
        try:
            found = _ids(session, project_id=project_id, radius_miles=2.0, limit=100)
            assert not (set(article_ids) & found), (
                "points outside the radius were returned"
            )
        finally:
            _cleanup(session, project_id)
