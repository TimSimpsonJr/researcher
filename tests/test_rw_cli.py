"""Tests for scripts/rw.py: each subcommand against a temp vault via rw.main()."""

import json
import os
import time
from pathlib import Path

import pytest

import rw
from config_manager import default_config


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    (v / ".researcher").mkdir(parents=True)
    (v / ".researcher" / "config.json").write_text(
        json.dumps(default_config(str(v))), encoding="utf-8"
    )
    (v / "Projects").mkdir()
    (v / "Projects" / "Alpha.md").write_text(
        "---\ntitle: Alpha\ntags: [research]\n---\nALPR cameras in Greenville.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(v)
    return v


@pytest.fixture
def cli(capsys):
    def _run(*argv):
        code = rw.main([str(a) for a in argv])
        cap = capsys.readouterr()
        out = json.loads(cap.out) if cap.out.strip() else None
        return code, out, cap.err
    return _run


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- vault, config, index ---------------------------------------------------

def test_locate_walks_up_from_subfolder(vault, cli, monkeypatch):
    monkeypatch.chdir(vault / "Projects")
    code, out, _ = cli("locate")
    root = vault.resolve()
    assert code == 0
    assert out == {
        "vault": str(root),
        "repo": str(rw.REPO_ROOT),
        "scripts": str(rw.REPO_ROOT / "scripts"),
        "state_dir": str(root / ".researcher" / "state"),
        "cases_dir": str(root / ".researcher" / "cases"),
    }


def test_locate_fails_outside_a_vault(tmp_path, cli, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, err = cli("locate")
    assert code == 1
    assert out is None
    assert "/researcher-setup" in err


def test_locate_honors_vault_flag(tmp_path, cli, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, _ = cli("locate", "--vault", tmp_path / "elsewhere")
    assert code == 0
    assert out["vault"] == str((tmp_path / "elsewhere").resolve())


def test_find_vault_accepts_pre_rename_dir(tmp_path):
    from config_manager import find_vault
    old = tmp_path / ".research-workflow"
    old.mkdir()
    (old / "config.json").write_text("{}", encoding="utf-8")
    assert find_vault(tmp_path / "sub" / "dir") == tmp_path
    sibling = tmp_path.parent / (tmp_path.name + "-sibling")
    sibling.mkdir()
    assert find_vault(sibling) is None


def test_migrate_renames_old_dir_keeps_stdout_json(tmp_path, cli, monkeypatch):
    old = tmp_path / ".research-workflow"
    old.mkdir()
    (old / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out, err = cli("migrate")
    assert code == 0
    assert out == "migrated"
    assert (tmp_path / ".researcher" / "config.json").is_file()
    assert "Migrated" in err


def test_load_config_prints_config_or_null(vault, cli, tmp_path):
    code, out, _ = cli("load-config")
    assert code == 0 and out["vault_root"] == str(vault)
    code, out, _ = cli("load-config", "--vault", tmp_path / "no-config-here")
    assert code == 0 and out is None


def test_save_config_writes_vault_config(tmp_path, cli):
    new_vault = tmp_path / "new"
    cfg = default_config("C:/vaults/new")
    src = _write(tmp_path / "setup_config.json", cfg)
    code, out, _ = cli("save-config", "--vault", new_vault, "--config-file", src)
    assert code == 0 and out == cfg
    saved = json.loads((new_vault / ".researcher" / "config.json").read_text(encoding="utf-8"))
    assert saved == cfg


def test_update_and_build_index(vault, cli):
    code, out, _ = cli("update-index")
    assert code == 0 and out == {"added": 1, "updated": 0, "removed": 0}
    code, out, _ = cli("build-index")
    assert code == 0 and out["notes_indexed"] == 1
    assert Path(out["db_path"]).is_file()


def test_vault_search_and_list_notes(vault, cli):
    cli("update-index")
    code, out, _ = cli("vault-search", "--query", "Alpha", "--limit", "5")
    assert code == 0 and [r["path"] for r in out] == ["Projects/Alpha.md"]
    assert cli("list-notes", "--folder", "Projects")[1] == ["Projects/Alpha.md"]
    assert cli("list-notes", "--folder", "Other")[1] == []
    assert cli("list-notes")[1] == ["Projects/Alpha.md"]
    assert cli("list-notes", "--folder", "Proj")[1] == []  # prefix collision
    (vault / "Projects" / "Sub").mkdir()
    (vault / "Projects" / "Sub" / "B.md").write_text("---\ntitle: B\n---\nb\n", encoding="utf-8")
    cli("update-index")
    assert cli("list-notes", "--folder", "Projects/Sub")[1] == ["Projects/Sub/B.md"]
    assert cli("list-notes", "--folder", "Projects\\Sub")[1] == ["Projects/Sub/B.md"]
