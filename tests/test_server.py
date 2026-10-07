"""Integration tests driving the tools the way an MCP client would.

Tools are invoked through the server's own dispatch so that argument coercion and
result serialisation are covered, not just the underlying query functions.
"""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from ftb_mcp import server


@pytest.fixture(scope="module", autouse=True)
def opened():
    from tests.conftest import SAMPLE

    server.state.open(str(SAMPLE), "cs")
    yield
    if server.state.db:
        server.state.db.close()
        server.state.db = None


async def call(name: str, **arguments):
    """Invoke a tool through the MCP server and return its structured result.

    mcp 2.x returns a CallToolResult; a failing tool raises ToolError rather than
    returning a result with is_error set.
    """
    result = await server.mcp.call_tool(name, arguments)
    assert result.is_error is False, result.content
    return result.structured_content


@pytest.mark.anyio
async def test_every_tool_is_registered():
    tools = await server.mcp.list_tools()
    names = {tool.name for tool in tools}
    assert names == {
        "get_tree_info",
        "search_persons",
        "list_surnames",
        "search_places",
        "search_notes",
        "get_person",
        "get_person_facts",
        "get_person_timeline",
        "get_relatives",
        "get_ancestors",
        "get_descendants",
        "find_relationship_path",
        "get_family",
        "get_sources",
        "get_citations",
        "get_media_metadata",
        "get_statistics",
    }


@pytest.mark.anyio
async def test_every_tool_has_a_description_and_schema():
    for tool in await server.mcp.list_tools():
        assert tool.description and len(tool.description) > 40, tool.name
        assert tool.input_schema["type"] == "object", tool.name


@pytest.mark.anyio
async def test_tree_info():
    info = await call("get_tree_info")
    assert info["counts"]["individuals"] == 15
    assert info["name_languages_used"] == {"cs": 15}


@pytest.mark.anyio
async def test_search_paginates():
    first = await call("search_persons", last_name="Herda", limit=2, offset=0)
    assert first["returned"] <= 2
    if first["total_count"] > 2:
        assert first["has_more"] is True
        second = await call("search_persons", last_name="Herda", limit=2, offset=2)
        ids_first = {p["person_id"] for p in first["results"]}
        ids_second = {p["person_id"] for p in second["results"]}
        assert not ids_first & ids_second


@pytest.mark.anyio
async def test_get_person_sections_are_selectable():
    match = await call("search_persons", last_name="Herda", limit=1)
    person_id = match["results"][0]["person_id"]

    full = await call("get_person", person_id=person_id)
    assert "facts" in full and "immediate_relatives" in full

    slim = await call("get_person", person_id=person_id, include=["facts"])
    assert "facts" in slim
    assert "immediate_relatives" not in slim


@pytest.mark.anyio
async def test_unknown_person_raises_clear_error():
    with pytest.raises(ToolError, match="999999"):
        await call("get_person", person_id=999999)


@pytest.mark.anyio
async def test_invalid_include_is_rejected():
    match = await call("search_persons", last_name="Herda", limit=1)
    with pytest.raises(ToolError, match="valid"):
        await call("get_person", person_id=match["results"][0]["person_id"], include=["bogus"])


@pytest.mark.anyio
async def test_all_read_tools_return_serialisable_results():
    """Smoke-call every tool and confirm the payload survives JSON round-tripping."""
    match = await call("search_persons", limit=1)
    person_id = match["results"][0]["person_id"]
    relatives = await call("get_relatives", person_id=person_id)
    family_id = None
    for group in ("parents", "spouses", "children"):
        for entry in relatives.get(group, []):
            family_id = entry.get("family_id", family_id)

    calls = [
        ("get_tree_info", {}),
        ("search_persons", {"limit": 3}),
        ("list_surnames", {"limit": 5}),
        ("search_places", {"limit": 5}),
        ("search_notes", {"limit": 5}),
        ("get_person", {"person_id": person_id}),
        ("get_person_facts", {"person_id": person_id}),
        ("get_person_timeline", {"person_id": person_id}),
        ("get_relatives", {"person_id": person_id}),
        ("get_ancestors", {"person_id": person_id, "generations": 2}),
        ("get_descendants", {"person_id": person_id, "generations": 2}),
        ("find_relationship_path", {"person_id_a": person_id, "person_id_b": person_id}),
        ("get_sources", {"limit": 3}),
        ("get_citations", {"person_id": person_id}),
        ("get_media_metadata", {"limit": 3}),
        ("get_statistics", {}),
    ]
    if family_id is not None:
        calls.append(("get_family", {"family_id": family_id}))

    for name, arguments in calls:
        payload = await call(name, **arguments)
        json.dumps(payload)  # raises if anything is not serialisable


@pytest.mark.anyio
async def test_timeline_includes_child_births():
    parents = await call("search_persons", limit=200)
    for candidate in parents["results"]:
        relatives = await call(
            "get_relatives", person_id=candidate["person_id"], kinds=["children"]
        )
        if relatives["counts"]["children"]:
            timeline = await call("get_person_timeline", person_id=candidate["person_id"])
            subjects = {entry["subject"] for entry in timeline["timeline"]}
            assert "child" in subjects or "self" in subjects
            return
    pytest.skip("no person with children in the first page")


@pytest.mark.anyio
async def test_media_never_leaks_binary_or_paths():
    media = await call("get_media_metadata", limit=50)
    for item in media["results"]:
        assert set(item) <= {"media_item_id", "title", "description", "date", "place", "person_id"}


@pytest.mark.anyio
async def test_ancestor_lineage_is_explicit_and_validated():
    from ftb_mcp.schema import ROLE_ADOPTED_CHILD, ROLE_FOSTER_CHILD

    index = server.state.index(server.state.default_lang)
    non_biological = [
        (pid, fid, role)
        for fid, children in index.family_children.items()
        for pid, role in children
        if role in (ROLE_ADOPTED_CHILD, ROLE_FOSTER_CHILD)
    ]
    assert non_biological, "sample fixture should exercise adoptive/foster relationships"

    for pid, fid, role in non_biological:
        inclusive_links = index.parent_links(pid)
        assert any(
            link_fid == fid and link_role == role
            for _, link_fid, link_role in inclusive_links
        )
        assert all(link_fid != fid for _, link_fid, _ in index.parent_links(pid, natural_only=True))

        biological = await call("get_ancestors", person_id=pid, generations=1)
        inclusive = await call("get_ancestors", person_id=pid, generations=1, lineage="all")
        assert biological["lineage_mode"] == "biological"
        assert inclusive["lineage_mode"] == "all"
        assert inclusive["recorded_parentage_is_not_proof"] is True
        assert inclusive["ancestors_found"] >= biological["ancestors_found"]
        # Every direct parent edge included in the pedigree is explicitly labelled.
        for branch in ("father", "mother"):
            if branch in inclusive["root"]:
                assert inclusive["root"][branch]["parentage_recorded_as"] in (
                    "natural child", "adopted child", "foster child"
                )

    with pytest.raises(ToolError, match="Invalid lineage"):
        await call("get_ancestors", person_id=non_biological[0][0], lineage="unknown")


def test_remote_http_bind_is_rejected_before_opening_tree(capsys):
    assert server.main(["--db-path", "irrelevant.ftb", "--host", "0.0.0.0"]) == 2
    assert "remote HTTP is disabled" in capsys.readouterr().err
    assert server.main(["--db-path", "irrelevant.ftb", "--host", "192.168.1.10"]) == 2
    assert server.main(["--db-path", "irrelevant.ftb", "--host", "::"]) == 2


def test_only_explicit_loopback_hosts_are_accepted():
    assert server._is_loopback_host("127.0.0.1")
    assert server._is_loopback_host("::1")
    assert server._is_loopback_host("localhost")
    assert not server._is_loopback_host("0.0.0.0")
    assert not server._is_loopback_host("example.com")
    assert not server._is_loopback_host("192.168.1.20")
