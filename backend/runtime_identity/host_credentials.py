"""Trusted platform projections for Adapter credential archives.

The archive schema is provider-owned and platform-neutral.  A Host runner may
need a deterministic filesystem projection when the same CLI uses different
native storage directories on Linux and macOS.  These helpers operate only in
the runner's private HOME and never expose plaintext credentials.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from runtime_identity.adapters import CredentialStateSpec, LarkManagedCliAdapter

_LARK_LINUX_STORE = Path(".lark-cli/.credential-data/lark-cli")
_LARK_DARWIN_STORE = Path("Library/Application Support/lark-cli")
_ENCRYPTED_ITEM = re.compile(r"[A-Za-z0-9._-]+\.enc")


def _is_lark_contract(spec: CredentialStateSpec) -> bool:
    return spec.fingerprint == LarkManagedCliAdapter().credential_state.fingerprint


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def prepare_host_credential_state(spec: CredentialStateSpec, home: Path) -> None:
    """Project canonical Lark file credentials into macOS' native file store."""

    if sys.platform != "darwin" or not _is_lark_contract(spec):
        return
    source = home / _LARK_LINUX_STORE
    if not source.is_dir() or source.is_symlink():
        return
    key = source / "master.key"
    if not _regular_file(key) or key.stat().st_size != 32:
        raise ValueError("Lark credential archive has an invalid file master key")
    target = home / _LARK_DARWIN_STORE
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(target, 0o700)
    shutil.copyfile(key, target / "master.key.file", follow_symlinks=False)
    os.chmod(target / "master.key.file", 0o600)
    for item in source.iterdir():
        if not _regular_file(item) or _ENCRYPTED_ITEM.fullmatch(item.name) is None:
            continue
        shutil.copyfile(item, target / item.name, follow_symlinks=False)
        os.chmod(target / item.name, 0o600)


def collect_host_credential_state(spec: CredentialStateSpec, home: Path) -> None:
    """Map macOS file credentials back into the canonical Vault archive root."""

    if sys.platform != "darwin" or not _is_lark_contract(spec):
        return
    source = home / _LARK_DARWIN_STORE
    if not source.is_dir() or source.is_symlink():
        return
    key = source / "master.key.file"
    if not _regular_file(key) or key.stat().st_size != 32:
        raise ValueError("Lark Host credential state has an invalid file master key")
    target = home / _LARK_LINUX_STORE
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    for item in target.iterdir():
        if item.is_file() and not item.is_symlink() and _ENCRYPTED_ITEM.fullmatch(item.name):
            item.unlink()
    shutil.copyfile(key, target / "master.key", follow_symlinks=False)
    os.chmod(target / "master.key", 0o600)
    for item in source.iterdir():
        if not _regular_file(item) or _ENCRYPTED_ITEM.fullmatch(item.name) is None:
            continue
        shutil.copyfile(item, target / item.name, follow_symlinks=False)
        os.chmod(target / item.name, 0o600)
