"""Find out why the vision tier is on the CPU, and what to install instead.

    python gpu_setup.py                 diagnose, print the command
    python gpu_setup.py --install       build .venv-gpu and install into it
    python gpu_setup.py --install --directml     force the DirectML route
    python gpu_setup.py --venv NAME     somewhere other than .venv-gpu

`pip install torch` on Windows gives a CPU-only wheel. That is the correct
default — it is the only build that works everywhere — but it means anyone
who does not know to pass `--index-url` ends up running vision models on the
CPU and never finds out. On this machine that was the whole difference
between 0.6s and 84.2s a pass.

**It never installs into the venv you are running.** A GPU wheel is a large,
vendor-specific, comparatively new binary, and ROCm on Windows is new enough
to still have open issues for this exact part. Swapping torch inside an
environment where 799 tests currently pass is a bad trade for a benchmark.
So this builds a sibling venv, and you compare the two.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import device   # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

#: The wheel index per vendor. Kept as data because these URLs move, and a
#: wrong one fails confusingly — pip installs the CPU build and says nothing.
WHEELS = {
    "nvidia": ("CUDA", ["--index-url", "https://download.pytorch.org/whl/cu128"]),
    "amd": ("ROCm", ["--index-url", "https://download.pytorch.org/whl/rocm6.2"]),
}

#: Everything the vision tier needs beyond torch itself.
VISION_PKGS = ["easyocr", "ultralytics", "timm", "einops",
               "transformers>=4.44.0,<4.49.0", "huggingface_hub"]


def run(cmd: list[str]) -> int:
    print("  $ " + " ".join(cmd), flush=True)
    return subprocess.call(cmd)


def install(venv: Path, directml: bool) -> int:
    v = device.vendor()
    if directml or v == "intel":
        label, extra, pkg = "DirectML", [], "torch-directml"
    elif v in WHEELS:
        label, extra = WHEELS[v]
        pkg = "torch"
    else:
        print(f"No GPU vendor recognised (adapters: {device.adapters()}). "
              f"Nothing to install.")
        return 1

    print(f"\nBuilding {venv} and installing the {label} build of torch.")
    print("This downloads a few GB and takes a while.\n")

    if not venv.exists():
        if run([sys.executable, "-m", "venv", str(venv)]) != 0:
            print("could not create the venv")
            return 1
    py = venv / "Scripts" / "python.exe"
    if not py.exists():
        py = venv / "bin" / "python"
    if not py.exists():
        print(f"no interpreter in {venv}")
        return 1

    if run([str(py), "-m", "pip", "install", "--upgrade", "pip"]) != 0:
        return 1
    if run([str(py), "-m", "pip", "install", pkg] + extra) != 0:
        print(f"\nThe {label} install failed. That is informative rather than "
              f"fatal — it usually means no wheel exists yet for this "
              f"architecture. The DirectML route needs no vendor build:\n"
              f"  python gpu_setup.py --install --directml")
        return 1
    if run([str(py), "-m", "pip", "install"] + VISION_PKGS) != 0:
        return 1

    print("\nInstalled. What the new environment sees:\n")
    run([str(py), str(HERE / "core" / "device.py")])
    print(f"\nCompare the two against the same ground truth:\n"
          f"  {py} {ROOT / 'bench' / 'detect_bench.py'}\n"
          f"Your existing .venv is untouched; nothing here changed it.")
    return 0


def main(argv: list[str]) -> int:
    print(device.report())

    if device.is_accelerated():
        print("\nAlready accelerated — nothing to do.")
        return 0

    venv = ROOT / ".venv-gpu"
    if "--venv" in argv:
        venv = ROOT / argv[argv.index("--venv") + 1]

    if "--install" not in argv:
        print(f"\nTo try it without touching your working environment:\n"
              f"  python gpu_setup.py --install\n"
              f"which builds {venv.name} alongside .venv and installs there.")
        return 0

    return install(venv, directml="--directml" in argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
