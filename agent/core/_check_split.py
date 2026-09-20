"""Sanity check: the shareable half must leak nothing machine- or user-specific."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.schema import (Action, Locator, LocatorType, LocalState, Macro, Step,
                         TargetApp, Trust, Verify)

m = Macro(
    name="colab-run-notebook",
    description="Upload a script to Google Colab, pick a GPU runtime, and run all cells.",
    steps=[Step(
        action=Action.CLICK,
        intent="Open the file upload dialog",
        locators=[
            Locator(LocatorType.WEB_ROLE, {"role": "button", "name": "Upload"}),
            Locator(LocatorType.WEB_TESTID, {"value": "upload-btn"}),
        ],
        verify=Verify(url_regex=r"colab\.research\.google\.com"),
    )],
    requires_capabilities=["chrome"],
    requires_symbols=["DOWNLOADS"],
    requires_secrets=["secret://colab-google"],
    permissions=["input", "net", "files:{DOWNLOADS}"],
    target_apps=[TargetApp(name="colab.research.google.com", kind="website")],
    author="ady",
)

local = LocalState(macro_id=m.id, trust=Trust.SUPERVISED, origin="local")
local.record(0, 0, True)
local.record(0, 0, True)
local.record(0, 1, False)

blob = json.dumps(m.to_dict())

print("shareable macro.json fields:")
print("  " + ", ".join(sorted(m.to_dict().keys())))
print()
print("leak check — none of these may appear in the shareable half:")
FORBIDDEN = ["C:/Users", "C:\\\\Users", "tries", "hits", "fingerprint",
             "trust", "artifact_dir", "machine", "adaptation"]
ok = True
for bad in FORBIDDEN:
    present = bad in blob
    ok &= not present
    print(f"  {bad:<16} {'PRESENT  !!' if present else 'absent   ok'}")

print()
print(f"locator ranking (local only): loc0={local.rank(0,0):.2f}  loc1={local.rank(0,1):.2f}")
print(f"  -> stack prefers {'loc0' if local.rank(0,0) > local.rank(0,1) else 'loc1'}")
print()
print(f"declared blast radius for a downloader: {m.permissions}")
print(f"requires on target machine: {m.requires_symbols + m.requires_capabilities}")
print()
print("RESULT:", "shareable half is clean" if ok else "LEAK DETECTED")
sys.exit(0 if ok else 1)
