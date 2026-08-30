from __future__ import annotations

import json

import pytest

from runtime_identity.paths import PuddingClawPaths
from runtime_identity.profiles import CredentialEnvelopeDecryptionError, CredentialVault
from runtime_identity.skill_runtimes import SkillRuntimeBindingStore
from runtime_identity.skill_secrets import SkillSecretStore, validate_skill_secret_name


def _store(tmp_path) -> SkillSecretStore:
    store = SkillSecretStore(PuddingClawPaths(tmp_path / ".puddingclaw"), "local")
    store._vault = CredentialVault(b"s" * 32)
    return store


@pytest.mark.parametrize(
    "name",
    ["PATH", "PYTHONPATH", "PYTHONHOME", "NODE_PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"],
)
def test_dangerous_skill_secret_names_are_rejected(name):
    with pytest.raises(ValueError, match="not eligible"):
        validate_skill_secret_name(name)


def test_secret_submission_atomically_saves_and_binds_current_skill(tmp_path):
    store = _store(tmp_path)

    revision = store.set_and_bind(
        skill_id="demo",
        skill_version="sha256-v1",
        env_name="DEMO_API_KEY",
        secret_value="super-secret",
    )
    projection = store.projection(skill_id="demo", skill_version="sha256-v1")

    assert projection.registry_revision == revision
    assert projection.environment == {"DEMO_API_KEY": "super-secret"}
    assert store.status(
        skill_id="demo", skill_version="sha256-v1", env_name="DEMO_API_KEY"
    ) == "bound"
    raw = store.path.read_bytes()
    assert b"super-secret" not in raw
    assert b"DEMO_API_KEY" not in raw
    json.loads(raw.decode("utf-8"))


def test_existing_secret_requires_explicit_binding_for_another_skill(tmp_path):
    store = _store(tmp_path)
    store.set_and_bind(
        skill_id="alpha",
        skill_version="sha256-a",
        env_name="SHARED_TOKEN",
        secret_value="value",
    )

    assert store.status(
        skill_id="beta", skill_version="sha256-b", env_name="SHARED_TOKEN"
    ) == "reusable"
    assert store.projection(skill_id="beta", skill_version="sha256-b").environment == {}

    store.bind_existing(
        skill_id="beta",
        skill_version="sha256-b",
        env_name="SHARED_TOKEN",
    )

    assert store.projection(skill_id="beta", skill_version="sha256-b").environment == {
        "SHARED_TOKEN": "value"
    }


def test_skill_content_change_invalidates_secret_binding(tmp_path):
    store = _store(tmp_path)
    store.set_and_bind(
        skill_id="demo",
        skill_version="sha256-v1",
        env_name="DEMO_TOKEN",
        secret_value="value",
    )

    assert store.projection(skill_id="demo", skill_version="sha256-v2").environment == {}


def test_registry_revision_detects_stale_execution_projection(tmp_path):
    store = _store(tmp_path)
    first = store.set_and_bind(
        skill_id="demo",
        skill_version="sha256-v1",
        env_name="DEMO_TOKEN",
        secret_value="old",
    )
    assert store.revision_is_current(first)

    store.set_and_bind(
        skill_id="demo",
        skill_version="sha256-v1",
        env_name="DEMO_TOKEN",
        secret_value="new",
    )

    assert not store.revision_is_current(first)


def test_unreadable_skill_secret_can_be_repaired_by_explicit_entry(tmp_path):
    store = _store(tmp_path)
    store.set_and_bind(
        skill_id="demo",
        skill_version="sha256-v1",
        env_name="DEMO_TOKEN",
        secret_value="old",
    )
    damaged = SkillSecretStore(PuddingClawPaths(tmp_path / ".puddingclaw"), "local")
    damaged._vault = CredentialVault(b"x" * 32)

    assert damaged.status(
        skill_id="demo", skill_version="sha256-v1", env_name="DEMO_TOKEN"
    ) == "unreadable"
    with pytest.raises(CredentialEnvelopeDecryptionError):
        damaged.projection(skill_id="demo", skill_version="sha256-v1")

    damaged.set_and_bind(
        skill_id="demo",
        skill_version="sha256-v1",
        env_name="DEMO_TOKEN",
        secret_value="new",
    )

    assert damaged.projection(skill_id="demo", skill_version="sha256-v1").environment == {
        "DEMO_TOKEN": "new"
    }
    assert list(damaged.path.parent.glob("registry.enc.unreadable-*"))


def test_explicit_skill_runtime_binding_is_scoped_to_content_version(tmp_path):
    store = SkillRuntimeBindingStore(PuddingClawPaths(tmp_path / ".puddingclaw"))

    assert store.runtime_for(skill_id="demo", skill_version="sha256-v1") == "host"
    store.bind(skill_id="demo", skill_version="sha256-v1", runtime="docker")

    assert store.runtime_for(skill_id="demo", skill_version="sha256-v1") == "docker"
    assert store.runtime_for(skill_id="demo", skill_version="sha256-v2") == "host"


def test_revoked_skill_secret_binding_stops_projection_without_deleting_value(tmp_path):
    store = _store(tmp_path)
    store.set_and_bind(
        skill_id="alpha",
        skill_version="sha256-a",
        env_name="SHARED_TOKEN",
        secret_value="value",
    )

    store.revoke_binding(skill_id="alpha", env_name="SHARED_TOKEN")

    assert store.projection(skill_id="alpha", skill_version="sha256-a").environment == {}
    assert store.status(
        skill_id="beta", skill_version="sha256-b", env_name="SHARED_TOKEN"
    ) == "reusable"


def test_secret_request_resolves_user_home_skill_version(tmp_path, monkeypatch):
    from api.skill_secret_requests import _current_skill_version
    from runtime_identity.software_runtime import skill_content_version

    home = tmp_path / ".puddingclaw"
    skill = home / "skills" / "home-only"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: home-only\ndescription: test\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PUDDINGCLAW_HOME", str(home))

    assert _current_skill_version("home-only") == skill_content_version(skill)
