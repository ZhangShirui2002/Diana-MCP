import pytest

from diana_mcp import config


def test_parse_version():
    assert config.parse_version("Diana 10.5") == "10.5"
    assert config.parse_version("DIANA10.10") == "10.10"
    assert config.parse_version("diana-latest") == "unknown"


def test_env_override_comes_first(tmp_path, monkeypatch):
    home = tmp_path / "Diana 9.9"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "DianaIE.exe").write_bytes(b"")
    monkeypatch.setenv("DIANA_MCP_DIANA_HOME", str(home))

    install = config.find_installs()[0]
    assert install.home == home
    assert install.version == "9.9"
    assert install.dianaie == home / "bin" / "DianaIE.exe"


def test_env_override_without_dianaie_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DIANA_MCP_DIANA_HOME", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        config.find_installs()


def test_state_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("DIANA_MCP_STATE_DIR", str(tmp_path))
    assert config.sessions_dir() == tmp_path / "sessions"
    assert config.api_dir() == tmp_path / "api"
