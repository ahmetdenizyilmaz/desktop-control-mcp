"""Cross-process lock manager for desktop controller.

Ensures only one MCP server process can control the desktop at a time.
The lock NEVER blocks the calling client — it only prevents OTHER Claude
instances (separate MCP server processes) from taking over.

Features:
- File-based lock for cross-process coordination
- Auto-detects caller identity (MCP client info or parent process)
- Lock auto-expires after 30s of inactivity (renewed on each tool call)
- ESC+ESC escape: user presses ESC twice to release lock + 30s cooldown
- Distinguishes physical vs synthetic keystrokes (ignores LLM-injected ESC)
- Overlay integration (shows/hides red border)
"""

import atexit
import ctypes
import ctypes.wintypes
import json
import os
import sys
import threading
import time

from overlay import DesktopOverlay

LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.lock.json')
LOCK_DURATION = 30       # seconds — lock auto-expires after this with no activity
COOLDOWN_DURATION = 30   # seconds — cooldown after ESC+ESC


class LockManager:
    def __init__(self):
        self._identity = None          # lazy — detected on first use
        self._overlay = DesktopOverlay()
        self._locked = False
        self._lock_file_lock = threading.Lock()  # protects file read/write
        self._esc_started = False
        self._expiry_started = False
        self._stdin_started = False
        atexit.register(self._cleanup)

    # ------------------------------------------------------------------
    # Lazy initialization — nothing heavy runs until first tool call
    # ------------------------------------------------------------------

    def _ensure_listeners(self):
        """Start background threads on first use, not on import."""
        if not self._esc_started:
            self._esc_started = True
            t = threading.Thread(target=self._esc_listener_thread, daemon=True)
            t.start()
        if not self._expiry_started:
            self._expiry_started = True
            t = threading.Thread(target=self._expiry_checker_thread, daemon=True)
            t.start()
        if not self._stdin_started:
            self._stdin_started = True
            t = threading.Thread(target=self._stdin_monitor_thread, daemon=True)
            t.start()

    # ------------------------------------------------------------------
    # Identity detection
    # ------------------------------------------------------------------

    def _detect_identity(self, mcp_ctx=None) -> str:
        """Detect who is calling us. Tries MCP client info, then parent process."""
        if self._identity:
            return self._identity

        # 1) Try MCP client info from context
        if mcp_ctx:
            try:
                client_info = mcp_ctx.session._client_params.clientInfo
                self._identity = f"{client_info.name}"
                return self._identity
            except Exception:
                pass

        # 2) Try parent process via psutil
        try:
            import psutil
            parent = psutil.Process(os.getppid())
            name = parent.name()
            cmdline = ' '.join(parent.cmdline()).lower()

            if 'claude' in cmdline:
                self._identity = "Claude Code"
            elif 'cursor' in cmdline:
                self._identity = "Cursor"
            elif 'windsurf' in cmdline:
                self._identity = "Windsurf"
            elif 'code' in cmdline and ('visual' in cmdline or 'vscode' in cmdline):
                self._identity = "VS Code"
            elif 'node' in name.lower():
                self._identity = f"Node App (PID {parent.pid})"
            elif 'python' in name.lower():
                self._identity = f"Python App (PID {parent.pid})"
            else:
                self._identity = f"{name} (PID {parent.pid})"
            return self._identity
        except Exception:
            pass

        # 3) Fallback
        self._identity = f"Process PID {os.getpid()}"
        return self._identity

    @property
    def identity(self) -> str:
        return self._detect_identity()

    # ------------------------------------------------------------------
    # Lock operations
    # ------------------------------------------------------------------

    def acquire(self, user_name: str = "", mcp_ctx=None) -> tuple:
        """Acquire the lock. Returns (success: bool, message: str).

        This process can ALWAYS re-acquire its own lock (never self-blocks).
        Only another live process with a non-expired lock blocks us.
        """
        self._ensure_listeners()
        owner = user_name or self._detect_identity(mcp_ctx)
        now = time.time()

        with self._lock_file_lock:
            lock_data = self._read_lock()

            if lock_data:
                # Check cooldown
                cd = lock_data.get('cooldown_until', 0)
                if cd > now:
                    remaining = int(cd - now)
                    return False, f"Lock in cooldown for {remaining}s (user pressed ESC+ESC)"

                # Check if ANOTHER live process holds a non-expired lock
                other_pid = lock_data.get('pid', 0)
                if other_pid and other_pid != os.getpid():
                    expires = lock_data.get('expires_at', 0)
                    if expires > now and self._is_process_alive(other_pid):
                        return False, f"Locked by: {lock_data['owner']}"
                # Otherwise: same PID, expired, or dead process — take over

            self._write_lock({
                'owner': owner,
                'pid': os.getpid(),
                'locked_at': now,
                'expires_at': now + LOCK_DURATION,
                'cooldown_until': 0,
            })

        self._locked = True
        self._overlay.start(owner)
        return True, f"Lock acquired: {owner}"

    def release(self) -> tuple:
        """Release the lock. Returns (success: bool, message: str).

        Always succeeds for the current process — also force-cleans stale locks.
        """
        with self._lock_file_lock:
            lock_data = self._read_lock()
            # Release if it's our lock OR if the lock is stale/orphaned
            if lock_data:
                lock_pid = lock_data.get('pid', 0)
                if lock_pid == os.getpid() or not self._is_process_alive(lock_pid):
                    self._delete_lock_file()
                    self._locked = False
                    self._overlay.hide()
                    return True, "Lock released"
            else:
                # No lock file at all — just clean up state
                self._locked = False
                self._overlay.hide()
                return True, "Lock released (was already clear)"

        self._locked = False
        self._overlay.hide()
        return True, "Lock released"

    def emergency_release(self):
        """Called on ESC+ESC. Release lock and set 30s cooldown."""
        with self._lock_file_lock:
            self._write_lock({
                'owner': '',
                'pid': 0,
                'locked_at': 0,
                'expires_at': 0,
                'cooldown_until': time.time() + COOLDOWN_DURATION,
            })
        self._locked = False
        self._overlay.hide()

    def ensure_locked(self, mcp_ctx=None) -> tuple:
        """Auto-acquire if not locked, renew if we hold it.

        Returns (ok: bool, message: str).
        This NEVER returns a BLOCKED error for the calling process itself.
        """
        self._ensure_listeners()
        now = time.time()

        with self._lock_file_lock:
            lock_data = self._read_lock()

            if lock_data:
                # Check cooldown first
                cd = lock_data.get('cooldown_until', 0)
                if cd > now:
                    self._locked = False
                    self._overlay.hide()
                    remaining = int(cd - now)
                    return False, f"Lock revoked — cooldown {remaining}s"

                lock_pid = lock_data.get('pid', 0)

                # If WE hold the lock, just renew
                if lock_pid == os.getpid():
                    lock_data['expires_at'] = now + LOCK_DURATION
                    self._write_lock(lock_data)
                    self._locked = True
                    return True, "OK"

                # If another process holds a live, non-expired lock, block
                if lock_pid and lock_pid != os.getpid():
                    expires = lock_data.get('expires_at', 0)
                    if expires > now and self._is_process_alive(lock_pid):
                        return False, f"Locked by: {lock_data['owner']}"
                    # Expired or dead — fall through to acquire

        # No valid lock exists — acquire
        return self.acquire(mcp_ctx=mcp_ctx)

    def get_status(self) -> dict:
        """Return current lock status."""
        with self._lock_file_lock:
            lock_data = self._read_lock()

        if not lock_data:
            return {'locked': False, 'owner': '', 'cooldown': False, 'cooldown_remaining': 0}

        now = time.time()
        cd = lock_data.get('cooldown_until', 0)
        expires = lock_data.get('expires_at', 0)
        is_locked = bool(lock_data.get('owner')) and expires > now
        return {
            'locked': is_locked,
            'owner': lock_data.get('owner', '') if is_locked else '',
            'pid': lock_data.get('pid', 0),
            'cooldown': cd > now,
            'cooldown_remaining': max(0, int(cd - now)),
        }

    # ------------------------------------------------------------------
    # Overlay helpers
    # ------------------------------------------------------------------

    def add_action(self, text: str):
        if self._locked:
            self._overlay.add_action(text)

    def hide_overlay(self):
        if self._locked:
            self._overlay.hide()

    def show_overlay(self):
        if self._locked:
            self._overlay.show()

    # ------------------------------------------------------------------
    # File lock I/O
    # ------------------------------------------------------------------

    def _read_lock(self) -> dict | None:
        try:
            with open(LOCK_FILE, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _write_lock(self, data: dict):
        try:
            with open(LOCK_FILE, 'w') as f:
                json.dump(data, f)
        except OSError:
            pass

    def _delete_lock_file(self):
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        if not pid:
            return False
        try:
            import psutil
            return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
        except Exception:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    # ------------------------------------------------------------------
    # Background: auto-expire lock after 30s of no activity
    # ------------------------------------------------------------------

    def _expiry_checker_thread(self):
        """Periodically check if our lock has expired and hide the overlay."""
        while True:
            time.sleep(3)
            if not self._locked:
                continue
            with self._lock_file_lock:
                lock_data = self._read_lock()
            if not lock_data:
                if self._locked:
                    self._locked = False
                    self._overlay.hide()
                continue
            if lock_data.get('pid') == os.getpid():
                expires = lock_data.get('expires_at', 0)
                if expires and time.time() > expires:
                    # Lock expired — clean up
                    with self._lock_file_lock:
                        self._delete_lock_file()
                    self._locked = False
                    self._overlay.hide()

    # ------------------------------------------------------------------
    # Background: parent process monitor — release lock when client dies
    # ------------------------------------------------------------------

    def _stdin_monitor_thread(self):
        """Watch if parent process (MCP client) is still alive. Release lock if it dies."""
        parent_pid = os.getppid()
        while True:
            time.sleep(2)
            if not self._is_process_alive(parent_pid):
                # Parent died — release our lock immediately
                if self._locked:
                    self.release()
                break

    # ------------------------------------------------------------------
    # ESC+ESC listener (low-level Windows keyboard hook)
    # ------------------------------------------------------------------

    def _esc_listener_thread(self):
        """Install a low-level keyboard hook that detects physical ESC+ESC.

        Uses LLKHF_INJECTED flag to ignore synthetic keystrokes sent by
        pyautogui/SendInput, so the LLM pressing ESC won't trigger release.
        """
        WH_KEYBOARD_LL = 13
        WM_KEYDOWN = 0x0100
        VK_ESCAPE = 0x1B
        LLKHF_INJECTED = 0x00000010

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ('vkCode', ctypes.wintypes.DWORD),
                ('scanCode', ctypes.wintypes.DWORD),
                ('flags', ctypes.wintypes.DWORD),
                ('time', ctypes.wintypes.DWORD),
                ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong)),
            ]

        last_esc = [0.0]

        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_int,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
        )

        def hook_proc(nCode, wParam, lParam):
            if nCode >= 0 and wParam == WM_KEYDOWN:
                kb = ctypes.cast(
                    lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)
                ).contents
                # Only physical key presses (not injected by SendInput)
                if kb.vkCode == VK_ESCAPE and not (kb.flags & LLKHF_INJECTED):
                    now = time.time()
                    if now - last_esc[0] < 0.5:
                        # Run async — hook callback MUST return fast or Windows kills it
                        threading.Thread(target=self.emergency_release, daemon=True).start()
                        last_esc[0] = 0.0
                    else:
                        last_esc[0] = now
            return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)

        # prevent garbage collection of the callback
        self._hook_proc_ref = HOOKPROC(hook_proc)

        hook = ctypes.windll.user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_proc_ref, None, 0
        )
        if not hook:
            return

        # Message pump (required for low-level hooks)
        msg = ctypes.wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(
            ctypes.byref(msg), None, 0, 0
        ) > 0:
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

        ctypes.windll.user32.UnhookWindowsHookEx(hook)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup(self):
        """Release lock on process exit — always attempts cleanup."""
        with self._lock_file_lock:
            lock_data = self._read_lock()
            if lock_data and lock_data.get('pid') == os.getpid():
                self._delete_lock_file()
        self._locked = False
        self._overlay.stop()
