"""Registry tests: namespacing rules + DB-backed resolution."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mcp_semantic_gateway.retrieval.registry import (
    MAX_TOOL_NAME,
    NAMESPACE_SEP,
    SUFFIX_SEP,
    ToolRegistry,
    canonicalize_names,
    derive_namespace_prefix,
)
from mcp_semantic_gateway.storage.metadata_db import MetadataDB, ToolRecord


# ---------------------------------------------------------------------------
# derive_namespace_prefix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "server_id, expected",
    [
        ("petstore", "pet"),
        ("Petstore", "pet"),
        ("pet", "pet"),
        ("p", "p"),
        ("users-api", "ua"),
        ("my_internal_api", "mia"),
        ("billing.public.api", "bpa"),
        ("a-b-c-d-e", "abc"),  # capped at 3
        ("123abc", "123"),
    ],
)
def test_derive_namespace_prefix(server_id, expected):
    assert derive_namespace_prefix(server_id) == expected


@pytest.mark.parametrize("server_id", ["", "---", "...", "   "])
def test_derive_namespace_prefix_rejects_pathological_input(server_id):
    with pytest.raises(ValueError):
        derive_namespace_prefix(server_id)


# ---------------------------------------------------------------------------
# canonicalize_names
# ---------------------------------------------------------------------------


def test_separator_is_dot():
    """Pin the public separator constant; downstream tests rely on it."""

    assert NAMESPACE_SEP == "."
    assert SUFFIX_SEP == "."


def test_unique_names_are_not_namespaced_by_default():
    entries = [("petstore", "addPet"), ("users-api", "createUser")]
    out = canonicalize_names(entries)
    assert out[("petstore", "addPet")] == "addPet"
    assert out[("users-api", "createUser")] == "createUser"


def test_collision_across_servers_triggers_namespacing():
    entries = [("petstore", "getUser"), ("users-api", "getUser")]
    out = canonicalize_names(entries)
    assert out[("petstore", "getUser")] == "pet.getUser"
    assert out[("users-api", "getUser")] == "ua.getUser"


def test_unique_names_remain_bare_when_others_collide():
    # ``addPet`` is unique; only ``getUser`` collides. The unique name should
    # not be namespaced.
    entries = [
        ("petstore", "addPet"),
        ("petstore", "getUser"),
        ("users-api", "getUser"),
    ]
    out = canonicalize_names(entries)
    assert out[("petstore", "addPet")] == "addPet"
    assert out[("petstore", "getUser")] == "pet.getUser"
    assert out[("users-api", "getUser")] == "ua.getUser"


def test_force_namespace_namespaces_everything():
    entries = [("petstore", "addPet"), ("users-api", "createUser")]
    out = canonicalize_names(entries, force_namespace=True)
    assert out[("petstore", "addPet")] == "pet.addPet"
    assert out[("users-api", "createUser")] == "ua.createUser"


def test_clamp_long_names_to_64_chars():
    long_name = "a" * 80
    entries = [("petstore", long_name)]
    out = canonicalize_names(entries)
    assert len(out[("petstore", long_name)]) == MAX_TOOL_NAME


def test_clamp_long_namespaced_names():
    long_name = "operationName" + "X" * 80
    entries = [("petstore", long_name), ("users-api", long_name)]
    out = canonicalize_names(entries)
    for v in out.values():
        assert len(v) <= MAX_TOOL_NAME
        # Each is namespaced; the post-clamp collision logic adds a numeric
        # suffix so we verify the prefix appears somewhere in the canonical.
        assert v.startswith(("pet.", "ua."))


def test_post_clamp_collisions_get_dot_numeric_suffixes_on_every_member():
    """When two server IDs share a 3-letter prefix and the operation name is
    long enough to clamp, every collision-group member gets a ``.N`` suffix.
    """

    long_op = "operationName" + "X" * 80  # > 60 chars after prefix + sep
    entries = [("petstore", long_op), ("petfinder", long_op)]
    out = canonicalize_names(entries)
    canon_pet_store = out[("petstore", long_op)]
    canon_pet_find = out[("petfinder", long_op)]
    assert canon_pet_store != canon_pet_find
    assert all(len(v) <= MAX_TOOL_NAME for v in out.values())
    # Both members get suffixed (no implicit winner).
    assert canon_pet_store.endswith(".1") or canon_pet_store.endswith(".2")
    assert canon_pet_find.endswith(".1") or canon_pet_find.endswith(".2")
    suffixes = sorted(v.rsplit(".", 1)[-1] for v in (canon_pet_store, canon_pet_find))
    assert suffixes == ["1", "2"]


def test_post_clamp_collisions_three_members_get_1_2_3():
    long_op = "operationName" + "X" * 80
    entries = [
        ("petstore", long_op),
        ("petfinder", long_op),
        ("petworld", long_op),
    ]
    out = canonicalize_names(entries)
    suffixes = sorted(v.rsplit(".", 1)[-1] for v in out.values())
    assert suffixes == ["1", "2", "3"]


def test_canonicalize_is_deterministic():
    entries = [("petstore", "x"), ("petfinder", "x"), ("petworld", "x")]
    out_a = canonicalize_names(entries)
    out_b = canonicalize_names(list(reversed(entries)))
    assert out_a == out_b


def test_empty_input():
    assert canonicalize_names([]) == {}


# ---------------------------------------------------------------------------
# ToolRegistry (DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_resolves_canonical_name():
    with tempfile.TemporaryDirectory() as d:
        db = MetadataDB(Path(d) / "m.db")
        await db.initialize()
        await db.save_tool(ToolRecord(
            tool_id="petstore::tool::addPet",
            server_id="petstore",
            name="addPet",
            description="Add a pet",
            input_schema={"type": "object"},
            route_metadata={"method": "POST", "path_template": "/pet"},
        ))

        reg = ToolRegistry(Path(d) / "m.db")
        await reg.initialize()
        handle = await reg.resolve("addPet")
        assert handle is not None
        assert handle.canonical_name == "addPet"
        assert handle.raw_name == "addPet"
        assert handle.route_metadata["method"] == "POST"


@pytest.mark.asyncio
async def test_registry_resolves_raw_name_when_unique_and_namespacing_forced():
    """Even when ``force_namespace`` rewrites the canonical, ``resolve`` still
    accepts the raw name when it's unambiguous."""

    with tempfile.TemporaryDirectory() as d:
        db = MetadataDB(Path(d) / "m.db")
        await db.initialize()
        await db.save_tool(ToolRecord(
            tool_id="petstore::tool::addPet",
            server_id="petstore",
            name="addPet",
            input_schema={"type": "object"},
        ))

        reg = ToolRegistry(Path(d) / "m.db", force_namespace=True)
        await reg.initialize()

        canon = reg.canonical_for("petstore", "addPet")
        assert canon == "pet.addPet"
        h_canon = await reg.resolve(canon)
        h_raw = await reg.resolve("addPet")
        assert h_canon and h_raw
        assert h_canon.tool_id == h_raw.tool_id


@pytest.mark.asyncio
async def test_registry_returns_none_for_ambiguous_raw_name():
    with tempfile.TemporaryDirectory() as d:
        db = MetadataDB(Path(d) / "m.db")
        await db.initialize()
        await db.save_tool(ToolRecord(
            tool_id="petstore::tool::getUser",
            server_id="petstore",
            name="getUser",
            input_schema={"type": "object"},
        ))
        await db.save_tool(ToolRecord(
            tool_id="users-api::tool::getUser",
            server_id="users-api",
            name="getUser",
            input_schema={"type": "object"},
        ))

        reg = ToolRegistry(Path(d) / "m.db")
        await reg.initialize()

        # Bare name is ambiguous -> None.
        assert await reg.resolve("getUser") is None

        # Canonical names succeed.
        h_pet = await reg.resolve("pet.getUser")
        h_ua = await reg.resolve("ua.getUser")
        assert h_pet and h_ua
        assert h_pet.server_id == "petstore"
        assert h_ua.server_id == "users-api"


@pytest.mark.asyncio
async def test_registry_skips_skills_and_prompts():
    """Only items with ``item_type='tool'`` participate in canonical naming."""

    with tempfile.TemporaryDirectory() as d:
        db = MetadataDB(Path(d) / "m.db")
        await db.initialize()
        await db.save_tool(ToolRecord(
            tool_id="petstore::tool::addPet",
            server_id="petstore", name="addPet", item_type="tool",
            input_schema={"type": "object"},
        ))
        await db.save_tool(ToolRecord(
            tool_id="skills::skill::do-thing",
            server_id="skills", name="do-thing", item_type="skill",
        ))

        reg = ToolRegistry(Path(d) / "m.db")
        await reg.initialize()

        handles = await reg.list_handles()
        assert {h.raw_name for h in handles} == {"addPet"}
        assert await reg.resolve("do-thing") is None


@pytest.mark.asyncio
async def test_registry_uninitialized_raises():
    with tempfile.TemporaryDirectory() as d:
        db = MetadataDB(Path(d) / "m.db")
        await db.initialize()
        reg = ToolRegistry(Path(d) / "m.db")
        with pytest.raises(RuntimeError):
            await reg.resolve("anything")
