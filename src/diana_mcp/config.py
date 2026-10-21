"""Locate DIANA installations and the diana-mcp state directory."""
from __future__ import annotations

import os
import re
import shutil
import string
from dataclasses import dataclass
from pathlib import Path

BRIDGE_SCRIPT = Path(__file__).resolve().parent / "bridge" / "diana_bridge.py"
REVIEW_SCRIPT = Path(__file__).resolve().parent / "bridge" / "diana_review.py"
# The source checkout (…/src/diana_mcp/config.py -> project root) when installed in editable mode.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# DIANA is a Windows program. On Windows we run it directly; elsewhere we run the same .exe through Wine, which
# means every path handed to DIANA has to be spelled the way Wine shows it (/work/x -> W:\x) and back.
IS_WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class DianaInstall:
    home: Path
    version: str

    @property
    def dianaie(self) -> Path:
        return self.home / "bin" / "DianaIE.exe"

    @property
    def solver(self) -> Path:
        return self.home / "bin" / "diana.exe"

    @property
    def python(self) -> Path:
        """The Python interpreter bundled with DIANA (same version as the one embedded in DianaIE)."""
        return self.home / "python" / "python.exe"


def parse_version(folder_name: str) -> str:
    match = re.search(r"(\d+(?:\.\d+)+)", folder_name)
    return match.group(1) if match else "unknown"


def _version_key(install: DianaInstall) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", install.version))


def _existing_drives() -> list[str]:
    if os.name != "nt":
        return []
    import ctypes

    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [f"{letter}:\\" for i, letter in enumerate(string.ascii_uppercase) if mask & (1 << i)]


def wine_prefix() -> Path:
    """The Wine prefix DIANA is installed in (WINEPREFIX, else ~/.wine). Only meaningful off Windows."""
    return Path(os.environ.get("WINEPREFIX") or Path.home() / ".wine")


def wine_user() -> str:
    """The Windows user name inside the prefix; Wine names it after the Unix account."""
    return os.environ.get("USER") or os.environ.get("LOGNAME") or Path.home().name


def _drive_map() -> list[tuple[str, Path]]:
    """Wine drive letters and the host directories behind them, most specific first ('/' sorts last)."""
    drives: list[tuple[str, Path]] = []
    for entry in sorted((wine_prefix() / "dosdevices").glob("?:")):
        try:
            target = entry.resolve(strict=True)
        except OSError:
            continue
        if target.is_dir():
            drives.append((entry.name[0].upper(), target))
    drives.sort(key=lambda drive: len(drive[1].parts), reverse=True)
    return drives


def to_windows(path: Path | str) -> str:
    """A host path spelled the way DIANA sees it: '/work/a.dpf' -> 'W:\\a.dpf'. Identity on Windows."""
    if IS_WINDOWS:
        return str(path)
    resolved = Path(path).resolve()
    for letter, target in _drive_map():
        if resolved == target or target in resolved.parents:
            rest = resolved.relative_to(target).as_posix().replace("/", "\\")
            return f"{letter}:\\{rest}" if rest else f"{letter}:\\"
    raise FileNotFoundError(
        f"{path} is outside every Wine drive of {wine_prefix()}; DIANA cannot reach it. "
        "Keep models under a mapped directory (e.g. /work)."
    )


def to_host(path: Path | str) -> Path:
    """A path DIANA reported, back on the host: 'W:\\a.dpf' -> '/work/a.dpf'. Identity on Windows."""
    if IS_WINDOWS:
        return Path(path)
    text = str(path).replace("\\", "/")
    match = re.match(r"^([A-Za-z]):(?:/(.*))?$", text)
    if not match:
        return Path(text)
    for letter, target in _drive_map():
        if letter == match.group(1).upper():
            return target / (match.group(2) or "")
    return Path(text)


def launch_argv(executable: Path | str, *arguments: str) -> list[str]:
    """Command line that runs a program: a Windows .exe goes through Wine off Windows, anything else (a native
    solver, a test stub, an interpreter) runs directly."""
    if IS_WINDOWS or Path(executable).suffix.lower() != ".exe":
        return [str(executable), *arguments]
    return [os.environ.get("DIANA_MCP_WINE", "wine"), str(executable), *arguments]


def batch_argv(executable: Path | str, *arguments: str) -> list[str]:
    """Like :func:`launch_argv`, for short non-interactive DianaIE runs (API dump, snapshots, verification).

    Off Windows they get their own virtual display when Xvfb is available, so they neither need the user to be
    logged in nor throw windows onto the screen they are working on.
    """
    argv = launch_argv(executable, *arguments)
    if IS_WINDOWS or argv[0] == str(executable) or shutil.which("xvfb-run") is None:
        return argv
    return ["xvfb-run", "-a", "-s", "-screen 0 1600x1000x24", *argv]


def launch_env(**extra: str) -> dict[str, str]:
    """Environment for such a launch: on Linux the Wine prefix plus the X display DIANA's windows go to."""
    env = dict(os.environ)
    if not IS_WINDOWS:
        env.setdefault("WINEPREFIX", str(wine_prefix()))
        env.setdefault("WINEDEBUG", "-all")
        env.setdefault("DISPLAY", ":0")
        env.setdefault("XAUTHORITY", str(Path.home() / ".Xauthority"))
    env.update(extra)
    return env


def _candidate_homes() -> list[Path]:
    homes: list[Path] = []
    roots = _existing_drives() if IS_WINDOWS else [str(wine_prefix() / "drive_c")]
    for root in roots:
        for parent in (Path(root) / "Program Files", Path(root) / "Program Files (x86)", Path(root)):
            try:
                homes.extend(p for p in parent.iterdir() if p.is_dir() and p.name.lower().startswith("diana"))
            except OSError:
                continue
    return homes


def find_installs() -> list[DianaInstall]:
    """All DIANA installations with a DianaIE executable; DIANA_MCP_DIANA_HOME first, then newest first."""
    installs: dict[Path, DianaInstall] = {}
    for home in _candidate_homes():
        if (home / "bin" / "DianaIE.exe").is_file():
            installs.setdefault(home.resolve(), DianaInstall(home, parse_version(home.name)))
    found = sorted(installs.values(), key=_version_key, reverse=True)

    override = os.environ.get("DIANA_MCP_DIANA_HOME")
    if override:
        home = Path(override)
        if not (home / "bin" / "DianaIE.exe").is_file():
            raise FileNotFoundError(f"DIANA_MCP_DIANA_HOME={override} has no bin\\DianaIE.exe")
        found = [DianaInstall(home, parse_version(home.name))] + [i for i in found if i.home.resolve() != home.resolve()]
    return found


def default_install() -> DianaInstall:
    installs = find_installs()
    if not installs:
        raise FileNotFoundError(
            "No DIANA installation found. Set DIANA_MCP_DIANA_HOME to the folder that contains bin\\DianaIE.exe."
        )
    return installs[0]


def state_dir() -> Path:
    """Sessions, the API dump and screenshots. Off Windows this sits inside the Wine prefix on purpose: the
    bridge is a Windows process and writes its session files to %LOCALAPPDATA%, which is that directory."""
    override = os.environ.get("DIANA_MCP_STATE_DIR")
    if override:
        return Path(override)
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "diana-mcp"
    return wine_prefix() / "drive_c" / "users" / wine_user() / "AppData" / "Local" / "diana-mcp"


def sessions_dir() -> Path:
    """Must match ``default_session_dir`` in bridge/diana_bridge.py for manually started bridges."""
    return state_dir() / "sessions"


def api_dir() -> Path:
    return state_dir() / "api"


def screenshots_dir() -> Path:
    return state_dir() / "screenshots"


def work_dir() -> Path:
    override = os.environ.get("DIANA_MCP_WORKDIR")
    return Path(override) if override else state_dir() / "work"


def models_dir() -> Path:
    """Embedding models, their download caches and the vector indexes (gigabytes): DIANA_MCP_MODELS_DIR, else
    <project>/models in a source checkout, else the state directory."""
    override = os.environ.get("DIANA_MCP_MODELS_DIR")
    if override:
        return Path(override)
    if (PROJECT_ROOT / "pyproject.toml").is_file():
        return PROJECT_ROOT / "models"
    return state_dir() / "models"


def knowledge_dir() -> Path:
    """The experience handbook (its own git repository): DIANA_MCP_KNOWLEDGE_DIR, else <project>/knowledge in a
    source checkout, else the state directory."""
    override = os.environ.get("DIANA_MCP_KNOWLEDGE_DIR")
    if override:
        return Path(override)
    if (PROJECT_ROOT / "pyproject.toml").is_file():
        return PROJECT_ROOT / "knowledge"
    return state_dir() / "knowledge"
