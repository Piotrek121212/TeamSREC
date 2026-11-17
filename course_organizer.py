from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple
import urllib.request


POLISH_TRANSLATION = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)

DAY_MAP = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
    "poniedzialek": 0,
    "pon": 0,
    "pn": 0,
    "wtorek": 1,
    "wt": 1,
    "sroda": 2,
    "sr": 2,
    "czwartek": 3,
    "czw": 3,
    "cz": 3,
    "piatek": 4,
    "pt": 4,
    "sobota": 5,
    "sob": 5,
    "niedziela": 6,
    "nd": 6,
    "ndz": 6,
}

PL_DAYS_FULL = [
    "Poniedziałek",
    "Wtorek",
    "Środa",
    "Czwartek",
    "Piątek",
    "Sobota",
    "Niedziela",
]

GENERIC_MEETING_TITLES = {
    "",
    "teams_meeting",
    "microsoft teams",
    "meeting",
    "spotkanie",
    "general",
    "ogólny",
    "ogolny",
    "call",
    "połączenie",
    "polaczenie",
}

DEFAULT_REMOTE_KEYWORDS = [
    "budynek sal wirtualnych",
    "w_0rbi",
    "wirtualn",
    "zdaln",
    "teams",
    "online",
]


@dataclass
class IcsRemoteEvent:
    session_id: str
    uid: str
    summary: str
    original_summary: str
    course_folder: str
    original_course_folder: str
    class_type: str
    start_dt: datetime
    end_dt: datetime
    description: str
    cadence: str = "Co tydzień"
    is_substituted: bool = False
    is_cancelled: bool = False
    note: str = ""
    recordings: List[Path] = field(default_factory=list)


@dataclass
class CourseMatch:
    course_name: str
    match_source: str
    meeting_title: str


def normalize_text(text: str) -> str:
    return text.translate(POLISH_TRANSLATION).lower().strip()


def sanitize_folder_name(name: str, max_len: int = 60) -> str:
    if not name:
        return "General_Meetings"
    ascii_name = name.translate(POLISH_TRANSLATION)
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", ascii_name.strip())
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._ ")
    if not cleaned:
        return "General_Meetings"
    return cleaned[:max_len]


def _parse_hhmm(time_str: str) -> Optional[int]:
    try:
        parts = time_str.strip().split(":")
        if len(parts) != 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        return h * 60 + m
    except Exception:
        return None


def _parse_ics_datetime(val: str) -> Optional[datetime]:
    val = val.strip().rstrip("Z")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    return None


def _split_usos_summary(summary: str) -> Tuple[str, str]:
    m = re.match(r"^([A-ZŁĆŚŹŻ]{1,4})\s*[-–]\s*(.+)$", summary.strip())
    if m:
        ctype = m.group(1).strip()
        subj = re.sub(r"\s+", " ", m.group(2).strip())
        return ctype, subj
    return "", re.sub(r"\s+", " ", summary.strip())


def resolve_output_root(config: Dict[str, Any], base_dir: Optional[Path] = None) -> Path:
    raw_output_dir = config.get("output_dir", "recordings")
    output_root = Path(str(raw_output_dir))
    if not output_root.is_absolute():
        root_base = base_dir if base_dir is not None else Path(__file__).resolve().parent
        output_root = (root_base / output_root).resolve()
    return output_root


def resolve_active_ics_path(config: Dict[str, Any], base_dir: Optional[Path] = None) -> Optional[Path]:
    candidates: List[Path] = []
    cfg_ics = config.get("ics_file")
    if cfg_ics:
        p = Path(str(cfg_ics))
        if not p.is_absolute() and base_dir is not None:
            candidates.append((base_dir / p).resolve())
        candidates.append(p)
    if base_dir is not None:
        candidates.append(base_dir / "u195091.ics")
        # Also check any .ics file placed inside base_dir
        for f in sorted(base_dir.glob("*.ics")):
            if f not in candidates:
                candidates.append(f)
    candidates.append(Path(r"C:\Users\polom\Downloads\u195091.ics"))
    return next((c for c in candidates if c.exists() and c.is_file()), None)


def import_ics_source(
    raw_input: str,
    base_dir: Path,
    custom_filename: Optional[str] = None,
) -> Path:
    """
    Imports an .ics calendar from:
      1) Raw ICS text ('BEGIN:VCALENDAR...'),
      2) A local file path (copies into base_dir/calendars/), or
      3) An HTTP/HTTPS/webcal URL (downloads into base_dir/calendars/).
    Returns the saved local Path inside base_dir/calendars/.
    """
    cal_dir = base_dir / "calendars"
    cal_dir.mkdir(parents=True, exist_ok=True)
    text = raw_input.strip().strip('"').strip("'")

    if not text:
        raise ValueError("Puste źródło kalendarza .ics.")

    # Case 1: Pasted raw ICS content
    if "BEGIN:VCALENDAR" in text.upper():
        if "END:VCALENDAR" not in text.upper():
            raise ValueError("Wklejony tekst nie zawiera pełnego bloku BEGIN:VCALENDAR ... END:VCALENDAR.")
        fname = custom_filename or f"wklejony_kalendarz_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ics"
        if not fname.lower().endswith(".ics"):
            fname += ".ics"
        dest = cal_dir / sanitize_folder_name(fname.replace(".ics", ""))
        dest = dest.with_suffix(".ics")
        dest.write_text(text, encoding="utf-8")
        return dest

    # Case 2: URL (http://, https://, webcal://)
    if text.lower().startswith(("http://", "https://", "webcal://")):
        url = re.sub(r"^webcal://", "https://", text, flags=re.IGNORECASE)
        req = urllib.request.Request(url, headers={"User-Agent": "TeamsRec/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_bytes = resp.read()
        decoded = content_bytes.decode("utf-8", errors="replace")
        if "BEGIN:VCALENDAR" not in decoded.upper():
            raise ValueError("Pobrany plik z podanego adresu URL nie jest prawidłowym kalendarzem .ics.")
        fname = custom_filename or f"kalendarz_url_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ics"
        dest = (cal_dir / sanitize_folder_name(fname.replace(".ics", ""))).with_suffix(".ics")
        dest.write_text(decoded, encoding="utf-8")
        return dest

    # Case 3: Local file path
    src_path = Path(text).expanduser()
    if not src_path.exists() or not src_path.is_file():
        raise FileNotFoundError(f"Nie znaleziono pliku: {src_path}")

    raw_content = src_path.read_text(encoding="utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in raw_content.upper():
        raise ValueError(f"Plik '{src_path.name}' nie jest prawidłowym plikiem kalendarza .ics (brak BEGIN:VCALENDAR).")

    dest = cal_dir / src_path.name
    if src_path.resolve() != dest.resolve():
        shutil.copy2(str(src_path), str(dest))
    return dest


def sync_courses_from_ics_events(config: Dict[str, Any], events: List[IcsRemoteEvent]) -> bool:
    """
    Ensures any courses present in the loaded .ics calendar exist in config['courses']
    so they appear in dropdowns and keyword matching. Returns True if config was modified.
    """
    courses: List[Dict[str, Any]] = config.setdefault("courses", [])
    existing_folders = {
        sanitize_folder_name(str(c.get("name", ""))).lower()
        for c in courses
        if c.get("name")
    }
    changed = False
    for ev in events:
        cf_low = ev.original_course_folder.lower()
        if cf_low and cf_low not in existing_folders:
            _, subj = _split_usos_summary(ev.original_summary)
            disp = subj or ev.original_course_folder.replace("_", " ")
            courses.append(
                {
                    "name": ev.original_course_folder,
                    "display_name": disp,
                    "keywords": [disp.lower(), ev.original_course_folder.lower().replace("_", " ")],
                    "schedule": [],
                }
            )
            existing_folders.add(cf_low)
            changed = True
    return changed


def _detect_cadences(events: List[IcsRemoteEvent]) -> None:
    grouped: Dict[Tuple[str, int], List[IcsRemoteEvent]] = defaultdict(list)
    for ev in events:
        if ev.cadence == "Dodatkowe":
            continue
        grouped[(ev.original_summary, ev.start_dt.weekday())].append(ev)

    for _, group in grouped.items():
        group.sort(key=lambda e: e.start_dt)
        if len(group) == 1:
            group[0].cadence = "1 termin w semestrze"
            continue
        deltas = [
            (group[i + 1].start_dt.date() - group[i].start_dt.date()).days
            for i in range(len(group) - 1)
        ]
        count_7 = sum(1 for d in deltas if 6 <= d <= 8)
        count_14 = sum(1 for d in deltas if 13 <= d <= 15)
        if count_14 >= count_7 and count_14 > 0:
            label = "Co 2 tygodnie"
        else:
            label = "Co tydzień"
        for ev in group:
            ev.cadence = label


def _attach_existing_recordings(
    events: List[IcsRemoteEvent],
    config: Dict[str, Any],
    base_dir: Optional[Path] = None,
) -> None:
    output_root = resolve_output_root(config, base_dir=base_dir)
    if not output_root.exists():
        return

    files_by_course_date: Dict[Tuple[str, str], List[Path]] = defaultdict(list)
    for course_dir in output_root.iterdir():
        if not course_dir.is_dir():
            continue
        c_key = course_dir.name.lower()
        for date_dir in course_dir.iterdir():
            if not date_dir.is_dir():
                continue
            d_key = date_dir.name
            audio_files = sorted(
                [
                    f
                    for f in date_dir.iterdir()
                    if f.is_file()
                    and f.suffix.lower() in (".mp3", ".wav")
                    and not f.name.endswith(".temp_loopback.wav")
                    and not f.name.endswith(".temp_mic.wav")
                ]
            )
            if audio_files:
                files_by_course_date[(c_key, d_key)].extend(audio_files)

    events_by_cd: Dict[Tuple[str, str], List[IcsRemoteEvent]] = defaultdict(list)
    for ev in events:
        d_str = ev.start_dt.strftime("%Y-%m-%d")
        events_by_cd[(ev.course_folder.lower(), d_str)].append(ev)

    for (c_key, d_str), ev_list in events_by_cd.items():
        candidate_files = files_by_course_date.get((c_key, d_str), [])
        if not candidate_files:
            continue
        if len(ev_list) == 1:
            ev_list[0].recordings = list(candidate_files)
        else:
            for f in candidate_files:
                m = re.match(r"^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})", f.name)
                if m:
                    try:
                        f_dt = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y-%m-%d_%H-%M-%S")
                        best_ev = min(
                            ev_list,
                            key=lambda e: abs((e.start_dt - f_dt).total_seconds()),
                        )
                        best_ev.recordings.append(f)
                        continue
                    except ValueError:
                        pass
                ev_list[0].recordings.append(f)


def parse_remote_ics_events(
    config: Dict[str, Any],
    base_dir: Optional[Path] = None,
    include_cancelled: bool = True,
) -> List[IcsRemoteEvent]:
    ics_path = resolve_active_ics_path(config, base_dir=base_dir)

    only_remote = bool(config.get("only_remote_classes", True))
    remote_kws = [
        normalize_text(k)
        for k in config.get("remote_location_keywords", DEFAULT_REMOTE_KEYWORDS)
    ]
    overrides: Dict[str, Dict[str, Any]] = config.get("session_overrides", {})
    custom_sessions: List[Dict[str, Any]] = config.get("custom_sessions", [])

    events: List[IcsRemoteEvent] = []

    if ics_path is not None:
        try:
            raw_text = ics_path.read_text(encoding="utf-8", errors="replace")
            unfolded = re.sub(r"\r?\n[ \t]", "", raw_text)
            lines = unfolded.splitlines()

            in_event = False
            cur: Dict[str, str] = {}

            for line in lines:
                stripped = line.strip()
                if stripped == "BEGIN:VEVENT":
                    in_event = True
                    cur = {}
                elif stripped == "END:VEVENT":
                    if in_event:
                        summary = cur.get("SUMMARY", "").replace(r"\,", ",").strip()
                        desc = (
                            cur.get("DESCRIPTION", "")
                            .replace(r"\n", "\n")
                            .replace(r"\,", ",")
                            .strip()
                        )
                        loc = cur.get("LOCATION", "").strip()
                        uid = cur.get("UID", "").strip()
                        dtstart = _parse_ics_datetime(cur.get("DTSTART", ""))
                        dtend = _parse_ics_datetime(cur.get("DTEND", ""))

                        if summary and dtstart and dtend:
                            combined_meta = normalize_text(f"{summary} {desc} {loc}")
                            is_remote = any(kw in combined_meta for kw in remote_kws)
                            if not only_remote or is_remote:
                                ctype, subj = _split_usos_summary(summary)
                                course_folder = sanitize_folder_name(subj)
                                for c in config.get("courses", []):
                                    c_disp = normalize_text(str(c.get("display_name", "")))
                                    c_name = str(c.get("name", ""))
                                    if c_disp and c_disp == normalize_text(subj):
                                        course_folder = sanitize_folder_name(c_name)
                                        break

                                session_id = f"{dtstart.strftime('%Y-%m-%d_%H:%M')}"
                                ev = IcsRemoteEvent(
                                    session_id=session_id,
                                    uid=uid or session_id,
                                    summary=summary,
                                    original_summary=summary,
                                    course_folder=course_folder,
                                    original_course_folder=course_folder,
                                    class_type=ctype,
                                    start_dt=dtstart,
                                    end_dt=dtend,
                                    description=desc,
                                )
                                events.append(ev)
                    in_event = False
                elif in_event and ":" in stripped:
                    key_part, val_part = stripped.split(":", 1)
                    base_key = key_part.split(";", 1)[0].upper()
                    cur[base_key] = val_part
        except Exception:
            pass

    _detect_cadences(events)

    for ev in events:
        ov = overrides.get(ev.session_id)
        if ov:
            new_course = str(ov.get("course_folder", "")).strip()
            new_summary = str(ov.get("summary", "")).strip()
            note = str(ov.get("note", "")).strip()
            cancelled = bool(ov.get("cancelled", False))

            if new_course and sanitize_folder_name(new_course) != ev.original_course_folder:
                ev.course_folder = sanitize_folder_name(new_course)
                ev.is_substituted = True
            if new_summary:
                ev.summary = new_summary
                ev.is_substituted = True
            elif ev.is_substituted:
                ev.summary = f"[Zastępstwo -> {ev.course_folder}] (było: {ev.original_summary})"
            ev.is_cancelled = cancelled
            ev.note = note

    for cs in custom_sessions:
        try:
            date_str = str(cs.get("date", "")).strip()
            start_str = str(cs.get("start", "10:00")).strip()
            end_str = str(cs.get("end", "11:30")).strip()
            course_folder = sanitize_folder_name(str(cs.get("course_folder", "Dodatkowe_Zajecia")))
            summary = str(cs.get("summary", f"Zastępstwo / Dodatkowe - {course_folder}")).strip()
            note = str(cs.get("note", "Dodane ręcznie")).strip()

            dtstart = datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %H:%M")
            dtend = datetime.strptime(f"{date_str} {end_str}", "%Y-%m-%d %H:%M")
            session_id = f"{dtstart.strftime('%Y-%m-%d_%H:%M')}"

            events.append(
                IcsRemoteEvent(
                    session_id=session_id,
                    uid=f"custom-{session_id}",
                    summary=summary,
                    original_summary=summary,
                    course_folder=course_folder,
                    original_course_folder=course_folder,
                    class_type="ZAST",
                    start_dt=dtstart,
                    end_dt=dtend,
                    description=note,
                    cadence="Dodatkowe / Zastępstwo",
                    is_substituted=True,
                    is_cancelled=False,
                    note=note,
                )
            )
        except Exception:
            continue

    if not include_cancelled:
        events = [e for e in events if not e.is_cancelled]

    events.sort(key=lambda e: e.start_dt)
    _attach_existing_recordings(events, config, base_dir=base_dir)
    return events


def get_upcoming_or_active_ics_events(
    config: Dict[str, Any],
    when: Optional[datetime] = None,
    limit: int = 5,
    base_dir: Optional[Path] = None,
) -> List[IcsRemoteEvent]:
    if when is None:
        when = datetime.now()
    all_remote = parse_remote_ics_events(config, base_dir=base_dir, include_cancelled=False)
    upcoming = [e for e in all_remote if e.end_dt >= when]
    return upcoming[:limit]


def match_course(
    config: Dict[str, Any],
    meeting_title: str = "",
    when: Optional[datetime] = None,
    manual_course: Optional[str] = None,
    base_dir: Optional[Path] = None,
) -> CourseMatch:
    if when is None:
        when = datetime.now()

    if manual_course:
        return CourseMatch(
            course_name=sanitize_folder_name(manual_course),
            match_source="manual (wybrano ręcznie / zastępstwo)",
            meeting_title=meeting_title or manual_course,
        )

    courses: List[Dict[str, Any]] = config.get("courses", [])
    detection_cfg: Dict[str, Any] = config.get("detection", {})
    buffer_mins: int = int(detection_cfg.get("schedule_buffer_minutes", 20))
    use_fallback: bool = bool(detection_cfg.get("use_window_title_as_course_fallback", True))
    strict_ics_dates: bool = bool(config.get("strict_ics_dates", True))

    ics_events = parse_remote_ics_events(config, base_dir=base_dir, include_cancelled=False)
    buf_delta = timedelta(minutes=buffer_mins)
    for ev in ics_events:
        if (ev.start_dt - buf_delta) <= when <= (ev.end_dt + buf_delta):
            type_label = f"{ev.class_type}_" if ev.class_type else ""
            title_used = (
                meeting_title
                if (meeting_title and normalize_text(meeting_title) not in GENERIC_MEETING_TITLES)
                else f"{type_label}{ev.course_folder}"
            )
            sub_tag = " [ZASTĘPSTWO]" if ev.is_substituted else f" [{ev.cadence}]"
            return CourseMatch(
                course_name=ev.course_folder,
                match_source=f"Kalendarz .ics{sub_tag} ({ev.summary}, {ev.start_dt.strftime('%H:%M')}-{ev.end_dt.strftime('%H:%M')})",
                meeting_title=title_used,
            )

    norm_title = normalize_text(meeting_title)

    if norm_title and norm_title not in GENERIC_MEETING_TITLES:
        for course in courses:
            c_name = str(course.get("name", "")).strip()
            c_disp = str(course.get("display_name", "")).strip()
            if not c_name:
                continue
            keywords = [c_name, c_name.replace("_", " "), c_disp] + [
                str(k) for k in course.get("keywords", [])
            ]
            for kw in keywords:
                norm_kw = normalize_text(kw)
                if norm_kw and norm_kw in norm_title:
                    return CourseMatch(
                        course_name=sanitize_folder_name(c_name),
                        match_source=f"Tytuł okna Teams ('{kw}')",
                        meeting_title=meeting_title,
                    )

    if not strict_ics_dates or not ics_events:
        weekday = when.weekday()
        now_mins = when.hour * 60 + when.minute

        for course in courses:
            c_name = str(course.get("name", "")).strip()
            if not c_name:
                continue
            for slot in course.get("schedule", []):
                day_str = normalize_text(str(slot.get("day", "")))
                slot_day = DAY_MAP.get(day_str)
                if slot_day != weekday:
                    continue
                start_mins = _parse_hhmm(str(slot.get("start", "")))
                end_mins = _parse_hhmm(str(slot.get("end", "")))
                if start_mins is None or end_mins is None:
                    continue
                if (start_mins - buffer_mins) <= now_mins <= (end_mins + buffer_mins):
                    slot_type = str(slot.get("type", "")).split(" ")[0].strip()
                    default_title = f"{slot_type}_{c_name}" if slot_type else c_name
                    return CourseMatch(
                        course_name=sanitize_folder_name(c_name),
                        match_source=f"Plan tygodniowy ({slot.get('day')} {slot.get('start')}-{slot.get('end')})",
                        meeting_title=meeting_title or default_title,
                    )

    if use_fallback and norm_title and norm_title not in GENERIC_MEETING_TITLES:
        return CourseMatch(
            course_name=sanitize_folder_name(meeting_title),
            match_source="Tytuł okna Teams (automatyczny folder)",
            meeting_title=meeting_title,
        )

    return CourseMatch(
        course_name="General_Meetings",
        match_source="Domyślny folder (poza planem)",
        meeting_title=meeting_title or "Teams_Meeting",
    )


def build_recording_paths(
    config: Dict[str, Any],
    base_dir: Path,
    course_match: CourseMatch,
    started_at: Optional[datetime] = None,
) -> Tuple[Path, Path]:
    if started_at is None:
        started_at = datetime.now()

    output_root = resolve_output_root(config, base_dir=base_dir)

    date_str = started_at.strftime("%Y-%m-%d")
    time_str = started_at.strftime("%H-%M-%S")
    course_folder = sanitize_folder_name(course_match.course_name)
    safe_title = sanitize_folder_name(course_match.meeting_title or "Meeting", max_len=45)

    folder_structure = config.get("folder_structure", "{course}/{date}")
    rel_folder = folder_structure.format(
        course=course_folder,
        date=date_str,
        year=started_at.strftime("%Y"),
        month=started_at.strftime("%m"),
    )
    target_dir = output_root / rel_folder
    target_dir.mkdir(parents=True, exist_ok=True)

    ext = str(config.get("audio", {}).get("format", "mp3")).lower().lstrip(".")
    if ext not in ("mp3", "wav"):
        ext = "mp3"

    if safe_title.lower() == course_folder.lower():
        filename = f"{date_str}_{time_str}_{course_folder}.{ext}"
    else:
        filename = f"{date_str}_{time_str}_{course_folder}_{safe_title}.{ext}"

    return target_dir, target_dir / filename


def append_recording_metadata(
    base_dir: Path,
    audio_path: Path,
    course_match: CourseMatch,
    started_at: datetime,
    ended_at: datetime,
    duration_seconds: float,
) -> None:
    log_file = audio_path.parent / "recordings_index.jsonl"
    entry = {
        "file": audio_path.name,
        "full_path": str(audio_path),
        "course": course_match.course_name,
        "match_source": course_match.match_source,
        "meeting_title": course_match.meeting_title,
        "date": started_at.strftime("%Y-%m-%d"),
        "started_at": started_at.isoformat(timespec="seconds"),
        "ended_at": ended_at.isoformat(timespec="seconds"),
        "duration_seconds": round(duration_seconds, 1),
    }
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def reassign_recording_course(
    config: Dict[str, Any],
    base_dir: Path,
    audio_path: Path,
    new_course_name: str,
) -> Optional[Path]:
    if not audio_path.exists():
        return None

    new_course_folder = sanitize_folder_name(new_course_name)
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})_", audio_path.name)
    if m:
        date_str = m.group(1)
        time_str = m.group(2)
    else:
        date_str = audio_path.parent.name
        time_str = datetime.now().strftime("%H-%M-%S")

    output_root = resolve_output_root(config, base_dir=base_dir)
    new_dir = output_root / new_course_folder / date_str
    new_dir.mkdir(parents=True, exist_ok=True)

    new_filename = f"{date_str}_{time_str}_{new_course_folder}_Zastepstwo{audio_path.suffix}"
    new_path = new_dir / new_filename
    counter = 1
    while new_path.exists():
        new_path = new_dir / f"{date_str}_{time_str}_{new_course_folder}_Zastepstwo_{counter}{audio_path.suffix}"
        counter += 1

    shutil.move(str(audio_path), str(new_path))

    now = datetime.now()
    cm = CourseMatch(
        course_name=new_course_folder,
        match_source="Przeniesiono ręcznie (Zastępstwo)",
        meeting_title=new_course_folder,
    )
    append_recording_metadata(base_dir, new_path, cm, now, now, 0.0)
    return new_path
