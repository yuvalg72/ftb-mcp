# Engineering backlog: secure, evidence-first MyHeritage MCP

> **Action required:** GitHub Issues is disabled in this fork (API returned HTTP 410).
> Enable it under **Settings > General > Features > Issues**, then promote the
> items below into individual GitHub Issues. These are **backlog entries, not
> existing Issues**. Do not close an item until its acceptance criteria are met.

## Safety principles

- MyHeritage remains the system of record; the MCP must not write to FTB files.
- Real personal genealogy data must not be used in public CI fixtures or logs.
- Natural-child labels are *recorded assertions*, not independent proof of descent.
- Validate MyHeritage account terms / obtain required authorization before AI
  processing of actual customer/user family-tree data.
- Do not expose unauthenticated MCP endpoints to the Internet.

## P0 / data integrity

### 1. Separate biological ancestry from adoptive/foster relationships (in progress)

Default `get_ancestors` to natural-child edges, provide an explicit inclusive
mode, and annotate every parent edge with its recorded family/role. Cover
FTB and GEDCOM synthetic fixtures, cycles, depth limits, and counts.

**Acceptance:** independent test for a foster/adopted child; no hidden
non-biological steps in a default pedigree; the tool never asserts proof.
Initial implementation in `feat/lineage-integrity-readonly`.

### 2. Authentication, authorization, and safe remote deployment

HTTP/SSE must reject non-loopback binds until an authenticated transport is
implemented. Review MCP Origin/session protections, TLS, OAuth or service
authentication, least-privilege authorization and audit logging.

**Acceptance:** network-facing unauthenticated requests fail closed, including
through tunnel/proxy deployments; integration test for negative access.

## P1 / correctness and usability

### 3. Consistent snapshots and live FTB refresh

Detect file changes, capture a SQLite-consistent snapshot (WAL aware),
rebuild indexes without serving mixed generations, publish freshness metadata
and handle failed refresh without replacing good state. Specify GEDCOM
refresh semantics separately.

**Acceptance:** tests for database update, concurrent readers and failed reload.

### 4. Relationship-level evidence and complete citation traversal

Expose person, event, family and source-level citations with stable identifiers.
Propagate evidence to parent-child claims when available. Distinguish an
independently documented link from a copied tree claim.

**Acceptance:** fixtures with citations linked to family facts and individual
facts; source paths are traceable without guessing.

### 5. Read-only audit engine and duplicate-candidate ranking

Add deterministic, paginated anomaly reporting: duplicates (aliases and
multilingual names), impossible dates, ages, disconnected links, conflicting
parentage and missing essential evidence. Preserve qualifiers like `ABT`,
`BEF` and `AFT`.

**Acceptance:** reports contain entity IDs, evidence, severity, candidate
confidence and a *proposed* correction, never automated merge/delete.

### 6. Protect living relatives and prevent prompt injection

Redact sensitive living-person data by default in external AI contexts,
bound free-text results and treat notes/citations/documents as untrusted
data rather than instructions. Ensure logs contain no secrets or PII.

**Acceptance:** privacy tests across get_person, search, sources, notes,
timelines and aggregate results; authenticated intentional opt-in only.

### 7. Format and parsing compatibility

Define supported FTB schemas, fail closed for incompatible database versions,
run SQLite integrity validation and malformed input tests. Reproduce
[upstream issue #3](https://github.com/mouchar/ftb-mcp/issues/3):
blank interior lines in exported GEDCOM values cause import failure.

**Acceptance:** regression fixture and test passing against the reported case;
no modifications to the original source GEDCOM/.ftb; bounded parser resources.

## P2 / productization

### 8. Setup, backups, validation, and changelog workflow

Document Windows FTB sync -> local MCP, atomic backups, restore tests,
read-only local operation, source fingerprints, documented limits,
no MyHeritage write API, and legally approved data use.

**Acceptance:** self-contained deployment guide with threat model and
operator validation checklist; no credentials/OTP exposed to the AI.

## Definition of Done for every engineering change

1. Synthetic regression tests and formatting/lint CI pass.
2. No regression to biological/adoptive/foster distinction or data ownership.
3. No write path to MyHeritage or local original FTB database.
4. Release note or documentation update for any API behavior change.
5. Reviewer can reproduce via documented tests; PR links to a corresponding
   GitHub Issue once the repository's Issues feature is enabled.
