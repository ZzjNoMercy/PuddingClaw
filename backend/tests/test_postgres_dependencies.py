from __future__ import annotations

from postgres_dependencies import normalize_pgvector_status, pgvector_install_command


def test_pgvector_install_command_tracks_server_major() -> None:
    assert pgvector_install_command(160013, system="Darwin") == "./scripts/start-local-infra.sh"
    assert pgvector_install_command(170004, system="Linux") == "sudo apt install postgresql-17-pgvector"


def test_pgvector_status_distinguishes_available_from_installed() -> None:
    available = normalize_pgvector_status(
        {
            "server_version_num": 160013,
            "available": True,
            "default_version": "0.8.5",
            "installed_version": None,
        }
    )
    assert available == {
        "required": True,
        "available": True,
        "installed": False,
        "version": "0.8.5",
        "server_major": 16,
        "install_command": "",
    }

    missing = normalize_pgvector_status(
        {
            "server_version_num": 160013,
            "available": False,
            "default_version": None,
            "installed_version": None,
        }
    )
    assert missing["available"] is False
    assert missing["install_command"] == "./scripts/start-local-infra.sh"
