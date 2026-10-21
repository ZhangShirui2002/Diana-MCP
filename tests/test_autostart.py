import runpy
import subprocess
import sys

import pytest

from diana_mcp import autostart, config


@pytest.fixture
def install(tmp_path):
    home = tmp_path / "Diana 10.5"
    (home / "bin").mkdir(parents=True)
    for dll in ("python37.dll", "python37_d.dll", "python3.dll"):
        (home / "bin" / dll).write_bytes(b"")
    return config.DianaInstall(home, "10.5")


def test_user_site_follows_the_embedded_python(install, tmp_path, monkeypatch):
    assert autostart.python_tag(install) == "Python37"
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "base"))
    assert autostart.user_site(install) == tmp_path / "base" / "Python37" / "site-packages"


def test_user_site_uses_the_wine_windows_profile_off_windows(install, tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONUSERBASE", raising=False)
    monkeypatch.setattr(config, "IS_WINDOWS", False)
    monkeypatch.setattr(config, "wine_prefix", lambda: tmp_path / "prefix")
    monkeypatch.setattr(config, "wine_user", lambda: "zhang")
    assert autostart.user_site(install) == (tmp_path / "prefix" / "drive_c" / "users" / "zhang"
                                             / "AppData" / "Roaming" / "Python" / "Python37" / "site-packages")


def test_enable_writes_paths_for_dianaies_windows_python(install, tmp_path, monkeypatch):
    base = tmp_path / "userbase"
    monkeypatch.setattr(config, "to_windows", lambda path: "W:\\" + str(path).replace("/", "\\").lstrip("\\"))
    info = autostart.enable(install, base, session_dir=tmp_path / "sessions")
    module = autostart.user_site(install, base) / "diana_mcp_autostart.py"
    text = module.read_text(encoding="utf-8")
    assert info["points_to_this_install"]
    assert "BRIDGE = 'W:\\\\" in text
    assert "SESSION_DIR = 'W:\\\\" in text


def test_enable_status_disable(install, tmp_path):
    base = tmp_path / "userbase"
    assert autostart.status(install, base)["enabled"] is False
    info = autostart.enable(install, base, session_dir=tmp_path / "sessions")
    assert info["enabled"] and info["points_to_this_install"] and info["generated_by_diana_mcp"]
    site = autostart.user_site(install, base)
    assert (site / "diana_mcp_autostart.pth").read_text(encoding="utf-8") == "import diana_mcp_autostart\n"
    assert autostart.disable(install, base)["enabled"] is False
    assert not any(site.iterdir())


def test_foreign_files_are_left_alone(install, tmp_path):
    base = tmp_path / "userbase"
    site = autostart.user_site(install, base)
    site.mkdir(parents=True)
    (site / "diana_mcp_autostart.py").write_text("# someone else's module\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        autostart.enable(install, base)
    with pytest.raises(FileExistsError):
        autostart.disable(install, base)


def test_generated_module_does_nothing_outside_dianaie(install, tmp_path):
    base = tmp_path / "userbase"
    autostart.enable(install, base, session_dir=tmp_path / "sessions")
    module = autostart.user_site(install, base) / "diana_mcp_autostart.py"
    namespace = runpy.run_path(str(module))
    assert namespace["_STATE"] == {}  # no timer outside DianaIE.exe


def test_generated_module_is_python37_syntax(install, tmp_path):
    python = None
    try:
        python = config.default_install().python
    except FileNotFoundError:
        pass
    if python is None or not python.is_file():
        pytest.skip("DIANA's bundled Python is not installed")
    if not config.IS_WINDOWS:
        pytest.skip("off Windows this interpreter runs under Wine and needs Windows paths; checked on Windows")
    base = tmp_path / "userbase"
    autostart.enable(install, base, session_dir=tmp_path / "sessions")
    module = autostart.user_site(install, base) / "diana_mcp_autostart.py"
    result = subprocess.run([str(python), "-c", f"import ast; ast.parse(open(r'{module}', encoding='utf-8').read())"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
