"""Transparent red-border overlay for desktop controller lock indicator.

Shows:
- Red border around screen edges (like screen recording apps)
- Owner name in top-left (red text with dark outline)
- "ESC+ESC to release" hint below the name
- Action notifications that fade and disappear
- Fully click-through (input passes to windows below)
- Hidden during screenshots so it doesn't appear in captures
"""

import ctypes
import ctypes.wintypes
import queue
import threading
import tkinter as tk


class DesktopOverlay:
    BORDER_WIDTH = 3
    BG_COLOR = '#010101'       # will be set as transparent
    RED = '#FF0000'
    HINT_COLOR = '#CC3333'     # dimmer red for hint text
    DARK_BG = '#1A0000'        # dark strip behind text for readability
    FONT_NAME = ('Consolas', 13, 'bold')
    FONT_HINT = ('Consolas', 9)
    FONT_ACTION = ('Consolas', 10)
    FADE_COLORS = [
        '#FF0000', '#EE0000', '#CC0000', '#AA0000',
        '#880000', '#660000', '#440000', '#220000',
    ]
    FADE_INTERVAL_MS = 450
    MAX_ACTIONS = 6
    ACTIONS_Y_START = 42       # y offset where actions begin (below name + hint)

    def __init__(self):
        self._thread = None
        self._root = None
        self._canvas = None
        self._cmd_queue = queue.Queue()
        self._ready = threading.Event()
        self._name_id = None
        self._hint_id = None
        self._name_shadow_ids = []
        self._hint_shadow_ids = []
        self._bg_rect_id = None
        self._actions = []          # [(text_id, [shadow_ids])]
        self._screen_w = 0
        self._screen_h = 0
        self._hwnd = None

    # ------------------------------------------------------------------
    # Public API (thread-safe — can be called from any thread)
    # ------------------------------------------------------------------

    def start(self, owner_name: str):
        """Start the overlay window (or update owner if already running)."""
        if self._thread and self._thread.is_alive():
            self.set_owner(owner_name)
            self.show()
            return
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, args=(owner_name,), daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5)

    def show(self):
        """Make the overlay visible."""
        self._cmd_queue.put(self._show_impl)

    def hide(self):
        """Hide the overlay (synchronous — waits until actually hidden)."""
        if not self._root:
            return
        evt = threading.Event()
        self._cmd_queue.put(lambda: self._hide_impl(evt))
        evt.wait(timeout=1)

    def set_owner(self, name: str):
        """Update the displayed owner name."""
        self._cmd_queue.put(lambda n=name: self._set_owner_impl(n))

    def add_action(self, text: str):
        """Show an action notification that fades over time."""
        self._cmd_queue.put(lambda t=text: self._add_action_impl(t))

    def stop(self):
        """Destroy the overlay window."""
        self._cmd_queue.put(self._stop_impl)

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Tkinter thread internals
    # ------------------------------------------------------------------

    def _run(self, owner_name: str):
        self._root = tk.Tk()
        self._root.withdraw()

        self._screen_w = self._root.winfo_screenwidth()
        self._screen_h = self._root.winfo_screenheight()
        w, h = self._screen_w, self._screen_h

        self._root.overrideredirect(True)
        self._root.geometry(f'{w}x{h}+0+0')
        self._root.configure(bg=self.BG_COLOR)
        self._root.wm_attributes('-topmost', True)
        self._root.wm_attributes('-transparentcolor', self.BG_COLOR)

        self._canvas = tk.Canvas(
            self._root, width=w, height=h,
            bg=self.BG_COLOR, highlightthickness=0
        )
        self._canvas.pack()

        bw = self.BORDER_WIDTH
        # Red border — 4 filled rectangles
        self._canvas.create_rectangle(0, 0, w, bw, fill=self.RED, outline='')
        self._canvas.create_rectangle(0, h - bw, w, h, fill=self.RED, outline='')
        self._canvas.create_rectangle(0, 0, bw, h, fill=self.RED, outline='')
        self._canvas.create_rectangle(w - bw, 0, w, h, fill=self.RED, outline='')

        # Dark background strip behind text
        self._bg_rect_id = self._canvas.create_rectangle(
            bw, bw, 500, bw + self.ACTIONS_Y_START, fill=self.DARK_BG, outline=''
        )

        # Owner name with shadow for readability
        tx, ty = bw + 8, bw + 5
        self._name_shadow_ids = []
        for dx, dy in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
            sid = self._canvas.create_text(
                tx + dx, ty + dy, text=owner_name,
                fill='#000000', font=self.FONT_NAME, anchor='nw'
            )
            self._name_shadow_ids.append(sid)
        self._name_id = self._canvas.create_text(
            tx, ty, text=owner_name,
            fill=self.RED, font=self.FONT_NAME, anchor='nw'
        )

        # Hint: "ESC+ESC to release"
        hx, hy = bw + 8, bw + 24
        hint_text = "ESC+ESC to release"
        self._hint_shadow_ids = []
        for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            sid = self._canvas.create_text(
                hx + dx, hy + dy, text=hint_text,
                fill='#000000', font=self.FONT_HINT, anchor='nw'
            )
            self._hint_shadow_ids.append(sid)
        self._hint_id = self._canvas.create_text(
            hx, hy, text=hint_text,
            fill=self.HINT_COLOR, font=self.FONT_HINT, anchor='nw'
        )

        self._root.update_idletasks()
        self._make_click_through()
        # Show without stealing focus (SW_SHOWNOACTIVATE = 4)
        ctypes.windll.user32.ShowWindow(self._hwnd, 4)

        self._ready.set()
        self._poll_queue()
        self._root.mainloop()

    def _make_click_through(self):
        """Set Win32 extended styles so the window never steals focus or captures input."""
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020   # click-through for mouse
        WS_EX_TOOLWINDOW = 0x00000080    # no taskbar entry
        WS_EX_NOACTIVATE = 0x08000000    # NEVER steal keyboard focus

        self._hwnd = ctypes.windll.user32.GetParent(self._root.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            self._hwnd, GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        )

    def _poll_queue(self):
        """Process pending commands from other threads."""
        try:
            while True:
                cmd = self._cmd_queue.get_nowait()
                cmd()
        except queue.Empty:
            pass
        if self._root:
            try:
                self._root.after(50, self._poll_queue)
            except tk.TclError:
                pass

    # --- impl methods (run on tkinter thread) ---

    def _show_impl(self):
        if self._root and self._hwnd:
            # Show without stealing focus (SW_SHOWNOACTIVATE = 4)
            ctypes.windll.user32.ShowWindow(self._hwnd, 4)

    def _hide_impl(self, evt: threading.Event):
        if self._root:
            self._root.withdraw()
            self._root.update_idletasks()
        evt.set()

    def _stop_impl(self):
        if self._root:
            self._root.destroy()
            self._root = None

    def _set_owner_impl(self, name: str):
        if not self._canvas:
            return
        self._canvas.itemconfig(self._name_id, text=name)
        for sid in self._name_shadow_ids:
            self._canvas.itemconfig(sid, text=name)

    def _add_action_impl(self, text: str):
        if not self._canvas:
            return

        bw = self.BORDER_WIDTH
        y = bw + self.ACTIONS_Y_START + len(self._actions) * 18

        # Shadows for readability
        shadow_ids = []
        for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            sid = self._canvas.create_text(
                bw + 8 + dx, y + dy, text=text,
                fill='#000000', font=self.FONT_ACTION, anchor='nw'
            )
            shadow_ids.append(sid)

        text_id = self._canvas.create_text(
            bw + 8, y, text=text,
            fill=self.RED, font=self.FONT_ACTION, anchor='nw'
        )
        self._actions.append((text_id, shadow_ids))

        # Expand background strip
        total_h = bw + self.ACTIONS_Y_START + len(self._actions) * 18 + 5
        self._canvas.coords(self._bg_rect_id, bw, bw, 500, total_h)
        self._raise_text_items()

        # Start fading
        self._schedule_fade(text_id, shadow_ids, 0)

        # Trim excess
        while len(self._actions) > self.MAX_ACTIONS:
            old_tid, old_sids = self._actions.pop(0)
            self._canvas.delete(old_tid)
            for s in old_sids:
                self._canvas.delete(s)
            self._reposition_actions()

    def _raise_text_items(self):
        """Ensure text is rendered above the dark background strip."""
        for sid in self._name_shadow_ids:
            self._canvas.tag_raise(sid)
        self._canvas.tag_raise(self._name_id)
        for sid in self._hint_shadow_ids:
            self._canvas.tag_raise(sid)
        if self._hint_id:
            self._canvas.tag_raise(self._hint_id)
        for tid, sids in self._actions:
            for s in sids:
                self._canvas.tag_raise(s)
            self._canvas.tag_raise(tid)

    def _schedule_fade(self, text_id, shadow_ids, step):
        if step >= len(self.FADE_COLORS):
            # Fully faded — remove
            try:
                self._canvas.delete(text_id)
                for s in shadow_ids:
                    self._canvas.delete(s)
            except tk.TclError:
                pass
            self._actions = [(t, ss) for t, ss in self._actions if t != text_id]
            self._reposition_actions()
            return

        try:
            self._canvas.itemconfig(text_id, fill=self.FADE_COLORS[step])
        except tk.TclError:
            return

        if self._root:
            try:
                self._root.after(
                    self.FADE_INTERVAL_MS,
                    lambda: self._schedule_fade(text_id, shadow_ids, step + 1)
                )
            except tk.TclError:
                pass

    def _reposition_actions(self):
        bw = self.BORDER_WIDTH
        offsets = [(-1, 0), (1, 0), (0, -1), (0, 1)]
        for i, (text_id, shadow_ids) in enumerate(self._actions):
            y = bw + self.ACTIONS_Y_START + i * 18
            try:
                self._canvas.coords(text_id, bw + 8, y)
                for j, (dx, dy) in enumerate(offsets):
                    if j < len(shadow_ids):
                        self._canvas.coords(shadow_ids[j], bw + 8 + dx, y + dy)
            except tk.TclError:
                pass
        total_h = bw + self.ACTIONS_Y_START + len(self._actions) * 18 + 5
        try:
            self._canvas.coords(
                self._bg_rect_id, bw, bw, 500, max(total_h, bw + self.ACTIONS_Y_START)
            )
            self._raise_text_items()
        except tk.TclError:
            pass
