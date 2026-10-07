"""MCP server exposing a Family Tree Builder or GEDCOM file over HTTP.

Read-only by construction: an .ftb file is opened with SQLite's ``mode=ro`` URI, a
GEDCOM file is read once into an in-memory copy, and no tool writes. Every tool
returns plain JSON-serialisable data; media tools return textual metadata only and
never image bytes or file paths.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import queries
from .db import FtbDatabase, FtbDatabaseError
from .gedcom_import import open_gedcom
from .graph import TreeIndex
from .schema import (
    ITEM_TYPE_INDIVIDUAL,
    ITEM_TYPE_INDIVIDUAL_FACT,
)

log = logging.getLogger("ftb_mcp")

MAX_LIMIT = 200
RELATIVE_KINDS = ("parents", "siblings", "spouses", "children")
PERSON_SECTIONS = ("facts", "families", "notes", "citations", "media")
STAT_METRICS = ("demographics", "lifespans", "names", "places", "completeness", "facts")

# Extensions routed to the GEDCOM importer rather than opened as SQLite.
GEDCOM_SUFFIXES = (".ged", ".gedcom")

mcp = MCPServer(
    name="ftb",
    title="Family Tree Builder",
    version="0.1.0",
    instructions=(
        "Read-only access to a genealogy database exported from MyHeritage Family "
        "Tree Builder, either as an .ftb file or as GEDCOM. "
        "Start with get_tree_info to learn the tree's size, languages and date span. "
        "Find people with search_persons (name matching ignores case and diacritics, "
        "so 'Kafkova' matches 'Kafková'), then use the returned person_id with "
        "get_person for a full profile. Use get_relatives, get_ancestors, "
        "get_descendants and find_relationship_path to explore how people connect. "
        "Genealogical dates are often approximate: a date may be a range or carry a "
        "BEF/AFT/BET qualifier, exposed as year_from/year_to alongside the original "
        "display string. Absent data is normal in genealogy -- a missing birth year "
        "means unrecorded, not zero."
    ),
)


class _State:
    """Holds the open database and a per-language index cache."""

    def __init__(self) -> None:
        self.db: FtbDatabase | None = None
        self.default_lang: int = 20
        self._indexes: dict[int, TreeIndex] = {}

    def open(self, path: str, language: str | None, gedcom: bool | None = None) -> None:
        """Open a tree.

        ``gedcom`` forces the loader; left unset, a ``.ged``/``.gedcom`` extension
        selects the GEDCOM importer and anything else is opened as an .ftb file.
        """
        if gedcom is None:
            gedcom = Path(path).suffix.lower() in GEDCOM_SUFFIXES
        opened = open_gedcom(path) if gedcom else FtbDatabase(path)

        # Only replace the current tree once the new one has opened successfully, and
        # release the old connection rather than leaving it to the garbage collector.
        if self.db is not None:
            self.db.close()
        self.db = opened
        # Indexes belong to the file they were built from; a reopen must not serve
        # people from the previous tree.
        self._indexes.clear()
        self.default_lang = queries.resolve_language(self.db, language)

    def database(self) -> FtbDatabase:
        if self.db is None:
            raise ToolError("No tree is open. Start the server with --db-path or --gedcom-path.")
        return self.db

    def lang(self, requested: str | None) -> int:
        if not requested:
            return self.default_lang
        return queries.resolve_language(self.database(), requested)

    def index(self, lang: int) -> TreeIndex:
        """Index for a language, built on first use and cached."""
        if lang not in self._indexes:
            self._indexes[lang] = TreeIndex(self.database(), lang)
        return self._indexes[lang]


state = _State()


def _clamp(limit: int | None, default: int = 25) -> int:
    if limit is None:
        return default
    return max(1, min(int(limit), MAX_LIMIT))


def _pick(requested: list[str] | None, allowed: tuple[str, ...]) -> list[str]:
    """Validate a caller's selection against the allowed set, defaulting to all."""
    if not requested:
        return list(allowed)
    chosen = [item for item in requested if item in allowed]
    if not chosen:
        raise ToolError(f"None of {requested} are valid; choose from {list(allowed)}")
    return chosen


def _person_or_error(index: TreeIndex, person_id: int):
    try:
        return index.require(person_id)
    except KeyError as exc:
        raise ToolError(str(exc)) from exc


# ------------------------------------------------------------------ discovery tools


@mcp.tool(
    description=(
        "Overview of the open family tree: name, originating application, GEDCOM "
        "version, available languages, entity counts and the span of recorded years. "
        "Call this first to orient yourself before querying people."
    )
)
def get_tree_info() -> dict[str, Any]:
    db = state.database()
    info = queries.tree_info(db)
    info["name_languages_used"] = state.index(state.default_lang).language_note()
    return info


@mcp.tool(
    description=(
        "Search people by name, gender, life dates or living status. Name matching is "
        "case- and diacritic-insensitive, so 'Kafkova' finds 'Kafková'. Year filters "
        "match only people whose date is actually recorded. Returns compact summaries; "
        "pass a person_id to get_person for the full record."
    )
)
def search_persons(
    name: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    gender: str | None = None,
    birth_year_from: int | None = None,
    birth_year_to: int | None = None,
    death_year_from: int | None = None,
    death_year_to: int | None = None,
    is_living: bool | None = None,
    limit: int = 25,
    offset: int = 0,
    language: str | None = None,
) -> dict[str, Any]:
    index = state.index(state.lang(language))
    matches = index.search(
        name=name,
        first_name=first_name,
        last_name=last_name,
        gender=gender,
        birth_year_from=birth_year_from,
        birth_year_to=birth_year_to,
        death_year_from=death_year_from,
        death_year_to=death_year_to,
        is_living=is_living,
    )
    limit = _clamp(limit)
    offset = max(0, offset)
    page = matches[offset : offset + limit]
    return {
        "total_count": len(matches),
        "offset": offset,
        "returned": len(page),
        "has_more": offset + len(page) < len(matches),
        "results": [person.summary() for person in page],
    }


@mcp.tool(
    description=(
        "List surnames in the tree with how many people carry each and the span of "
        "birth years they cover. Useful for finding which families the tree documents."
    )
)
def list_surnames(
    prefix: str | None = None, min_count: int = 1, limit: int = 50, language: str | None = None
) -> dict[str, Any]:
    index = state.index(state.lang(language))
    surnames = index.surnames(prefix, max(1, min_count))
    limit = _clamp(limit, 50)
    return {
        "total_count": len(surnames),
        "returned": min(limit, len(surnames)),
        "results": surnames[:limit],
    }


@mcp.tool(
    description=(
        "Search recorded places, ranked by how many events happened there. Place names "
        "are stored per language; each result reports which language it came from."
    )
)
def search_places(
    query: str | None = None, limit: int = 50, language: str | None = None
) -> dict[str, Any]:
    lang = state.lang(language)
    results = queries.search_places(state.database(), lang, query, _clamp(limit, 50))
    return {"returned": len(results), "results": results}


@mcp.tool(
    description=(
        "Full-text search across research notes attached to people and events. Notes "
        "are stored as HTML and returned as clean plain text. Pass an empty query to "
        "browse all notes."
    )
)
def search_notes(query: str = "", limit: int = 25, language: str | None = None) -> dict[str, Any]:
    lang = state.lang(language)
    results = queries.search_notes(state.database(), lang, query, _clamp(limit))
    return {"returned": len(results), "results": results}


# --------------------------------------------------------------------- person tools


@mcp.tool(
    description=(
        "Full profile for one person: all recorded name variants, gender, living "
        "status, life events, immediate family, research notes, source citations and "
        "textual media metadata. Use the `include` parameter to request only the "
        "sections you need."
    )
)
def get_person(
    person_id: int, include: list[str] | None = None, language: str | None = None
) -> dict[str, Any]:
    lang = state.lang(language)
    db = state.database()
    index = state.index(lang)
    person = _person_or_error(index, person_id)
    sections = _pick(include, PERSON_SECTIONS)

    profile: dict[str, Any] = person.summary()

    if "facts" in sections:
        profile["facts"] = queries.person_facts(db, [person_id], lang).get(person_id, [])

    if "families" in sections:
        families = queries.families_of(db, person_id)
        all_ids = families["as_spouse"] + families["as_child"]
        records = queries.family_records(db, all_ids)
        members = queries.family_members(db, all_ids)
        facts = queries.family_facts(db, families["as_spouse"], lang)

        def describe(family_id: int, include_facts: bool) -> dict[str, Any]:
            record = dict(records.get(family_id, {"family_id": family_id}))
            record["members"] = [
                {**index.people[m["person_id"]].summary(), "role": m["role"]}
                for m in members.get(family_id, [])
                if m["person_id"] in index.people
            ]
            if include_facts:
                record["facts"] = facts.get(family_id, [])
            return record

        profile["families_as_spouse"] = [describe(fid, True) for fid in families["as_spouse"]]
        profile["families_as_child"] = [describe(fid, False) for fid in families["as_child"]]
        profile["immediate_relatives"] = index.relatives(person_id, list(RELATIVE_KINDS))

    if "notes" in sections:
        profile["notes"] = queries.notes_for(db, [person_id], ITEM_TYPE_INDIVIDUAL, lang).get(
            person_id, []
        )
        fact_ids = [
            row["individual_fact_id"]
            for row in db.query(
                "SELECT individual_fact_id FROM individual_fact_main_data "
                "WHERE individual_id = :pid AND delete_flag = 0",
                {"pid": person_id},
            )
        ]
        fact_notes = queries.notes_for(db, fact_ids, ITEM_TYPE_INDIVIDUAL_FACT, lang)
        flattened = [note for notes in fact_notes.values() for note in notes]
        if flattened:
            profile["event_notes"] = flattened

    if "citations" in sections:
        profile["citations"] = queries.citations_for(
            db, [person_id], ITEM_TYPE_INDIVIDUAL, lang
        ).get(person_id, [])

    if "media" in sections:
        profile["media"] = queries.media_for(db, [person_id], lang, 50)

    return profile


@mcp.tool(
    description=(
        "Life events for a person -- birth, death, burial, occupation, residence, "
        "census and custom events -- in chronological order, with events of unknown "
        "date last. Filter with `tags` using GEDCOM tags such as BIRT, DEAT or OCCU."
    )
)
def get_person_facts(
    person_id: int, tags: list[str] | None = None, language: str | None = None
) -> dict[str, Any]:
    lang = state.lang(language)
    index = state.index(lang)
    person = _person_or_error(index, person_id)
    facts = queries.person_facts(state.database(), [person_id], lang, tags).get(person_id, [])
    return {
        "person_id": person_id,
        "name": person.full_name,
        "returned": len(facts),
        "available_tags": queries.FACT_TAGS,
        "facts": facts,
    }


@mcp.tool(
    description=(
        "A person's life story in one chronological sequence, merging their own events "
        "with family events they took part in -- marriages, divorces, and the births of "
        "their children. Undated events are listed at the end."
    )
)
def get_person_timeline(
    person_id: int, include_family_events: bool = True, language: str | None = None
) -> dict[str, Any]:
    lang = state.lang(language)
    db = state.database()
    index = state.index(lang)
    person = _person_or_error(index, person_id)

    entries: list[dict[str, Any]] = []
    for fact in queries.person_facts(db, [person_id], lang).get(person_id, []):
        entries.append({**fact, "subject": "self"})

    if include_family_events:
        families = queries.families_of(db, person_id)["as_spouse"]
        for family_id, facts in queries.family_facts(db, families, lang).items():
            for fact in facts:
                entries.append({**fact, "subject": "family", "family_id": family_id})

        children = list(dict.fromkeys(index.children(person_id)))
        for child_id, facts in queries.person_facts(db, children, lang, ["BIRT"]).items():
            for fact in facts:
                entries.append(
                    {
                        **fact,
                        "subject": "child",
                        "type": "Birth of child",
                        "person_id": child_id,
                        "person_name": index.people[child_id].full_name,
                    }
                )

    entries.sort(
        key=lambda e: (
            e["date"] is None or e["date"]["sort_key"] is None,
            (e["date"] or {}).get("sort_key") or 0,
        )
    )
    return {
        "person_id": person_id,
        "name": person.full_name,
        "returned": len(entries),
        "timeline": entries,
    }


# ------------------------------------------------------------------ relation tools


@mcp.tool(
    description=(
        "Immediate family of a person: parents, siblings, spouses and children. Each "
        "relative carries the relationship type recorded in the tree, so adopted and "
        "foster links are distinguishable from natural ones."
    )
)
def get_relatives(
    person_id: int, kinds: list[str] | None = None, language: str | None = None
) -> dict[str, Any]:
    index = state.index(state.lang(language))
    person = _person_or_error(index, person_id)
    groups = index.relatives(person_id, _pick(kinds, RELATIVE_KINDS))
    return {
        "person_id": person_id,
        "name": person.full_name,
        "counts": {kind: len(members) for kind, members in groups.items()},
        **groups,
    }


@mcp.tool(
    description=(
        "Direct ancestors of a person as a pedigree tree, with standard Ahnentafel "
        "numbering (subject 1, father 2n, mother 2n+1). Defaults to recorded natural-child "
        "parentage only. Use lineage='all' to include adopted/foster parent edges, which "
        "are labelled in results. Recorded natural parentage is NOT proof of biological "
        "or historical descent. Nodes at the depth limit have has_more_ancestors."
    )
)
def get_ancestors(
    person_id: int,
    generations: int = 4,
    language: str | None = None,
    lineage: str = "biological",
) -> dict[str, Any]:
    if lineage not in ("biological", "all"):
        raise ToolError("Invalid lineage; expected 'biological' or 'all'")
    index = state.index(state.lang(language))
    _person_or_error(index, person_id)
    return index.ancestors(
        person_id, max(1, min(int(generations), 15)), natural_only=lineage == "biological"
    )


@mcp.tool(
    description=(
        "Descendants of a person as a tree, with a count of how many were found in "
        "each generation. Nodes at the depth limit are flagged with "
        "has_more_descendants when the line continues."
    )
)
def get_descendants(
    person_id: int, generations: int = 3, language: str | None = None
) -> dict[str, Any]:
    index = state.index(state.lang(language))
    _person_or_error(index, person_id)
    return index.descendants(person_id, max(1, min(int(generations), 15)))


@mcp.tool(
    description=(
        "How two people are related. Returns the shortest chain of parent, child and "
        "spouse links between them plus a kinship label such as 'first cousin once "
        "removed'. Paths routed through a marriage are labelled as relationships by "
        "marriage, since no single English term applies."
    )
)
def find_relationship_path(
    person_id_a: int, person_id_b: int, max_depth: int = 15, language: str | None = None
) -> dict[str, Any]:
    index = state.index(state.lang(language))
    person_a = _person_or_error(index, person_id_a)
    person_b = _person_or_error(index, person_id_b)
    result = index.relationship_path(person_id_a, person_id_b, max(1, min(int(max_depth), 25)))
    return {
        "person_a": {"person_id": person_id_a, "name": person_a.full_name},
        "person_b": {"person_id": person_id_b, "name": person_b.full_name},
        **result,
    }


@mcp.tool(
    description=(
        "One family unit: its spouses, marital status, marriage and divorce events, and "
        "its children in the order recorded by the tree's author."
    )
)
def get_family(family_id: int, language: str | None = None) -> dict[str, Any]:
    lang = state.lang(language)
    db = state.database()
    index = state.index(lang)

    record = queries.family_records(db, [family_id]).get(family_id)
    if record is None:
        raise ToolError(f"No family with id {family_id} in this tree")

    members = queries.family_members(db, [family_id]).get(family_id, [])
    detailed = [
        {**index.people[m["person_id"]].summary(), "role": m["role"], "role_code": m["role_code"]}
        for m in members
        if m["person_id"] in index.people
    ]
    return {
        **record,
        "spouses": [m for m in detailed if m["role_code"] in (2, 3)],
        "children": [m for m in detailed if m["role_code"] not in (2, 3)],
        "facts": queries.family_facts(db, [family_id], lang).get(family_id, []),
    }


# ------------------------------------------------------------------ evidence tools


@mcp.tool(
    description=(
        "Genealogical sources cited in the tree -- archives, record collections, "
        "matched family trees -- with author, publisher and how many citations each "
        "supports. Filter by id or search across title, author and body text."
    )
)
def get_sources(
    source_id: int | None = None,
    query: str | None = None,
    limit: int = 25,
    language: str | None = None,
) -> dict[str, Any]:
    lang = state.lang(language)
    results = queries.search_sources(state.database(), lang, source_id, query, _clamp(limit))
    return {"returned": len(results), "results": results}


@mcp.tool(
    description=(
        "Source citations, optionally for one person or from one source. Each citation "
        "carries the transcribed record text, a confidence level, and either a page "
        "reference or a record URL."
    )
)
def get_citations(
    person_id: int | None = None,
    source_id: int | None = None,
    limit: int = 25,
    language: str | None = None,
) -> dict[str, Any]:
    lang = state.lang(language)
    db = state.database()
    limit = _clamp(limit)

    if person_id is not None:
        index = state.index(lang)
        _person_or_error(index, person_id)
        found = queries.citations_for(db, [person_id], ITEM_TYPE_INDIVIDUAL, lang, limit)
        results = found.get(person_id, [])
        if source_id is not None:
            results = [c for c in results if c["source_id"] == source_id]
        return {"person_id": person_id, "returned": len(results), "results": results}

    if source_id is None:
        raise ToolError("Provide person_id, source_id, or both")

    rows = db.query(
        "SELECT t.entity_id FROM citation_main_data c "
        "JOIN token_on_item t ON t.token_on_item_id = c.external_token_on_item_id "
        "WHERE c.delete_flag = 0 AND c.source_id = :sid AND t.item_type = :itype "
        "LIMIT :limit",
        {"sid": source_id, "itype": ITEM_TYPE_INDIVIDUAL, "limit": limit},
    )
    person_ids = list(dict.fromkeys(row["entity_id"] for row in rows))
    found = queries.citations_for(db, person_ids, ITEM_TYPE_INDIVIDUAL, lang, limit)
    index = state.index(lang)
    results = []
    for pid, citations in found.items():
        for citation in citations:
            if citation["source_id"] != source_id:
                continue
            person = index.get(pid)
            results.append(
                {**citation, "person_id": pid, "person_name": person.full_name if person else None}
            )
    return {"source_id": source_id, "returned": len(results), "results": results}


@mcp.tool(
    description=(
        "Textual metadata for photos and documents: title, description, date and place. "
        "Returns no image data, binary content or file paths -- descriptions of "
        "scanned records often contain genealogical detail found nowhere else."
    )
)
def get_media_metadata(
    person_id: int | None = None, limit: int = 25, language: str | None = None
) -> dict[str, Any]:
    lang = state.lang(language)
    person_ids = None
    if person_id is not None:
        _person_or_error(state.index(lang), person_id)
        person_ids = [person_id]
    results = queries.media_for(state.database(), person_ids, lang, _clamp(limit))
    return {"returned": len(results), "results": results}


@mcp.tool(
    description=(
        "Aggregate statistics over the whole tree: gender and living-status breakdown, "
        "average lifespan by birth century, most common names, most frequent places, "
        "fact-type frequency, and how complete the research is (share of people with a "
        "known birth date, a place, or a source citation)."
    )
)
def get_statistics(metrics: list[str] | None = None, language: str | None = None) -> dict[str, Any]:
    lang = state.lang(language)
    chosen = _pick(metrics, STAT_METRICS)
    result = queries.statistics(state.database(), lang, chosen)
    if "names" in chosen:
        result["names"] = state.index(lang).stats_names()
    return result


# ------------------------------------------------------------------------- entrypoint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ftb-mcp",
        description=("MCP server exposing a MyHeritage Family Tree Builder (.ftb) or GEDCOM file."),
    )
    parser.add_argument(
        "--db-path",
        default=os.environ.get("FTB_DB_PATH"),
        help="Path to the .ftb file (or set FTB_DB_PATH). A .ged/.gedcom path here is "
        "read as GEDCOM.",
    )
    parser.add_argument(
        "--gedcom-path",
        default=os.environ.get("FTB_GEDCOM_PATH"),
        help="Path to a GEDCOM file (or set FTB_GEDCOM_PATH). Loaded into memory; the "
        "file itself is never modified.",
    )
    parser.add_argument("--host", default=os.environ.get("FTB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FTB_PORT", "8000")))
    parser.add_argument("--path", default="/mcp", help="HTTP path for the MCP endpoint.")
    parser.add_argument(
        "--transport",
        choices=("streamable-http", "stdio", "sse"),
        default="streamable-http",
        help="Transport to serve. Defaults to streamable HTTP.",
    )
    parser.add_argument(
        "--language",
        default=os.environ.get("FTB_LANGUAGE"),
        help="Preferred text language, e.g. cs or en. Defaults to the project language.",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def _is_loopback_host(host: str) -> bool:
    """Prevent unauthenticated genealogy data from being served on network interfaces."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )

    if args.db_path and args.gedcom_path:
        print("error: pass either --db-path or --gedcom-path, not both", file=sys.stderr)
        return 2

    if args.transport != "stdio" and not _is_loopback_host(args.host):
        print(
            "error: remote HTTP is disabled (no MCP authentication is configured); "
            "bind to a loopback address and use a vetted authenticated proxy",
            file=sys.stderr,
        )
        return 2

    source = args.gedcom_path or args.db_path
    if not source:
        print(
            "error: one of --db-path or --gedcom-path is required "
            "(or set FTB_DB_PATH / FTB_GEDCOM_PATH)",
            file=sys.stderr,
        )
        return 2

    try:
        state.open(source, args.language, gedcom=True if args.gedcom_path else None)
    except FtbDatabaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    db = state.database()
    index = state.index(state.default_lang)
    log.info(
        "Loaded %s: %d people, %d families, language %s",
        db.path.name,
        len(index.people),
        db.count("family_main_data"),
        queries.language_code(state.default_lang),
    )

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "sse":
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        log.info("Serving MCP on http://%s:%d%s", args.host, args.port, args.path)
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=args.path,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
