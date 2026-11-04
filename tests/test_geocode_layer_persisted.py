"""The geocoder's reported layer survives consolidation and reaches the row.

The pipeline already decides with the Pelias ``layer``: an area model scores a
wrong-layer candidate out, and ``street_road`` refuses one that is not
``street``. It was then dropped before the row was written, so nothing
downstream could ask whether a geometry was the granularity the text asked
for. These tests cover the two ends of the path it now takes.
"""

from __future__ import annotations

import asyncio

from agate_nodes.geocode_agent.nodes.consolidate import consolidate_node
from agate_utils.geocoding.geocoding_types import (
    GeocodingResult,
    GeocodingResultData,
    GeometryPoint,
)
from worker.substrate.entities.location.upsert import (
    _apply_substrate_location_merge,
    _geocode_layer_from_entry,
)


def _gr(*, confidence: dict, geocoder: str = "pelias_structured") -> GeocodingResult:
    return GeocodingResult(
        geocoder=geocoder,
        input_str="q",
        result=GeocodingResultData(
            id="x",
            processed_str="Fells Point, Baltimore, MD, USA",
            geometry=GeometryPoint(type="Point", coordinates=[-76.59, 39.28]),
            confidence=confidence,
        ),
    )


def _consolidate(gr: GeocodingResult, location_type: str = "neighborhood") -> dict:
    """Return the geocode result of the single entry consolidation produced.

    Which bucket the entry lands in -- an area kind, ``points`` or
    ``needs_review`` -- is routing, and not what these tests are about, so the
    buckets are flattened and the one entry is taken from wherever it went.
    """
    state = {
        "geocoding_result": gr,
        "location_type": location_type,
        "location_text": "Fells Point",
        "original_text": "Fells Point",
        "extra_fields": {},
    }
    out = asyncio.run(consolidate_node(state))
    places = (out.get("final_output") or {}).get("places") or {}

    entries: list[dict] = []
    for key, value in places.items():
        if key == "areas" and isinstance(value, dict):
            for area_entries in value.values():
                entries.extend(area_entries or [])
        elif isinstance(value, list):
            entries.extend(value)

    assert len(entries) == 1, f"expected one consolidated entry, got {len(entries)}: {entries}"
    return entries[0]["geocode"]["result"]


# --- consolidation keeps it -------------------------------------------------


def test_consolidate_carries_the_pelias_layer_into_the_result() -> None:
    result = _consolidate(_gr(confidence={"pelias_layer": "neighbourhood"}))
    assert result["geocode_layer"] == "neighbourhood"


def test_consolidate_omits_the_layer_when_the_provider_reports_none() -> None:
    result = _consolidate(_gr(confidence={}, geocoder="nominatim"))
    assert "geocode_layer" not in result


def test_consolidate_keeps_what_it_already_carried() -> None:
    """The layer is additive. It must not displace the existing fields."""
    result = _consolidate(_gr(confidence={"pelias_layer": "locality"}))
    assert result["formatted_address"] == "Fells Point, Baltimore, MD, USA"
    assert result["geometry"]["type"] == "Point"
    assert result["id"] == "x"


# --- the worker reads it ----------------------------------------------------


def test_geocode_layer_from_entry_reads_the_stored_result() -> None:
    entry = {
        "geocode": {
            "geocode_type": "pelias_structured",
            "result": {"geocode_layer": "county"},
        }
    }
    assert _geocode_layer_from_entry(entry) == "county"


def test_geocode_layer_from_entry_is_none_when_absent_or_malformed() -> None:
    assert _geocode_layer_from_entry({}) is None
    assert _geocode_layer_from_entry({"geocode": {"result": {}}}) is None
    assert _geocode_layer_from_entry({"geocode": {"result": {"geocode_layer": "  "}}}) is None
    assert _geocode_layer_from_entry({"geocode": {"result": {"geocode_layer": 7}}}) is None


def test_geocode_layer_from_entry_normalises_case() -> None:
    entry = {"geocode": {"result": {"geocode_layer": "  LocalAdmin "}}}
    assert _geocode_layer_from_entry(entry) == "localadmin"


# --- the merge stores it ----------------------------------------------------


def _merge_kwargs(**overrides: object) -> dict:
    values: dict = {
        "display_name": "Fells Point",
        "normalized": "fells point",
        "location_type_str": "neighborhood",
        "status": "resolved",
        "external_source": "pelias",
        "external_id": "gid-1",
        "fingerprint": "fp-1",
        "geocode_type": "pelias_structured",
        "formatted_address": "Fells Point, Baltimore, MD, USA",
        "details": {},
        "geometry_value": None,
        "geometry_type_str": None,
        "geometry_json": None,
        "geocode_router_audit_json": None,
        "h3_cell": None,
        "h3_resolution": None,
    }
    values.update(overrides)
    return values


def test_merge_sets_the_layer() -> None:
    from backfield_db import SubstrateLocation

    loc = SubstrateLocation(project_id=1, name="T", normalized_name="t")
    _apply_substrate_location_merge(loc, **_merge_kwargs(geocode_layer="neighbourhood"))
    assert loc.geocode_layer == "neighbourhood"


def test_merge_keeps_the_stored_layer_when_the_incoming_one_is_none() -> None:
    """Same rule the other geocode fields follow: a missing value does not erase."""
    from backfield_db import SubstrateLocation

    loc = SubstrateLocation(project_id=1, name="T", normalized_name="t")
    _apply_substrate_location_merge(loc, **_merge_kwargs(geocode_layer="county"))
    _apply_substrate_location_merge(loc, **_merge_kwargs(geocode_layer=None))
    assert loc.geocode_layer == "county"


def test_merge_clears_the_layer_with_the_rest_of_the_geocode_identity() -> None:
    from backfield_db import SubstrateLocation

    loc = SubstrateLocation(project_id=1, name="T", normalized_name="t")
    _apply_substrate_location_merge(loc, **_merge_kwargs(geocode_layer="county"))
    _apply_substrate_location_merge(
        loc, **_merge_kwargs(geocode_layer=None), clear_geocode_identity=True
    )
    assert loc.geocode_layer is None
    assert loc.geocode_type is None
    assert loc.formatted_address is None
