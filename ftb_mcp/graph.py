"""In-memory index over people and their relationships.

Family trees are small -- kafkova.ftb holds 1864 people and 2428 connections -- so the
whole relationship graph is loaded once at startup. That buys two things SQL would
make awkward:

* Diacritic-insensitive name search. SQLite's LIKE folds case for ASCII only, so a
  query for "Kafkova" would never match "Kafková". Here names are folded once at load.
* Cheap graph traversal for pedigrees, descendant trees and relationship paths.

The index is rebuilt only when explicitly asked; the underlying file is opened
read-only and is not expected to change while the server runs.
"""

from __future__ import annotations

import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .db import FtbDatabase
from .decode import year_of
from .queries import LANG_RANK, VITALS_CTE, language_code
from .schema import (
    CHILD_ROLES,
    GENDER,
    LIVING_STATUS,
    NATURAL_CHILD_ROLES,
    ROLE_TYPE,
    SPOUSE_ROLES,
    label_for,
)


def fold(text: str | None) -> str:
    """Casefold and strip diacritics so 'Kafková' and 'kafkova' compare equal."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


@dataclass(slots=True)
class Person:
    person_id: int
    first_name: str
    last_name: str
    prefix: str = ""
    suffix: str = ""
    nickname: str = ""
    married_surname: str = ""
    alias_name: str = ""
    aka: str = ""
    former_name: str = ""
    religious_name: str = ""
    gender: str = "U"
    living_status: int | None = None
    birth_year: int | None = None
    death_year: int | None = None
    text_language: int | None = None
    searchable: str = field(default="", repr=False)

    @property
    def full_name(self) -> str:
        parts = [self.prefix, self.first_name, self.last_name, self.suffix]
        return " ".join(p for p in parts if p).strip() or "(unnamed)"

    def summary(self) -> dict[str, Any]:
        """Compact shape used in search results and relative listings."""
        out: dict[str, Any] = {
            "person_id": self.person_id,
            "name": self.full_name,
            "first_name": self.first_name or None,
            "last_name": self.last_name or None,
            "gender": GENDER.get(self.gender, "unknown"),
            "birth_year": self.birth_year,
            "death_year": self.death_year,
            "living_status": label_for(LIVING_STATUS, self.living_status),
        }
        extras = {
            "nickname": self.nickname,
            "married_surname": self.married_surname,
            "alias_name": self.alias_name,
            "aka": self.aka,
            "former_name": self.former_name,
            "religious_name": self.religious_name,
        }
        for key, value in extras.items():
            if value:
                out[key] = value
        return out


class TreeIndex:
    """People, their names and the family graph, held in memory."""

    def __init__(self, db: FtbDatabase, lang: int) -> None:
        self.db = db
        self.lang = lang
        self.people: dict[int, Person] = {}
        # family_id -> role bucket -> person ids
        self.family_spouses: dict[int, list[int]] = {}
        self.family_children: dict[int, list[tuple[int, int]]] = {}
        # person_id -> family ids
        self.spouse_families: dict[int, list[int]] = {}
        self.child_families: dict[int, list[int]] = {}
        self._load()

    # -------------------------------------------------------------------------- load

    def _load(self) -> None:
        sql = f"""
        WITH {VITALS_CTE},
        names AS (
            SELECT d.individual_id, l.first_name, l.last_name, l.prefix, l.suffix,
                   l.nickname, l.religious_name, l.former_name, l.married_surname,
                   l.alias_name, l.aka, l.data_language,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.individual_id
                       ORDER BY {LANG_RANK.format(col="l.data_language")}
                   ) AS rn
            FROM individual_data_set d
            JOIN individual_lang_data l
              ON l.individual_data_set_id = d.individual_data_set_id
            WHERE d.delete_flag = 0
        )
        SELECT i.individual_id, i.gender, i.is_alive,
               n.first_name, n.last_name, n.prefix, n.suffix, n.nickname,
               n.religious_name, n.former_name, n.married_surname, n.alias_name, n.aka,
               n.data_language, v.birth_sd, v.death_sd
        FROM individual_main_data i
        LEFT JOIN names n ON n.individual_id = i.individual_id AND n.rn = 1
        LEFT JOIN vitals v ON v.individual_id = i.individual_id
        WHERE i.delete_flag = 0
        """
        for row in self.db.query(sql, {"lang": self.lang}):
            birth_year = year_of(row["birth_sd"])
            death_year = year_of(row["death_sd"])

            person = Person(
                person_id=row["individual_id"],
                first_name=(row["first_name"] or "").strip(),
                last_name=(row["last_name"] or "").strip(),
                prefix=(row["prefix"] or "").strip(),
                suffix=(row["suffix"] or "").strip(),
                nickname=(row["nickname"] or "").strip(),
                married_surname=(row["married_surname"] or "").strip(),
                alias_name=(row["alias_name"] or "").strip(),
                aka=(row["aka"] or "").strip(),
                former_name=(row["former_name"] or "").strip(),
                religious_name=(row["religious_name"] or "").strip(),
                gender=row["gender"] or "U",
                living_status=row["is_alive"],
                birth_year=birth_year,
                death_year=death_year,
                text_language=row["data_language"],
            )
            person.searchable = fold(
                " ".join(
                    filter(
                        None,
                        (
                            person.first_name,
                            person.last_name,
                            person.nickname,
                            person.married_surname,
                            person.alias_name,
                            person.aka,
                            person.former_name,
                            person.religious_name,
                        ),
                    )
                )
            )
            self.people[person.person_id] = person

        for row in self.db.query(
            """
            SELECT family_id, individual_id, individual_role_type, child_order_in_family
            FROM family_individual_connection
            WHERE delete_flag = 0
            ORDER BY family_id,
                     CASE WHEN child_order_in_family < 0 THEN 1 ELSE 0 END,
                     child_order_in_family, individual_id
            """
        ):
            fid, pid, role = row["family_id"], row["individual_id"], row["individual_role_type"]
            if pid not in self.people:
                continue  # connection pointing at a deleted individual
            if role in SPOUSE_ROLES:
                self.family_spouses.setdefault(fid, []).append(pid)
                self.spouse_families.setdefault(pid, []).append(fid)
            elif role in CHILD_ROLES:
                self.family_children.setdefault(fid, []).append((pid, role))
                self.child_families.setdefault(pid, []).append(fid)

    # ------------------------------------------------------------------------ lookup

    def get(self, person_id: int) -> Person | None:
        return self.people.get(person_id)

    def require(self, person_id: int) -> Person:
        person = self.people.get(person_id)
        if person is None:
            raise KeyError(f"No person with id {person_id} in this tree")
        return person

    # ------------------------------------------------------------------------ search

    def search(
        self,
        *,
        name: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        gender: str | None = None,
        birth_year_from: int | None = None,
        birth_year_to: int | None = None,
        death_year_from: int | None = None,
        death_year_to: int | None = None,
        is_living: bool | None = None,
    ) -> list[Person]:
        """Filter people. Name matching ignores case and diacritics."""
        needle = fold(name)
        first = fold(first_name)
        last = fold(last_name)
        want_gender = (gender or "").strip().upper()[:1] or None

        matches: list[Person] = []
        for person in self.people.values():
            if needle and needle not in person.searchable:
                continue
            if first and first not in fold(person.first_name):
                continue
            if last and not (
                last in fold(person.last_name) or last in fold(person.married_surname)
            ):
                continue
            if want_gender and person.gender != want_gender:
                continue
            if birth_year_from is not None and (
                person.birth_year is None or person.birth_year < birth_year_from
            ):
                continue
            if birth_year_to is not None and (
                person.birth_year is None or person.birth_year > birth_year_to
            ):
                continue
            if death_year_from is not None and (
                person.death_year is None or person.death_year < death_year_from
            ):
                continue
            if death_year_to is not None and (
                person.death_year is None or person.death_year > death_year_to
            ):
                continue
            if is_living is not None:
                living = person.living_status == 3
                if living is not is_living:
                    continue
            matches.append(person)

        matches.sort(key=lambda p: (p.birth_year or 9999, fold(p.last_name), fold(p.first_name)))
        return matches

    def surnames(self, prefix: str | None, min_count: int) -> list[dict[str, Any]]:
        """Surname frequency with the year range each surname spans."""
        buckets: dict[str, dict[str, Any]] = {}
        folded_prefix = fold(prefix)
        for person in self.people.values():
            surname = person.last_name
            if not surname or (folded_prefix and not fold(surname).startswith(folded_prefix)):
                continue
            bucket = buckets.setdefault(
                surname,
                {"surname": surname, "count": 0, "earliest_birth": None, "latest_birth": None},
            )
            bucket["count"] += 1
            if person.birth_year is not None:
                low, high = bucket["earliest_birth"], bucket["latest_birth"]
                bucket["earliest_birth"] = min(low or person.birth_year, person.birth_year)
                bucket["latest_birth"] = max(high or person.birth_year, person.birth_year)

        results = [b for b in buckets.values() if b["count"] >= min_count]
        results.sort(key=lambda b: (-b["count"], fold(b["surname"])))
        return results

    # --------------------------------------------------------------------- relatives

    def parent_links(
        self, person_id: int, natural_only: bool = False
    ) -> list[tuple[int, int, int]]:
        """Return (parent_id, family_id, child's recorded role) for each parent edge.

        A child in an adopted/foster family has parents in the family graph, but that
        edge must not be treated as evidence of a biological pedigree.
        """
        out: list[tuple[int, int, int]] = []
        for fid in self.child_families.get(person_id, []):
            role = next(
                (role for pid, role in self.family_children.get(fid, []) if pid == person_id),
                None,
            )
            if role not in CHILD_ROLES or (natural_only and role not in NATURAL_CHILD_ROLES):
                continue
            out.extend((parent_id, fid, role) for parent_id in self.family_spouses.get(fid, []))
        return out

    def parents(self, person_id: int, natural_only: bool = False) -> list[int]:
        """Return recorded parents; natural_only excludes adoption and foster links."""
        return [parent_id for parent_id, _, _ in self.parent_links(person_id, natural_only)]

    def children(self, person_id: int, natural_only: bool = False) -> list[int]:
        roles = NATURAL_CHILD_ROLES if natural_only else CHILD_ROLES
        out: list[int] = []
        for fid in self.spouse_families.get(person_id, []):
            out.extend(pid for pid, role in self.family_children.get(fid, []) if role in roles)
        return out

    def spouses(self, person_id: int) -> list[tuple[int, int]]:
        """(spouse_id, family_id) pairs."""
        out: list[tuple[int, int]] = []
        for fid in self.spouse_families.get(person_id, []):
            out.extend((pid, fid) for pid in self.family_spouses.get(fid, []) if pid != person_id)
        return out

    def siblings(self, person_id: int, include_half: bool = True) -> list[int]:
        """Siblings, optionally restricted to those sharing every parent family."""
        out: list[int] = []
        for fid in self.child_families.get(person_id, []):
            out.extend(pid for pid, _ in self.family_children.get(fid, []) if pid != person_id)
        if include_half:
            return out
        mine = set(self.parents(person_id))
        return [pid for pid in out if set(self.parents(pid)) == mine]

    def relatives(self, person_id: int, kinds: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Immediate relatives grouped by kind, each with role context."""
        self.require(person_id)
        out: dict[str, list[dict[str, Any]]] = {}

        if "parents" in kinds:
            entries = []
            for fid in self.child_families.get(person_id, []):
                role = next(
                    (r for pid, r in self.family_children.get(fid, []) if pid == person_id), None
                )
                for parent_id in self.family_spouses.get(fid, []):
                    entry = self.people[parent_id].summary()
                    entry["family_id"] = fid
                    entry["relationship"] = label_for(ROLE_TYPE, role)
                    entries.append(entry)
            out["parents"] = entries

        if "siblings" in kinds:
            out["siblings"] = [
                self.people[pid].summary() for pid in dict.fromkeys(self.siblings(person_id))
            ]

        if "spouses" in kinds:
            entries = []
            for spouse_id, fid in self.spouses(person_id):
                entry = self.people[spouse_id].summary()
                entry["family_id"] = fid
                entries.append(entry)
            out["spouses"] = entries

        if "children" in kinds:
            entries = []
            for fid in self.spouse_families.get(person_id, []):
                for child_id, role in self.family_children.get(fid, []):
                    entry = self.people[child_id].summary()
                    entry["family_id"] = fid
                    entry["relationship"] = label_for(ROLE_TYPE, role)
                    entries.append(entry)
            out["children"] = entries

        return out

    # --------------------------------------------------------------------- pedigrees

    def ancestors(
        self, person_id: int, generations: int, natural_only: bool = True
    ) -> dict[str, Any]:
        """Pedigree upward, defaulting to recorded natural-child edges only.

        FTB roles are *recorded parentage*, not independent proof of biological descent.
        When natural_only=False, adopted and foster ancestors are included explicitly
        and each step carries its parentage classification.
        """
        self.require(person_id)

        def build(
            pid: int,
            depth: int,
            ahnentafel: int,
            seen: frozenset[int],
            via: tuple[int, int] | None = None,
        ) -> dict[str, Any]:
            node = self.people[pid].summary()
            node["generation"] = depth
            node["ahnentafel"] = ahnentafel
            if via is not None:
                family_id, role = via
                node["family_id"] = family_id
                node["parentage_recorded_as"] = label_for(ROLE_TYPE, role)
            # Guard against cycles in incorrect genealogical records.
            parents = [
                (parent_id, fid, role)
                for parent_id, fid, role in self.parent_links(pid, natural_only)
                if parent_id not in seen
            ]
            if depth >= generations:
                if parents:
                    node["has_more_ancestors"] = True
                return node

            father = mother = None
            for link in parents:
                parent = self.people[link[0]]
                if parent.gender == "M" and father is None:
                    father = link
                elif parent.gender == "F" and mother is None:
                    mother = link
                elif father is None:
                    father = link
                elif mother is None:
                    mother = link

            if father is not None:
                parent_id, fid, role = father
                node["father"] = build(
                    parent_id, depth + 1, ahnentafel * 2, seen | {parent_id}, (fid, role)
                )
            if mother is not None:
                parent_id, fid, role = mother
                node["mother"] = build(
                    parent_id, depth + 1, ahnentafel * 2 + 1, seen | {parent_id}, (fid, role)
                )
            return node

        tree = build(person_id, 0, 1, frozenset({person_id}))
        found = self._reachable(person_id, generations, upward=True, natural_only=natural_only)
        return {
            "root": tree,
            "generations_requested": generations,
            "ancestors_found": len(found),
            "lineage_mode": "biological" if natural_only else "all",
            "recorded_parentage_is_not_proof": True,
        }

    def descendants(self, person_id: int, generations: int) -> dict[str, Any]:
        """Descendant tree, breaking cycles defensively on malformed trees."""
        self.require(person_id)

        def build(pid: int, depth: int, seen: frozenset[int]) -> dict[str, Any]:
            node = self.people[pid].summary()
            node["generation"] = depth
            kids = [c for c in dict.fromkeys(self.children(pid)) if c not in seen]
            if depth >= generations:
                if kids:
                    node["has_more_descendants"] = True
                return node
            if kids:
                node["children"] = [build(k, depth + 1, seen | {k}) for k in kids]
            return node

        tree = build(person_id, 0, frozenset({person_id}))
        found = self._reachable(person_id, generations, upward=False)
        by_generation: dict[int, int] = {}
        for _, depth in found.items():
            by_generation[depth] = by_generation.get(depth, 0) + 1
        return {
            "root": tree,
            "generations_requested": generations,
            "descendants_found": len(found),
            "by_generation": {f"generation_{d}": n for d, n in sorted(by_generation.items())},
        }

    def _reachable(
        self, person_id: int, generations: int, upward: bool, natural_only: bool = False
    ) -> dict[int, int]:
        """BFS returning {person_id: depth} excluding the starting person."""
        seen: dict[int, int] = {}
        queue = deque([(person_id, 0)])
        visited = {person_id}
        while queue:
            pid, depth = queue.popleft()
            if depth >= generations:
                continue
            nexts = self.parents(pid, natural_only=natural_only) if upward else self.children(pid)
            for other in nexts:
                if other in visited:
                    continue
                visited.add(other)
                seen[other] = depth + 1
                queue.append((other, depth + 1))
        return seen

    # -------------------------------------------------------------- relationship path

    def _neighbours(self, person_id: int) -> list[tuple[int, str, int, int | None]]:
        """Adjacent relatives with (id, direction, family_id, child's role).

        The same family link can encode natural, adopted or foster parentage. Keep
        that record on each edge to avoid presenting all paths as blood ancestry.
        """
        out: list[tuple[int, str, int, int | None]] = []
        out.extend(
            (parent_id, "parent", fid, role)
            for parent_id, fid, role in self.parent_links(person_id)
        )
        for fid in self.spouse_families.get(person_id, []):
            out.extend(
                (child_id, "child", fid, role)
                for child_id, role in self.family_children.get(fid, [])
            )
        out.extend((pid, "spouse", fid, None) for pid, fid in self.spouses(person_id))
        return out

    def relationship_path(
        self, person_id_a: int, person_id_b: int, max_depth: int = 15
    ) -> dict[str, Any]:
        """Find a recorded family path without implying that all links are biological."""
        self.require(person_id_a)
        self.require(person_id_b)

        if person_id_a == person_id_b:
            return {
                "found": True,
                "same_person": True,
                "steps": [],
                "relationship": "same person",
                "includes_non_natural_parentage": False,
            }

        previous: dict[int, tuple[int, str, int, int | None]] = {}
        visited = {person_id_a}
        queue = deque([(person_id_a, 0)])
        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for other, link, fid, role in self._neighbours(current):
                if other in visited:
                    continue
                visited.add(other)
                previous[other] = (current, link, fid, role)
                if other == person_id_b:
                    queue.clear()
                    break
                queue.append((other, depth + 1))
            if person_id_b in previous:
                break

        if person_id_b not in previous:
            return {
                "found": False,
                "searched_within_steps": max_depth,
                "relationship": None,
                "steps": [],
            }

        chain: list[tuple[int, str, int, int | None]] = []
        node = person_id_b
        while node != person_id_a:
            parent, link, fid, role = previous[node]
            chain.append((node, link, fid, role))
            node = parent
        chain.reverse()

        steps = []
        for pid, link, fid, role in chain:
            step: dict[str, Any] = {
                "person_id": pid,
                "name": self.people[pid].full_name,
                "link": f"is the {link} of the previous person",
                "family_id": fid,
            }
            if role is not None:
                step["parentage_recorded_as"] = label_for(ROLE_TYPE, role)
            steps.append(step)

        links = [link for _, link, _, _ in chain]
        non_natural = any(
            role is not None and role not in NATURAL_CHILD_ROLES
            for _, _, _, role in chain
        )
        relationship = self._label_path(links)
        if non_natural:
            relationship += " (includes adoptive/foster family ties)"
        return {
            "found": True,
            "same_person": False,
            "degrees_of_separation": len(chain),
            "relationship": relationship,
            "includes_non_natural_parentage": non_natural,
            "recorded_parentage_is_not_proof": True,
            "steps": steps,
        }

    @staticmethod
    def _label_path(links: list[str]) -> str:
        """Name a kinship path, e.g. 'first cousin once removed'.

        Handles the blood-relative shapes (ancestor / descendant / cousin) exactly and
        falls back to a readable chain description for anything routed through a
        marriage, where no single English word applies.
        """
        ordinal = {
            1: "first",
            2: "second",
            3: "third",
            4: "fourth",
            5: "fifth",
            6: "sixth",
            7: "seventh",
            8: "eighth",
        }
        greats = {0: "", 1: "grand", 2: "great-grand"}

        def generation_word(n: int, base: str) -> str:
            if n == 1:
                return base
            prefix = greats.get(n - 1) or ("great-" * (n - 2) + "grand")
            return f"{prefix}{base}"

        if "spouse" in links:
            if links == ["spouse"]:
                return "spouse"
            return " -> ".join(links) + " (relationship by marriage)"

        up = links.count("parent")
        down = links.count("child")

        # A clean run of ups then downs is a blood relationship.
        if links == ["parent"] * up + ["child"] * down:
            if down == 0:
                return generation_word(up, "parent")
            if up == 0:
                return generation_word(down, "child")
            if up == 1 and down == 1:
                return "sibling"
            if up == 1:
                return generation_word(down - 1, "nephew/niece")
            if down == 1:
                return generation_word(up - 1, "uncle/aunt")
            degree = min(up, down) - 1
            removed = abs(up - down)
            label = f"{ordinal.get(degree, f'{degree}th')} cousin"
            if removed == 1:
                return f"{label} once removed"
            if removed == 2:
                return f"{label} twice removed"
            if removed:
                return f"{label} {removed} times removed"
            return label

        return " -> ".join(links)

    def stats_names(self, limit: int = 15) -> dict[str, Any]:
        """Most common given names and surnames."""
        firsts: dict[str, int] = {}
        for person in self.people.values():
            for token in person.first_name.split():
                if token:
                    firsts[token] = firsts.get(token, 0) + 1
        top_first = sorted(firsts.items(), key=lambda kv: (-kv[1], fold(kv[0])))[:limit]
        return {
            "top_given_names": [{"name": n, "count": c} for n, c in top_first],
            "top_surnames": self.surnames(None, 1)[:limit],
        }

    def language_note(self) -> dict[str, Any]:
        """Which language the indexed names actually came from."""
        counts: dict[str, int] = {}
        for person in self.people.values():
            code = language_code(person.text_language) or "none"
            counts[code] = counts.get(code, 0) + 1
        return counts
