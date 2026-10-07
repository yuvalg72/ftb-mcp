# ftb-mcp

[![CI](https://github.com/mouchar/ftb-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/mouchar/ftb-mcp/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

A read-only MCP server exposing genealogy data from a MyHeritage **Family Tree Builder**
(`.ftb`) file or a **GEDCOM** (`.ged`) export over HTTP.

`.ftb` files are plain SQLite databases, but the format is undocumented. This repository
contains both the server and — in [Format notes](#format-notes) — the results of reverse
engineering the schema against a real 1864-person tree.

The FTB schema is also the server's internal representation: a GEDCOM file is imported
into an in-memory database of that shape, so all 17 tools work identically against
either source. See [GEDCOM support](#gedcom-support).

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Run

```bash
ftb-mcp --db-path kafkova.ftb                      # http://127.0.0.1:8000/mcp
ftb-mcp --gedcom-path kafkova.ged                  # a GEDCOM export instead
ftb-mcp --db-path kafkova.ged                      # .ged/.gedcom is detected too
ftb-mcp --db-path kafkova.ftb --host 127.0.0.1 --port 9000 --path /mcp
ftb-mcp --db-path kafkova.ftb --transport stdio    # for stdio-based clients
```

| Option | Env var | Default |
|---|---|---|
| `--db-path` | `FTB_DB_PATH` | — |
| `--gedcom-path` | `FTB_GEDCOM_PATH` | — |
| `--host` | `FTB_HOST` | `127.0.0.1` |
| `--port` | `FTB_PORT` | `8000` |
| `--language` | `FTB_LANGUAGE` | the file's own project language |
| `--path` | — | `/mcp` |
| `--transport` | — | `streamable-http` |

Exactly one of `--db-path` / `--gedcom-path` is required.

An `.ftb` file is opened with SQLite's `mode=ro` URI; a GEDCOM file is read once into
memory. No tool writes, and there is no code path that can modify either file.

**Security boundary:** MCP over HTTP/SSE is restricted to loopback interfaces.
The server refuses `--host 0.0.0.0`, LAN/WAN addresses, and non-loopback names.
The MCP tools expose sensitive information about living relatives, and this
project does **not** provide application-level authentication. Do not directly
publish its HTTP endpoint, including through a public tunnel. Remote clients
need a separately reviewed TLS/authentication/authorization layer. Local
`stdio` transport is available for clients on the same machine.

### Registering with Claude Code

```bash
claude mcp add --transport http ftb http://127.0.0.1:8000/mcp
```

## Tools

**Discovery**

| Tool | Purpose |
|---|---|
| `get_tree_info` | Tree name, source application, languages, entity counts, year span |
| `search_persons` | Search by name, gender, life dates, living status; paginated |
| `list_surnames` | Surname frequency with birth-year span |
| `search_places` | Places ranked by event count |
| `search_notes` | Full-text search over research notes |

**Person detail**

| Tool | Purpose |
|---|---|
| `get_person` | Full profile; `include` selects `facts`/`families`/`notes`/`citations`/`media` |
| `get_person_facts` | Life events, filterable by GEDCOM tag |
| `get_person_timeline` | Own events merged with marriages and children's births |

**Relations**

| Tool | Purpose |
|---|---|
| `get_relatives` | Parents, siblings, spouses, children with relationship types |
| `get_ancestors` | Pedigree with Ahnentafel numbering; `lineage=biological` (default) excludes adopted/foster links; `lineage=all` includes them with recorded parentage labels |
| `get_descendants` | Descendant tree with per-generation counts |
| `find_relationship_path` | Shortest kinship path plus a label such as `first cousin once removed` |
| `get_family` | One family: spouses, status, marriage/divorce events, ordered children |

**Evidence and analysis**

| Tool | Purpose |
|---|---|
| `get_sources` | Archives, record collections, matched trees |
| `get_citations` | Citations for a person or from a source |
| `get_media_metadata` | Title, description, date, place — **text only** |
| `get_statistics` | Demographics, lifespans, names, places, research completeness |

Every text-returning tool accepts an optional `language` (`cs`, `en`, or an FTB language
number). Text falls back through *requested → Czech → English → any*, and results report
the language actually used when it differs from the one requested.

Media tools deliberately return no image bytes, no file names and no paths. Scanned-record
descriptions often carry genealogical detail found nowhere else, so their text is exposed;
the binary content is not.

## Genealogical evidence and lineage safety

`get_ancestors` defaults to **recorded natural-child** relationships, not all
family ties. This prevents an adoptive or foster path from silently being presented
as biological descent. Specify `lineage="all"` to include every recorded parent
relationship; the returned nodes carry `parentage_recorded_as` and `family_id`.
Other family tools continue to include adoptive/foster relationships.

**Important:** An FTB/GEDCOM natural-child flag is an assertion in an editable
family tree, not proof of biological or historical ancestry. Validate each
generational link against independent sources before claiming a lineage.

## GEDCOM support

Parsing is done by [ged4py](https://pypi.org/project/ged4py/). The file is read into an
in-memory SQLite database using the FTB schema, so `queries.py` and `graph.py` — language
ranking, fact shaping, pedigrees, statistics — are shared by both backends rather than
duplicated.

### What is mapped

| GEDCOM | FTB |
|---|---|
| `INDI` `NAME`/`GIVN`/`SURN`/`NPFX`/`NSFX`/`NICK`/`_MARNM` | `individual_lang_data` columns |
| `NAME` with `TYPE AKA` / `MARRIED` / `BIRTH` | `aka` / `married_surname` / `former_name` |
| `SEX` | `gender` |
| presence of `DEAT`/`BURI`/`CREM` | `is_alive` 2 (deceased), otherwise 3 |
| `BIRT`, `DEAT`, `OCCU`, `CENS`, … and `EVEN` + `TYPE` | `individual_fact_main_data.token` / `.fact_type` |
| an event's own value, or its `ADDR` lines | `individual_fact_lang_data.header` |
| `AGE`, `CAUS` | `age`, `cause_of_death` |
| `PLAC` strings, deduplicated | `places_main_data` / `places_lang_data` |
| `FAM` `HUSB`/`WIFE`/`CHIL`, `FAMC`/`FAMS` | `family_individual_connection` |
| `FAMC` `PEDI` `Adopted`/`Foster` | role 7 / role 6 |
| `MARR` present → status 3, `DIV` → 5 | `family_main_data.status` |
| `SOUR` `PAGE`/`QUAY`/`DATA.TEXT`/`EVEN.ROLE` | `citation_main_data` + `citation_lang_data` |
| `SOUR` record `TITL`/`AUTH`/`PUBL`/`TEXT`/`_TYPE`/`_MEDI` | `source_lang_data` |
| `NOTE`, on records and on events | `note_main_data` via `token_on_item` |
| `OBJE` `TITL`/`NOTE` | `media_item_*` — `FILE` is deliberately **not** imported |
| `HEAD` `SOUR`/`GEDC`/`CHAR`/`LANG` | `project_parameters` category `Header` |

GEDCOM states each family membership twice — on the family as `CHIL`/`HUSB`/`WIFE` and on
the individual as `FAMC`/`FAMS`. A file can carry only one side, so both are read and
memberships the family record failed to mirror are added afterwards. `kafkova.ged` does
this for two children whose `FAMC` also records them as adopted and fostered; reading only
the family side would drop both relationships.

### Two defects in MyHeritage's own export

`kafkova.ged` is not a conforming GEDCOM file, in two ways worth knowing about because
they break other parsers:

**1. `CONC` records split mid-character.** Nine values have a multi-byte UTF-8 sequence
straddling a line break — `Jarmila Mat` / `4 CONC \x9bj\xc5\xaf` for `Matějů`. ged4py
concatenates `CONC` values as bytes and decodes once the record is complete, so this
survives; a parser that decodes line by line raises `UnicodeDecodeError` on the file.

**2. Bare newlines inside values.** 49 lines continue a value with a raw newline instead
of a `CONT` record, so they arrive with no level number. `repair_bare_newlines` re-tags
each as the `CONT` it was meant to be, preserving the line break. This pre-pass runs only
after a first parse attempt has failed, so well-formed files are never rewritten.

### Deliberate differences from the `.ftb` path

- **Date qualifiers are normalised.** ged4py renders `BEF 1856` as `BEFORE 1856` and
  `ABT 1762` as `ABOUT 1762`, where FTB stores the short form. 182 of 4319 dates in
  `kafkova.ged`; the rest are byte-identical. `year_from`/`year_to` are unaffected.
- **Open-ended bounds match FTB exactly.** For `AFT`/`FROM` dates the importer writes
  FTB's own `99999999` "no upper bound", and `-99999999` below a `BEF` date, so `year_to`
  and `year_from` read the same from either source.
- **`language` becomes a no-op.** GEDCOM has one text language, taken from `HEAD.LANG`.
  Languages FTB has no number for are assigned one at or above 200 and registered with
  their real name and ISO code, so a Polish file is not labelled English.
- **`tree_name` comes from the filename.** A GEDCOM's `HEAD.FILE` is an export
  description (`Exported by MyHeritage.com from … on Sun, 02 Aug 2026`), not a name; it is
  kept as the `ExportDescription` parameter instead.
- **Nothing is soft-deleted.** `delete_flag` is always 0, since a GEDCOM export contains
  only live records.

## Development

```bash
.venv/bin/python -m pytest -q      # 214 tests
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

### Test data

Tests run against static fixtures in `tests/data`, not against any real tree:

| File | Purpose |
|---|---|
| `ftb_schema.sql` | The `CREATE TABLE` statements of a real FTB file, extracted verbatim |
| `sample.ftb` | A 15-person tree built on that schema |
| `sample.ged` | The same family as GEDCOM |

Both are generated by `python -m tests.make_fixtures` and checked in, so the suite never
depends on the generator having run. Regeneration is byte-for-byte reproducible.

The fixture is small but deliberately awkward: protobuf date and `RESI` header columns
holding bytes copied from genuine rows, a soft-deleted person, connection and fact, an
`is_alive` value that is not in the documented set, text present in one language and
missing in the other, every child role and family status, doubly-escaped HTML in notes,
a citation whose "page" is really a URL, and a place whose name is blank in the preferred
language. That is what lets counts be asserted exactly.

`kafkova.ftb` and `kafkova.ged` in the repository root are **live working files** — the
tree gains people whenever the author records one, so no test may assert a count, name or
date against them. `tests/test_live_files.py` uses them when present, for invariants only:
that the file opens, that no text decoded to a replacement character, that the
relationship graph is reciprocal, that notes and media come back clean, and that every
tool payload is JSON-serialisable. It skips when the files are absent.

Those two files are gitignored: they are personal data, not test input. Clone the
repository and the suite runs on the fixtures alone.

### Continuous integration

`ci.yml` runs ruff and the suite on Python 3.11 through 3.14, installing from `uv.lock`
with `--frozen` so CI cannot silently resolve something the lockfile does not pin. It
also regenerates the fixtures and fails if a byte changed, which catches the generator
drifting away from the files the tests read.

Dependabot proposes weekly updates for the `uv` and `github-actions` ecosystems. Patch
and minor bumps are grouped into one pull request and merge themselves once CI is green;
major bumps arrive individually and wait for review, because a passing suite only
evidences the behaviour the tests already cover.

## Licence

[Apache License 2.0](LICENSE). The reverse-engineering notes below describe MyHeritage's
file format; they are not affiliated with or endorsed by MyHeritage.

---

## Format notes

Everything below was derived by inspecting a real file (`kafkova.ftb`, FTB 8.0.0.8640,
`db_version 1.7`, GEDCOM 5.5.1 dialect `FTBDB`, UTF-8). Values are marked UNVERIFIED
where they did not occur in that file.

The row counts quoted below are the evidence each mapping was established from, measured
against that tree as it stood at the time. `kafkova.ftb` is a live file and has grown
since, so treat the counts as provenance for a conclusion, not as current statistics.

### Table layout

FTB uses a consistent three-layer pattern:

- `*_main_data` — identity, flags, dates, foreign keys
- `*_lang_data` — all human-readable text, keyed by `data_language`
- `*_connection` — many-to-many joins

Soft deletion is pervasive: nearly every table has `delete_flag`, and rows with
`delete_flag = 1` must be filtered out. Soft-deleted family connections would otherwise
appear as phantom relatives.

**It is also two levels deep, which is easy to miss.** FTB deletes a person by flagging
their `individual_main_data` row and leaves the facts, citations and media hanging off
them with `delete_flag = 0` of their own. Filtering only the child table therefore still
counts data belonging to someone who is no longer in the tree — and if a numerator does
that while its denominator does not, the result can exceed 100% of the tree. Any
aggregate over facts has to join back to the owner and check its flag too.

### Languages

`project_parameters.project_languages` is a protobuf blob; `0A 02 00 14` decodes to
field 1 = bytes `[0, 20]`, the list of language codes in use.

| Code | Language |
|---|---|
| 0 | English |
| 20 | Czech |

Text rows exist per language and are frequently missing for one of them, so any read
needs a fallback chain.

### Polymorphic references

Notes, citations and media attach to entities through `token_on_item(entity_id,
item_type)` rather than direct foreign keys.

| `item_type` | Entity | Evidence in `kafkova.ftb` |
|---|---|---|
| 1 | Individual | 1016 rows; `entity_id` equals `individual_id` |
| 2 | Family | 2 rows |
| 3 | Individual fact | 30 rows |
| 4 | Family fact | 3 rows |

### `family_individual_connection.individual_role_type`

Verified against gender: every role 2 is male, every role 3 is female.

| Value | Meaning | Count |
|---|---|---|
| 2 | husband | 540 |
| 3 | wife | 532 |
| 5 | natural child | 1354 |
| 6 | foster child | 1 |
| 7 | adopted child | 1 |

`child_order_in_family` holds the author's chosen ordering; `-1` means unordered.

### `family_main_data.status`

Verified against co-occurring facts — every status 3 family carries a `MARR` fact, every
status 5 a `DIV` fact, every status 8 a *Death of Spouse* event.

| Value | Meaning | Count |
|---|---|---|
| 0 | unspecified | 95 |
| 1 | engaged (UNVERIFIED) | — |
| 2 | separated (UNVERIFIED) | — |
| 3 | married | 438 |
| 5 | divorced | 6 |
| 8 | widowed | 5 |
| 9 | life partners | 3 |

### `individual_main_data.is_alive`

Despite the name, this is **not** a boolean.

| Value | Meaning | Count |
|---|---|---|
| 2 | deceased | 1655 |
| 3 | living | 209 |

### Facts

`individual_fact_main_data.token` holds a GEDCOM tag; custom events use `EVEN` with the
real name in `fact_type`.

Observed: `BIRT` 1724, `DEAT` 1658, `BURI` 299, `OCCU` 225, `CENS` 209, `RESI` 111,
`CHR` 37, `BAPM` 22, `EDUC` 22, `RELI` 18, `IMMI` 9, `NATI` 4, `PROP` 3, `DSCR` 1, plus
custom `EVEN` subtypes (`Settlement`, `AKA`, `Hobbies`, `MYHERITAGE:REL_PARTNERS`, …).
Family facts: `MARR` 391, `DIV` 6.

### Three traps

**1. `date` is protobuf, not text** — despite the schema comment claiming free text like
`"22 NOV 1963"`. The real layout:

```
0A 0B "19 MAR 1791"          field 1, length 11 — display string
22 2D                        field 4, length 45 — nested date message
   08 01                       modifier (1 = exact, 5 = BETWEEN)
   20 13                       day   = 0x13 = 19
   28 03                       month = 3
   30 FF 0D                    year  = 0x7F | 0x0D<<7 = 1791
   58 E4 0F                    end year for ranges (999999 = none)
```

The parsed integer columns `sorted_date`, `lower_bound_search_date` and
`upper_bound_search_date` (all `YYYYMMDD`) carry the same information in a far more usable
form, so this server reads only field 1 for display and takes structured values from those
columns.

**Field 4 is deliberately never used as a date source**, even when field 1 and all three
columns are empty. It is the editor's working copy rather than the stored fact, and it can
hold a date belonging to an entirely different record. In `kafkova.ftb` twelve facts that
record no date carry one there, and four of those are demonstrably wrong — three repeat
the person's own birth date as their death date, and one puts a daughter's birth date on
her mother. A fact with an empty display string and sentinel columns has **no date**; that
is the answer. Reading field 4 anyway produced 29 impossible parent/child chronologies —
posthumous children, marriages after death — in a full sweep of the tree.

Those columns have **three** distinct ways of saying "there is no date here", and reading
any of them as a date invents one:

| Value | Meaning | Written for |
|---|---|---|
| `999999999` | unknown or absent | a fact with no date at all |
| `99999999` | no upper bound | `AFT` and `FROM` dates |
| `-99999999` | no lower bound | `BEF` and `TO` dates |

Each exceeds the magnitude of any real `YYYYMMDD` — the largest observed is `20250127`,
and nothing falls between that and `99999999` — so one test on the absolute value
recognises all three. Taking `99999999` at face value yields the year **9999**, which is
larger than every real date and therefore wins any `MAX()`: it reported `AFT 1904` as
having an upper bound of 9999, and put 9999 as a tree's latest event year.

**2. `individual_fact_lang_data.header` is protobuf for `RESI` facts only** — 110 of 606
non-empty headers in the sample. Field 1 is address line 1, field 2 the full address.
Every other fact type stores plain text in the same column, so the fact's token decides
how to read it.

**3. Note and citation text is HTML, sometimes escaped twice** — notes contain
`<p>` markup and named entities (`&scaron;`, `&iacute;`), and some citation descriptions
arrive doubly escaped as `&amp;lt;br&amp;gt;`, needing two unescape passes before the
line break appears.

### Empty tables

Empty in `kafkova.ftb` and ignored by this server: `album_main_data`, `album_lang_data`,
`media_item_to_album_connection`, `repository_main_data`, `repository_lang_data`,
`task_main_data`, `task_lang_data`, `task_to_individual_connection`,
`individual_family_connection_order`, `intermediate_state`, `intermediate_state_ids`.

`intermediate_state*` hold uncommitted editor state and are not genealogical data.
