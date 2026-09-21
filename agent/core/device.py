"""Which processor the vision tier runs on, and whether it is worth using.

The project already refuses to assume things about the machine it is on —
`machine_profile.py` exists so a job recorded on one PC runs on another. GPU
vendor is the same class of fact and was the one nobody checked, which cost
more than any of the others: the vision tier benchmarked at **84.24s** for a
pipeline whose own authors quote 0.6s on an A100, because `pip install torch`
on Windows quietly installs a CPU-only wheel and `ui_parser.py` asked
`torch.cuda.is_available()`, got False, and carried on.

**CUDA and ROCm are the same call.** AMD's HIP layer mirrors the CUDA API
deliberately, so a ROCm build of PyTorch answers `torch.cuda.is_available()`
with True and accepts `.to("cuda")` on Radeon hardware. That is why the fix
for this machine is an install, not an edit — and why "cuda" here means "the
accelerated path", not "an NVIDIA card".

So this module reports three things that are usually conflated:

  `pick()`        which torch device string to use
  `backend()`     what is actually underneath — cuda, rocm, directml, cpu
  `advice()`      if it is cpu, what to install to change that

The distinction matters when reading a benchmark. `torch.cuda.is_available()`
being True tells you the call will work; it does not tell you whether you are
on an A100 or an integrated Radeon, and those differ by two orders of
magnitude. A result that does not record `describe()` is not reproducible.

Nothing here raises. A device probe that takes down a task would be worse
than the slow path it was trying to avoid.
"""

from __future__ import annotations

import ctypes
import functools
import sys

#: Vendor substrings as they appear in the adapter name Windows reports.
_VENDORS = (("nvidia", "nvidia"), ("geforce", "nvidia"), ("quadro", "nvidia"),
            ("rtx", "nvidia"), ("tesla", "nvidia"),
            ("radeon", "amd"), ("amd ", "amd"), ("firepro", "amd"),
            ("intel", "intel"), ("arc ", "intel"))


@functools.lru_cache(maxsize=1)
def adapters() -> list[str]:
    """Display adapter names, from Win32 directly.

    `EnumDisplayDevices` rather than a WMI query through PowerShell: it is a
    single syscall instead of a subprocess, it answers in microseconds, and
    it cannot flash a console window on a frozen build — which is a real
    failure mode here, see `noconsole.py`.
    """
    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong),
                    ("DeviceName", ctypes.c_wchar * 32),
                    ("DeviceString", ctypes.c_wchar * 128),
                    ("StateFlags", ctypes.c_ulong),
                    ("DeviceID", ctypes.c_wchar * 128),
                    ("DeviceKey", ctypes.c_wchar * 128)]

    out: list[str] = []
    if not sys.platform.startswith("win"):
        return out
    try:
        user32 = ctypes.windll.user32
        i = 0
        while True:
            d = DISPLAY_DEVICEW()
            d.cb = ctypes.sizeof(DISPLAY_DEVICEW)
            if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
                break
            name = (d.DeviceString or "").strip()
            if name and name not in out:
                out.append(name)
            i += 1
    except Exception:
        return out
    return out


@functools.lru_cache(maxsize=1)
def vendor() -> str:
    """"nvidia", "amd", "intel", or "" — from the hardware, not from torch.

    Deliberately independent of whether torch can *use* the card. That
    separation is the whole point: knowing there is a Radeon present while
    torch reports no GPU is exactly the diagnosis that was missing.
    """
    for name in adapters():
        low = name.lower()
        for needle, who in _VENDORS:
            if needle in low:
                return who
    return ""


@functools.lru_cache(maxsize=1)
def _probe() -> tuple[str, str, str]:
    """(device string, backend, detail). Resolved once; torch imported lazily.

    Lazy because importing torch costs seconds and pulls hundreds of MB into
    memory. Anything that only wants `vendor()` — the installer, a log line —
    must not pay that.
    """
    try:
        import torch
    except Exception as e:
        return "cpu", "cpu", f"torch not importable: {type(e).__name__}"

    build = getattr(torch, "__version__", "?")
    hip = getattr(torch.version, "hip", None)
    cuda = getattr(torch.version, "cuda", None)

    try:
        if torch.cuda.is_available():
            try:
                name = torch.cuda.get_device_name(0)
            except Exception:
                name = "unknown device"
            # A ROCm wheel answers the cuda API. Report which one it really
            # is, or a benchmark row claiming "cuda" is uninterpretable.
            if hip:
                return "cuda", "rocm", f"{name} via ROCm {hip} (torch {build})"
            return "cuda", "cuda", f"{name} via CUDA {cuda} (torch {build})"
    except Exception:
        pass

    # DirectML is the other way onto an AMD or Intel GPU on Windows, and the
    # only one that does not need a vendor-specific torch build.
    try:
        import torch_directml
        if torch_directml.is_available():
            try:
                name = torch_directml.device_name(0)
            except Exception:
                name = "DirectML device"
            return "directml", "directml", f"{name} via DirectML (torch {build})"
    except Exception:
        pass

    seen = ", ".join(adapters()) or "no adapter reported"
    return "cpu", "cpu", f"CPU only (torch {build}; display: {seen})"


def pick() -> str:
    """The torch device string to use: "cuda", "directml" or "cpu"."""
    return _probe()[0]


def backend() -> str:
    """What is actually underneath: "cuda", "rocm", "directml" or "cpu"."""
    return _probe()[1]


def is_accelerated() -> bool:
    """True when anything other than the CPU will run the work.

    This is the flag to hand libraries that take a plain `gpu=` boolean, such
    as `easyocr.Reader(..., gpu=...)`.
    """
    return _probe()[0] != "cpu"


def torch_device():
    """A real torch device object, or None to leave a model where it is."""
    d = pick()
    if d == "cpu":
        return None
    if d == "directml":
        try:
            import torch_directml
            return torch_directml.device()
        except Exception:
            return None
    try:
        import torch
        return torch.device("cuda")
    except Exception:
        return None


def describe() -> str:
    """One line naming the hardware and the stack reaching it.

    Log this next to any timing. "84.2s" means nothing on its own; "84.2s,
    CPU only (torch 2.13.0+cpu; display: AMD Radeon 8060S Graphics)" is a
    diagnosis.
    """
    return _probe()[2]


def advice() -> str:
    """What to install to stop using the CPU, or "" if already accelerated.

    Vendor-specific, because the answer genuinely differs and the default
    `pip install torch` is wrong for everyone with a GPU.
    """
    if is_accelerated():
        return ""
    v = vendor()
    if v == "nvidia":
        return ("An NVIDIA GPU is present but torch is a CPU build. Install a "
                "CUDA wheel:\n"
                "  pip install torch --index-url "
                "https://download.pytorch.org/whl/cu128")
    if v == "amd":
        return ("An AMD GPU is present but torch is a CPU build. On Windows "
                "this needs a ROCm wheel (ROCm 7.2.1+ supports Radeon and "
                "Ryzen AI on Windows; gfx1151 is the Radeon 8060S). See "
                "rocm.docs.amd.com/projects/radeon-ryzen. DirectML is the "
                "fallback that needs no vendor build:\n"
                "  pip install torch-directml\n"
                "Install either into a SEPARATE venv first — swapping torch "
                "in the working one risks a test suite that currently passes.")
    if v == "intel":
        return ("An Intel GPU is present but torch is a CPU build. DirectML "
                "is the simplest route:\n  pip install torch-directml")
    return ("No usable GPU detected. The vision tier will run on the CPU, "
            "which measured 84s per pass against 0.52s for UI Automation — "
            "prefer the structured tiers wherever they work.")


def report() -> str:
    """Everything, for a log line or a benchmark header."""
    lines = [f"device : {pick()}  (backend {backend()})",
             f"detail : {describe()}",
             f"vendor : {vendor() or 'unknown'}"]
    tip = advice()
    if tip:
        lines.append("")
        lines.append(tip)
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
