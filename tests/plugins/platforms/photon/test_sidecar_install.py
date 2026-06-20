"""Tests for Photon sidecar install verification.

The sidecar pin is exact because ``spectrum-ts`` ships breaking majors. After
``npm ci`` / ``npm install`` completes, Hermes should verify that
``node_modules`` really matches the committed pin instead of silently
continuing with a drifted tree that will break outbound iMessage sends.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.platforms.photon import cli


def _write_sidecar_tree(
    root: Path,
    *,
    expected: str = "3.1.0",
    lock_spec: str = "3.1.0",
    lock_version: str = "3.1.0",
    installed: str = "3.1.0",
) -> Path:
    sidecar_dir = root / "sidecar"
    (sidecar_dir / "node_modules" / "spectrum-ts").mkdir(parents=True)
    (sidecar_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "@hermes-agent/photon-sidecar",
                "private": True,
                "dependencies": {"spectrum-ts": expected},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (sidecar_dir / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "@hermes-agent/photon-sidecar",
                "lockfileVersion": 3,
                "packages": {
                    "": {"dependencies": {"spectrum-ts": lock_spec}},
                    "node_modules/spectrum-ts": {"version": lock_version},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (sidecar_dir / "node_modules" / "spectrum-ts" / "package.json").write_text(
        json.dumps({"name": "spectrum-ts", "version": installed}, indent=2) + "\n",
        encoding="utf-8",
    )
    return sidecar_dir


def test_spectrum_drift_messages_detect_installed_mismatch(tmp_path: Path) -> None:
    sidecar_dir = _write_sidecar_tree(tmp_path, installed="2.0.0")

    issues = cli._sidecar_spectrum_drift_messages(sidecar_dir)

    assert any("installed spectrum-ts@2.0.0" in issue for issue in issues)


def test_install_sidecar_fails_when_verification_detects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    sidecar_dir = _write_sidecar_tree(tmp_path, installed="2.0.0")
    monkeypatch.setattr(cli, "_SIDECAR_DIR", sidecar_dir)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/npm")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0),
    )

    rc = cli._install_sidecar()

    assert rc == 1
    err = capsys.readouterr().err
    assert "sidecar install verification failed" in err
    assert "installed spectrum-ts@2.0.0" in err


def test_install_sidecar_succeeds_when_versions_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    sidecar_dir = _write_sidecar_tree(tmp_path)
    monkeypatch.setattr(cli, "_SIDECAR_DIR", sidecar_dir)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/npm")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0),
    )

    rc = cli._install_sidecar()

    assert rc == 0
    out = capsys.readouterr().out
    assert "sidecar deps verified" in out
