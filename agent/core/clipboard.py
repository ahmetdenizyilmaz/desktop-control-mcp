"""The Windows clipboard, for text and images.

Why this exists: the reliable way to put a picture into a web app like
WhatsApp Web or Gmail is to place it on the clipboard and paste it — no native
file-open dialog to drive, no upload input to find. The agent screenshots a
window, this puts the image on the clipboard, and a Ctrl+V drops it into the
message box. Text works the same way, which is also the safe way to enter a
long or non-ASCII string that a per-key typer could mangle.

Everything is best-effort and returns a bool: a clipboard set that fails must
never raise into a running task. The clipboard is a shared, contended global
resource — another process can hold it open — so opens are retried briefly.
"""

from __future__ import annotations

import io
import time

#: Why the last operation failed, for a caller that must tell the user
#: something more useful than "it did not work". "Another program is holding
#: the clipboard" and "that file is not an image" need different responses.
_last_error = ""


def last_error() -> str:
    return _last_error


def _with_clipboard(fn):
    """Run fn() with the clipboard open, retrying the open (it is a global
    lock other apps grab for a few ms) and always closing it."""
    import win32clipboard

    last = None
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
        except Exception as e:                 # another process holds it
            last = e
            time.sleep(0.05)
            continue
        try:
            return fn(win32clipboard)
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
    if last:
        raise last
    return None


def set_text(text: str) -> bool:
    """Put text on the clipboard. Unicode, so Turkish survives intact."""
    global _last_error
    try:
        def _do(wc):
            wc.EmptyClipboard()
            wc.SetClipboardText(text or "", wc.CF_UNICODETEXT)
            return True
        _last_error = ""
        return bool(_with_clipboard(_do))
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e}"
        return False


def set_image(path: str) -> bool:
    """Put an image file on the clipboard as a device-independent bitmap.

    CF_DIB is a BMP without its 14-byte file header — that is exactly what a
    paste target expects. Any format Pillow can open (JPEG/PNG/…) is accepted;
    it is flattened onto white so a transparent PNG does not paste black.
    """
    global _last_error
    try:
        from PIL import Image

        img = Image.open(path)
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "BMP")
        dib = buf.getvalue()[14:]

        def _do(wc):
            wc.EmptyClipboard()
            wc.SetClipboardData(wc.CF_DIB, dib)
            return True
        # The decode happens above, BEFORE the clipboard is opened, so a file
        # that turns out not to be an image can never leave the user with an
        # emptied clipboard.
        _last_error = ""
        return bool(_with_clipboard(_do))
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e}"
        return False


def get_text() -> str:
    """Read text off the clipboard, or '' if there is none."""
    try:
        def _do(wc):
            if wc.IsClipboardFormatAvailable(wc.CF_UNICODETEXT):
                return wc.GetClipboardData(wc.CF_UNICODETEXT) or ""
            return ""
        return _with_clipboard(_do) or ""
    except Exception:
        return ""


def has_image() -> bool:
    """True if an image is currently on the clipboard — used to confirm a set."""
    try:
        def _do(wc):
            return bool(wc.IsClipboardFormatAvailable(wc.CF_DIB))
        return bool(_with_clipboard(_do))
    except Exception:
        return False
