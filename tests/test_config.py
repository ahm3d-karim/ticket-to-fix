import pytest

from fde.config import ConfigError, load_repo_manifest, load_user_config


def test_defaults_when_no_file(tmp_path):
    cfg = load_user_config(tmp_path / "missing.yaml")
    assert cfg["agent_backend"] == "codex"
    assert cfg["deploy"] == {"prod_branch": "prod", "port": 8124, "preview_port": 8123}


def test_overrides_honored(tmp_path):
    p = tmp_path / "fde.yaml"
    p.write_text(
        "agent_backend: claude\ndeploy:\n  port: 9999\n", encoding="utf-8"
    )
    cfg = load_user_config(p)
    assert cfg["agent_backend"] == "claude"
    assert cfg["deploy"]["port"] == 9999
    # unspecified defaults survive the merge
    assert cfg["deploy"]["prod_branch"] == "prod"
    assert cfg["deploy"]["preview_port"] == 8123


def test_repo_manifest_missing_field_error(tmp_path):
    (tmp_path / "fde.yaml").write_text(
        "install_cmd: \"\"\ntest_cmd: node --test\nrun_cmd: node app.js\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as ei:
        load_repo_manifest(tmp_path)
    assert "app_type" in str(ei.value)


def test_repo_manifest_missing_file_error(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_repo_manifest(tmp_path)
    assert "fde.yaml" in str(ei.value)


def test_repo_manifest_valid(tmp_path):
    (tmp_path / "fde.yaml").write_text(
        "install_cmd: \"\"\ntest_cmd: node --test\nrun_cmd: node app.js\napp_type: js\n",
        encoding="utf-8",
    )
    m = load_repo_manifest(tmp_path)
    assert m["test_cmd"] == "node --test"
    assert m["app_type"] == "js"


def test_repo_manifest_bad_app_type(tmp_path):
    (tmp_path / "fde.yaml").write_text(
        "install_cmd: \"\"\ntest_cmd: node --test\nrun_cmd: node app.js\napp_type: rust\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_repo_manifest(tmp_path)
