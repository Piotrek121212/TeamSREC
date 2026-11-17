import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import os
import re
from typing import List, Optional
import psutil

try:
    import winreg
except ImportError:
    winreg = None


TEAMS_PROCESS_NAMES = {
    "ms-teams.exe",
    "teams.exe",
    "msteams.exe",
}

BROWSER_PROCESS_NAMES = {
    "msedge.exe",
    "chrome.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "vivaldi.exe",
}

# All standard navigation / non-meeting prefixes in Microsoft Teams (Polish & English)
IDLE_NAV_PREFIXES = {
    # English
    "activity",
    "chat",
    "teams",
    "teams and channels",
    "calendar",
    "calls",
    "files",
    "onedrive",
    "assignments",
    "grades",
    "approvals",
    "shifts",
    "updates",
    "search",
    "settings",
    "apps",
    "people",
    "walkie talkie",
    "general",
    # Polish
    "aktywność",
    "aktywnosc",
    "czat",
    "zespoły",
    "zespoly",
    "zespoły i kanały",
    "zespoly i kanaly",
    "kalendarz",
    "połączenia",
    "polaczenia",
    "pliki",
    "zadania",
    "oceny",
    "zatwierdzenia",
    "wyszukiwanie",
    "ustawienia",
    "aplikacje",
    "ogólny",
    "ogolny",
    " kanał ogólny",
}

IGNORED_EXACT_TITLES = {
    "",
    "microsoft teams",
    "microsoft teams (work or school)",
    "microsoft teams (wersja klasyczna)",
    "microsoft teams classic",
    "microsoft teams - trwa ładowanie...",
    "microsoft teams - loading...",
    "program manager",
    "default ime",
    "msctfime ui",
    "powiadomienie aplikacji microsoft teams",
    "microsoft teams notification",
}


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    process_name: str
    raw_title: str
    clean_title: str
    pipe_segments: List[str]
    is_teams_app: bool
    is_browser_teams: bool
    is_navigation_view: bool
    looks_like_meeting_window: bool


@dataclass
class MeetingDetectionResult:
    in_meeting: bool
    teams_running: bool = False
    meeting_title: str = ""
    confidence_reasons: List[str] = field(default_factory=list)
    mic_active: bool = False
    udp_media_active: bool = False
    teams_windows: List[WindowInfo] = field(default_factory=list)


def _get_process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower()
    except Exception:
        pass
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    return os.path.basename(buf.value).lower()
            finally:
                kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def _strip_notification_prefix(title: str) -> str:
    return re.sub(r"^\(\d+\)\s*", "", title.strip())


def _clean_teams_title(raw_title: str) -> str:
    title = _strip_notification_prefix(raw_title)
    title = re.sub(
        r"\s*[-–—]\s*(Google Chrome|Microsoft\u200b? Edge|Mozilla Firefox|Brave|Opera|Vivaldi).*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s*\|\s*Microsoft Teams.*$", "", title, flags=re.IGNORECASE)
    return title.strip()


def _classify_teams_window(hwnd: int, pid: int, proc_name: str, raw_title: str) -> Optional[WindowInfo]:
    stripped = _strip_notification_prefix(raw_title)
    low_raw = stripped.lower()

    is_teams_app = proc_name in TEAMS_PROCESS_NAMES
    is_browser_teams = (
        proc_name in BROWSER_PROCESS_NAMES and "microsoft teams" in low_raw
    )

    if not is_teams_app and not is_browser_teams:
        return None

    without_browser = re.sub(
        r"\s*[-–—]\s*(Google Chrome|Microsoft\u200b? Edge|Mozilla Firefox|Brave|Opera|Vivaldi).*$",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    parts = [p.strip() for p in without_browser.split("|") if p.strip()]
    first_part_low = parts[0].lower() if parts else ""
    second_part_low = parts[1].lower() if len(parts) >= 2 else ""

    if low_raw in IGNORED_EXACT_TITLES:
        return WindowInfo(
            hwnd=hwnd,
            pid=pid,
            process_name=proc_name,
            raw_title=raw_title,
            clean_title="",
            pipe_segments=parts,
            is_teams_app=is_teams_app,
            is_browser_teams=is_browser_teams,
            is_navigation_view=True,
            looks_like_meeting_window=False,
        )

    # In New Teams (ms-teams.exe):
    # - Main window navigation views have 3 pipe-separated parts:
    #   e.g. "Czat | Jan Kowalski | Microsoft Teams"
    #   e.g. "Zespoły i kanały | Ogólny | Nazwa przedmiotu | Microsoft Teams"
    #   e.g. "Ogólny | Teoretyczne podstawy informatyki | Microsoft Teams"
    #   e.g. "Kalendarz | Microsoft Teams"
    # - A pop-out Meeting Window has 2 pipe-separated parts:
    #   "<Meeting Subject> | Microsoft Teams" where <Meeting Subject> is NOT a nav tab!
    is_nav = False
    if first_part_low in IDLE_NAV_PREFIXES or second_part_low in IDLE_NAV_PREFIXES:
        is_nav = True
    elif len(parts) >= 3 and parts[-1].lower().startswith("microsoft teams"):
        # 3+ segment titles ("<Channel/Chat> | <Team> | Microsoft Teams") are channel/chat views in the main window
        is_nav = True

    clean = _clean_teams_title(raw_title)
    if clean.lower() in IDLE_NAV_PREFIXES or clean.lower() in IGNORED_EXACT_TITLES:
        is_nav = True

    looks_like_meeting = False
    if not is_nav and clean:
        if len(parts) == 2 and parts[-1].lower().startswith("microsoft teams"):
            looks_like_meeting = True
        elif is_teams_app and len(parts) == 1:
            looks_like_meeting = True

    return WindowInfo(
        hwnd=hwnd,
        pid=pid,
        process_name=proc_name,
        raw_title=raw_title,
        clean_title=clean,
        pipe_segments=parts,
        is_teams_app=is_teams_app,
        is_browser_teams=is_browser_teams,
        is_navigation_view=is_nav,
        looks_like_meeting_window=looks_like_meeting,
    )


def enumerate_teams_windows(detect_browser: bool = True) -> List[WindowInfo]:
    results: List[WindowInfo] = []
    user32 = ctypes.windll.user32

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, lparam: int) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            raw_title = buf.value.strip()
            if not raw_title:
                return True

            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc_name = _get_process_name(pid.value)
            if not proc_name:
                return True

            if not detect_browser and proc_name in BROWSER_PROCESS_NAMES:
                return True

            info = _classify_teams_window(hwnd, pid.value, proc_name, raw_title)
            if info is not None:
                results.append(info)
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return results


def is_teams_microphone_active(include_browsers: bool = True) -> bool:
    """
    Checks Windows CapabilityAccessManager ConsentStore for active microphone usage by Teams.
    When you join a Teams meeting (even if muted inside Teams UI), Windows sets LastUsedTimeStop == 0.
    When you are merely browsing Teams (not in a call), LastUsedTimeStop > 0.
    """
    if winreg is None:
        return False

    base_path = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"

    def _check_subkeys(root_key_path: str, match_terms: List[str]) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root_key_path) as root_key:
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(root_key, i)
                        i += 1
                    except OSError:
                        break
                    low_name = subkey_name.lower()
                    if any(term in low_name for term in match_terms):
                        try:
                            with winreg.OpenKey(root_key, subkey_name) as app_key:
                                stop_val, _ = winreg.QueryValueEx(app_key, "LastUsedTimeStop")
                                start_val, _ = winreg.QueryValueEx(app_key, "LastUsedTimeStart")
                                if start_val > 0 and stop_val == 0:
                                    return True
                        except OSError:
                            pass
        except OSError:
            pass
        return False

    if _check_subkeys(base_path, ["msteams", "teams"]):
        return True

    non_packaged_terms = ["teams"]
    if include_browsers:
        non_packaged_terms.extend(["msedge", "chrome", "firefox", "brave"])
    if _check_subkeys(base_path + r"\NonPackaged", non_packaged_terms):
        return True

    return False


def is_teams_udp_media_active() -> bool:
    """
    Checks if Microsoft Teams has active real-time media UDP sockets.
    Note: Idle ms-teams.exe always binds 4 unconnected UDP sockets (2 IPv4 + 2 IPv6).
    During an actual call/meeting, Teams opens 10+ UDP sockets (ICE/STUN/TURN/RTP media streams)
    or connected UDP sockets to remote media relays (ports 3478-3481).
    """
    try:
        teams_pids = {
            p.info["pid"]
            for p in psutil.process_iter(["pid", "name"])
            if p.info.get("name") and p.info["name"].lower() in TEAMS_PROCESS_NAMES
        }
        if not teams_pids:
            return False

        udp_conns = [
            c for c in psutil.net_connections(kind="udp") if c.pid in teams_pids
        ]
        # Check for connected UDP sockets to Teams media relay ports (3478-3481)
        for c in udp_conns:
            if c.raddr and getattr(c.raddr, "port", 0) in (3478, 3479, 3480, 3481):
                return True

        # Or significantly more UDP sockets than the 4 idle listeners (>= 10 sockets)
        return len(udp_conns) >= 10
    except Exception:
        return False


def detect_teams_meeting(
    detect_browser: bool = True,
    require_audio_or_mic_for_browser: bool = True,
) -> MeetingDetectionResult:
    """
    Determines whether the user is actively inside a Microsoft Teams meeting/call,
    specifically avoiding false positives when Teams is merely open in the background or browsing chats/channels.
    """
    windows = enumerate_teams_windows(detect_browser=detect_browser)
    desktop_windows = [w for w in windows if w.is_teams_app]
    browser_windows = [w for w in windows if w.is_browser_teams]

    teams_running = len(desktop_windows) > 0 or len(browser_windows) > 0
    mic_active = is_teams_microphone_active(include_browsers=detect_browser)
    udp_active = is_teams_udp_media_active()

    desktop_meeting_windows = [w for w in desktop_windows if w.looks_like_meeting_window]
    browser_meeting_windows = [w for w in browser_windows if w.looks_like_meeting_window]

    reasons: List[str] = []
    chosen_title = ""

    # Helper to extract best title from open Teams windows
    def _extract_best_title() -> str:
        if desktop_meeting_windows:
            return desktop_meeting_windows[0].clean_title
        for w in desktop_windows:
            # If viewing a channel like "Ogólny | Programowanie | Microsoft Teams", extract the team/course name ("Programowanie")
            if len(w.pipe_segments) >= 3:
                return w.pipe_segments[1]
            if w.clean_title and not w.is_navigation_view:
                return w.clean_title
        if browser_meeting_windows:
            return browser_meeting_windows[0].clean_title
        return ""

    # RULE 1: Teams Microphone Stream is Active (Ground truth on Windows 10/11 when in a call)
    if mic_active and teams_running:
        chosen_title = _extract_best_title() or "Spotkanie_Teams"
        reasons.append("Aktywny mikrofon w Teams")
        if desktop_meeting_windows:
            reasons.append(f"Okno spotkania: '{desktop_meeting_windows[0].clean_title}'")

    # RULE 2: Active UDP Media Stream (10+ UDP sockets or port 3478-3481 relay) while Teams window is open
    elif udp_active and teams_running:
        chosen_title = _extract_best_title() or "Spotkanie_Teams"
        reasons.append("Aktywny strumień audio/wideo UDP Teams")

    # RULE 3: Pop-out Meeting Window is open alongside the Main Teams Window (2+ Teams windows, one is a meeting window)
    elif len(desktop_windows) >= 2 and desktop_meeting_windows:
        chosen_title = desktop_meeting_windows[0].clean_title
        reasons.append(f"Osobne okno spotkania Teams: '{chosen_title}'")

    in_meeting = len(reasons) > 0
    return MeetingDetectionResult(
        in_meeting=in_meeting,
        teams_running=teams_running,
        meeting_title=chosen_title,
        confidence_reasons=reasons,
        mic_active=mic_active,
        udp_media_active=udp_active,
        teams_windows=windows,
    )
