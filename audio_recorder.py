import glob
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional
import wave

import pyaudiowpatch as pyaudio


def find_ffmpeg() -> Optional[str]:
    path_bin = shutil.which("ffmpeg")
    if path_bin:
        return path_bin

    local_appdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    pattern = os.path.join(
        local_appdata,
        "Microsoft",
        "WinGet",
        "Packages",
        "*FFmpeg*",
        "**",
        "ffmpeg.exe",
    )
    matches = glob.glob(pattern, recursive=True)
    if matches:
        return matches[0]
    return None


def list_audio_devices() -> Dict[str, Any]:
    p = pyaudio.PyAudio()
    try:
        loopback_dev = None
        mic_dev = None
        try:
            loopback_dev = p.get_default_wasapi_loopback()
        except Exception:
            pass

        try:
            wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_in_idx = wasapi_info.get("defaultInputDevice", -1)
            if default_in_idx is not None and default_in_idx >= 0:
                mic_dev = p.get_device_info_by_index(default_in_idx)
        except Exception:
            try:
                mic_dev = p.get_default_input_device_info()
            except Exception:
                pass

        return {
            "loopback": loopback_dev,
            "microphone": mic_dev,
            "ffmpeg": find_ffmpeg(),
        }
    finally:
        p.terminate()


def _compute_rms_db(pcm_bytes: bytes) -> float:
    if not pcm_bytes or len(pcm_bytes) < 2:
        return 0.0
    try:
        count = len(pcm_bytes) // 2
        step = max(1, count // 64)
        total_sq = 0.0
        n = 0
        for i in range(0, count * 2 - 1, step * 2):
            sample = struct.unpack_from("<h", pcm_bytes, i)[0]
            total_sq += sample * sample
            n += 1
        if n == 0:
            return 0.0
        rms = math.sqrt(total_sq / n) / 32768.0
        return min(1.0, rms * 3.5)
    except Exception:
        return 0.0


class DualAudioRecorder:
    """
    Records system output (WASAPI Loopback) and user microphone simultaneously,
    keeping both streams synchronized via a silent WASAPI output keep-alive clock,
    then mixes and encodes them into an MP3 or WAV file via FFmpeg.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.audio_cfg = config.get("audio", {})
        self.record_loopback = bool(self.audio_cfg.get("record_loopback", True))
        self.record_microphone = bool(self.audio_cfg.get("record_microphone", True))
        self.mp3_bitrate = str(self.audio_cfg.get("mp3_bitrate", "192k"))
        self.loopback_volume = float(self.audio_cfg.get("loopback_volume", 1.0))
        self.mic_volume = float(self.audio_cfg.get("mic_volume", 1.0))
        self.keep_raw_wav = bool(self.audio_cfg.get("keep_raw_wav", False))

        self._stop_event = threading.Event()
        self._pa_lock = threading.Lock()
        self._pa: Optional[pyaudio.PyAudio] = None
        self._threads: List[threading.Thread] = []
        self._start_time: Optional[float] = None
        self._target_path: Optional[Path] = None
        self._loopback_wav_path: Optional[Path] = None
        self._mic_wav_path: Optional[Path] = None

        self.loopback_level: float = 0.0
        self.mic_level: float = 0.0
        self.loopback_device_name: str = "None"
        self.mic_device_name: str = "None"
        self.is_recording: bool = False

    @property
    def elapsed_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    def start(self, target_audio_path: Path) -> None:
        if self.is_recording:
            return

        self._stop_event.clear()
        self._threads.clear()
        self._target_path = target_audio_path
        target_audio_path.parent.mkdir(parents=True, exist_ok=True)

        stem = target_audio_path.with_suffix("")
        self._loopback_wav_path = Path(f"{stem}.temp_loopback.wav")
        self._mic_wav_path = Path(f"{stem}.temp_mic.wav")

        self._pa = pyaudio.PyAudio()
        self._start_time = time.monotonic()
        self.is_recording = True

        if self.record_loopback:
            t_lb = threading.Thread(
                target=self._record_loopback_worker,
                args=(self._loopback_wav_path,),
                daemon=True,
                name="LoopbackRecorder",
            )
            self._threads.append(t_lb)
            t_lb.start()

        if self.record_microphone:
            t_mic = threading.Thread(
                target=self._record_mic_worker,
                args=(self._mic_wav_path,),
                daemon=True,
                name="MicRecorder",
            )
            self._threads.append(t_mic)
            t_mic.start()

    def _record_loopback_worker(self, wav_path: Path) -> None:
        p = self._pa
        if p is None:
            return
        keepalive_stream = None
        loopback_stream = None
        wf = None
        try:
            chunk = 1024
            with self._pa_lock:
                lb_dev = p.get_default_wasapi_loopback()
                self.loopback_device_name = str(lb_dev.get("name", "WASAPI Loopback"))
                channels = max(1, int(lb_dev.get("maxInputChannels", 2)))
                rate = int(lb_dev.get("defaultSampleRate", 48000))
                sampwidth = p.get_sample_size(pyaudio.paInt16)

                try:
                    wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
                    out_dev = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
                    out_ch = max(1, int(out_dev.get("maxOutputChannels", 2)))
                    out_rate = int(out_dev.get("defaultSampleRate", 48000))
                    keepalive_stream = p.open(
                        format=pyaudio.paInt16,
                        channels=out_ch,
                        rate=out_rate,
                        output=True,
                        output_device_index=out_dev["index"],
                        frames_per_buffer=chunk,
                    )
                    silent_out_frame = b"\x00" * (chunk * 2 * out_ch)
                except Exception:
                    keepalive_stream = None
                    silent_out_frame = b""

                loopback_stream = p.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=lb_dev["index"],
                    frames_per_buffer=chunk,
                )

            wf = wave.open(str(wav_path), "wb")
            wf.setnchannels(channels)
            wf.setsampwidth(sampwidth)
            wf.setframerate(rate)

            while not self._stop_event.is_set():
                if keepalive_stream is not None:
                    try:
                        keepalive_stream.write(silent_out_frame, exception_on_underflow=False)
                    except Exception:
                        pass
                try:
                    data = loopback_stream.read(chunk, exception_on_overflow=False)
                    wf.writeframes(data)
                    self.loopback_level = _compute_rms_db(data)
                except Exception:
                    time.sleep(0.01)
        except Exception as exc:
            self.loopback_device_name = f"Unavailable ({exc})"
        finally:
            self.loopback_level = 0.0
            if wf is not None:
                try:
                    wf.close()
                except Exception:
                    pass
            with self._pa_lock:
                if loopback_stream is not None:
                    try:
                        loopback_stream.close()
                    except Exception:
                        pass
                if keepalive_stream is not None:
                    try:
                        keepalive_stream.close()
                    except Exception:
                        pass

    def _record_mic_worker(self, wav_path: Path) -> None:
        p = self._pa
        if p is None:
            return
        mic_stream = None
        wf = None
        try:
            chunk = 1024
            with self._pa_lock:
                mic_dev = None
                try:
                    wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
                    default_in_idx = wasapi_info.get("defaultInputDevice", -1)
                    if default_in_idx is not None and default_in_idx >= 0:
                        mic_dev = p.get_device_info_by_index(default_in_idx)
                except Exception:
                    pass
                if mic_dev is None:
                    mic_dev = p.get_default_input_device_info()

                self.mic_device_name = str(mic_dev.get("name", "Default Microphone"))
                channels = max(1, int(mic_dev.get("maxInputChannels", 1)))
                rate = int(mic_dev.get("defaultSampleRate", 48000))
                sampwidth = p.get_sample_size(pyaudio.paInt16)

                mic_stream = p.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=mic_dev["index"],
                    frames_per_buffer=chunk,
                )

            wf = wave.open(str(wav_path), "wb")
            wf.setnchannels(channels)
            wf.setsampwidth(sampwidth)
            wf.setframerate(rate)

            while not self._stop_event.is_set():
                try:
                    data = mic_stream.read(chunk, exception_on_overflow=False)
                    wf.writeframes(data)
                    self.mic_level = _compute_rms_db(data)
                except Exception:
                    time.sleep(0.01)
        except Exception as exc:
            self.mic_device_name = f"Unavailable ({exc})"
        finally:
            self.mic_level = 0.0
            if wf is not None:
                try:
                    wf.close()
                except Exception:
                    pass
            with self._pa_lock:
                if mic_stream is not None:
                    try:
                        mic_stream.close()
                    except Exception:
                        pass

    def stop(
        self,
        discard: bool = False,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Optional[Path]:
        if not self.is_recording:
            return None

        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=5.0)
        self._threads.clear()

        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

        self.is_recording = False

        lb_path = self._loopback_wav_path
        mic_path = self._mic_wav_path
        final_path = self._target_path

        if discard or final_path is None:
            self._cleanup_temp_files()
            return None

        has_lb = lb_path is not None and lb_path.exists() and lb_path.stat().st_size > 100
        has_mic = mic_path is not None and mic_path.exists() and mic_path.stat().st_size > 100

        if not has_lb and not has_mic:
            self._cleanup_temp_files()
            return None

        ffmpeg_bin = find_ffmpeg()
        if ffmpeg_bin:
            success = self._mix_with_ffmpeg(
                ffmpeg_bin=ffmpeg_bin,
                lb_path=lb_path if has_lb else None,
                mic_path=mic_path if has_mic else None,
                final_path=final_path,
                metadata=metadata or {},
            )
            if success:
                if not self.keep_raw_wav:
                    self._cleanup_temp_files()
                return final_path

        fallback_wav = final_path.with_suffix(".wav")
        if has_lb and lb_path is not None:
            shutil.move(str(lb_path), str(fallback_wav))
        elif has_mic and mic_path is not None:
            shutil.move(str(mic_path), str(fallback_wav))
        self._cleanup_temp_files()
        return fallback_wav

    def _mix_with_ffmpeg(
        self,
        ffmpeg_bin: str,
        lb_path: Optional[Path],
        mic_path: Optional[Path],
        final_path: Path,
        metadata: Dict[str, str],
    ) -> bool:
        cmd: List[str] = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error"]

        if lb_path and mic_path:
            cmd.extend(["-i", str(lb_path), "-i", str(mic_path)])
            filter_complex = (
                f"[0:a]volume={self.loopback_volume}[a0];"
                f"[1:a]volume={self.mic_volume}[a1];"
                f"[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[outa]"
            )
            cmd.extend(["-filter_complex", filter_complex, "-map", "[outa]"])
        elif lb_path:
            cmd.extend(["-i", str(lb_path)])
            if self.loopback_volume != 1.0:
                cmd.extend(["-af", f"volume={self.loopback_volume}"])
        elif mic_path:
            cmd.extend(["-i", str(mic_path)])
            if self.mic_volume != 1.0:
                cmd.extend(["-af", f"volume={self.mic_volume}"])
        else:
            return False

        for k, v in metadata.items():
            if v:
                cmd.extend(["-metadata", f"{k}={v}"])

        if final_path.suffix.lower() == ".mp3":
            cmd.extend(["-c:a", "libmp3lame", "-b:a", self.mp3_bitrate])
        else:
            cmd.extend(["-c:a", "pcm_s16le"])

        cmd.append(str(final_path))

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            return res.returncode == 0 and final_path.exists()
        except Exception:
            return False

    def _cleanup_temp_files(self) -> None:
        for p in (self._loopback_wav_path, self._mic_wav_path):
            if p is not None and p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass
