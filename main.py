import argparse
from datetime import datetime
import json
from pathlib import Path
import signal
import sys
import time
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from audio_recorder import DualAudioRecorder, list_audio_devices
from course_organizer import (
    CourseMatch,
    append_recording_metadata,
    build_recording_paths,
    get_upcoming_or_active_ics_events,
    match_course,
    parse_remote_ics_events,
)
from teams_detector import MeetingDetectionResult, detect_teams_meeting


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
console = Console()


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: Dict[str, Any], config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def level_bar(level: float, width: int = 16) -> str:
    filled = min(width, max(0, int(round(level * width))))
    return "█" * filled + "░" * (width - filled)


def cmd_remote_schedule(config: Dict[str, Any]) -> None:
    remote_events = parse_remote_ics_events(config, base_dir=BASE_DIR)
    table = Table(
        title=f"TeamsRec — Remote Classes Filtered from USOS Calendar ({len(remote_events)} sessions in Budynek sal wirtualnych)",
        show_header=True,
    )
    table.add_column("Date", style="cyan", no_wrap=True)
    table.add_column("Day", style="green", no_wrap=True)
    table.add_column("Time", style="yellow", no_wrap=True)
    table.add_column("USOS Class", style="white")
    table.add_column("Target Course Folder", style="bold magenta")

    pl_days = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]
    for ev in remote_events:
        table.add_row(
            ev.start_dt.strftime("%Y-%m-%d"),
            pl_days[ev.start_dt.weekday()],
            f"{ev.start_dt.strftime('%H:%M')}–{ev.end_dt.strftime('%H:%M')}",
            ev.summary,
            ev.course_folder,
        )
    console.print(table)


def cmd_status(config: Dict[str, Any]) -> None:
    devices = list_audio_devices()
    det_cfg = config.get("detection", {})
    det = detect_teams_meeting(
        detect_browser=bool(det_cfg.get("detect_browser_teams", True)),
        require_audio_or_mic_for_browser=bool(det_cfg.get("require_audio_or_mic_for_browser", True)),
    )
    cm = match_course(config, meeting_title=det.meeting_title, base_dir=BASE_DIR)
    upcoming = get_upcoming_or_active_ics_events(config, limit=4, base_dir=BASE_DIR)

    table = Table(title="TeamsRec — System & Detection Status", show_header=True)
    table.add_column("Component", style="cyan", no_wrap=True)
    table.add_column("Details", style="white")

    lb = devices.get("loopback")
    mic = devices.get("microphone")
    ffmpeg_bin = devices.get("ffmpeg")

    table.add_row(
        "Speaker Loopback (WASAPI)",
        f"[green]{lb['name']}[/green] ({int(lb['defaultSampleRate'])} Hz, {lb['maxInputChannels']} ch)"
        if lb
        else "[red]Not found[/red]",
    )
    table.add_row(
        "Microphone (WASAPI)",
        f"[green]{mic['name']}[/green] ({int(mic['defaultSampleRate'])} Hz, {mic['maxInputChannels']} ch)"
        if mic
        else "[yellow]Not found (will record meeting audio only)[/yellow]",
    )
    table.add_row(
        "FFmpeg Mixer/MP3 Encoder",
        f"[green]{ffmpeg_bin}[/green]" if ffmpeg_bin else "[yellow]Not found (will save WAV)[/yellow]",
    )
    table.add_row(
        "Teams Meeting Active?",
        "[bold red]YES (In Meeting)[/bold red]" if det.in_meeting else "[green]No (Idle)[/green]",
    )
    table.add_row(
        "Detected Meeting Title",
        det.meeting_title or "[dim]None[/dim]",
    )
    table.add_row(
        "Matched Course Folder",
        f"[bold yellow]{cm.course_name}[/bold yellow] [dim](via {cm.match_source})[/dim]",
    )
    table.add_row(
        "Next Remote Classes (.ics)",
        "\n".join(
            f"• {e.start_dt.strftime('%Y-%m-%d %H:%M')}–{e.end_dt.strftime('%H:%M')}: [bold]{e.summary}[/bold] -> [magenta]{e.course_folder}[/magenta]"
            for e in upcoming
        )
        or "[dim]No upcoming remote classes found in .ics[/dim]",
    )
    table.add_row(
        "Visible Teams Windows",
        "\n".join(
            f"• [{w.process_name}] {w.raw_title} (meeting={w.looks_like_meeting_window})"
            for w in det.teams_windows
        )
        or "[dim]No Microsoft Teams windows currently open[/dim]",
    )

    console.print(table)


def cmd_add_course(
    config: Dict[str, Any],
    config_path: Path,
    name: str,
    keywords_str: Optional[str],
    schedule_str: Optional[str],
) -> None:
    courses: List[Dict[str, Any]] = config.setdefault("courses", [])
    keywords = [k.strip() for k in (keywords_str or "").split(",") if k.strip()]
    schedule_slots: List[Dict[str, str]] = []

    if schedule_str:
        for item in schedule_str.split(","):
            parts = item.strip().split()
            if len(parts) == 2 and "-" in parts[1]:
                day_part = parts[0].strip().lower()
                start_part, end_part = parts[1].split("-", 1)
                schedule_slots.append(
                    {"day": day_part, "start": start_part.strip(), "end": end_part.strip()}
                )

    existing = next((c for c in courses if c.get("name", "").lower() == name.lower()), None)
    if existing:
        existing["name"] = name
        if keywords:
            existing["keywords"] = sorted(list(set(existing.get("keywords", []) + keywords)))
        if schedule_slots:
            existing.setdefault("schedule", []).extend(schedule_slots)
        action = "Updated"
    else:
        courses.append(
            {
                "name": name,
                "keywords": keywords or [name.lower().replace("_", " ")],
                "schedule": schedule_slots,
            }
        )
        action = "Added"

    save_config(config, config_path)
    console.print(
        f"[bold green]{action} course '{name}'[/bold green] in [cyan]{config_path}[/cyan]."
    )


def run_manual_or_test_recording(
    config: Dict[str, Any],
    course_name: Optional[str],
    meeting_title: Optional[str],
    duration: Optional[float],
) -> int:
    cm = match_course(
        config,
        meeting_title=meeting_title or course_name or "Test_Session",
        manual_course=course_name,
        base_dir=BASE_DIR,
    )
    started_at = datetime.now()
    target_dir, target_file = build_recording_paths(
        config, BASE_DIR, cm, started_at=started_at
    )

    recorder = DualAudioRecorder(config)
    console.print(
        Panel(
            f"[bold red]🔴 Recording Started[/bold red]\n"
            f"[cyan]Course:[/cyan] {cm.course_name} ({cm.match_source})\n"
            f"[cyan]Destination:[/cyan] {target_file}\n"
            f"[dim]{'Recording for ' + str(duration) + 's...' if duration else 'Press Ctrl+C to stop and save MP3.'}[/dim]",
            title="TeamsRec",
        )
    )

    recorder.start(target_file)
    try:
        while True:
            time.sleep(0.25)
            if duration and recorder.elapsed_seconds >= duration:
                break
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping recording and mixing audio to MP3...[/yellow]")

    ended_at = datetime.now()
    el = recorder.elapsed_seconds
    saved_path = recorder.stop(
        discard=False,
        metadata={
            "title": f"{cm.course_name} - {cm.meeting_title}",
            "album": cm.course_name,
            "artist": "TeamsRec",
            "date": started_at.strftime("%Y-%m-%d"),
        },
    )

    if saved_path and saved_path.exists():
        append_recording_metadata(BASE_DIR, saved_path, cm, started_at, ended_at, el)
        size_kb = saved_path.stat().st_size / 1024.0
        console.print(
            f"[bold green]✔ Saved recording ({format_duration(el)}, {size_kb:.1f} KB):[/bold green]\n"
            f"  [underline]{saved_path}[/underline]"
        )
        return 0
    else:
        console.print("[bold red]✖ Failed to save audio recording.[/bold red]")
        return 1


def build_dashboard(
    config: Dict[str, Any],
    det: MeetingDetectionResult,
    recorder: DualAudioRecorder,
    active_course: Optional[CourseMatch],
    active_file: Optional[Path],
    recent_files: List[str],
    debounce_progress: float,
    grace_remaining: Optional[float],
) -> Panel:
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(ratio=1)

    if recorder.is_recording and active_course and active_file:
        status_line = Text.from_markup(
            f"[bold white on red] 🔴 RECORDING [/bold white on red] "
            f"[bold yellow]{active_course.course_name}[/bold yellow] — "
            f"[bold cyan]{format_duration(recorder.elapsed_seconds)}[/bold cyan]"
        )
        if grace_remaining is not None:
            status_line.append(
                f"  (Meeting signal paused — stopping in {int(grace_remaining)}s if not resumed)",
                style="yellow",
            )
    elif debounce_progress > 0:
        status_line = Text.from_markup(
            f"[bold black on yellow] ⏳ DETECTED TEAMS CALL [/bold black on yellow] "
            f"Verifying stability ({int(debounce_progress * 100)}%)..."
        )
    else:
        status_line = Text.from_markup(
            "[bold black on green] 🟢 STANDBY [/bold black on green] "
            "Monitoring for active Microsoft Teams meetings (Remote USOS courses)..."
        )

    grid.add_row(status_line)
    grid.add_row(Text(""))

    info = Table(show_header=False, box=None, padding=(0, 2))
    info.add_column("Key", style="cyan", no_wrap=True)
    info.add_column("Value", style="white")

    preview_match = active_course or match_course(
        config, meeting_title=det.meeting_title, base_dir=BASE_DIR
    )
    info.add_row(
        "Detected Meeting Title:",
        det.meeting_title or "[dim]No active meeting window[/dim]",
    )
    info.add_row(
        "Detection Signals:",
        ", ".join(det.confidence_reasons) if det.confidence_reasons else "[dim]None[/dim]",
    )
    info.add_row(
        "Target Course Folder:",
        f"[bold]{preview_match.course_name}[/bold] [dim](via {preview_match.match_source})[/dim]",
    )

    upcoming = get_upcoming_or_active_ics_events(config, limit=2, base_dir=BASE_DIR)
    if upcoming:
        info.add_row(
            "Next Remote USOS Class:",
            "\n".join(
                f"{e.start_dt.strftime('%Y-%m-%d %H:%M')}–{e.end_dt.strftime('%H:%M')} [bold]{e.summary}[/bold] ({e.course_folder})"
                for e in upcoming
            ),
        )

    if recorder.is_recording and active_file:
        info.add_row("Output File:", f"[green]{active_file}[/green]")
        info.add_row(
            f"Meeting Audio ({recorder.loopback_device_name[:28]}):",
            f"[cyan]{level_bar(recorder.loopback_level)}[/cyan]",
        )
        info.add_row(
            f"Microphone ({recorder.mic_device_name[:28]}):",
            f"[magenta]{level_bar(recorder.mic_level)}[/magenta]",
        )

    if recent_files:
        info.add_row(
            "Recent Saved Files:",
            "\n".join(f"✔ {p}" for p in recent_files[-3:]),
        )

    grid.add_row(info)
    grid.add_row(Text(""))
    grid.add_row(
        Text.from_markup(
            "[dim]Press Ctrl+C to stop monitoring (any active recording will be safely saved to MP3).[/dim]"
        )
    )

    return Panel(
        grid,
        title="[bold cyan]TeamsRec — Microsoft Teams Auto-Recorder & USOS Remote Course Organizer[/bold cyan]",
        border_style="red" if recorder.is_recording else "green",
    )


def run_watcher(config_path: Path) -> None:
    config = load_config(config_path)
    det_cfg = config.get("detection", {})
    poll_interval = float(det_cfg.get("poll_interval_seconds", 3.0))
    start_debounce = float(det_cfg.get("start_debounce_seconds", 6.0))
    stop_grace = float(det_cfg.get("stop_grace_period_seconds", 20.0))
    min_rec_secs = float(det_cfg.get("min_recording_seconds", 5.0))

    recorder = DualAudioRecorder(config)
    active_course: Optional[CourseMatch] = None
    active_file: Optional[Path] = None
    started_at: Optional[datetime] = None
    recent_files: List[str] = []

    meeting_seen_since: Optional[float] = None
    meeting_lost_since: Optional[float] = None

    stop_requested = False

    def handle_sigint(signum: int, frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, handle_sigint)

    det = detect_teams_meeting()
    with Live(
        build_dashboard(config, det, recorder, None, None, recent_files, 0.0, None),
        console=console,
        refresh_per_second=4,
    ) as live:
        while not stop_requested:
            try:
                config = load_config(config_path)
                det_cfg = config.get("detection", {})
            except Exception:
                pass

            det = detect_teams_meeting(
                detect_browser=bool(det_cfg.get("detect_browser_teams", True)),
                require_audio_or_mic_for_browser=bool(
                    det_cfg.get("require_audio_or_mic_for_browser", True)
                ),
            )
            now_mono = time.monotonic()
            debounce_progress = 0.0
            grace_remaining: Optional[float] = None

            if not recorder.is_recording:
                if det.in_meeting:
                    if meeting_seen_since is None:
                        meeting_seen_since = now_mono
                    elapsed_seen = now_mono - meeting_seen_since
                    debounce_progress = min(1.0, elapsed_seen / max(1.0, start_debounce))
                    if elapsed_seen >= start_debounce:
                        started_at = datetime.now()
                        active_course = match_course(
                            config,
                            meeting_title=det.meeting_title,
                            when=started_at,
                            base_dir=BASE_DIR,
                        )
                        _, active_file = build_recording_paths(
                            config, BASE_DIR, active_course, started_at=started_at
                        )
                        recorder.start(active_file)
                        meeting_seen_since = None
                        meeting_lost_since = None
                else:
                    meeting_seen_since = None
            else:
                if det.in_meeting:
                    meeting_lost_since = None
                    if (
                        active_course
                        and active_course.match_source in ("default", "weekly_remote_timetable")
                        and det.meeting_title
                    ):
                        new_match = match_course(
                            config,
                            meeting_title=det.meeting_title,
                            when=started_at,
                            base_dir=BASE_DIR,
                        )
                        if new_match.match_source.startswith("window_title") or new_match.match_source.startswith("USOS"):
                            active_course = new_match
                else:
                    if meeting_lost_since is None:
                        meeting_lost_since = now_mono
                    lost_elapsed = now_mono - meeting_lost_since
                    grace_remaining = max(0.0, stop_grace - lost_elapsed)
                    if lost_elapsed >= stop_grace:
                        ended_at = datetime.now()
                        rec_duration = recorder.elapsed_seconds
                        discard = rec_duration < min_rec_secs
                        saved = recorder.stop(
                            discard=discard,
                            metadata={
                                "title": f"{active_course.course_name} - {active_course.meeting_title}"
                                if active_course
                                else "Teams Meeting",
                                "album": active_course.course_name if active_course else "Teams",
                                "artist": "TeamsRec",
                                "date": started_at.strftime("%Y-%m-%d") if started_at else "",
                            },
                        )
                        if saved and active_course and started_at:
                            append_recording_metadata(
                                BASE_DIR,
                                saved,
                                active_course,
                                started_at,
                                ended_at,
                                rec_duration,
                            )
                            recent_files.append(
                                f"{active_course.course_name}/{started_at.strftime('%Y-%m-%d')}/{saved.name} ({format_duration(rec_duration)})"
                            )
                        active_course = None
                        active_file = None
                        started_at = None
                        meeting_lost_since = None

            live.update(
                build_dashboard(
                    config,
                    det,
                    recorder,
                    active_course,
                    active_file,
                    recent_files,
                    debounce_progress,
                    grace_remaining,
                )
            )

            steps = max(1, int(poll_interval / 0.25))
            for _ in range(steps):
                if stop_requested:
                    break
                time.sleep(0.25)
                if recorder.is_recording:
                    live.update(
                        build_dashboard(
                            config,
                            det,
                            recorder,
                            active_course,
                            active_file,
                            recent_files,
                            debounce_progress,
                            grace_remaining,
                        )
                    )

    if recorder.is_recording and active_course and started_at:
        console.print("\n[yellow]Finalizing active recording and encoding MP3...[/yellow]")
        ended_at = datetime.now()
        rec_duration = recorder.elapsed_seconds
        saved = recorder.stop(
            discard=False,
            metadata={
                "title": f"{active_course.course_name} - {active_course.meeting_title}",
                "album": active_course.course_name,
                "artist": "TeamsRec",
                "date": started_at.strftime("%Y-%m-%d"),
            },
        )
        if saved:
            append_recording_metadata(
                BASE_DIR, saved, active_course, started_at, ended_at, rec_duration
            )
            console.print(f"[bold green]✔ Saved recording to:[/bold green] {saved}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TeamsRec — Automatic Microsoft Teams Meeting Audio Recorder & USOS Remote Course Organizer"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.json",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check audio devices, FFmpeg, upcoming remote classes, and current Teams detection state",
    )
    parser.add_argument(
        "--remote-schedule",
        action="store_true",
        help="List all remote (zdalne) classes parsed from u195091.ics",
    )
    parser.add_argument(
        "--record-now",
        action="store_true",
        help="Start recording immediately without waiting for Teams detection",
    )
    parser.add_argument(
        "--test-audio",
        action="store_true",
        help="Run a quick 4-second audio recording & MP3 mix test",
    )
    parser.add_argument(
        "--course",
        type=str,
        default=None,
        help="Course name override for --record-now or --test-audio",
    )
    parser.add_argument(
        "--title",
        type=str,
        default=None,
        help="Meeting title override for --record-now",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Recording duration in seconds (for --record-now or --test-audio)",
    )
    parser.add_argument(
        "--add-course",
        type=str,
        default=None,
        help="Add or update a course in config.json",
    )
    parser.add_argument(
        "--keywords",
        type=str,
        default=None,
        help="Comma-separated window title keywords for --add-course",
    )
    parser.add_argument(
        "--schedule",
        type=str,
        default=None,
        help="Comma-separated schedule slots for --add-course",
    )

    parser.add_argument(
        "--terminal",
        action="store_true",
        help="Run in terminal mode instead of launching the System Tray & Calendar GUI",
    )
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="Start the GUI minimized in the Windows System Tray",
    )

    args = parser.parse_args()
    config = load_config(args.config)

    if args.add_course:
        cmd_add_course(config, args.config, args.add_course, args.keywords, args.schedule)
        return 0

    if args.remote_schedule:
        cmd_remote_schedule(config)
        return 0

    if args.status:
        cmd_status(config)
        return 0

    if args.test_audio:
        dur = args.duration or 4.0
        return run_manual_or_test_recording(
            config,
            course_name=args.course or "Test_Course",
            meeting_title=args.title or "Audio_Verification",
            duration=dur,
        )

    if args.record_now:
        return run_manual_or_test_recording(
            config,
            course_name=args.course,
            meeting_title=args.title,
            duration=args.duration,
        )

    if not args.terminal:
        from gui_app import launch_gui

        return launch_gui(config_path=args.config, start_hidden=args.minimized)

    from gui_app import SingleInstanceManager

    lock = SingleInstanceManager()
    if lock.already_running:
        console.print("[bold yellow]TeamSREC już działa w tle — przywracam istniejące okno.[/bold yellow]")
        lock.signal_existing_instance()
        lock.close()
        return 0
    try:
        run_watcher(args.config)
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
