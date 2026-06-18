from __future__ import annotations

import ctypes
import shlex
import subprocess
import threading
import time
import uuid
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, Deque, Optional, Protocol
from urllib.parse import parse_qs, urlparse


class CommandName:
    PLAY = "play"
    SKIP = "skip"
    SKIP_PLAYLIST = "skip_playlist"
    QUEUE = "queue"
    CLEAR = "clear"
    QUIT = "quit"
    HELP = "help"
    EMPTY = "empty"
    UNKNOWN = "unknown"
    INVALID = "invalid"


@dataclass(frozen=True)
class Command:
    name: str
    argument: str = ""
    error: str = ""


@dataclass
class Track:
    query: str
    title: str = ""
    source_url: str = ""
    playlist_key: str = ""
    playlist_title: str = ""
    requested_at: float = field(default_factory=time.time)

    @property
    def display_name(self) -> str:
        return self.title or self.query


@dataclass
class PreparedTrack:
    track: Track
    title: str
    source_url: str
    audio_url: str
    http_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class QueueSnapshot:
    current: Optional[Track]
    queued: list[Track]


class PlaybackSkipped(Exception):
    pass


class SilentYtdlpLogger:
    def debug(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass


class TrackResolver(Protocol):
    def resolve(self, query: str) -> list[Track]:
        ...

    def prepare(
        self,
        track: Track,
        skip_event: threading.Event,
        stop_event: threading.Event,
    ) -> PreparedTrack:
        ...

    def cleanup(self, prepared: PreparedTrack) -> None:
        ...

    def close(self) -> None:
        ...


class AudioBackend(Protocol):
    def play(
        self,
        audio_url: str,
        http_headers: dict[str, str],
        stop_event: threading.Event,
        skip_event: threading.Event,
    ) -> None:
        ...

    def stop(self) -> None:
        ...

    def close(self) -> None:
        ...


def parse_command(line: str) -> Command:
    raw = line.strip()
    if not raw:
        return Command(CommandName.EMPTY)

    lower = raw.lower()
    exact_commands = {
        "m!s": CommandName.SKIP,
        "m!sp": CommandName.SKIP_PLAYLIST,
        "m!splaylist": CommandName.SKIP_PLAYLIST,
        "m!fila": CommandName.QUEUE,
        "m!limpar": CommandName.CLEAR,
        "m!q": CommandName.QUIT,
        "m!help": CommandName.HELP,
    }
    if lower in exact_commands:
        return Command(exact_commands[lower])

    if lower == "m!p":
        return Command(CommandName.INVALID, error="Use: m!p <nome ou URL>")

    if lower.startswith("m!p "):
        argument = raw[4:].strip()
        if not argument:
            return Command(CommandName.INVALID, error="Use: m!p <nome ou URL>")
        try:
            parts = shlex.split(argument)
        except ValueError as exc:
            return Command(CommandName.INVALID, error=f"Comando com aspas invalidas: {exc}")
        query = " ".join(parts).strip()
        if not query:
            return Command(CommandName.INVALID, error="Use: m!p <nome ou URL>")
        return Command(CommandName.PLAY, query)

    return Command(CommandName.UNKNOWN, raw)


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_playlist_url(value: str) -> bool:
    if not is_http_url(value):
        return False

    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return "list" in query or ("youtube" in host and "/playlist" in path)


class YouTubeTrackResolver:
    def resolve(self, query: str) -> list[Track]:
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError(
                "Dependencias faltando. Rode: pip install -r requirements.txt"
            ) from exc

        playlist_allowed = is_playlist_url(query)
        target = query if is_http_url(query) else f"ytsearch1:{query}"
        ydl_opts = self._base_ytdlp_options()
        ydl_opts.update(
            {
                "extract_flat": True,
                "noplaylist": not playlist_allowed,
            }
        )

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target, download=False)
        except Exception as exc:
            raise RuntimeError(f"Nao consegui encontrar '{query}': {exc}") from exc

        tracks = self._tracks_from_info(info, query, playlist_allowed)
        if not tracks:
            raise RuntimeError("Nenhum resultado encontrado no YouTube.")
        return tracks

    def prepare(
        self,
        track: Track,
        skip_event: threading.Event,
        stop_event: threading.Event,
    ) -> PreparedTrack:
        self._raise_if_cancelled(skip_event, stop_event)

        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError(
                "Dependencias faltando. Rode: pip install -r requirements.txt"
            ) from exc

        target = track.source_url or (
            track.query if is_http_url(track.query) else f"ytsearch1:{track.query}"
        )
        ydl_opts = self._base_ytdlp_options()
        ydl_opts.update(
            {
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "noplaylist": True,
            }
        )

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target, download=False)
        except Exception as exc:
            if skip_event.is_set() or stop_event.is_set():
                raise PlaybackSkipped() from exc
            raise RuntimeError(f"Nao consegui abrir '{track.display_name}': {exc}") from exc

        self._raise_if_cancelled(skip_event, stop_event)
        info = self._select_entry(info)
        audio_format = self._pick_audio_format(info)
        if not audio_format or not audio_format.get("url"):
            raise RuntimeError(f"Nao encontrei um audio tocavel para '{track.display_name}'.")

        headers: dict[str, str] = {}
        headers.update(info.get("http_headers") or {})
        headers.update(audio_format.get("http_headers") or {})
        title = str(info.get("title") or track.display_name)
        source_url = str(info.get("webpage_url") or info.get("original_url") or track.source_url)

        return PreparedTrack(
            track=track,
            title=title,
            source_url=source_url,
            audio_url=str(audio_format["url"]),
            http_headers=headers,
        )

    def cleanup(self, _prepared: PreparedTrack) -> None:
        pass

    def close(self) -> None:
        pass

    def _base_ytdlp_options(self) -> dict:
        return {
            "noprogress": True,
            "quiet": True,
            "no_warnings": True,
            "default_search": "ytsearch1",
            "socket_timeout": 10,
            "retries": 3,
            "fragment_retries": 3,
            "logger": SilentYtdlpLogger(),
        }

    def _tracks_from_info(self, info: dict, query: str, playlist_allowed: bool) -> list[Track]:
        if "entries" not in info:
            return [self._track_from_entry(info, query)]

        entries = [entry for entry in info.get("entries") or [] if entry]
        if not entries:
            return []

        playlist_key = ""
        playlist_title = ""
        selected = entries[:1]
        if playlist_allowed:
            playlist_id = str(info.get("id") or uuid.uuid4().hex)
            playlist_key = f"playlist:{playlist_id}"
            playlist_title = str(info.get("title") or "YouTube playlist")
            selected = entries

        return [
            self._track_from_entry(entry, query, playlist_key, playlist_title)
            for entry in selected
        ]

    def _track_from_entry(
        self,
        entry: dict,
        query: str,
        playlist_key: str = "",
        playlist_title: str = "",
    ) -> Track:
        title = str(entry.get("title") or query)
        source_url = self._entry_url(entry, query)
        return Track(
            query=query,
            title=title,
            source_url=source_url,
            playlist_key=playlist_key,
            playlist_title=playlist_title,
        )

    def _entry_url(self, entry: dict, fallback: str) -> str:
        for key in ("webpage_url", "original_url", "url"):
            value = entry.get(key)
            if isinstance(value, str) and is_http_url(value):
                return value

        video_id = entry.get("id") or entry.get("url")
        if isinstance(video_id, str) and video_id:
            return f"https://www.youtube.com/watch?v={video_id}"

        return fallback

    def _select_entry(self, info: dict) -> dict:
        if "entries" not in info:
            return info

        for entry in info.get("entries") or []:
            if entry:
                return entry
        raise RuntimeError("Nenhum resultado encontrado no YouTube.")

    def _pick_audio_format(self, info: dict) -> Optional[dict]:
        candidates: list[dict] = []

        if info.get("url") and info.get("acodec") and info.get("acodec") != "none":
            candidates.append(info)

        for item in info.get("formats") or []:
            if not item.get("url"):
                continue
            if not item.get("acodec") or item.get("acodec") == "none":
                continue
            if item.get("vcodec") and item.get("vcodec") != "none":
                continue
            candidates.append(item)

        candidates.sort(
            key=lambda item: (
                1 if item.get("ext") == "m4a" else 0,
                float(item.get("abr") or 0),
            ),
            reverse=True,
        )
        return candidates[0] if candidates else None

    def _raise_if_cancelled(
        self,
        skip_event: threading.Event,
        stop_event: threading.Event,
    ) -> None:
        if skip_event.is_set() or stop_event.is_set():
            raise PlaybackSkipped()


class WaveFormatEx(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WaveHeader(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]


class FfmpegWaveOutAudioBackend:
    WAVE_FORMAT_PCM = 1
    WAVE_MAPPER = 0xFFFFFFFF
    CALLBACK_NULL = 0
    WHDR_DONE = 0x00000001
    BUFFER_SIZE = 32768
    MAX_PENDING_BUFFERS = 8

    def __init__(self, sample_rate: int = 44100, channels: int = 2) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.bits_per_sample = 16
        self.block_align = self.channels * self.bits_per_sample // 8
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._wave_handle: Optional[ctypes.c_void_p] = None
        self._winmm = self._load_winmm()

    def play(
        self,
        audio_url: str,
        http_headers: dict[str, str],
        stop_event: threading.Event,
        skip_event: threading.Event,
    ) -> None:
        command = self._build_ffmpeg_command(audio_url, http_headers)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        wave_handle = self._open_wave_out()
        pending: list[tuple[ctypes.Array, WaveHeader]] = []
        carry = b""

        with self._lock:
            self._process = process
            self._wave_handle = wave_handle

        try:
            stdout = process.stdout
            if stdout is None:
                raise RuntimeError("FFmpeg nao abriu o audio.")

            while not stop_event.is_set() and not skip_event.is_set():
                self._drain_done_buffers(wave_handle, pending)
                self._wait_for_buffer_room(wave_handle, pending, stop_event, skip_event)
                if stop_event.is_set() or skip_event.is_set():
                    break

                chunk = stdout.read(self.BUFFER_SIZE)
                if not chunk:
                    break

                chunk = carry + chunk
                usable_length = len(chunk) - (len(chunk) % self.block_align)
                if usable_length:
                    self._write_chunk(wave_handle, chunk[:usable_length], pending)
                carry = chunk[usable_length:]

            if stop_event.is_set() or skip_event.is_set():
                self._reset_wave_out(wave_handle)
                self._terminate_process(process)
                raise PlaybackSkipped()

            return_code = process.wait(timeout=5)
            if return_code != 0:
                stderr = self._read_stderr(process)
                raise RuntimeError(f"FFmpeg falhou ao tocar o audio: {stderr}")

            while pending and not stop_event.is_set() and not skip_event.is_set():
                self._drain_done_buffers(wave_handle, pending)
                if pending:
                    time.sleep(0.02)
        finally:
            if pending or stop_event.is_set() or skip_event.is_set():
                self._reset_wave_out(wave_handle)
            self._terminate_process(process)
            self._unprepare_all(wave_handle, pending)
            self._close_wave_out(wave_handle)
            with self._lock:
                if self._process is process:
                    self._process = None
                if self._wave_handle is wave_handle:
                    self._wave_handle = None

    def stop(self) -> None:
        with self._lock:
            process = self._process
            wave_handle = self._wave_handle

        if wave_handle is not None:
            self._reset_wave_out(wave_handle)
        if process is not None:
            self._terminate_process(process)

    def close(self) -> None:
        self.stop()

    def _build_ffmpeg_command(self, audio_url: str, http_headers: dict[str, str]) -> list[str]:
        try:
            import imageio_ffmpeg
        except ImportError as exc:
            raise RuntimeError(
                "Dependencias faltando. Rode: pip install -r requirements.txt"
            ) from exc

        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
        ]

        header_text = self._format_ffmpeg_headers(http_headers)
        if header_text:
            command.extend(["-headers", header_text])

        command.extend(
            [
                "-i",
                audio_url,
                "-vn",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(self.sample_rate),
                "-ac",
                str(self.channels),
                "pipe:1",
            ]
        )
        return command

    def _format_ffmpeg_headers(self, headers: dict[str, str]) -> str:
        return "".join(f"{key}: {value}\r\n" for key, value in headers.items() if value)

    def _load_winmm(self):
        try:
            winmm = ctypes.WinDLL("winmm")
        except AttributeError as exc:
            raise RuntimeError("Este backend de audio funciona no Windows.") from exc

        winmm.waveOutOpen.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.UINT,
            ctypes.POINTER(WaveFormatEx),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        winmm.waveOutOpen.restype = wintypes.UINT
        winmm.waveOutPrepareHeader.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(WaveHeader),
            wintypes.UINT,
        ]
        winmm.waveOutPrepareHeader.restype = wintypes.UINT
        winmm.waveOutWrite.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(WaveHeader),
            wintypes.UINT,
        ]
        winmm.waveOutWrite.restype = wintypes.UINT
        winmm.waveOutUnprepareHeader.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(WaveHeader),
            wintypes.UINT,
        ]
        winmm.waveOutUnprepareHeader.restype = wintypes.UINT
        winmm.waveOutReset.argtypes = [ctypes.c_void_p]
        winmm.waveOutReset.restype = wintypes.UINT
        winmm.waveOutClose.argtypes = [ctypes.c_void_p]
        winmm.waveOutClose.restype = wintypes.UINT
        winmm.waveOutGetErrorTextW.argtypes = [
            wintypes.UINT,
            wintypes.LPWSTR,
            wintypes.UINT,
        ]
        winmm.waveOutGetErrorTextW.restype = wintypes.UINT
        return winmm

    def _open_wave_out(self) -> ctypes.c_void_p:
        fmt = WaveFormatEx(
            wFormatTag=self.WAVE_FORMAT_PCM,
            nChannels=self.channels,
            nSamplesPerSec=self.sample_rate,
            nAvgBytesPerSec=self.sample_rate * self.block_align,
            nBlockAlign=self.block_align,
            wBitsPerSample=self.bits_per_sample,
            cbSize=0,
        )
        handle = ctypes.c_void_p()
        result = self._winmm.waveOutOpen(
            ctypes.byref(handle),
            self.WAVE_MAPPER,
            ctypes.byref(fmt),
            0,
            0,
            self.CALLBACK_NULL,
        )
        self._check_result(result, "abrir audio do Windows")
        return handle

    def _write_chunk(
        self,
        wave_handle: ctypes.c_void_p,
        chunk: bytes,
        pending: list[tuple[ctypes.Array, WaveHeader]],
    ) -> None:
        data = ctypes.create_string_buffer(chunk)
        header = WaveHeader(
            lpData=ctypes.cast(data, ctypes.c_void_p).value,
            dwBufferLength=len(chunk),
            dwBytesRecorded=0,
            dwUser=None,
            dwFlags=0,
            dwLoops=0,
            lpNext=None,
            reserved=None,
        )
        header_size = ctypes.sizeof(header)
        self._check_result(
            self._winmm.waveOutPrepareHeader(wave_handle, ctypes.byref(header), header_size),
            "preparar buffer de audio",
        )
        try:
            self._check_result(
                self._winmm.waveOutWrite(wave_handle, ctypes.byref(header), header_size),
                "enviar audio para o Windows",
            )
        except Exception:
            self._winmm.waveOutUnprepareHeader(wave_handle, ctypes.byref(header), header_size)
            raise
        pending.append((data, header))

    def _wait_for_buffer_room(
        self,
        wave_handle: ctypes.c_void_p,
        pending: list[tuple[ctypes.Array, WaveHeader]],
        stop_event: threading.Event,
        skip_event: threading.Event,
    ) -> None:
        while len(pending) >= self.MAX_PENDING_BUFFERS:
            if stop_event.is_set() or skip_event.is_set():
                return
            self._drain_done_buffers(wave_handle, pending)
            if len(pending) >= self.MAX_PENDING_BUFFERS:
                time.sleep(0.015)

    def _drain_done_buffers(
        self,
        wave_handle: ctypes.c_void_p,
        pending: list[tuple[ctypes.Array, WaveHeader]],
    ) -> None:
        still_pending: list[tuple[ctypes.Array, WaveHeader]] = []
        for data, header in pending:
            if header.dwFlags & self.WHDR_DONE:
                self._winmm.waveOutUnprepareHeader(
                    wave_handle,
                    ctypes.byref(header),
                    ctypes.sizeof(header),
                )
            else:
                still_pending.append((data, header))
        pending[:] = still_pending

    def _unprepare_all(
        self,
        wave_handle: ctypes.c_void_p,
        pending: list[tuple[ctypes.Array, WaveHeader]],
    ) -> None:
        for _data, header in pending:
            self._winmm.waveOutUnprepareHeader(
                wave_handle,
                ctypes.byref(header),
                ctypes.sizeof(header),
            )
        pending.clear()

    def _reset_wave_out(self, wave_handle: ctypes.c_void_p) -> None:
        try:
            self._winmm.waveOutReset(wave_handle)
        except Exception:
            pass

    def _close_wave_out(self, wave_handle: ctypes.c_void_p) -> None:
        try:
            self._winmm.waveOutClose(wave_handle)
        except Exception:
            pass

    def _terminate_process(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def _read_stderr(self, process: subprocess.Popen) -> str:
        stderr = b""
        if process.stderr is not None:
            try:
                stderr = process.stderr.read() or b""
            except Exception:
                stderr = b""
        return stderr.decode("utf-8", errors="replace").strip() or "erro desconhecido"

    def _check_result(self, result: int, action: str) -> None:
        if result != 0:
            raise RuntimeError(f"Falha ao {action}: {self._wave_error_text(result)}")

    def _wave_error_text(self, result: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        try:
            self._winmm.waveOutGetErrorTextW(result, buffer, len(buffer))
        except Exception:
            return f"codigo {result}"
        return buffer.value or f"codigo {result}"


class MusicQueuePlayer:
    def __init__(
        self,
        resolver: TrackResolver,
        audio_backend: AudioBackend,
        say: Callable[[str], None] = print,
    ) -> None:
        self.resolver = resolver
        self.audio_backend = audio_backend
        self.say = say
        self._queue: Deque[Track] = deque()
        self._current: Optional[Track] = None
        self._lock = threading.Lock()
        self._item_event = threading.Event()
        self._skip_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="music-worker",
            daemon=True,
        )
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def enqueue(self, track: Track) -> int:
        return self.enqueue_many([track])

    def enqueue_many(self, tracks: list[Track]) -> int:
        if not tracks:
            return 0

        with self._lock:
            start_position = len(self._queue) + (1 if self._current else 0) + 1
            self._queue.extend(tracks)
        self._item_event.set()
        return start_position

    def skip(self) -> bool:
        with self._lock:
            has_current = self._current is not None

        if not has_current:
            return False

        self._skip_event.set()
        self.audio_backend.stop()
        return True

    def skip_playlist(self) -> tuple[bool, int]:
        with self._lock:
            current = self._current
            if current is None:
                return False, 0

            removed = 0
            if current.playlist_key:
                before = len(self._queue)
                self._queue = deque(
                    track for track in self._queue if track.playlist_key != current.playlist_key
                )
                removed = before - len(self._queue)

        self._skip_event.set()
        self.audio_backend.stop()
        return True, removed

    def clear_queue(self) -> int:
        with self._lock:
            removed = len(self._queue)
            self._queue.clear()
        return removed

    def snapshot(self) -> QueueSnapshot:
        with self._lock:
            return QueueSnapshot(self._current, list(self._queue))

    def shutdown(self) -> None:
        self._stop_event.set()
        self._skip_event.set()
        self._item_event.set()
        self.audio_backend.stop()
        if self._started and threading.current_thread() is not self._thread:
            self._thread.join(timeout=10)
        self.audio_backend.close()
        if not self._thread.is_alive():
            self.resolver.close()
        else:
            self.say("Player ainda encerrando; tente fechar de novo se o audio continuar.")

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            self._item_event.wait(timeout=0.2)
            self._item_event.clear()

            while not self._stop_event.is_set():
                with self._lock:
                    if not self._queue:
                        self._current = None
                        break
                    track = self._queue.popleft()
                    self._current = track
                    self._skip_event.clear()

                prepared: Optional[PreparedTrack] = None
                try:
                    prepared = self.resolver.prepare(track, self._skip_event, self._stop_event)
                    track.title = prepared.title
                    track.source_url = prepared.source_url

                    if self._skip_event.is_set() or self._stop_event.is_set():
                        raise PlaybackSkipped()

                    self.say(f"Tocando: {track.display_name}")
                    self.audio_backend.play(
                        prepared.audio_url,
                        prepared.http_headers,
                        self._stop_event,
                        self._skip_event,
                    )
                except PlaybackSkipped:
                    pass
                except Exception as exc:
                    if not self._stop_event.is_set():
                        self.say(f"Erro: {exc}")
                finally:
                    if prepared is not None:
                        self.resolver.cleanup(prepared)
                    with self._lock:
                        if self._current is track:
                            self._current = None
                    self._skip_event.clear()


def format_queue(snapshot: QueueSnapshot) -> str:
    lines: list[str] = []
    if snapshot.current is None:
        lines.append("Tocando agora: nada")
    else:
        lines.append(f"Tocando agora: {snapshot.current.display_name}")

    if not snapshot.queued:
        lines.append("Fila: vazia")
        return "\n".join(lines)

    lines.append("Fila:")
    for index, track in enumerate(snapshot.queued, start=1):
        lines.append(f"{index}. {track.display_name}")
    return "\n".join(lines)


def print_help() -> None:
    print(
        "\n".join(
            [
                "Comandos:",
                "  m!p <nome ou URL>    adiciona musica/video/playlist do YouTube",
                "  m!s                   para a musica atual e pula para a proxima",
                "  m!sp                  pula a playlist atual",
                "  m!splaylist           igual ao m!sp",
                "  m!fila                mostra a fila",
                "  m!limpar              limpa a fila pendente",
                "  m!q                   sai do player",
                "  m!help                mostra esta ajuda",
            ]
        )
    )


def handle_command(command: Command, player: MusicQueuePlayer) -> bool:
    if command.name == CommandName.EMPTY:
        return True
    if command.name == CommandName.INVALID:
        print(command.error)
        return True
    if command.name == CommandName.UNKNOWN:
        print("Comando desconhecido. Use m!help para ver os comandos.")
        return True
    if command.name == CommandName.PLAY:
        try:
            tracks = player.resolver.resolve(command.argument)
        except Exception as exc:
            print(f"Erro: {exc}")
            return True

        position = player.enqueue_many(tracks)
        if len(tracks) == 1:
            print(f"{tracks[0].display_name} adicionado a fila (posicao {position})")
        else:
            print(f"{len(tracks)} musicas adicionadas a fila (a partir da posicao {position})")
        return True
    if command.name == CommandName.SKIP:
        if player.skip():
            print("Pulando musica atual...")
        else:
            print("Nada tocando agora.")
        return True
    if command.name == CommandName.SKIP_PLAYLIST:
        skipped, removed = player.skip_playlist()
        if skipped:
            if removed:
                print(f"Pulando playlist atual... {removed} musica(s) removida(s).")
            else:
                print("Pulando musica atual...")
        else:
            print("Nada tocando agora.")
        return True
    if command.name == CommandName.QUEUE:
        print(format_queue(player.snapshot()))
        return True
    if command.name == CommandName.CLEAR:
        removed = player.clear_queue()
        print(f"Fila limpa. {removed} musica(s) removida(s).")
        return True
    if command.name == CommandName.HELP:
        print_help()
        return True
    if command.name == CommandName.QUIT:
        return False

    return True


def make_prompt_reader(prompt: str = "> ") -> Callable[[], str]:
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout
    except ImportError:
        return lambda: input(prompt)

    session = PromptSession()

    def read_line() -> str:
        with patch_stdout():
            return session.prompt(prompt)

    return read_line


def main() -> None:
    player = MusicQueuePlayer(YouTubeTrackResolver(), FfmpegWaveOutAudioBackend())
    player.start()
    read_line = make_prompt_reader()

    print("Player iniciado. Use m!help para ver os comandos.")
    try:
        running = True
        while running:
            try:
                line = read_line()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            running = handle_command(parse_command(line), player)
    finally:
        print("Encerrando...")
        player.shutdown()


if __name__ == "__main__":
    main()