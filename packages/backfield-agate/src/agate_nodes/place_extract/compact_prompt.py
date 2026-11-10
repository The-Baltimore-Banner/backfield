"""Compact PlaceExtract prompt instructions appended after extract.md."""

from __future__ import annotations

from agate_nodes.place_extract.compact_codes import COMPACT_CODE_LEGEND

COMPACT_OUTPUT_INSTRUCTIONS = """\
Return ONLY compact JSON with this shape:
{"locations": [["location", "type", "nature", "address_place_kind", "description", "geocode_hints", "evidence_anchor", "extent"], ...]}

Rules for compact output:
- Apply all editorial inclusion, exclusion, typing, and formatting rules above —
  especially **Hard stops**.
- **Field 1 (location) is only for literal, mappable geography.** Never emit teams
  (`Chicago Bears`), athletic conferences, classes, or brackets (`Class 3a, IL`,
  `IHSA 4A`, `West Suburban Conference Silver`), events or camp titles
  (`NFL Scouting Combine`), organizations with inferred headquarters, or narrative
  phrases. When unsure, omit the row.
- **Block addresses:** never emit "block of" in `location`. Normalize journalistic
  block references to mailing-style addresses (`6500 block of South Hermitage Avenue`
  → `6500 S Hermitage Ave, Chicago, IL`).
- **Place + street co-mention:** when a named venue appears with its street beside it
  (comma or parentheses), emit **one `pl` / place row only**. Put venue name first,
  then the street as a comma segment, then city/state when inferable
  (`Garfield Community Service Center, 10 S. Kedzie Ave., Chicago, IL`). Do **not**
  also emit that street as a separate `ad` / address row. Standalone streets with no
  named venue still use `ad`.
- **Intersections and spans:** use full street-type words and include city/state
  (`Main Street and 2nd Street, Chicago, IL`; `Lake Street from Nicollet Avenue to
  28th Avenue, Minneapolis, MN`).
- **Neighborhoods and regions:** include city/state in `location` when inferable
  (`Longfellow, Minneapolis, MN`); emit parent city/state rows for region types.
- Each location is ONE JSON array with EXACTLY eight string fields in this order:
  1. location — full geocodable string per the rules above
  2. type — short type code (see legend below)
  3. nature — short nature code (see legend below)
  4. address_place_kind — short kind code for street-level types; use "" for all other types
  5. description — one brief sentence on the location's role in the story
  6. geocode_hints — geographic disambiguation for geocoding/search (street, neighborhood,
     nearby landmark, which branch); never story synopsis; use "" when location is already unique
  7. evidence_anchor — the shortest useful verbatim phrase copied from the article that justifies
     this location. This can differ from `location` when the output is normalized (`1400 block of
     West Argyle Street` for `1400 W Argyle St, Chicago, IL`; `Ald. Jessie Fuentes (26th)` for
     `Ward 26, Chicago, IL`). Use "" only when no article phrase supports the row.
  8. extent — short extent code (see legend below). Answer this **only for area and
     line types**: nb, rg, ci, cn, rs, st, rn, cy, pd, nt, sr, sp and ot. For pl, ad,
     ir and ih use "".
- **How to pick the extent code.** Ask one question: does the article describe something
  that fills this whole shape, or something that happened at one spot inside it?
  - `cx` coextensive — the shape is the subject. A storm warning for six counties; a
    measles count for one county; a school-board race in one district; car thefts
    reported across four named cities.
  - `wi` within_place — the shape is only as close as the article gets. A shooting the
    story places no more precisely than "in Annapolis"; a crash somewhere along a named
    road; a protest "downtown" with no address. The event has a real spot; the article
    did not give it.
  - Use "" when the article supports neither reading. "" is not a wrong answer; a
    guess is.
- **Column 3 (nature) is ALWAYS one single letter**, from p/s/j/c/h/r/u and nothing
  else. Columns 2, 4 and 8 are always two letters or "". A two-letter token in
  column 3 is always wrong: `cx` and `wi` belong in column 8, `pn`, `pv` and `uk`
  in column 4, and every type code in column 2. Fill column 3 before you think
  about column 8, and do not let the extent answer change it.
- Do NOT use full enum names (e.g. intersection_highway) or objects with keys.
- Do NOT include components, mentions, original_text, nature_secondary_tags, or any other fields.
- Output every distinct editorially relevant location from the article.
- If none, return {"locations": []}.
- When a neighborhood already appears inside a location string (e.g. "River North, Chicago, IL"),
  do not also emit a standalone neighborhood row for that same neighborhood.
- When the story is datelined or clearly based in a local anchor city, emit that city as its own
  city row even if it also appears inside longer location strings elsewhere.
- Prefer the same location-string granularity you would use in full mode (e.g. "Ragadan, Chicago, IL"
  rather than "Ragadan, Uptown, Chicago, IL") unless an extra segment is needed to geocode or
  disambiguate.
- Scheduled scoreboard lines without scores ("Team A at Team B"), final score lines ("Team A 55, Team B 53"), and state-tournament bracket lines ("Title: St. Rita 12, Triad 11"; "Semifinals"; "Third place: Naperville Central vs. Mount Carmel, 9"): emit **every participating school** as separate **`pl` / place** rows with expanded school names plus city/state — even when a shared venue is also named. Never use **`ot` / other** for school tokens, never bare tokens (`Belvidere`, `Smith`, `East Peoria`) as `location`, and never put a school name in the city component. Bracket labels (`CLASS 3A`, `Title:`, `Semifinals`) are not locations.

""" + COMPACT_CODE_LEGEND
