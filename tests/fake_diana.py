"""Stand-in for diana.exe in tests: ``fake_diana.py [-n N] <name> <dat> <dcf> <ff>``.

The .dcf content selects the behaviour: FAIL writes an aborted log and exits 1, SLOW waits (for cancel
tests); otherwise the real log of the simply supported beam run is written.
"""
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if args[:1] == ["-n"]:
    args = args[2:]
name, dat, dcf, ff = args
command = Path(dcf).read_text(encoding="utf-8", errors="replace")
Path(ff).write_bytes(b"\0" * 4096)
out = Path(f"{name}.out")
if "SLOW" in command:
    out.write_text(" /DIANA/AP/LS41    16:34:00      1.28-CPU     0.84-IO      120-FA   BEGIN\n", encoding="utf-8")
    time.sleep(120)
if "FAIL" in command:
    out.write_text(" /DIANA/AP/LS41    16:34:00      1.28-CPU     0.84-IO      120-FA   BEGIN\n", encoding="utf-8")
    print("diana: segment aborted", file=sys.stderr)
    sys.exit(1)
if "DIVERGE" in command:  # the real log of a nonlinear run that diverged (DIANA exits 0 anyway)
    out.write_text((Path(__file__).parent / "data" / "nonlin_beam_aborted.out").read_text(encoding="utf-8"),
                   encoding="utf-8")
    sys.exit(0)
out.write_text((Path(__file__).parent / "data" / "linsta_beam.out").read_text(encoding="utf-8"), encoding="utf-8")
Path(f"{name}.tb").write_text((Path(__file__).parent / "data" / "linsta_beam_small.tb").read_text(encoding="utf-8"), encoding="utf-8")
Path(f"{name}.dnb").write_bytes(b"DNB")
