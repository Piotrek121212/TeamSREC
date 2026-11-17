import ctypes
from ctypes import wintypes
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw
import pystray

from audio_recorder import DualAudioRecorder
from course_organizer import (
    CourseMatch,
    IcsRemoteEvent,
    PL_DAYS_FULL,
    append_recording_metadata,
    build_recording_paths,
    import_ics_source,
    match_course,
    parse_remote_ics_events,
    reassign_recording_course,
    resolve_active_ics_path,
    resolve_output_root,
    sanitize_folder_name,
    sync_courses_from_ics_events,
)
from teams_detector import MeetingDetectionResult, detect_teams_meeting


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
ICON_PATH = BASE_DIR / "teamsrec.ico"

# Win32 Constants for Single-Instance Mutex & Wakeup Event
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0x00000000
EVENT_MODIFY_STATE = 0x0002

# Minimalist Dark Palette
BG_MAIN = "#0d0f14"
BG_CARD = "#161a23"
BG_CARD_HOVER = "#1c212d"
BG_ELEVATED = "#222836"
BG_INPUT = "#11141c"
BORDER_SUBTLE = "#262d3d"
FG_PRIMARY = "#f8fafc"
FG_SECONDARY = "#94a3b8"
FG_MUTED = "#64748b"

ACCENT_INDIGO = "#6366f1"
GREEN_BG = "#064e3b"
GREEN_FG = "#34d399"
RED_BG = "#4c1d1d"
RED_FG = "#f87171"
BLUE_BG = "#1e293b"
BLUE_FG = "#38bdf8"
AMBER_BG = "#452c0a"
AMBER_FG = "#fbbf24"


class SingleInstanceManager:
    """
    Ensures only one instance of TeamsRec runs in the background on Windows.
    If a second instance is started, it signals the existing instance via a Win32 Named Event
    to restore its window from the System Tray and immediately exits.
    """

    def __init__(
        self,
        mutex_name: str = r"Local\TeamsRec_SingleInstance_Mutex",
        event_name: str = r"Local\TeamsRec_ShowWindow_Event",
    ) -> None:
        self.mutex_name = mutex_name
        self.event_name = event_name
        self.kernel32 = ctypes.windll.kernel32

        self.kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel32.CreateMutexW.restype = wintypes.HANDLE
        self.kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel32.CreateEventW.restype = wintypes.HANDLE
        self.kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel32.OpenEventW.restype = wintypes.HANDLE
        self.kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        self.kernel32.SetEvent.restype = wintypes.BOOL
        self.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

        self.mutex_handle = self.kernel32.CreateMutexW(None, False, self.mutex_name)
        self.already_running = self.kernel32.GetLastError() == ERROR_ALREADY_EXISTS
        self.event_handle: Optional[int] = None

        if not self.already_running:
            # Auto-reset event (bManualReset=False, bInitialState=False)
            self.event_handle = self.kernel32.CreateEventW(None, False, False, self.event_name)

    def signal_existing_instance(self) -> bool:
        h_evt = self.kernel32.OpenEventW(EVENT_MODIFY_STATE, False, self.event_name)
        if h_evt:
            ok = bool(self.kernel32.SetEvent(h_evt))
            self.kernel32.CloseHandle(h_evt)
            return ok
        return False

    def check_wakeup_signal(self) -> bool:
        if not self.event_handle:
            return False
        res = self.kernel32.WaitForSingleObject(self.event_handle, 0)
        return res == WAIT_OBJECT_0

    def close(self) -> None:
        if self.event_handle:
            try:
                self.kernel32.CloseHandle(self.event_handle)
            except Exception:
                pass
            self.event_handle = None
        if self.mutex_handle:
            try:
                self.kernel32.CloseHandle(self.mutex_handle)
            except Exception:
                pass
            self.mutex_handle = None


def set_windows_app_user_model_id() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TeamsRec.App.1")
    except Exception:
        pass


def apply_dark_titlebar(window: tk.Misc) -> None:
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except Exception:
        pass


def apply_window_icon(window: tk.Wm) -> None:
    try:
        ensure_ico_file(ICON_PATH)
        if ICON_PATH.exists():
            window.iconbitmap(str(ICON_PATH))
    except Exception:
        pass


def get_clipboard_files_win32() -> List[str]:
    CF_HDROP = 15
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT

    files: List[str] = []
    if not user32.OpenClipboard(None):
        return files
    try:
        hdrop = user32.GetClipboardData(CF_HDROP)
        if hdrop:
            count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
            for i in range(count):
                length = shell32.DragQueryFileW(hdrop, i, None, 0)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    shell32.DragQueryFileW(hdrop, i, buf, length + 1)
                    files.append(buf.value)
    except Exception:
        pass
    finally:
        user32.CloseClipboard()
    return files


def create_tray_icon_image(state: str = "standby", size: int = 256) -> Image.Image:
    """
    Draws the TeamsRec icon at arbitrary resolution (default 256x256 for .ico, 64x64 for Tray).
    """
    scale = size / 64.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def sc(v: float) -> int:
        return int(round(v * scale))

    # Dark rounded squircle background
    draw.rounded_rectangle(
        (sc(4), sc(4), sc(60), sc(60)),
        radius=sc(14),
        fill=(22, 26, 35, 255),
        outline=(99, 102, 241, 255) if state == "standby" else (239, 68, 68, 255),
        width=max(1, sc(2.5)),
    )

    # Microphone capsule
    draw.rounded_rectangle(
        (sc(25), sc(13), sc(39), sc(34)),
        radius=sc(7),
        fill=(241, 245, 249, 255),
    )
    # Mic cradle arc & stand
    draw.arc(
        (sc(19), sc(19), sc(45), sc(42)),
        start=0,
        end=180,
        fill=(165, 180, 252, 255),
        width=max(1, sc(3)),
    )
    draw.line((sc(32), sc(42), sc(32), sc(49)), fill=(165, 180, 252, 255), width=max(1, sc(3)))
    draw.line((sc(24), sc(49), sc(40), sc(49)), fill=(165, 180, 252, 255), width=max(1, sc(3)))

    if state == "recording":
        badge_color = (239, 68, 68, 255)
    elif state == "paused":
        badge_color = (100, 116, 139, 255)
    else:
        badge_color = (16, 185, 129, 255)

    draw.ellipse((sc(40), sc(40), sc(62), sc(62)), fill=(13, 15, 20, 255))
    draw.ellipse((sc(42), sc(42), sc(60), sc(60)), fill=badge_color)

    if state == "recording":
        draw.ellipse((sc(48), sc(48), sc(54), sc(54)), fill=(255, 255, 255, 255))
    elif state == "standby":
        draw.line((sc(46), sc(51), sc(50), sc(55)), fill=(255, 255, 255, 255), width=max(1, sc(2.2)))
        draw.line((sc(50), sc(55), sc(56), sc(47)), fill=(255, 255, 255, 255), width=max(1, sc(2.2)))

    return img


def ensure_ico_file(ico_path: Path = ICON_PATH) -> Path:
    if not ico_path.exists():
        base_img = create_tray_icon_image("standby", size=256)
        base_img.save(
            str(ico_path),
            format="ICO",
            sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
    return ico_path


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def get_monday_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())


class TeamsRecApp:
    def __init__(
        self,
        config_path: Path = DEFAULT_CONFIG_PATH,
        start_hidden: bool = False,
        instance_lock: Optional[SingleInstanceManager] = None,
    ) -> None:
        set_windows_app_user_model_id()
        ensure_ico_file(ICON_PATH)

        self.instance_lock = instance_lock
        self.config_path = config_path
        self.config = self._load_config()

        self.root = tk.Tk()
        self.root.title("TeamSREC")
        self.root.geometry("860x650")
        self.root.minsize(780, 520)
        self.root.configure(bg=BG_MAIN)
        apply_window_icon(self.root)
        apply_dark_titlebar(self.root)

        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        # Single child dialog guard (blocks opening multiple sub-windows)
        self._active_dialog: Optional[tk.Toplevel] = None

        # Recorder & Watcher State
        self.auto_detect_enabled = True
        self.manual_recording_active = False
        self.recorder = DualAudioRecorder(self.config)
        self.active_course: Optional[CourseMatch] = None
        self.active_file: Optional[Path] = None
        self.started_at: Optional[datetime] = None
        self.last_detection: MeetingDetectionResult = MeetingDetectionResult(in_meeting=False)

        self.meeting_seen_since: Optional[float] = None
        self.meeting_lost_since: Optional[float] = None
        self._stop_lock = threading.Lock()
        self._quitting = False

        # View Mode: "WEEK", "MISSING", "RECORDED", "ALL"
        self.view_mode: str = "WEEK"
        self.current_week_monday: date = get_monday_of_week(datetime.now().date())
        self.events_cache: List[IcsRemoteEvent] = []

        self._setup_styles()
        self._build_ui()
        self.refresh_cards()

        # System Tray Icon
        self.tray_icon: Optional[pystray.Icon] = None
        self._last_tray_state = ""
        self._start_tray_icon()

        if start_hidden:
            self.root.withdraw()

        self._watcher_thread = threading.Thread(target=self._background_watcher_loop, daemon=True)
        self._watcher_thread.start()
        self.root.after(250, self._ui_tick)

    def _open_singleton_dialog(self, title: str, geometry: str) -> Optional[tk.Toplevel]:
        """
        Ensures only one child dialog window can be open at a time.
        If a dialog is already open, brings it to the front and returns None.
        """
        if self._active_dialog is not None:
            try:
                if self._active_dialog.winfo_exists():
                    self._active_dialog.deiconify()
                    self._active_dialog.lift()
                    self._active_dialog.focus_force()
                    return None
            except Exception:
                pass
            self._active_dialog = None

        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.geometry(geometry)
        dlg.configure(bg=BG_CARD)
        dlg.transient(self.root)
        dlg.grab_set()
        apply_window_icon(dlg)
        apply_dark_titlebar(dlg)
        self._active_dialog = dlg

        def _on_close() -> None:
            self._active_dialog = None
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", _on_close)
        return dlg

    def _load_config(self) -> Dict[str, Any]:
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_config(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)

    def _get_course_list(self) -> List[Tuple[str, str]]:
        res: List[Tuple[str, str]] = []
        seen = set()
        for c in self.config.get("courses", []):
            fn = sanitize_folder_name(str(c.get("name", "")))
            dn = str(c.get("display_name") or fn.replace("_", " "))
            if fn and fn not in seen:
                res.append((fn, dn))
                seen.add(fn)
        return res

    def _get_course_display_name(self, folder_name: str) -> str:
        for fn, dn in self._get_course_list():
            if fn.lower() == folder_name.lower():
                return dn
        return folder_name.replace("_", " ")

    def _setup_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Dark.TCombobox",
            fieldbackground=BG_INPUT,
            background=BG_ELEVATED,
            foreground=FG_PRIMARY,
            arrowcolor=FG_PRIMARY,
            bordercolor=BORDER_SUBTLE,
            lightcolor=BORDER_SUBTLE,
            darkcolor=BORDER_SUBTLE,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", BG_INPUT)],
            selectbackground=[("readonly", BG_INPUT)],
            selectforeground=[("readonly", FG_PRIMARY)],
        )

    def _build_ui(self) -> None:
        # 1. TOP STATUS CARD
        status_card = tk.Frame(
            self.root,
            bg=BG_CARD,
            highlightbackground=BORDER_SUBTLE,
            highlightthickness=1,
        )
        status_card.pack(fill="x", padx=18, pady=(16, 10))

        top_inner = tk.Frame(status_card, bg=BG_CARD)
        top_inner.pack(fill="x", padx=16, pady=12)

        status_left = tk.Frame(top_inner, bg=BG_CARD)
        status_left.pack(side="left", fill="both", expand=True)

        self.status_title_lbl = tk.Label(
            status_left,
            text="● Czuwanie w tle",
            font=("Segoe UI Semibold", 12),
            bg=BG_CARD,
            fg=GREEN_FG,
        )
        self.status_title_lbl.pack(anchor="w")

        self.status_desc_lbl = tk.Label(
            status_left,
            text="Nagrywanie włączy się automatycznie, gdy dołączysz do spotkania na Teams",
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=FG_SECONDARY,
        )
        self.status_desc_lbl.pack(anchor="w", pady=(2, 6))

        override_row = tk.Frame(status_left, bg=BG_CARD)
        override_row.pack(anchor="w")

        tk.Label(
            override_row,
            text="Przedmiot przy nagrywaniu:",
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=FG_MUTED,
        ).pack(side="left", padx=(0, 8))

        self.live_course_var = tk.StringVar(value="Automatycznie (z planu .ics)")
        self.live_course_combo = ttk.Combobox(
            override_row,
            textvariable=self.live_course_var,
            style="Dark.TCombobox",
            state="readonly",
            width=32,
            font=("Segoe UI", 9),
        )
        self._refresh_course_dropdown()
        self.live_course_combo.bind("<<ComboboxSelected>>", self._on_live_course_selected)
        self.live_course_combo.pack(side="left")

        status_right = tk.Frame(top_inner, bg=BG_CARD)
        status_right.pack(side="right")

        self.record_btn = tk.Button(
            status_right,
            text="🔴 Nagraj teraz",
            command=self.toggle_manual_recording,
            bg="#dc2626",
            fg="#ffffff",
            activebackground="#b91c1c",
            activeforeground="#ffffff",
            relief="flat",
            font=("Segoe UI Semibold", 9),
            padx=14,
            pady=7,
            cursor="hand2",
        )
        self.record_btn.pack(side="left", padx=(0, 8))

        tk.Button(
            status_right,
            text="📂 Folder nagrań",
            command=self.open_recordings_root,
            bg=BG_ELEVATED,
            fg=FG_PRIMARY,
            activebackground=BORDER_SUBTLE,
            activeforeground="#ffffff",
            relief="flat",
            font=("Segoe UI", 9),
            padx=12,
            pady=7,
            cursor="hand2",
        ).pack(side="left")

        # 2. NAVIGATION & FILTER BAR
        nav_bar = tk.Frame(self.root, bg=BG_MAIN)
        nav_bar.pack(fill="x", padx=18, pady=(2, 8))

        week_nav = tk.Frame(nav_bar, bg=BG_MAIN)
        week_nav.pack(side="left")

        tk.Button(
            week_nav,
            text="◀",
            command=self._prev_week,
            bg=BG_CARD,
            fg=FG_PRIMARY,
            activebackground=BG_ELEVATED,
            activeforeground="#ffffff",
            relief="flat",
            font=("Segoe UI", 9),
            padx=8,
            pady=4,
            cursor="hand2",
        ).pack(side="left")

        self.week_label = tk.Label(
            week_nav,
            text="16.11 – 22.11.2026",
            font=("Segoe UI Semibold", 10),
            bg=BG_MAIN,
            fg=FG_PRIMARY,
            width=22,
        )
        self.week_label.pack(side="left", padx=6)

        tk.Button(
            week_nav,
            text="▶",
            command=self._next_week,
            bg=BG_CARD,
            fg=FG_PRIMARY,
            activebackground=BG_ELEVATED,
            activeforeground="#ffffff",
            relief="flat",
            font=("Segoe UI", 9),
            padx=8,
            pady=4,
            cursor="hand2",
        ).pack(side="left")

        tk.Button(
            week_nav,
            text="Ten tydzień",
            command=self._go_to_current_week,
            bg=BG_CARD,
            fg=FG_SECONDARY,
            activebackground=BG_ELEVATED,
            activeforeground=FG_PRIMARY,
            relief="flat",
            font=("Segoe UI", 9),
            padx=10,
            pady=4,
            cursor="hand2",
        ).pack(side="left", padx=(8, 0))

        pills_frame = tk.Frame(nav_bar, bg=BG_MAIN)
        pills_frame.pack(side="right")

        self.mode_buttons: Dict[str, tk.Button] = {}
        modes = [
            ("WEEK", "📅 Wybrany tydzień"),
            ("MISSING", "✖ Tylko braki"),
            ("RECORDED", "✔ Nagrane"),
            ("ALL", "Wszystkie"),
        ]
        for m_code, m_text in modes:
            b = tk.Button(
                pills_frame,
                text=m_text,
                command=lambda c=m_code: self._set_view_mode(c),
                bg=ACCENT_INDIGO if m_code == "WEEK" else BG_CARD,
                fg="#ffffff" if m_code == "WEEK" else FG_SECONDARY,
                relief="flat",
                font=("Segoe UI Semibold", 9),
                padx=10,
                pady=4,
                cursor="hand2",
            )
            b.pack(side="left", padx=(4, 0))
            self.mode_buttons[m_code] = b

        # 3. SCROLLABLE CARDS LIST
        list_wrapper = tk.Frame(self.root, bg=BG_MAIN)
        list_wrapper.pack(fill="both", expand=True, padx=18, pady=(0, 8))

        self.canvas = tk.Canvas(list_wrapper, bg=BG_MAIN, highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(list_wrapper, orient="vertical", command=self.canvas.yview)
        self.cards_container = tk.Frame(self.canvas, bg=BG_MAIN)

        self.cards_container.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self._canvas_win = self.canvas.create_window((0, 0), window=self.cards_container, anchor="nw")
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig(self._canvas_win, width=e.width),
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

        # 4. MINIMAL FOOTER
        footer = tk.Frame(self.root, bg=BG_MAIN)
        footer.pack(fill="x", padx=18, pady=(0, 14))

        self.footer_stats_lbl = tk.Label(
            footer,
            text="✔ Nagrane: 0   •   ✖ Brak nagrania: 0   •   🕒 W planie: 0",
            font=("Segoe UI", 9),
            bg=BG_MAIN,
            fg=FG_SECONDARY,
        )
        self.footer_stats_lbl.pack(side="left")

        tk.Button(
            footer,
            text="⬇ Do paska (Tray)",
            command=self.hide_to_tray,
            bg=BG_CARD,
            fg=FG_SECONDARY,
            activebackground=BG_ELEVATED,
            activeforeground=FG_PRIMARY,
            relief="flat",
            font=("Segoe UI", 9),
            padx=10,
            pady=4,
            cursor="hand2",
        ).pack(side="right", padx=(6, 0))

        tk.Button(
            footer,
            text="➕ Dodaj zajęcia",
            command=self.open_add_custom_session_dialog,
            bg=BG_CARD,
            fg=FG_PRIMARY,
            activebackground=BG_ELEVATED,
            activeforeground="#ffffff",
            relief="flat",
            font=("Segoe UI", 9),
            padx=10,
            pady=4,
            cursor="hand2",
        ).pack(side="right", padx=(6, 0))

        tk.Button(
            footer,
            text="📅 Wczytaj / Wklej kalendarz .ics",
            command=self.open_import_ics_dialog,
            bg=ACCENT_INDIGO,
            fg="#ffffff",
            activebackground="#4f46e5",
            activeforeground="#ffffff",
            relief="flat",
            font=("Segoe UI Semibold", 9),
            padx=12,
            pady=4,
            cursor="hand2",
        ).pack(side="right")

    def _on_mousewheel(self, event: tk.Event) -> None:
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass

    def _refresh_course_dropdown(self) -> None:
        courses = [fn for fn, _ in self._get_course_list()]
        self.live_course_combo["values"] = ["Automatycznie (z planu .ics)"] + courses

    # -------------------------------------------------------------------------
    # DIALOG: IMPORT / PASTE .ICS CALENDAR FILE, TEXT OR URL
    # -------------------------------------------------------------------------
    def open_import_ics_dialog(self) -> None:
        dlg = self._open_singleton_dialog("Wczytaj lub wklej własny kalendarz .ics", "540x430")
        if dlg is None:
            return

        active_ics = resolve_active_ics_path(self.config, base_dir=BASE_DIR)
        active_name = active_ics.name if active_ics else "Brak"

        tk.Label(
            dlg,
            text="Wczytaj lub wklej własny plik kalendarza (.ics)",
            font=("Segoe UI Semibold", 11),
            bg=BG_CARD,
            fg=FG_PRIMARY,
        ).pack(anchor="w", padx=18, pady=(16, 2))

        tk.Label(
            dlg,
            text=f"Aktualnie używany plik: {active_name}",
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=GREEN_FG,
        ).pack(anchor="w", padx=18, pady=(0, 12))

        quick_row = tk.Frame(dlg, bg=BG_CARD)
        quick_row.pack(fill="x", padx=18, pady=(0, 10))

        text_box = tk.Text(
            dlg,
            bg=BG_INPUT,
            fg=FG_PRIMARY,
            insertbackground=FG_PRIMARY,
            relief="flat",
            font=("Consolas", 9),
            height=9,
            wrap="word",
        )

        only_remote_var = tk.BooleanVar(value=bool(self.config.get("only_remote_classes", True)))

        def _close_dlg() -> None:
            self._active_dialog = None
            dlg.destroy()

        def _apply_imported_ics(saved_path: Path) -> None:
            self.config["ics_file"] = str(saved_path)
            self.config["only_remote_classes"] = bool(only_remote_var.get())

            events = parse_remote_ics_events(self.config, base_dir=BASE_DIR, include_cancelled=True)
            if not events and only_remote_var.get():
                if messagebox.askyesno(
                    "Brak oznaczeń 'zdalne / W_0RBI' w pliku .ics",
                    f"W pliku '{saved_path.name}' nie znaleziono słów kluczowych 'Budynek sal wirtualnych / W_0RBI'.\n\n"
                    "Czy chcesz wczytać WSZYSTKIE zajęcia z tego pliku kalendarza?",
                    parent=dlg,
                ):
                    self.config["only_remote_classes"] = False
                    events = parse_remote_ics_events(self.config, base_dir=BASE_DIR, include_cancelled=True)

            sync_courses_from_ics_events(self.config, events)
            self._save_config()
            self.refresh_cards()
            _close_dlg()
            messagebox.showinfo(
                "Kalendarz .ics załadowany",
                f"Załadowano kalendarz: {saved_path.name}\n"
                f"Liczba rozpoznanych sesji zajęć: {len(events)}",
                parent=self.root,
            )

        def on_paste_clipboard() -> None:
            copied_files = get_clipboard_files_win32()
            ics_files = [f for f in copied_files if f.lower().endswith(".ics")]
            if ics_files:
                try:
                    saved = import_ics_source(ics_files[0], BASE_DIR)
                    _apply_imported_ics(saved)
                    return
                except Exception as exc:
                    messagebox.showerror("Błąd wklejania pliku", str(exc), parent=dlg)
                    return

            try:
                clip_text = self.root.clipboard_get().strip()
            except Exception:
                clip_text = ""

            if not clip_text:
                messagebox.showwarning(
                    "Pusty schowek",
                    "Schowek nie zawiera pliku .ics ani tekstu kalendarza.\n"
                    "Skopiuj plik .ics (Ctrl+C w Eksploratorze) lub jego tekst/link i spróbuj ponownie.",
                    parent=dlg,
                )
                return

            text_box.delete("1.0", "end")
            text_box.insert("1.0", clip_text)
            if (
                "BEGIN:VCALENDAR" in clip_text.upper()
                or clip_text.lower().endswith(".ics")
                or clip_text.lower().startswith(("http://", "https://", "webcal://"))
            ):
                try:
                    saved = import_ics_source(clip_text, BASE_DIR)
                    _apply_imported_ics(saved)
                except Exception as exc:
                    messagebox.showerror("Błąd importu ze schowka", str(exc), parent=dlg)

        def on_browse_file() -> None:
            chosen = filedialog.askopenfilename(
                parent=dlg,
                title="Wybierz plik kalendarza .ics",
                initialdir=str(Path.home() / "Downloads"),
                filetypes=[("Kalendarz iCalendar (.ics)", "*.ics"), ("Wszystkie pliki", "*.*")],
            )
            if not chosen:
                return
            try:
                saved = import_ics_source(chosen, BASE_DIR)
                _apply_imported_ics(saved)
            except Exception as exc:
                messagebox.showerror("Błąd wczytywania pliku .ics", str(exc), parent=dlg)

        tk.Button(
            quick_row,
            text="📋 Wklej ze schowka (Ctrl+V — skopiowany plik .ics, tekst lub link)",
            command=on_paste_clipboard,
            bg=ACCENT_INDIGO,
            fg="#ffffff",
            activebackground="#4f46e5",
            activeforeground="#ffffff",
            relief="flat",
            font=("Segoe UI Semibold", 9),
            padx=12,
            pady=6,
            cursor="hand2",
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            quick_row,
            text="📂 Wybierz plik z dysku...",
            command=on_browse_file,
            bg=BG_ELEVATED,
            fg=FG_PRIMARY,
            activebackground=BORDER_SUBTLE,
            activeforeground="#ffffff",
            relief="flat",
            font=("Segoe UI", 9),
            padx=12,
            pady=6,
            cursor="hand2",
        ).pack(side="left")

        tk.Label(
            dlg,
            text="Lub wklej poniżej zawartość pliku .ics (BEGIN:VCALENDAR...), ścieżkę pliku albo link URL:",
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=FG_SECONDARY,
        ).pack(anchor="w", padx=18, pady=(4, 4))

        text_box.pack(fill="both", expand=True, padx=18, pady=(0, 8))

        tk.Checkbutton(
            dlg,
            text="Uwzględniaj tylko zajęcia zdalne (Budynek sal wirtualnych / W_0RBI / Teams / Online)",
            variable=only_remote_var,
            bg=BG_CARD,
            fg=FG_PRIMARY,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=FG_PRIMARY,
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=16, pady=(0, 8))

        bottom_row = tk.Frame(dlg, bg=BG_CARD)
        bottom_row.pack(fill="x", padx=18, pady=(4, 14))

        def on_save_from_textbox() -> None:
            raw = text_box.get("1.0", "end").strip()
            if not raw:
                self.config["only_remote_classes"] = bool(only_remote_var.get())
                events = parse_remote_ics_events(self.config, base_dir=BASE_DIR, include_cancelled=True)
                sync_courses_from_ics_events(self.config, events)
                self._save_config()
                self.refresh_cards()
                _close_dlg()
                return
            try:
                saved = import_ics_source(raw, BASE_DIR)
                _apply_imported_ics(saved)
            except Exception as exc:
                messagebox.showerror("Błąd importu kalendarza .ics", str(exc), parent=dlg)

        tk.Button(
            bottom_row,
            text="💾 Zapisz i załaduj kalendarz",
            command=on_save_from_textbox,
            bg=GREEN_BG,
            fg=GREEN_FG,
            relief="flat",
            font=("Segoe UI Semibold", 9),
            padx=14,
            pady=6,
            cursor="hand2",
        ).pack(side="left")

        tk.Button(
            bottom_row,
            text="Anuluj",
            command=_close_dlg,
            bg=BG_ELEVATED,
            fg=FG_PRIMARY,
            relief="flat",
            font=("Segoe UI", 9),
            padx=12,
            pady=6,
            cursor="hand2",
        ).pack(side="right")

    # -------------------------------------------------------------------------
    # WEEK & VIEW MODE NAVIGATION
    # -------------------------------------------------------------------------
    def _prev_week(self) -> None:
        self.current_week_monday -= timedelta(days=7)
        self._set_view_mode("WEEK")

    def _next_week(self) -> None:
        self.current_week_monday += timedelta(days=7)
        self._set_view_mode("WEEK")

    def _go_to_current_week(self) -> None:
        self.current_week_monday = get_monday_of_week(datetime.now().date())
        self._set_view_mode("WEEK")

    def _set_view_mode(self, mode: str) -> None:
        self.view_mode = mode
        for code, btn in self.mode_buttons.items():
            if code == mode:
                btn.configure(bg=ACCENT_INDIGO, fg="#ffffff")
            else:
                btn.configure(bg=BG_CARD, fg=FG_SECONDARY)
        self.refresh_cards()

    # -------------------------------------------------------------------------
    # RENDER SESSION CARDS
    # -------------------------------------------------------------------------
    def refresh_cards(self) -> None:
        self.config = self._load_config()
        self._refresh_course_dropdown()
        self.events_cache = parse_remote_ics_events(self.config, base_dir=BASE_DIR, include_cancelled=True)
        self.mode_buttons["ALL"].configure(text=f"Wszystkie ({len(self.events_cache)})")

        now = datetime.now()
        n_rec = sum(1 for e in self.events_cache if len(e.recordings) > 0 and not e.is_cancelled)
        n_miss = sum(1 for e in self.events_cache if len(e.recordings) == 0 and e.end_dt < now and not e.is_cancelled)
        n_up = sum(1 for e in self.events_cache if len(e.recordings) == 0 and e.end_dt >= now and not e.is_cancelled)
        self.footer_stats_lbl.configure(
            text=f"✔ Mam nagranie: {n_rec}   •   ✖ Brak nagrania: {n_miss}   •   🕒 W planie: {n_up}"
        )

        week_sun = self.current_week_monday + timedelta(days=6)
        today_monday = get_monday_of_week(now.date())
        week_prefix = "Ten tydzień: " if self.current_week_monday == today_monday else "Tydzień: "
        self.week_label.configure(
            text=f"{week_prefix}{self.current_week_monday.strftime('%d.%m')} – {week_sun.strftime('%d.%m.%Y')}"
        )

        filtered: List[IcsRemoteEvent] = []
        for ev in self.events_cache:
            has_rec = len(ev.recordings) > 0
            is_past = ev.end_dt < now
            if self.view_mode == "WEEK":
                if self.current_week_monday <= ev.start_dt.date() <= week_sun:
                    filtered.append(ev)
            elif self.view_mode == "MISSING":
                if not has_rec and is_past and not ev.is_cancelled:
                    filtered.append(ev)
            elif self.view_mode == "RECORDED":
                if has_rec:
                    filtered.append(ev)
            else:
                filtered.append(ev)

        for child in self.cards_container.winfo_children():
            child.destroy()

        if not filtered:
            empty_msg = {
                "WEEK": "Brak zajęć zdalnych w tym tygodniu.",
                "MISSING": "Świetnie! Nie masz żadnych zaległych zajęć bez nagrania.",
                "RECORDED": "Nie masz jeszcze żadnych zapisanych nagrań MP3.",
                "ALL": "Brak zajęć w kalendarzu. Kliknij '📅 Wczytaj / Wklej kalendarz .ics' na dole.",
            }.get(self.view_mode, "Brak zajęć.")
            tk.Label(
                self.cards_container,
                text=empty_msg,
                font=("Segoe UI", 10),
                bg=BG_MAIN,
                fg=FG_MUTED,
                pady=40,
            ).pack(fill="x")
            return

        for ev in filtered:
            self._create_session_card(ev, now)

        self.canvas.yview_moveto(0.0)

    def _create_session_card(self, ev: IcsRemoteEvent, now: datetime) -> None:
        has_rec = len(ev.recordings) > 0
        is_past = ev.end_dt < now
        is_today = ev.start_dt.date() == now.date()

        border_col = ACCENT_INDIGO if is_today else BORDER_SUBTLE
        card = tk.Frame(
            self.cards_container,
            bg=BG_CARD,
            highlightbackground=border_col,
            highlightthickness=1,
        )
        card.pack(fill="x", pady=4, ipady=4)

        inner = tk.Frame(card, bg=BG_CARD)
        inner.pack(fill="x", padx=14, pady=8)

        if ev.is_cancelled:
            badge_bg, badge_fg, badge_text = BG_ELEVATED, FG_MUTED, "🚫 ODWOŁANE"
        elif has_rec:
            badge_bg, badge_fg, badge_text = GREEN_BG, GREEN_FG, "✔ MAM NAGRANIE"
        elif is_past:
            badge_bg, badge_fg, badge_text = RED_BG, RED_FG, "✖ BRAK NAGRANIA"
        else:
            badge_bg, badge_fg, badge_text = BLUE_BG, BLUE_FG, "🕒 W PLANIE"

        badge = tk.Label(
            inner,
            text=badge_text,
            font=("Segoe UI Semibold", 9),
            bg=badge_bg,
            fg=badge_fg,
            width=16,
            pady=6,
        )
        badge.pack(side="left", padx=(0, 14))

        mid = tk.Frame(inner, bg=BG_CARD)
        mid.pack(side="left", fill="both", expand=True)

        day_name = PL_DAYS_FULL[ev.start_dt.weekday()]
        today_tag = " (DZISIAJ)" if is_today else ""
        date_time_str = f"{day_name}, {ev.start_dt.strftime('%d.%m.%Y')}{today_tag}   •   {ev.start_dt.strftime('%H:%M')} – {ev.end_dt.strftime('%H:%M')}"

        course_title = self._get_course_display_name(ev.course_folder)
        if ev.class_type and not ev.is_substituted:
            type_prefix = {"W": "Wykład", "A": "Ćwiczenia", "ZAST": "Zastępstwo"}.get(ev.class_type, ev.class_type)
            main_line_text = f"{course_title}  ({type_prefix})"
        else:
            main_line_text = course_title

        top_row = tk.Frame(mid, bg=BG_CARD)
        top_row.pack(anchor="w", fill="x")

        tk.Label(
            top_row,
            text=main_line_text,
            font=("Segoe UI Semibold", 10),
            bg=BG_CARD,
            fg=AMBER_FG if ev.is_substituted else FG_PRIMARY,
        ).pack(side="left")

        if ev.is_substituted:
            tk.Label(
                top_row,
                text=f" 🔁 Zastępstwo (w planie było: {ev.original_summary}) ",
                font=("Segoe UI", 8),
                bg=AMBER_BG,
                fg=AMBER_FG,
            ).pack(side="left", padx=(8, 0))
        elif ev.cadence == "Co 2 tygodnie":
            tk.Label(
                top_row,
                text=" Co 2 tygodnie ",
                font=("Segoe UI", 8),
                bg=BG_ELEVATED,
                fg=FG_SECONDARY,
            ).pack(side="left", padx=(8, 0))

        sub_parts = [date_time_str]
        if has_rec:
            sub_parts.append(f"🎵 {ev.recordings[-1].name}")
        elif ev.note:
            sub_parts.append(f"💬 {ev.note}")

        tk.Label(
            mid,
            text="   |   ".join(sub_parts),
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=GREEN_FG if has_rec else FG_SECONDARY,
        ).pack(anchor="w", pady=(2, 0))

        right_btns = tk.Frame(inner, bg=BG_CARD)
        right_btns.pack(side="right")

        if has_rec:
            tk.Button(
                right_btns,
                text="▶ Odtwórz",
                command=lambda p=ev.recordings[-1]: os.startfile(str(p)) if p.exists() else None,
                bg=GREEN_BG,
                fg=GREEN_FG,
                activebackground="#047857",
                activeforeground="#ffffff",
                relief="flat",
                font=("Segoe UI Semibold", 9),
                padx=10,
                pady=4,
                cursor="hand2",
            ).pack(side="left", padx=(0, 6))

        tk.Button(
            right_btns,
            text="🔁 Zastępstwo / Edytuj",
            command=lambda e=ev: self.open_edit_session_dialog(e),
            bg=BG_ELEVATED,
            fg=FG_PRIMARY,
            activebackground=ACCENT_INDIGO,
            activeforeground="#ffffff",
            relief="flat",
            font=("Segoe UI", 9),
            padx=10,
            pady=4,
            cursor="hand2",
        ).pack(side="left")

    # -------------------------------------------------------------------------
    # DIALOG: EDIT / SUBSTITUTE SESSION
    # -------------------------------------------------------------------------
    def open_edit_session_dialog(self, ev: IcsRemoteEvent) -> None:
        dlg = self._open_singleton_dialog("Zastępstwo / Edycja zajęć", "440x360")
        if dlg is None:
            return

        def _close_dlg() -> None:
            self._active_dialog = None
            dlg.destroy()

        tk.Label(
            dlg,
            text=f"{PL_DAYS_FULL[ev.start_dt.weekday()]}, {ev.start_dt.strftime('%d.%m.%Y  %H:%M')}–{ev.end_dt.strftime('%H:%M')}",
            font=("Segoe UI Semibold", 11),
            bg=BG_CARD,
            fg=FG_PRIMARY,
        ).pack(anchor="w", padx=18, pady=(16, 2))

        tk.Label(
            dlg,
            text=f"Oryginalnie w kalendarzu: {ev.original_summary} ({ev.cadence})",
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=FG_SECONDARY,
        ).pack(anchor="w", padx=18, pady=(0, 12))

        tk.Label(
            dlg,
            text="Przypisany przedmiot (zmień jeśli było zastępstwo):",
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=FG_SECONDARY,
        ).pack(anchor="w", padx=18, pady=(2, 4))

        course_names = [fn for fn, _ in self._get_course_list()]
        course_var = tk.StringVar(value=ev.course_folder)
        ttk.Combobox(
            dlg,
            textvariable=course_var,
            values=course_names,
            style="Dark.TCombobox",
            font=("Segoe UI", 10),
        ).pack(fill="x", padx=18, pady=(0, 10))

        tk.Label(
            dlg,
            text="Notatka (opcjonalnie):",
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=FG_SECONDARY,
        ).pack(anchor="w", padx=18, pady=(2, 4))

        note_var = tk.StringVar(value=ev.note)
        tk.Entry(
            dlg,
            textvariable=note_var,
            bg=BG_INPUT,
            fg=FG_PRIMARY,
            insertbackground=FG_PRIMARY,
            relief="flat",
            font=("Segoe UI", 10),
        ).pack(fill="x", padx=18, pady=(0, 10), ipady=4)

        cancel_var = tk.BooleanVar(value=ev.is_cancelled)
        tk.Checkbutton(
            dlg,
            text="Zajęcia zostały odwołane (nie wymagaj nagrania)",
            variable=cancel_var,
            bg=BG_CARD,
            fg=FG_PRIMARY,
            selectcolor=BG_INPUT,
            activebackground=BG_CARD,
            activeforeground=FG_PRIMARY,
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=16, pady=2)

        move_var = tk.BooleanVar(value=True)
        if ev.recordings:
            tk.Checkbutton(
                dlg,
                text="Przenieś nagrany plik MP3 do folderu nowego przedmiotu",
                variable=move_var,
                bg=BG_CARD,
                fg=GREEN_FG,
                selectcolor=BG_INPUT,
                activebackground=BG_CARD,
                activeforeground=GREEN_FG,
                font=("Segoe UI", 9),
            ).pack(anchor="w", padx=16, pady=2)

        btn_row = tk.Frame(dlg, bg=BG_CARD)
        btn_row.pack(fill="x", padx=18, pady=(16, 12))

        def on_save() -> None:
            new_c = sanitize_folder_name(course_var.get().strip() or ev.original_course_folder)
            overrides = self.config.setdefault("session_overrides", {})
            overrides[ev.session_id] = {
                "course_folder": new_c,
                "note": note_var.get().strip(),
                "cancelled": bool(cancel_var.get()),
            }
            self._save_config()

            if ev.recordings and move_var.get() and new_c.lower() != ev.course_folder.lower():
                for rfile in list(ev.recordings):
                    reassign_recording_course(self.config, BASE_DIR, rfile, new_c)

            self.refresh_cards()
            _close_dlg()

        def on_reset() -> None:
            overrides = self.config.get("session_overrides", {})
            if ev.session_id in overrides:
                del overrides[ev.session_id]
                self._save_config()
            self.refresh_cards()
            _close_dlg()

        tk.Button(
            btn_row,
            text="💾 Zapisz",
            command=on_save,
            bg=ACCENT_INDIGO,
            fg="#ffffff",
            relief="flat",
            font=("Segoe UI Semibold", 9),
            padx=14,
            pady=6,
            cursor="hand2",
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            btn_row,
            text="Przywróć oryginał",
            command=on_reset,
            bg=BG_ELEVATED,
            fg=FG_SECONDARY,
            relief="flat",
            font=("Segoe UI", 9),
            padx=10,
            pady=6,
            cursor="hand2",
        ).pack(side="left")

        tk.Button(
            btn_row,
            text="Anuluj",
            command=_close_dlg,
            bg=BG_ELEVATED,
            fg=FG_PRIMARY,
            relief="flat",
            font=("Segoe UI", 9),
            padx=10,
            pady=6,
            cursor="hand2",
        ).pack(side="right")

    def open_add_custom_session_dialog(self) -> None:
        dlg = self._open_singleton_dialog("Dodaj dodatkowe zajęcia / odrabianie", "420x330")
        if dlg is None:
            return

        def _close_dlg() -> None:
            self._active_dialog = None
            dlg.destroy()

        tk.Label(
            dlg,
            text="Dodaj dodatkowe zajęcia zdalne",
            font=("Segoe UI Semibold", 11),
            bg=BG_CARD,
            fg=FG_PRIMARY,
        ).pack(anchor="w", padx=18, pady=(16, 12))

        tk.Label(dlg, text="Data (RRRR-MM-DD):", font=("Segoe UI", 9), bg=BG_CARD, fg=FG_SECONDARY).pack(anchor="w", padx=18)
        date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        tk.Entry(dlg, textvariable=date_var, bg=BG_INPUT, fg=FG_PRIMARY, insertbackground=FG_PRIMARY, relief="flat", font=("Segoe UI", 10)).pack(fill="x", padx=18, pady=(2, 8), ipady=4)

        t_row = tk.Frame(dlg, bg=BG_CARD)
        t_row.pack(fill="x", padx=18, pady=(2, 8))
        tk.Label(t_row, text="Od (GG:MM):", font=("Segoe UI", 9), bg=BG_CARD, fg=FG_SECONDARY).pack(side="left")
        s_var = tk.StringVar(value="16:00")
        tk.Entry(t_row, textvariable=s_var, bg=BG_INPUT, fg=FG_PRIMARY, insertbackground=FG_PRIMARY, relief="flat", width=8, font=("Segoe UI", 10)).pack(side="left", padx=(6, 16), ipady=4)
        tk.Label(t_row, text="Do (GG:MM):", font=("Segoe UI", 9), bg=BG_CARD, fg=FG_SECONDARY).pack(side="left")
        e_var = tk.StringVar(value="17:30")
        tk.Entry(t_row, textvariable=e_var, bg=BG_INPUT, fg=FG_PRIMARY, insertbackground=FG_PRIMARY, relief="flat", width=8, font=("Segoe UI", 10)).pack(side="left", padx=6, ipady=4)

        tk.Label(dlg, text="Przedmiot (wybierz lub wpisz nowy):", font=("Segoe UI", 9), bg=BG_CARD, fg=FG_SECONDARY).pack(anchor="w", padx=18)
        course_names = [fn for fn, _ in self._get_course_list()]
        c_var = tk.StringVar(value=course_names[0] if course_names else "Dodatkowe_Zajecia")
        ttk.Combobox(dlg, textvariable=c_var, values=course_names, style="Dark.TCombobox", font=("Segoe UI", 10)).pack(fill="x", padx=18, pady=(2, 14))

        def on_add() -> None:
            try:
                dt_s = datetime.strptime(f"{date_var.get().strip()} {s_var.get().strip()}", "%Y-%m-%d %H:%M")
                datetime.strptime(f"{date_var.get().strip()} {e_var.get().strip()}", "%Y-%m-%d %H:%M")
            except ValueError:
                messagebox.showerror("Błędny format", "Podaj datę RRRR-MM-DD oraz godziny GG:MM.", parent=dlg)
                return

            c_folder = sanitize_folder_name(c_var.get().strip())
            self.config.setdefault("custom_sessions", []).append(
                {
                    "date": date_var.get().strip(),
                    "start": s_var.get().strip(),
                    "end": e_var.get().strip(),
                    "course_folder": c_folder,
                    "summary": f"ZAST - {c_folder.replace('_', ' ')}",
                    "note": "Dodatkowy termin",
                }
            )
            self._save_config()
            self.current_week_monday = get_monday_of_week(dt_s.date())
            self.refresh_cards()
            _close_dlg()

        tk.Button(
            dlg,
            text="➕ Dodaj do kalendarza",
            command=on_add,
            bg=ACCENT_INDIGO,
            fg="#ffffff",
            relief="flat",
            font=("Segoe UI Semibold", 9),
            padx=14,
            pady=6,
            cursor="hand2",
        ).pack(anchor="w", padx=18)

    # -------------------------------------------------------------------------
    # RECORDING & BACKGROUND TEAMS WATCHER
    # -------------------------------------------------------------------------
    def _get_live_course_override(self) -> Optional[str]:
        val = self.live_course_var.get().strip()
        if not val or val.startswith("Automatycznie"):
            return None
        return sanitize_folder_name(val)

    def _on_live_course_selected(self, event: Any = None) -> None:
        override = self._get_live_course_override()
        if self.recorder.is_recording and override:
            self.active_course = CourseMatch(
                course_name=override,
                match_source="Zmieniono ręcznie w trakcie nagrania",
                meeting_title=override,
            )

    def open_recordings_root(self) -> None:
        out_root = resolve_output_root(self.config, base_dir=BASE_DIR)
        out_root.mkdir(parents=True, exist_ok=True)
        os.startfile(str(out_root))

    def toggle_manual_recording(self) -> None:
        with self._stop_lock:
            if not self.recorder.is_recording:
                self._start_new_recording(manual=True)
            else:
                self._stop_and_save_recording(discard_short=False)

    def _start_new_recording(self, manual: bool = False) -> None:
        if self.recorder.is_recording:
            return
        self.started_at = datetime.now()
        override = self._get_live_course_override()
        self.active_course = match_course(
            self.config,
            meeting_title=self.last_detection.meeting_title,
            when=self.started_at,
            manual_course=override,
            base_dir=BASE_DIR,
        )
        _, self.active_file = build_recording_paths(
            self.config, BASE_DIR, self.active_course, started_at=self.started_at
        )
        self.manual_recording_active = manual
        self.recorder.start(self.active_file)
        self.meeting_seen_since = None
        self.meeting_lost_since = None

    def _stop_and_save_recording(self, discard_short: bool = True) -> None:
        if not self.recorder.is_recording:
            return
        ended_at = datetime.now()
        rec_duration = self.recorder.elapsed_seconds
        min_rec_secs = float(self.config.get("detection", {}).get("min_recording_seconds", 5.0))
        discard = discard_short and (rec_duration < min_rec_secs)

        override = self._get_live_course_override()
        if override and self.active_course and override != self.active_course.course_name:
            self.active_course = CourseMatch(
                course_name=override,
                match_source="Zmieniono ręcznie (Zastępstwo)",
                meeting_title=override,
            )

        saved_path = self.recorder.stop(
            discard=discard,
            metadata={
                "title": f"{self.active_course.course_name} - {self.active_course.meeting_title}"
                if self.active_course
                else "Teams Meeting",
                "album": self.active_course.course_name if self.active_course else "Teams",
                "artist": "TeamSREC",
                "date": self.started_at.strftime("%Y-%m-%d") if self.started_at else "",
            },
        )

        if saved_path and self.active_course and self.started_at:
            expected_dir, _ = build_recording_paths(
                self.config, BASE_DIR, self.active_course, started_at=self.started_at
            )
            if saved_path.parent.resolve() != expected_dir.resolve():
                moved = reassign_recording_course(
                    self.config, BASE_DIR, saved_path, self.active_course.course_name
                )
                if moved:
                    saved_path = moved
            else:
                append_recording_metadata(
                    BASE_DIR,
                    saved_path,
                    self.active_course,
                    self.started_at,
                    ended_at,
                    rec_duration,
                )

        self.active_course = None
        self.active_file = None
        self.started_at = None
        self.manual_recording_active = False
        self.meeting_lost_since = None
        self.root.after(100, self.refresh_cards)

    def _background_watcher_loop(self) -> None:
        while not self._quitting:
            try:
                det_cfg = self.config.get("detection", {})
                start_debounce = float(det_cfg.get("start_debounce_seconds", 6.0))
                stop_grace = float(det_cfg.get("stop_grace_period_seconds", 20.0))

                det = detect_teams_meeting(
                    detect_browser=bool(det_cfg.get("detect_browser_teams", True)),
                    require_audio_or_mic_for_browser=bool(
                        det_cfg.get("require_audio_or_mic_for_browser", True)
                    ),
                )
                self.last_detection = det
                now_mono = time.monotonic()

                if self.auto_detect_enabled:
                    with self._stop_lock:
                        if not self.recorder.is_recording:
                            if det.in_meeting:
                                if self.meeting_seen_since is None:
                                    self.meeting_seen_since = now_mono
                                if (now_mono - self.meeting_seen_since) >= start_debounce:
                                    self._start_new_recording(manual=False)
                            else:
                                self.meeting_seen_since = None
                        elif not self.manual_recording_active:
                            if det.in_meeting:
                                self.meeting_lost_since = None
                            else:
                                if self.meeting_lost_since is None:
                                    self.meeting_lost_since = now_mono
                                if (now_mono - self.meeting_lost_since) >= stop_grace:
                                    self._stop_and_save_recording(discard_short=True)
            except Exception:
                pass

            time.sleep(1.5)

    def _ui_tick(self) -> None:
        if self._quitting:
            return

        # Check if a 2nd launch of TeamSREC signaled us to pop up our window
        if self.instance_lock is not None and self.instance_lock.check_wakeup_signal():
            self.show_from_tray()

        if self.recorder.is_recording and self.active_course:
            dur = format_duration(self.recorder.elapsed_seconds)
            c_disp = self._get_course_display_name(self.active_course.course_name)
            self.status_title_lbl.configure(
                text=f"🔴 NAGRYWANIE: {c_disp}  ({dur})",
                fg=RED_FG,
            )
            self.status_desc_lbl.configure(
                text=f"Zapis do folderu: {self.active_course.course_name}/{datetime.now().strftime('%Y-%m-%d')}",
                fg=FG_PRIMARY,
            )
            self.record_btn.configure(text="⏹ Zakończ i zapisz MP3", bg="#b91c1c")
            self._update_tray_icon_state("recording")
        else:
            self.record_btn.configure(text="🔴 Nagraj teraz", bg="#dc2626")
            if self.last_detection.teams_running:
                self.status_title_lbl.configure(
                    text="● Czuwanie — Microsoft Teams jest włączony (poza spotkaniem)",
                    fg=GREEN_FG,
                )
                self.status_desc_lbl.configure(
                    text="Nagrywanie włączy się dopiero wtedy, gdy dołączysz do rozmowy / wykładu",
                    fg=FG_SECONDARY,
                )
            else:
                self.status_title_lbl.configure(
                    text="● Czuwanie w tle (Auto-detekcja spotkań Teams)",
                    fg=GREEN_FG,
                )
                self.status_desc_lbl.configure(
                    text="Gdy dołączysz do spotkania na Teams, nagranie rozpocznie się automatycznie",
                    fg=FG_SECONDARY,
                )
            self._update_tray_icon_state("standby")

        self.root.after(250, self._ui_tick)

    # -------------------------------------------------------------------------
    # SYSTEM TRAY INTEGRATION
    # -------------------------------------------------------------------------
    def _start_tray_icon(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem(
                "Otwórz TeamSREC",
                lambda icon, item: self.root.after(0, self.show_from_tray),
                default=True,
            ),
            pystray.MenuItem(
                "Nagraj teraz / Zatrzymaj",
                lambda icon, item: self.root.after(0, self.toggle_manual_recording),
            ),
            pystray.MenuItem(
                "Wczytaj / Wklej kalendarz .ics",
                lambda icon, item: self.root.after(0, self._open_ics_from_tray),
            ),
            pystray.MenuItem(
                "Otwórz folder nagrań",
                lambda icon, item: self.root.after(0, self.open_recordings_root),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Zamknij całkowicie",
                lambda icon, item: self.root.after(0, self.quit_app),
            ),
        )
        self.tray_icon = pystray.Icon(
            "TeamSREC",
            create_tray_icon_image("standby", size=64),
            "TeamSREC — Czuwanie w tle",
            menu,
        )
        self._last_tray_state = "standby"
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _open_ics_from_tray(self) -> None:
        self.show_from_tray()
        self.open_import_ics_dialog()

    def _update_tray_icon_state(self, state: str) -> None:
        if self.tray_icon is None or state == self._last_tray_state:
            return
        self._last_tray_state = state
        try:
            self.tray_icon.icon = create_tray_icon_image(state, size=64)
            if state == "recording" and self.active_course:
                self.tray_icon.title = f"TeamSREC — NAGRYWANIE: {self.active_course.course_name}"
            else:
                self.tray_icon.title = "TeamSREC — Czuwanie w tle"
        except Exception:
            pass

    def hide_to_tray(self) -> None:
        self.root.withdraw()

    def show_from_tray(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()
        apply_dark_titlebar(self.root)
        self.refresh_cards()

    def quit_app(self) -> None:
        self._quitting = True
        with self._stop_lock:
            if self.recorder.is_recording:
                self._stop_and_save_recording(discard_short=False)
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        if self.instance_lock is not None:
            self.instance_lock.close()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def launch_gui(config_path: Path = DEFAULT_CONFIG_PATH, start_hidden: bool = False) -> int:
    lock = SingleInstanceManager()
    if lock.already_running:
        lock.signal_existing_instance()
        lock.close()
        return 0

    app = TeamsRecApp(config_path=config_path, start_hidden=start_hidden, instance_lock=lock)
    app.run()
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="TeamSREC Minimalist Dark Tray & Calendar GUI")
    parser.add_argument("--minimized", action="store_true", help="Uruchom schowane w zasobniku systemowym (Tray)")
    args = parser.parse_args()

    sys.exit(launch_gui(start_hidden=args.minimized))
