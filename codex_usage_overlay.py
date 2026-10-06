#!/usr/bin/env python3
"""Codex usage strip — rounded capsule docked to the top-center of the Codex
desktop window. 2026-09 rewrite: same behavior contract, fresh implementation.

Contract:
  - docks top-center of the Codex window's caption row; follows moves/resizes
    event-driven (WinEvent hook) with a slow poll as backstop
  - z-order: inserted directly above the Codex window, never globally topmost,
    so whatever covers the Codex window covers the strip too
  - hides while the Codex window is minimized/closed/cloaked; a manually
    launched instance lingers 30 minutes after the window disappears, then
    self-exits (--parent-pid mode keeps the strict die-with-parent contract)
  - official /usage polled every 60s (same cadence as the Codex CLI);
    left-click cycles data sources (active -> cc-switch relays -> official),
    hover expands details, right-click offers only 退出

Zero third-party dependencies: tkinter + ctypes + urllib.
Data fetching lives in usage_sources.py next to this file.
Tunables live in overlay.toml next to this file (built-in defaults apply when
it is missing or corrupt); uncaught errors append to crash.log (pythonw has
no stderr).

Run:   pythonw codex_usage_overlay.py
Quit:  right-click the strip -> 退出
"""

import ctypes
import os
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usage_sources import fetch_usage, fetch_named, ccs_relay_providers  # noqa: E402
import codex_metrics  # noqa: E402

# ------------------------------------------------------------------ settings
# Anchor only the OpenAI Codex desktop app: its windows are hosted by
# ChatGPT.exe inside the MSIX package path (ZCode.exe also runs windows).
TARGET_EXE = "chatgpt.exe"
TARGET_PATH_MARKER = "\\windowsapps\\openai.codex"
MIN_W, MIN_H = 400, 300          # ignore tiny/suspended host windows

__version__ = "2.2.0"

# Built-in defaults; user-tunable via overlay.toml next to this file (see load_config).
DEFAULTS = {
    "refresh_seconds": 60,       # usage API interval (Codex CLI's own cadence)
    "gone_exit_minutes": 30,     # linger after the anchor vanishes (manual instances)
    "hover_delay_ms": 350,       # hover expands the details panel
    "poll_docked_ms": 800,       # validation poll while docked (follow is event-driven)
    "poll_idle_ms": 2000,        # poll while no Codex window exists
    "max_backoff_seconds": 600,  # consecutive fetch failures back off up to this
}
# sanity clamps applied to overlay.toml values
LIMITS = {
    "refresh_seconds": (15, 600),
    "gone_exit_minutes": (1, 240),
    "hover_delay_ms": (100, 2000),
    "poll_docked_ms": (200, 5000),
    "poll_idle_ms": (500, 10000),
    "max_backoff_seconds": (60, 3600),
}
COUNTDOWN_MS = 30_000            # countdown text refresh without refetching
HOVER_GRACE_MS = 250             # tolerance for moving the pointer strip -> panel
BAR_W = 76
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay.toml")
CRASH_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log")

BG, FG, FG_DIM = "#17171a", "#ececf1", "#9b9ba4"
EDGE = "#2a2a31"
GREEN, AMBER, RED = "#10a37f", "#f59e0b", "#ef4444"
BAR_TRACK = "#2f2f36"

GA_ROOT = 2
GW_HWNDPREV = 3
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080

EVENT_OBJECT_REORDER = 0x8004
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SYNCHRONIZE = 0x00100000

WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                  wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
dwmapi = ctypes.WinDLL("dwmapi")
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CreateMutexW.restype = ctypes.c_void_p


def load_config(path=CONFIG_PATH) -> dict:
    """overlay.toml overrides DEFAULTS; missing/corrupt file -> defaults; clamped."""
    cfg = dict(DEFAULTS)
    try:
        import tomllib
        with open(path, "rb") as f:
            user = tomllib.load(f)
    except Exception:
        return cfg
    for key, (lo, hi) in LIMITS.items():
        v = user.get(key)
        if isinstance(v, (int, float)) and v > 0:
            cfg[key] = int(min(max(v, lo), hi))
    return cfg


def log_crash(exc_tuple=None) -> None:
    """pythonw has no stderr: append tracebacks to crash.log (capped at 128 KB).

    Called with the live exception (default) or an explicit (type, value, tb)
    tuple from Tk's report_callback_exception / threading.excepthook, whose
    exceptions would otherwise vanish without a console."""
    try:
        import traceback
        if os.path.exists(CRASH_LOG) and os.path.getsize(CRASH_LOG) > 128_000:
            os.remove(CRASH_LOG)
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n")
            if exc_tuple is None:
                traceback.print_exc(file=f)
            else:
                traceback.print_exception(*exc_tuple, file=f)
    except Exception:
        pass


# ------------------------------------------------------------------ win32 api
def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def window_rect(hwnd):
    r = wintypes.RECT()
    return (r.left, r.top, r.right, r.bottom) if user32.GetWindowRect(hwnd, ctypes.byref(r)) else None


def proc_path(hwnd) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(1024)
    ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    kernel32.CloseHandle(h)
    return buf.value if ok else ""


def is_codex_app(hwnd) -> bool:
    path = proc_path(hwnd).lower()
    return (os.path.basename(path) == TARGET_EXE
            and TARGET_PATH_MARKER in path)


def is_shown(hwnd) -> bool:
    """Visible, not minimized, not a suspended (cloaked) MSIX host window."""
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return False
    cloaked = ctypes.c_int(0)
    if dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4) == 0 and cloaked.value:
        return False
    return True


def caption_height(hwnd) -> int:
    """Caption-button row height in window coords.

    The Codex app is effectively frameless: min/max/close overlay the top of
    the client area, so the strip is centered on that row.
    """
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, 5, ctypes.byref(r), 16) == 0 and r.bottom > 0:
        return r.bottom
    dpi = user32.GetDpiForWindow(hwnd) or 96
    try:
        return user32.GetSystemMetricsForDpi(4, dpi)  # SM_CYCAPTION
    except AttributeError:
        return 51


def alive_but_hidden(hwnd) -> bool:
    """Anchor window still exists but is intentionally off-screen (minimized,
    or parked on another virtual desktop). Not the same as GONE: the linger
    timer only applies once the window is destroyed."""
    return bool(hwnd and user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd)
                and is_codex_app(hwnd))


def find_codex_window():
    """Foreground Codex window if any, else the largest visible one."""
    fg = user32.GetForegroundWindow()
    if fg and is_shown(fg) and is_codex_app(fg):
        r = window_rect(fg)
        if r and r[2] - r[0] >= MIN_W and r[3] - r[1] >= MIN_H:
            return fg
    best, best_area = None, 0
    out = []

    @WNDENUMPROC
    def cb(hwnd, _):
        out.append(hwnd)
        return True
    user32.EnumWindows(cb, 0)
    for hwnd in out:
        if not is_shown(hwnd) or not is_codex_app(hwnd):
            continue
        rect = window_rect(hwnd)
        if not rect:
            continue
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w >= MIN_W and h >= MIN_H and w * h > best_area:
            best, best_area = hwnd, w * h
    return best


def set_noactivate(hwnd) -> None:
    """Never take focus from the Codex window; stay out of Alt-Tab."""
    ex = WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    try:
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                 user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) | ex)
    except AttributeError:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                              user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | ex)


def round_corners(hwnd, w: int, h: int) -> None:
    """Win11 DWM rounded corners; classic region as the fallback."""
    pref = ctypes.c_int(2)  # DWMWCP_ROUND
    if dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4) != 0:
        dpi = user32.GetDpiForWindow(hwnd) or 96
        r = max(4, int(dpi * 8 / 96))
        user32.SetWindowRgn(hwnd, gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r, r), True)


def acquire_single_instance(suffix=None) -> bool:
    """True if we are the first instance for this mutex name."""
    name = "Local\\codex_usage_overlay" + (f"_{suffix}" if suffix else "")
    kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def watch_parent(ppid: int, strip: "UsageStrip") -> None:
    """Flag the strip when its spawner (the MCP server) dies."""
    h = kernel32.OpenProcess(PROCESS_SYNCHRONIZE, False, ppid)
    if h:
        kernel32.WaitForSingleObject(h, 0xFFFFFFFF)  # INFINITE
        kernel32.CloseHandle(h)
    strip.parent_gone = True


# ------------------------------------------------------------------ overlay
class UsageStrip:
    def __init__(self, cfg=None, ppid=None) -> None:
        self.cfg = dict(DEFAULTS)
        self.cfg.update(cfg or {})
        self.root = tk.Tk()
        self.root.title("codex-usage")
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", 0.96)
        self.root.configure(bg=EDGE)  # root shows as a 1px outline around the capsule

        self.anchor = None
        self._pos = None
        self.usage = None
        self.fetched_at = 0.0          # wall clock of last successful fetch
        self.last_fetch = 0.0          # wall clock of last attempt
        self.view_idx = 0              # 0 = active provider; >0 = cycling the list
        self.cycling_name = None
        self.shown = False
        self.panel_shown = False
        self.parent_gone = False
        self._quit = False
        self._fetch_busy = False
        self._refetch_pending = False    # click raced an in-flight fetch
        self._usage_target = "__active__"  # source the cached usage came from
        self._fail_streak = 0
        self._stale = False
        self._last_error = None
        self._metrics = None          # live model metrics (rate / cache hit)
        self._strip_w = self._strip_h = 0
        self._hwnd = None               # strip HWND, cached in _measure (Tk context)
        self._panel_hwnd_cache = None
        self._panel_w = self._panel_h = 0
        self._rounded_size = None
        self._gone_since = None        # monotonic time the anchor vanished
        self._hover_job = None
        self._hover_hide_job = None
        self._winevent_proc = WINEVENTPROC(self._on_win_event)  # keep ref: GC would crash the hook
        self._winevent_hook = None
        self._hooked_pid = None

        self._build_strip()
        self._build_panel()
        self._build_menu()
        # Route Tk-callback exceptions into crash.log (they never reach the
        # __main__ handler, and pythonw has no stderr to show them)
        self.root.report_callback_exception = lambda e, v, t: log_crash((e, v, t))
        self.root.update_idletasks()
        set_noactivate(self._strip_hwnd())
        set_noactivate(self._panel_hwnd())
        # Park off-screen, hidden; visibility is managed purely via SetWindowPos
        # (Tk withdraw/deiconify fights with overrideredirect on Windows)
        for h in (self._strip_hwnd(), self._panel_hwnd()):
            user32.SetWindowPos(h, None, -32000, -32000, 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE | SWP_HIDEWINDOW)
        self.root.after(200, self._loop)
        self.root.after(COUNTDOWN_MS, self._countdown_loop)
        self.refresh_data()

    # ---- hwnd helpers
    # The WinEvent callback runs INSIDE Tcl's message pump: it must never touch
    # Tcl (winfo_*/update_idletasks) or Tk panics -> abort (observed as
    # 0xc0000409 in ucrtbase). Both HWNDs are therefore cached here in Tk
    # context and the callback reads the caches only.
    def _strip_hwnd(self):
        return self._hwnd or user32.GetAncestor(self.root.winfo_id(), GA_ROOT)

    def _panel_hwnd(self):
        return self._panel_hwnd_cache or user32.GetAncestor(self.panel.winfo_id(), GA_ROOT)

    # ---- ui construction
    def _build_strip(self) -> None:
        f = tk.Frame(self.root, bg=BG, padx=11, pady=6)
        f.pack(padx=1, pady=1)
        self.lbl_pct = tk.Label(f, text="--%", font=("Segoe UI", 10, "bold"),
                                fg=FG_DIM, bg=BG)
        self.lbl_pct.pack(side="left")
        self.bar_bg = tk.Frame(f, width=BAR_W, height=4, bg=BAR_TRACK)
        self.bar_bg.pack(side="left", padx=(8, 8), pady=2)
        self.bar_bg.pack_propagate(False)
        self.bar_fill = tk.Frame(self.bar_bg, width=0, height=4, bg=GREEN)
        self.bar_fill.place(x=0, y=0)
        self.lbl_info = tk.Label(f, text="加载中…", font=("Segoe UI", 8),
                                 fg=FG_DIM, bg=BG)
        self.lbl_info.pack(side="left")

        self.root.bind("<Button-1>", self._on_click)
        self.root.bind("<Button-3>", self._popup_menu)
        self.root.bind("<Enter>", self._on_strip_enter)
        self.root.bind("<Leave>", self._on_strip_leave)

    def _build_panel(self) -> None:
        self.panel = tk.Toplevel(self.root)
        self.panel.overrideredirect(True)
        self.panel.attributes("-alpha", 0.97)
        self.panel.configure(bg=EDGE)
        self.panel_body = tk.Frame(self.panel, bg=BG, padx=12, pady=9)
        self.panel_body.pack(padx=1, pady=1)
        self.panel.bind("<Enter>", self._on_panel_enter)
        self.panel.bind("<Leave>", self._on_panel_leave)

    def _build_menu(self) -> None:
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="退出", command=self._quit)

    def _popup_menu(self, _e) -> None:
        self._hide_panel()
        self.menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    # ---- hover
    def _on_strip_enter(self, _e) -> None:
        self._cancel_hover_jobs()
        self._hover_job = self.root.after(self.cfg["hover_delay_ms"], self._show_panel)

    def _on_strip_leave(self, _e) -> None:
        self._cancel_hover_jobs()
        self._hover_hide_job = self.root.after(HOVER_GRACE_MS, self._hide_panel)

    def _on_panel_enter(self, _e) -> None:
        self._cancel_hover_jobs()

    def _on_panel_leave(self, _e) -> None:
        self._cancel_hover_jobs()
        self._hover_hide_job = self.root.after(HOVER_GRACE_MS, self._hide_panel)

    def _cancel_hover_jobs(self) -> None:
        for attr in ("_hover_job", "_hover_hide_job"):
            job = getattr(self, attr)
            if job:
                self.root.after_cancel(job)
                setattr(self, attr, None)

    def _show_panel(self) -> None:
        self._hover_job = None
        if not self.shown:
            return
        self._fill_panel()
        self._measure_panel()  # Tk context: cache metrics for Tcl-free placement
        self._place_panel()

    def _hide_panel(self) -> None:
        self._hover_hide_job = None
        if not self.panel_shown:
            return
        user32.SetWindowPos(self._panel_hwnd(), None, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_HIDEWINDOW)
        self.panel_shown = False

    def _measure_panel(self) -> None:
        self.panel.update_idletasks()
        self._panel_w = self.panel.winfo_reqwidth()
        self._panel_h = self.panel.winfo_reqheight()
        self._panel_hwnd_cache = user32.GetAncestor(self.panel.winfo_id(), GA_ROOT)

    def _place_panel(self) -> None:
        """Tcl-free (safe inside the WinEvent callback): uses cached sizes."""
        if not self._pos or not self._panel_w:
            return
        w, h = self._panel_w, self._panel_h
        x, y = self._pos[0], self._pos[1] + self._strip_h + 3
        vx, vy = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw, vh = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        x = max(vx + 4, min(x, vx + vw - w - 4))
        if y + h > vy + vh - 4:
            y = max(vy + 4, self._pos[1] - h - 3)
        hwnd = self._panel_hwnd()
        user32.SetWindowPos(hwnd, None, x, y, 0, 0,
                            SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        round_corners(hwnd, w, h)
        self.panel_shown = True

    def _fill_panel(self) -> None:
        for c in self.panel_body.winfo_children():
            c.destroy()
        u = self.usage
        rows = []
        if u is None:
            rows.append(("获取中…", FG_DIM, False))
        elif u.get("error"):
            rows.append(("获取失败", RED, True))
            rows.append((str(u["error"])[:120], FG_DIM, False))
        elif u.get("mode") == "relay":
            cur = "\u00a5" if u.get("currency") == "CNY" else "$"
            rows.append((u.get("provider") or "relay", FG, True))
            rows.append((f"余额  {cur}{u.get('balance') or 0:,.2f}", FG, False))
            if u.get("total"):
                rows.append((f"总额  {cur}{u['total']:,.2f}   已用  {cur}{u.get('used') or 0:,.2f}",
                             FG_DIM, False))
            if u.get("today_cost") is not None:
                rows.append((f"今日  {cur}{u['today_cost']:,.2f}", GREEN, False))
            if u.get("protocol"):
                rows.append((u["protocol"], FG_DIM, False))
        else:
            p = u.get("primary_window") or {}
            remain = max(0, 100 - round(p.get("used_percent") or 0))
            rows.append((f"ChatGPT · {u.get('plan_type') or '?'}", FG, True))
            rows.append((f"主限额剩余  {remain}%", FG, False))
            rows.append(("重置  " + self._reset_text(p), FG_DIM, False))
            w2 = u.get("secondary_window")
            if w2:
                rows.append((f"周限额剩余  {max(0, 100 - round(w2.get('used_percent') or 0))}%",
                             FG_DIM, False))
            if u.get("credits_balance") is not None:
                rows.append((f"credits  ${u['credits_balance']:,.2f}", FG_DIM, False))
        rows.extend(self._metrics_rows())
        if self._stale:
            rows.append(("拉取失败 · 显示缓存值", AMBER, False))
            if self._last_error:
                rows.append((self._last_error[:80], FG_DIM, False))
        if self.fetched_at and time.time() - self.fetched_at > 5:
            rows.append((f"更新于 {int((time.time() - self.fetched_at) // 60)} 分钟前"
                         f" · 每 {self.cfg['refresh_seconds']}s 自动刷新", FG_DIM, False))
        rows.append((f"Codex Display v{__version__}", FG_DIM, False))
        for i, (txt, color, bold) in enumerate(rows):
            tk.Label(self.panel_body, text=txt, bg=BG, fg=color, anchor="w",
                     font=("Segoe UI", 8, "bold") if bold else ("Segoe UI", 8),
                     ).pack(fill="x", pady=(0 if i == 0 else 3))

    # ---- live model metrics (parsed from local session logs)
    def _refresh_metrics(self) -> None:
        try:
            self._metrics = codex_metrics.session_metrics()
        except Exception:  # noqa: BLE001 - metrics are decorative, never fatal
            self._metrics = None

    def _rate_suffix(self) -> str:
        """Compact capsule segment when the last response is fresh."""
        m = self._metrics
        if not m or m.get("last_rate") is None:
            return ""
        age = m.get("age_s")
        if age is None or age > 600:
            return ""
        return f" \u00b7 {m['last_rate']:.0f} tok/s"

    def _metrics_rows(self) -> list:
        m = self._metrics
        if not m:
            return []
        rows = []
        if m.get("last_rate") is not None:
            age = m.get("age_s")
            ago = f" \u00b7 {int(age // 60)} 分钟前" if age is not None and age > 90 else ""
            rows.append((f"输出速率  {m['last_rate']:.0f} tok/s"
                         f" \u00b7 最近响应 {m.get('last_out_tok') or 0:,} tok"
                         f" / {m.get('last_dur_s') or 0:.0f}s{ago}", FG, False))
        if m.get("cache_hit") is not None:
            rows.append((f"输入缓存命中  {m['cache_hit'] * 100:.0f}%"
                         f" \u00b7 会话累计 {m['in_tok']:,} tok 中缓存 {m['cached_tok']:,}",
                         FG, False))
        if m.get("model"):
            rows.append((f"模型  {m['model']}", FG_DIM, False))
        return rows

    # ---- data
    def _cycle_list(self) -> list:
        names = ["__active__"] + [p["name"] for p in ccs_relay_providers()]
        if os.path.exists(os.path.join(os.path.expanduser("~"), ".codex", "auth.json")):
            names.append("OpenAI Official")
        return names

    def _on_click(self, _e) -> None:
        self._hide_panel()
        self.view_idx = (self.view_idx + 1) % max(1, len(self._cycle_list()))
        self.refresh_data()

    def refresh_data(self) -> None:
        if self._fetch_busy:
            self._refetch_pending = True  # view changed mid-fetch: redo soon
            return
        if not self.shown and self.usage is not None:
            # Nobody is looking and we hold last-good data: skip the network but
            # keep the timer alive. The very first fetch always runs so the strip
            # shows real numbers the moment the window appears.
            self.root.after(self.cfg["refresh_seconds"] * 1000, self.refresh_data)
            return
        self._fetch_busy = True
        self.last_fetch = time.time()
        target = "__active__"
        if self.view_idx > 0:
            names = self._cycle_list()[1:]
            target = names[(self.view_idx - 1) % len(names)] if names else "__active__"
        self.cycling_name = None if target == "__active__" else target

        def worker():
            try:
                u = fetch_named(target) if target != "__active__" else fetch_usage()
            except Exception as e:  # noqa: BLE001 - surface any failure in the strip
                u = {"error": str(e)}
            self._fetch_busy = False
            self.root.after(0, lambda: self._apply(u, target))
        threading.Thread(target=worker, daemon=True).start()

    def _apply(self, u: dict, target: str = "__active__") -> None:
        self.last_fetch = time.time()
        if u.get("error"):
            self._fail_streak += 1
            self._last_error = str(u["error"])
            if self.usage is not None and self._usage_target == target:
                self._render_stale()  # same source: keep last-good numbers, marked stale
            else:
                # no (or mismatched) cached data for THIS source: honest error
                self.lbl_pct.config(text="--%", fg=RED)
                self.lbl_info.config(text="获取失败 " + self._last_error[:24])
                self.bar_fill.place_forget()
        else:
            self._fail_streak = 0
            self._stale = False
            self._last_error = None
            self.usage = u
            self._usage_target = target
            self.fetched_at = time.time()
            self._refresh_metrics()
            self._render(u)
        if self._refetch_pending:
            self._refetch_pending = False
            self.root.after(50, self.refresh_data)  # a click raced this fetch
        else:
            self.root.after(self._next_refresh_ms(), self.refresh_data)
        if self.shown:
            self.root.after_idle(self._sync_position)

    def _next_refresh_ms(self) -> int:
        """refresh interval, doubling on consecutive failures up to max_backoff."""
        base = self.cfg["refresh_seconds"] * 1000
        return min(base * (2 ** min(self._fail_streak, 10)),
                   self.cfg["max_backoff_seconds"] * 1000)

    def _render_stale(self) -> None:
        """Fetch failed but last-good data exists: dim it instead of blanking."""
        self._stale = True
        self._render(self.usage)
        age = int((time.time() - self.fetched_at) // 60)
        self.lbl_pct.config(fg=FG_DIM)
        self.lbl_info.config(text=f"获取失败 · 缓存（{age} 分钟前）")

    def _render(self, u: dict) -> None:
        if u.get("mode") == "relay":
            self._render_relay(u)
        else:
            self._render_official(u)

    def _render_official(self, u: dict) -> None:
        p = u.get("primary_window") or {}
        remain = max(0, 100 - round(p.get("used_percent") or 0))
        color = RED if remain <= 10 else AMBER if remain <= 30 else GREEN
        self.lbl_pct.config(text=f"{remain}%", fg=color)
        self.bar_fill.place(x=0, y=0, width=max(2, int(BAR_W * min(100, remain) / 100)))
        self.bar_fill.config(bg=color)
        extra = ""
        w = u.get("secondary_window")
        if w:
            extra += f" \u00b7 周余 {max(0, 100 - round(w.get('used_percent') or 0))}%"
        if u.get("credits_balance") is not None:
            extra += f" \u00b7 ${u['credits_balance']:.2f}"
        if self.cycling_name:
            extra += f" \u00b7 {self.cycling_name}"
        extra += self._rate_suffix()
        self.lbl_info.config(fg=FG_DIM,
                             text=self._reset_text(p) + extra)

    def _render_relay(self, u: dict) -> None:
        cur = "\u00a5" if u.get("currency") == "CNY" else "$"
        bal = u.get("balance") or 0
        total = u.get("total")
        pct = round(max(0.0, min(1.0, bal / total)) * 100) if total else None
        color = GREEN if pct is None else (
            RED if pct <= 10 else AMBER if pct <= 30 else GREEN)
        self.lbl_pct.config(text=f"{cur}{bal:,.2f}", fg=color)
        width = BAR_W if pct is None else max(2, int(BAR_W * min(100, pct) / 100))
        self.bar_fill.place(x=0, y=0, width=width)
        self.bar_fill.config(bg=color)
        parts = []
        if pct is not None:
            parts.append(f"剩 {pct}%")
        if u.get("today_cost") is not None:
            parts.append(f"今日 {cur}{u['today_cost']:.2f}")
        if total is not None:
            parts.append(f"总额 {cur}{total:,.2f}")
        if u.get("provider"):
            parts.append(u["provider"])
        parts.append(self._rate_suffix().strip(" \u00b7"))
        self.lbl_info.config(text=" \u00b7 ".join(p for p in parts if p), fg=FG_DIM)

    def _reset_text(self, p: dict) -> str:
        secs = max(0, (p.get("reset_after_seconds") or 0) - (time.time() - self.fetched_at))
        d, h = int(secs // 86400), int(secs % 86400 // 3600)
        if d:
            return f"{d}天{h}小时后重置"
        return f"{h}小时{int(secs % 3600 // 60)}分后重置"

    def _countdown_loop(self) -> None:
        try:
            u = self.usage
            if u and self.shown and not self._stale:
                self._refresh_metrics()
                if u.get("mode") != "relay":
                    self._render_official(u)
                elif self._metrics:  # relay: metrics segment may have gone stale
                    self._render_relay(u)
        except Exception:
            pass
        self.root.after(COUNTDOWN_MS, self._countdown_loop)

    # ---- window tracking
    def _measure(self) -> None:
        self.root.update_idletasks()
        self._strip_w = self.root.winfo_reqwidth()
        self._strip_h = self.root.winfo_reqheight()
        self._hwnd = user32.GetAncestor(self.root.winfo_id(), GA_ROOT)

    def _strip_pos(self, hwnd):
        """(x, y) centering the strip on the anchor's caption row."""
        rect = window_rect(hwnd)
        if not rect:
            return None
        x = rect[0] + ((rect[2] - rect[0]) - self._strip_w) // 2
        y = rect[1] + max(0, (caption_height(hwnd) - self._strip_h) // 2)
        return x, y

    def _install_hook(self, anchor) -> None:
        """Event-driven follow: the system pushes an event on every anchor move."""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(anchor, ctypes.byref(pid))
        if self._winevent_hook and pid.value == self._hooked_pid:
            return
        if self._winevent_hook:
            user32.UnhookWinEvent(self._winevent_hook)
        self._hooked_pid = pid.value
        self._winevent_hook = user32.SetWinEventHook(
            EVENT_OBJECT_REORDER, EVENT_OBJECT_LOCATIONCHANGE, 0,
            self._winevent_proc, pid.value, 0, 0)  # WINEVENT_OUTOFCONTEXT

    def _on_win_event(self, _hook, event, hwnd, id_object, _child, _thread, _ts) -> None:
        # Runs on the Tk thread inside the message pump; Win32 calls only here.
        if id_object != 0 or hwnd != self.anchor or not self._strip_w:
            return
        try:
            if event == EVENT_OBJECT_LOCATIONCHANGE and self.shown:
                pos = self._strip_pos(hwnd)
                if pos:  # plain move: keep z-order
                    user32.SetWindowPos(self._strip_hwnd(), None, pos[0], pos[1], 0, 0,
                                        SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER)
                    self._pos = pos
                    if self.panel_shown:
                        self._place_panel()
            elif event == EVENT_OBJECT_REORDER and self.shown:
                self._dock(hwnd)  # anchor was raised: re-dock z-order
        except Exception:
            pass

    def _dock(self, anchor) -> None:
        """Place the strip directly ABOVE the anchor in z-order (never topmost).

        SetWindowPos(B, insertAfter=A) puts B *below* A, so to sit above the
        anchor we insert after whatever window currently precedes the anchor.
        """
        pos = self._strip_pos(anchor)
        if not pos:
            return
        strip = self._strip_hwnd()
        if self._rounded_size != (self._strip_w, self._strip_h):
            self._rounded_size = (self._strip_w, self._strip_h)
            round_corners(strip, self._strip_w, self._strip_h)
        prev = user32.GetWindow(anchor, GW_HWNDPREV)
        if prev == strip:  # already in place: move only
            user32.SetWindowPos(strip, None, pos[0], pos[1], 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_NOZORDER)
        else:
            user32.SetWindowPos(strip, prev or 0, pos[0], pos[1], 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)  # 0 = HWND_TOP
        self._pos = pos
        self.shown = True
        if self.panel_shown:
            self._place_panel()

    def _sync_position(self) -> None:
        """Re-center after a content change resized the capsule."""
        if not (self.shown and self.anchor):
            return
        self._measure()
        pos = self._strip_pos(self.anchor)
        if pos:
            user32.SetWindowPos(self._strip_hwnd(), None, pos[0], pos[1], 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER)
            self._pos = pos

    def _hide(self) -> None:
        self._hide_panel()
        if self.shown:
            user32.SetWindowPos(self._strip_hwnd(), None, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_HIDEWINDOW)
            self.shown = False

    def _quit(self) -> None:
        os._exit(0)  # hard exit: Tk destroy() inside callbacks is unreliable

    def _loop(self) -> None:
        try:
            self._tick()
        except Exception:  # noqa: BLE001 - never break the follow loop
            pass
        if self._quit:
            self._quit = False
            os._exit(0)
        self.root.after(self.cfg["poll_docked_ms"] if self.shown else self.cfg["poll_idle_ms"],
                        self._loop)

    def _tick(self) -> None:
        prev = self.anchor
        hwnd = prev if (prev and is_shown(prev) and is_codex_app(prev)) else None
        if hwnd is None:
            hwnd = find_codex_window()
        if hwnd:
            self.anchor = hwnd
            self._gone_since = None
            self._measure()
            self._install_hook(hwnd)
            was_shown = self.shown
            self._dock(hwnd)
            if not was_shown and time.time() - self.last_fetch > 30:
                self.refresh_data()  # window reappeared: freshen stale data
        else:
            self._hide()
            if self.parent_gone:
                self._quit = True  # spawner gone and Codex window too
            elif alive_but_hidden(prev):
                # minimized or on another virtual desktop: window still exists,
                # so the linger timer (for a GONE window) must not run — wait
                # indefinitely and re-dock on restore
                self._gone_since = None
            elif self._gone_since is None:
                self._gone_since = time.monotonic()
            elif time.monotonic() - self._gone_since > self.cfg["gone_exit_minutes"] * 60:
                self._quit = True  # manual instance: linger, then self-exit

    def run(self) -> None:
        self.root.mainloop()


# ------------------------------------------------------------------ entry
def main() -> None:
    threading.excepthook = lambda a: log_crash((a.exc_type, a.exc_value, a.exc_traceback))
    ppid = None
    instance = None
    for i, a in enumerate(sys.argv):
        if a == "--parent-pid" and i + 1 < len(sys.argv):
            try:
                ppid = int(sys.argv[i + 1])
            except ValueError:
                pass
        elif a == "--instance" and i + 1 < len(sys.argv):
            instance = sys.argv[i + 1]
    if not acquire_single_instance(instance):
        return  # another instance owns this mutex already
    enable_dpi_awareness()
    strip = UsageStrip(cfg=load_config(), ppid=ppid)
    if ppid:
        threading.Thread(target=watch_parent, args=(ppid, strip), daemon=True).start()
    strip.run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_crash()  # pythonw swallows stderr: keep the evidence on disk
        raise
