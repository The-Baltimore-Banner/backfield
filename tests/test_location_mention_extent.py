"""Extent -- whether a geometry IS an article's event, or only contains it.

Two articles can share one location row and mean opposite things by it. The pilot's
"Annapolis, MD" serves seven articles: in one the city is the subject, in the next it
is only as close as the story gets to a single shooting. The shape is identical in
both, so the distinction cannot live on the location, and it cannot be read off the
geometry. These tests cover the path the answer takes from the model to the row, and
the one case the pipeline answers for itself.
"""

from __future__ import annotations

from agate_nodes.extraction.grounding import ChunkCandidate, GroundedSpan
from agate_nodes.place_extract.compact_array_parse import ROLE_ARRAY_FIELDS, row_to_entry
from agate_nodes.place_extract.compact_codes import (
    EXTENT_FROM_CODE,
    EXTENT_TO_CODE,
    expand_extent,
)
from agate_nodes.place_extract.compact_expand import expand_compact_entry
from agate_nodes.place_extract.llm_location_parse import place_from_llm_location_entry
from agate_nodes.place_extract.reconcile import stitch_place_candidates
from backfield_db import (
    BackfieldOrganization,
    BackfieldProject,
    SubstrateArticle,
    SubstrateLocation,
    SubstrateLocationMention,
)
from backfield_entities.entities.location.types import PLACE_MENTION_EXTENT_VALUES
from sqlmodel import Session, SQLModel, col, create_engine, select
from worker.substrate.entities.location.mentions import _upsert_mention_and_occurrence

from tests.project_helpers import project_ownership_fields

# --- the codec --------------------------------------------------------------


def test_extent_codes_cover_every_value() -> None:
    assert set(EXTENT_TO_CODE) == set(PLACE_MENTION_EXTENT_VALUES)
    assert set(EXTENT_FROM_CODE.values()) == set(EXTENT_TO_CODE)
    assert len(EXTENT_FROM_CODE) == len(EXTENT_TO_CODE)


def test_expand_extent_accepts_codes_and_full_names() -> None:
    assert expand_extent("cx") == "coextensive"
    assert expand_extent("wi") == "within_place"
    assert expand_extent("coextensive") == "coextensive"
    assert expand_extent("WITHIN_PLACE") == "within_place"


def test_expand_extent_never_guesses() -> None:
    """``nature`` falls back to its ``unknown`` member; extent has none to fall to."""
    assert expand_extent("") == ""
    assert expand_extent("probably") == ""
    assert expand_extent("point") == ""


def test_extent_is_the_last_compact_column() -> None:
    """Appended, never inserted: a shorter legacy row must keep its earlier columns."""
    assert ROLE_ARRAY_FIELDS[-1] == "extent"
    assert ROLE_ARRAY_FIELDS[:7] == (
        "location",
        "type",
        "nature",
        "address_place_kind",
        "description",
        "geocode_hints",
        "evidence_anchor",
    )


# --- a real compact row, all the way through the parse spine ----------------

ARTICLE = (
    "A severe thunderstorm warning covered Baltimore County through the evening. "
    "Separately, police said a man was shot in Annapolis on Tuesday night."
)


def _through_the_spine(row: list[str]) -> dict:
    """Parse one array row exactly as the node does, and return the dumped Place.

    Going through ``row_to_entry`` -> ``expand_compact_entry`` -> ``Place`` is the
    point: each of those three steps has its own allowlist, and a hand-built dict
    would skip all of them.
    """
    entry = row_to_entry(row)
    expanded = expand_compact_entry(ARTICLE, entry)
    return place_from_llm_location_entry(expanded).model_dump()


def test_a_compact_row_carries_extent_to_the_parsed_place() -> None:
    dumped = _through_the_spine(
        [
            "Baltimore County, MD",
            "cn",
            "p",
            "",
            "County under a severe thunderstorm warning.",
            "",
            "Baltimore County",
            "cx",
        ]
    )
    assert dumped["extent"] == "coextensive"


def test_the_same_shape_of_row_can_say_the_opposite() -> None:
    dumped = _through_the_spine(
        [
            "Annapolis, MD",
            "ci",
            "p",
            "",
            "City where a man was shot.",
            "",
            "Annapolis",
            "wi",
        ]
    )
    assert dumped["extent"] == "within_place"


def test_a_legacy_seven_cell_row_keeps_its_columns_and_gains_no_extent() -> None:
    """A model still answering with seven cells must not have its fields shifted."""
    dumped = _through_the_spine(
        [
            "Annapolis, MD",
            "ci",
            "p",
            "",
            "City where a man was shot.",
            "Maryland state capital.",
            "Annapolis",
        ]
    )
    assert "extent" not in dumped
    assert dumped["description"] == "City where a man was shot."
    assert dumped["geocode_hints"] == "Maryland state capital."
    assert dumped["location"]["full"] == "Annapolis, MD"


def test_stitching_a_duplicate_does_not_drop_its_extent() -> None:
    """A second chunk naming the same city must not cost the row its extent."""

    def candidate(location: str, start: int, **extra: object) -> ChunkCandidate[dict]:
        return ChunkCandidate(
            payload={
                "location": location,
                "type": "city",
                "components": {"city": "Annapolis", "state": "Maryland"},
                "mentions": [{"text": location}],
                **extra,
            },
            chunk_index=0,
            evidence=GroundedSpan(start=start, end=start + len(location), text=location),
            owned=True,
        )

    places, unresolved = stitch_place_candidates(
        [
            candidate("Annapolis", 10),
            candidate("Annapolis, MD", 400, extent="within_place"),
        ]
    )
    assert unresolved == 0
    assert len(places) == 1, places
    assert places[0]["extent"] == "within_place"


# --- the row ----------------------------------------------------------------


class _Fixture:
    def __init__(self, session: Session, project_id: int) -> None:
        self.session = session
        self.project_id = project_id

    def article(self, headline: str) -> int:
        row = SubstrateArticle(project_id=self.project_id, headline=headline, text=ARTICLE)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return int(row.id)

    def location(self, name: str, geometry_type: str | None) -> int:
        row = SubstrateLocation(
            project_id=self.project_id,
            name=name,
            normalized_name=name.lower(),
            geometry_type=geometry_type,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return int(row.id)

    def mention(self, *, article_id: int, location_id: int, entry: dict) -> str | None:
        _upsert_mention_and_occurrence(
            self.session,
            article_id=article_id,
            location_id=location_id,
            article_text=ARTICLE,
            entry=entry,
            run_id="run-extent",
            graph_id="graph-extent",
            bucket="areas",
        )
        self.session.commit()
        row = self.session.exec(
            select(SubstrateLocationMention).where(
                col(SubstrateLocationMention.article_id) == article_id,
                col(SubstrateLocationMention.location_id) == location_id,
            )
        ).one()
        return row.extent


def _fixture(session: Session) -> _Fixture:
    organization = BackfieldOrganization(name="Org", slug="mention-extent")
    session.add(organization)
    session.commit()
    session.refresh(organization)
    project = BackfieldProject(
        **project_ownership_fields(session, int(organization.id)),
        organization_id=int(organization.id),
        name="Project",
        slug="mention-extent",
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return _Fixture(session, int(project.id))


def _entry(location: str, **extra: object) -> dict:
    return {
        "id": f"entry:{location}",
        "location": location,
        "type": "city",
        "original_text": location,
        "mentions": [{"text": location}],
        **extra,
    }


def test_one_shape_two_articles_two_answers() -> None:
    """The disagreeing fixture: identical stored geometry, opposite extents.

    This is the whole reason the column is on the mention. If it sat on the
    location, the second article would overwrite the first.
    """
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        fx = _fixture(session)
        annapolis = fx.location("Annapolis, MD", "Polygon")
        storm_story = fx.article("Flooding closes roads across Annapolis")
        shooting_story = fx.article("Man shot in Annapolis")

        assert (
            fx.mention(
                article_id=storm_story,
                location_id=annapolis,
                entry=_entry("Annapolis, MD", extent="coextensive"),
            )
            == "coextensive"
        )
        assert (
            fx.mention(
                article_id=shooting_story,
                location_id=annapolis,
                entry=_entry("Annapolis, MD", extent="within_place"),
            )
            == "within_place"
        )


def test_a_point_geometry_overrides_what_the_model_said() -> None:
    """Derived, not asked. A point has no interior for an event to be inside of."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        fx = _fixture(session)
        courthouse = fx.location("Circuit Courthouse, Annapolis, MD", "Point")
        article = fx.article("Verdict delivered at the courthouse")
        assert (
            fx.mention(
                article_id=article,
                location_id=courthouse,
                entry=_entry("Circuit Courthouse", extent="within_place"),
            )
            == "coextensive"
        )


def test_an_area_the_model_said_nothing_about_stays_null() -> None:
    """NULL is the third state: nothing recorded, so no containment claim either way."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        fx = _fixture(session)
        county = fx.location("Baltimore County, MD", "Polygon")
        article = fx.article("County budget passes")
        assert fx.mention(
            article_id=article,
            location_id=county,
            entry=_entry("Baltimore County, MD"),
        ) is None


def test_a_value_outside_the_enum_is_not_guessed_at() -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        fx = _fixture(session)
        county = fx.location("Baltimore County, MD", "Polygon")
        article = fx.article("County budget passes")
        assert fx.mention(
            article_id=article,
            location_id=county,
            entry=_entry("Baltimore County, MD", extent="probably the whole thing"),
        ) is None


def test_a_rerun_clears_a_stale_extent_rather_than_keeping_it() -> None:
    """``nature`` keeps its old value on a blank rerun; extent must not.

    A ``coextensive`` left behind from an earlier run is a containment claim about a
    shape that may since have changed. Replacing it with NULL loses a claim; keeping
    it asserts one nobody made this time.
    """
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        fx = _fixture(session)
        county = fx.location("Baltimore County, MD", "Polygon")
        article = fx.article("County budget passes")
        assert (
            fx.mention(
                article_id=article,
                location_id=county,
                entry=_entry("Baltimore County, MD", extent="coextensive"),
            )
            == "coextensive"
        )
        assert fx.mention(
            article_id=article,
            location_id=county,
            entry=_entry("Baltimore County, MD"),
        ) is None
