"""Geographic article search via location mention geometry.

The two backends do not agree, and that predates the coarse-area rule below.
PostGIS matches any geometry against the search area. The SQLite path reads
``geometry_json`` and matches points only, so a polygon never matches there.
Both paths apply the coarse-area rule, but on SQLite every area that can match
is a point, whose radius is zero, so the rule never binds. A SQLite test of the
rule would therefore pass without it. The coverage lives in
``tests/entities/test_public_article_geo_search_postgres.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Literal

from backfield_db import SubstrateArticle, SubstrateLocation, SubstrateLocationMention
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlmodel import Session, col, select

from backfield_entities.entities.location.types import (
    PLACE_MENTION_EXTENT_WITHIN_PLACE,
)
from backfield_entities.public.article_hub import (
    PublicArticleLocationOut,
    location_mentions_out_by_ids,
)
from backfield_entities.public.articles import (
    ArticleMetaClause,
    PublicArticleOut,
    _apply_public_article_list_filters,
    _article_to_public_out,
    _meta_rows_for_articles,
)
from backfield_entities.public.nature_filters import normalize_natures, postgres_natures_in_filter

MILES_TO_METERS = 1609.34
EARTH_RADIUS_MILES = 3958.7613

#: How much coarser than the radius asked for a stored area may be.
#:
#: Backfield stores the geocoder viewport box, not the outline of the place. A
#: box always covers more ground than the place it describes, and the error
#: grows with the size of the place. So an area much larger than the radius
#: asked for carries no information about whether the place is near the center
#: of the query. Matching it anyway gives a precise-looking answer that is
#: wrong: an article bound only to a state answers a two-mile radius anywhere in
#: that state.
#:
#: A radius query therefore keeps an area only while the area's own radius --
#: half the diagonal of its bounding box -- is at most this multiple of the
#: radius asked for. A point has radius zero and always qualifies.
#:


class PublicArticleGeoSearchMode(StrEnum):
    point = "point"
    bbox = "bbox"


@dataclass(frozen=True)
class PublicArticleGeoSearchParams:
    mode: PublicArticleGeoSearchMode
    center_lng: float | None = None
    center_lat: float | None = None
    radius_miles: float | None = None
    min_lng: float | None = None
    min_lat: float | None = None
    max_lng: float | None = None
    max_lat: float | None = None
    location_types: tuple[str, ...] = ()
    natures: tuple[str, ...] = ()
    meta_type: str | None = None
    meta_category: str | None = None
    exclude_meta_type: str | None = None
    exclude_meta_category: str | None = None
    meta_clauses: tuple[ArticleMetaClause, ...] = ()
    pub_date_from: date | None = None
    pub_date_to: date | None = None
    limit: int = 25
    offset: int = 0


class PublicGeoBboxOut(BaseModel):
    min_lng: float
    min_lat: float
    max_lng: float
    max_lat: float


class PublicArticleGeoSearchQueryOut(BaseModel):
    """Echo of geographic search filters applied to a list response."""

    search_mode: Literal["point", "bbox"]
    center_lng: float | None = None
    center_lat: float | None = None
    radius_miles: float | None = None
    bbox: PublicGeoBboxOut | None = None
    location_types: list[str] = Field(default_factory=list)
    natures: list[str] = Field(default_factory=list)
    meta_type: str | None = None
    meta_category: str | None = None
    exclude_meta_type: str | None = None
    exclude_meta_category: str | None = None
    pub_date_from: date | None = None
    pub_date_to: date | None = None


def _geometry_bounds(geometry_json: dict | None) -> tuple[float, float, float, float] | None:
    """Return (min_lng, min_lat, max_lng, max_lat) for any GeoJSON geometry."""
    if not isinstance(geometry_json, dict):
        return None
    coordinates = geometry_json.get("coordinates")
    lngs: list[float] = []
    lats: list[float] = []

    def _walk(node: object) -> None:
        if isinstance(node, (list, tuple)):
            if (
                len(node) >= 2
                and isinstance(node[0], (int, float))
                and isinstance(node[1], (int, float))
            ):
                lngs.append(float(node[0]))
                lats.append(float(node[1]))
                return
            for child in node:
                _walk(child)

    _walk(coordinates)
    if not lngs:
        return None
    return min(lngs), min(lats), max(lngs), max(lats)


def public_article_geo_search_query_out(
    params: PublicArticleGeoSearchParams,
) -> PublicArticleGeoSearchQueryOut:
    """Build the query echo object from geographic search params."""
    bbox: PublicGeoBboxOut | None = None
    if params.mode is PublicArticleGeoSearchMode.bbox:
        assert params.min_lng is not None
        assert params.min_lat is not None
        assert params.max_lng is not None
        assert params.max_lat is not None
        bbox = PublicGeoBboxOut(
            min_lng=params.min_lng,
            min_lat=params.min_lat,
            max_lng=params.max_lng,
            max_lat=params.max_lat,
        )
    return PublicArticleGeoSearchQueryOut(
        search_mode=params.mode.value,
        center_lng=params.center_lng if params.mode is PublicArticleGeoSearchMode.point else None,
        center_lat=params.center_lat if params.mode is PublicArticleGeoSearchMode.point else None,
        radius_miles=params.radius_miles
        if params.mode is PublicArticleGeoSearchMode.point
        else None,
        bbox=bbox,
        location_types=list(params.location_types),
        natures=list(normalize_natures(params.natures)),
        meta_type=params.meta_type,
        meta_category=params.meta_category,
        exclude_meta_type=params.exclude_meta_type,
        exclude_meta_category=params.exclude_meta_category,
        pub_date_from=params.pub_date_from,
        pub_date_to=params.pub_date_to,
    )


class PublicArticleGeoSearchItemOut(PublicArticleOut):
    matching_locations: list[PublicArticleLocationOut] = Field(default_factory=list)


def _point_coordinates(geometry_json: dict | None) -> tuple[float, float] | None:
    if not isinstance(geometry_json, dict):
        return None
    geom_type = geometry_json.get("type")
    coordinates = geometry_json.get("coordinates")
    if geom_type == "Point" and isinstance(coordinates, list) and len(coordinates) >= 2:
        return float(coordinates[0]), float(coordinates[1])
    return None


def _haversine_miles(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(d_lng / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))


def _sqlite_geometry_matches(
    geometry_json: dict | None,
    *,
    params: PublicArticleGeoSearchParams,
) -> bool:
    point = _point_coordinates(geometry_json)
    if point is None:
        return False
    lng, lat = point
    if params.mode is PublicArticleGeoSearchMode.point:
        assert params.center_lng is not None
        assert params.center_lat is not None
        assert params.radius_miles is not None
        return (
            _haversine_miles(params.center_lng, params.center_lat, lng, lat) <= params.radius_miles
        )
    assert params.min_lng is not None
    assert params.min_lat is not None
    assert params.max_lng is not None
    assert params.max_lat is not None
    return params.min_lng <= lng <= params.max_lng and params.min_lat <= lat <= params.max_lat


def _postgres_location_types_filter(
    location_types: tuple[str, ...],
    bind: dict[str, object],
) -> str:
    if not location_types:
        return ""
    placeholders = ", ".join(f":location_type_{index}" for index in range(len(location_types)))
    for index, value in enumerate(location_types):
        bind[f"location_type_{index}"] = value
    return f"AND sl.location_type IN ({placeholders})"


def _postgres_matching_pairs(
    session: Session,
    *,
    project_id: int,
    params: PublicArticleGeoSearchParams,
) -> list[tuple[int, int]]:
    bind: dict[str, object] = {
        "project_id": project_id,
        "extent_within_place": PLACE_MENTION_EXTENT_WITHIN_PLACE,
    }
    location_type_filter = _postgres_location_types_filter(params.location_types, bind)
    nature_filter = postgres_natures_in_filter(params.natures, bind, column_sql="lm.nature")

    if params.mode is PublicArticleGeoSearchMode.point:
        assert params.center_lng is not None
        assert params.center_lat is not None
        assert params.radius_miles is not None
        bind["center_lng"] = params.center_lng
        bind["center_lat"] = params.center_lat
        bind["radius_meters"] = params.radius_miles * MILES_TO_METERS
        # Hand the radius to ST_DWithin as its distance, so the search area is
        # the center point itself. That measures geodesic distance on the WGS84
        # spheroid.
        #
        # The proximity arm hands the radius to ST_DWithin as its distance, so
        # the search area is the centre point itself and the measurement is
        # geodesic on the WGS84 spheroid. That form also lets the geography
        # index do the work.
        #
        # The containment arm needs a real polygon, because ST_CoveredBy
        # against a bare point can never be true. ST_Buffer on a geography
        # returns an inscribed polygon, so its edges sit slightly inside the
        # radius asked for. Measured on PostGIS 3.6 at a 2-mile radius: 15.00 m
        # inside at the middle of each edge at the default 32 sides, 0.47 m at
        # quad_segs=32 (128 sides). Raising it further overshoots -- at 64 the
        # edge lands 0.26 m OUTSIDE the true circle -- because the floor is set
        # by the geography buffer's project/buffer/unproject round trip, not by
        # segment count. So 32 is the sweet spot, and overshoot matters more
        # than undershoot here: it would make containment too generous.
        #
        # (An earlier version of this comment said 13.3 m. That figure is wrong
        # and does not reproduce at any radius or latitude tried; the closed
        # form R*(1-cos(pi/32)) gives 15.50 m, just above the measurement.)
        distance_sql = ":radius_meters"
        contains_sql = "ST_CoveredBy(sl.geometry::geography, sa.area)"
        area_cte = """
        WITH search_area AS (
            SELECT ST_SetSRID(
                ST_MakePoint(:center_lng, :center_lat), 4326
            )::geography AS geom,
            ST_Buffer(
                ST_SetSRID(ST_MakePoint(:center_lng, :center_lat), 4326)::geography,
                :radius_meters,
                32
            ) AS area
        )
        """
    else:
        assert params.min_lng is not None
        assert params.min_lat is not None
        assert params.max_lng is not None
        assert params.max_lat is not None
        bind.update(
            {
                "min_lng": params.min_lng,
                "min_lat": params.min_lat,
                "max_lng": params.max_lng,
                "max_lat": params.max_lat,
            }
        )
        distance_sql = "0"
        # The containment arm compares GEOMETRIES here, not geographies, and
        # that is deliberate. Cast to geography and the envelope's top and
        # bottom edges become great circles, which bow poleward of the
        # parallels the caller actually drew. Measured on PostGIS 3.6 at 39N: a
        # point sitting exactly on the drawn max_lat line is 18.98 m inside the
        # geography edge for a 0.4-degree box and 118.63 m inside for a
        # 1-degree box. That is the same order as the buffer error on the
        # radius arm, and in the generous direction.
        #
        # In geometry space the envelope's edges are exactly the lat/lon lines
        # asked for, which is what a box drawn on a map means. It is also
        # strictly tighter than the geography form, so containment stays a
        # subset of the ST_DWithin proximity arm above and cannot admit a row
        # that arm rejected.
        contains_sql = "ST_CoveredBy(sl.geometry, sa.area)"
        area_cte = """
        WITH search_area AS (
            SELECT ST_SetSRID(
                ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326),
                4326
            )::geography AS geom,
            ST_SetSRID(
                ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326),
                4326
            ) AS area
        )
        """

    stmt = text(
        area_cte
        + """
        SELECT lm.id AS mention_id, lm.article_id
        FROM substrate_location_mention lm
        INNER JOIN substrate_article a ON a.id = lm.article_id
        INNER JOIN substrate_location sl ON sl.id = lm.location_id
        CROSS JOIN search_area sa
        WHERE a.project_id = :project_id
          AND a.deleted = false
          AND lm.deleted = false
          AND sl.geometry IS NOT NULL
          AND ST_DWithin(sl.geometry::geography, sa.geom, """
        + distance_sql
        + """)
          AND (
                lm.extent IS DISTINCT FROM :extent_within_place
                OR """
        + contains_sql
        + """
              )
        """
        + location_type_filter
        + nature_filter
    )
    rows = session.exec(stmt.bindparams(**bind)).all()
    if not rows:
        return []

    article_ids = {int(row.article_id) for row in rows}
    article_filter_stmt = select(SubstrateArticle.id).where(
        col(SubstrateArticle.id).in_(article_ids),
        SubstrateArticle.project_id == project_id,
        SubstrateArticle.deleted == False,  # noqa: E712
    )
    article_filter_stmt = _apply_public_article_list_filters(
        article_filter_stmt,
        meta_type=params.meta_type,
        meta_category=params.meta_category,
        exclude_meta_type=params.exclude_meta_type,
        exclude_meta_category=params.exclude_meta_category,
        meta_clauses=params.meta_clauses,
        pub_date_from=params.pub_date_from,
        pub_date_to=params.pub_date_to,
    )
    allowed_article_ids = {int(aid) for aid in session.exec(article_filter_stmt).all()}
    return [
        (int(row.mention_id), int(row.article_id))
        for row in rows
        if int(row.article_id) in allowed_article_ids
    ]


def _sqlite_matching_pairs(
    session: Session,
    *,
    project_id: int,
    params: PublicArticleGeoSearchParams,
) -> list[tuple[int, int]]:
    stmt = (
        select(SubstrateLocationMention, SubstrateLocation, SubstrateArticle)
        .join(SubstrateLocation, SubstrateLocation.id == SubstrateLocationMention.location_id)
        .join(SubstrateArticle, SubstrateArticle.id == SubstrateLocationMention.article_id)
        .where(
            SubstrateArticle.project_id == project_id,
            SubstrateArticle.deleted == False,  # noqa: E712
            SubstrateLocationMention.deleted == False,  # noqa: E712
        )
    )
    stmt = _apply_public_article_list_filters(
        stmt,
        meta_type=params.meta_type,
        meta_category=params.meta_category,
        exclude_meta_type=params.exclude_meta_type,
        exclude_meta_category=params.exclude_meta_category,
        meta_clauses=params.meta_clauses,
        pub_date_from=params.pub_date_from,
        pub_date_to=params.pub_date_to,
    )
    if params.location_types:
        stmt = stmt.where(col(SubstrateLocation.location_type).in_(params.location_types))
    natures = normalize_natures(params.natures)
    if natures:
        stmt = stmt.where(col(SubstrateLocationMention.nature).in_(natures))

    pairs: list[tuple[int, int]] = []
    for mention, loc, _article in session.exec(stmt).all():
        if mention.id is None:
            continue
        if not _sqlite_geometry_matches(loc.geometry_json, params=params):
            continue
        pairs.append((int(mention.id), int(mention.article_id)))
    return pairs


def group_and_page_articles_by_mention_pairs(
    session: Session,
    *,
    pairs: list[tuple[int, int]],
    limit: int,
    offset: int,
) -> tuple[list[tuple[int, list[int]]], int]:
    mentions_by_article: dict[int, list[int]] = {}
    for mention_id, article_id in pairs:
        mentions_by_article.setdefault(article_id, []).append(mention_id)

    if not mentions_by_article:
        return [], 0

    article_ids = list(mentions_by_article.keys())
    articles = session.exec(
        select(SubstrateArticle)
        .where(col(SubstrateArticle.id).in_(article_ids))
        .order_by(
            col(SubstrateArticle.pub_date).desc().nulls_last(),
            col(SubstrateArticle.id).desc(),
        )
    ).all()
    ordered_article_ids = [int(a.id) for a in articles if a.id is not None]
    total = len(ordered_article_ids)
    page_article_ids = ordered_article_ids[offset : offset + limit]
    page = [
        (article_id, mentions_by_article.get(article_id, [])) for article_id in page_article_ids
    ]
    return page, total


def group_and_page_articles_by_mention_cell_triples(
    session: Session,
    *,
    triples: list[tuple[int, int, str]],
    limit: int,
    offset: int,
) -> tuple[list[tuple[int, list[int], list[str]]], int]:
    """Group mention matches by article, merge matched H3 cells, and page globally."""
    mentions_by_article: dict[int, list[int]] = {}
    cells_by_article: dict[int, set[str]] = {}
    for mention_id, article_id, display_cell in triples:
        mentions_by_article.setdefault(article_id, []).append(mention_id)
        cells_by_article.setdefault(article_id, set()).add(display_cell)

    if not mentions_by_article:
        return [], 0

    article_ids = list(mentions_by_article.keys())
    articles = session.exec(
        select(SubstrateArticle)
        .where(col(SubstrateArticle.id).in_(article_ids))
        .order_by(
            col(SubstrateArticle.pub_date).desc().nulls_last(),
            col(SubstrateArticle.id).desc(),
        )
    ).all()
    ordered_article_ids = [int(a.id) for a in articles if a.id is not None]
    total = len(ordered_article_ids)
    page_article_ids = ordered_article_ids[offset : offset + limit]
    page = [
        (
            article_id,
            mentions_by_article.get(article_id, []),
            sorted(cells_by_article.get(article_id, set())),
        )
        for article_id in page_article_ids
    ]
    return page, total


def search_public_articles_by_geo(
    session: Session,
    *,
    project_id: int,
    params: PublicArticleGeoSearchParams,
) -> tuple[list[PublicArticleGeoSearchItemOut], int]:
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        pairs = _postgres_matching_pairs(session, project_id=project_id, params=params)
    else:
        pairs = _sqlite_matching_pairs(session, project_id=project_id, params=params)

    page, total = group_and_page_articles_by_mention_pairs(
        session,
        pairs=pairs,
        limit=params.limit,
        offset=params.offset,
    )
    if not page:
        return [], total

    page_article_ids = [article_id for article_id, _ in page]
    all_mention_ids = [mid for _, mention_ids in page for mid in mention_ids]
    articles = {
        int(a.id): a
        for a in session.exec(
            select(SubstrateArticle).where(col(SubstrateArticle.id).in_(page_article_ids))
        ).all()
        if a.id is not None
    }
    meta_by_id = _meta_rows_for_articles(session, page_article_ids)
    locations_by_mention_id = location_mentions_out_by_ids(session, all_mention_ids)

    items: list[PublicArticleGeoSearchItemOut] = []
    for article_id, mention_ids in page:
        article = articles.get(article_id)
        if article is None:
            continue
        matching_locations = [
            locations_by_mention_id[mid] for mid in mention_ids if mid in locations_by_mention_id
        ]
        items.append(
            PublicArticleGeoSearchItemOut(
                **_article_to_public_out(
                    article,
                    metadata=meta_by_id.get(article_id, []),
                ).model_dump(),
                matching_locations=matching_locations,
            )
        )
    return items, total
