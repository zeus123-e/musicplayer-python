from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Optional, Protocol
from urllib.parse import parse_qs, urlparse


class CommandName:
    PLAY = "play"
    SKIP = "skip"
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
    requested_at: float = field(default_factory=time.time)

    @property
    def display_name(self) -> str:
        return self.title or self.query


@dataclass
class PreparedTrack:
    track: Track
    title: str
    source_url: str
    path: Path


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
        path: Path,
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
        "m!fila": CommandName.QUEUE,
        "m!limpar": CommandName.CLEAR,
        "m!q": CommandName.QUIT,
        "m!help": CommandName.HELP,
    }
    if lower in exact_commands:
        return Command(exact_commands[lower])

    if lower == "m!p":
        return Command(CommandName.INVALID, error='Use: m!p <nome ou URL>')

    if lower.startswith("m!p "):
        argument = raw[4:].strip()
        if not argument:
            return Command(CommandName.INVALID, error='Use: m!p <nome ou URL>')
        try:
            parts = shlex.split(argument)
        except ValueError as exc:
            return Command(CommandName.INVALID, error=f"Comando com aspas invalidas: {exc}")
        query = " ".join(parts).strip()
        if not query:
            return Command(CommandName.INVALID, error='Use: m!p <nome ou URL>')
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
    def __init__(self, temp_dir: Optional[Path] = None) -> None:
        self._owns_temp_dir = temp_dir is None
        self.temp_dir = temp_dir or Path(tempfile.mkdtemp(prefix="console_music_player_"))
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def resolve(self, query: str) -> list[Track]:
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError(
                "Dependencias faltando. Rode: pip install -r requirements.txt"
            ) from exc

        playlist_allowed = is_playlist_url(query)
        target = query if is_http_url(query) else f"ytsearch1:{query}"
        ydl_opts = {
            "extract_flat": "in_playlist" if playlist_allowed else False,
            "noplaylist": not playlist_allowed,
            "noprogress": True,
            "quiet": True,
            "no_warnings": True,
            "default_search": "ytsearch1",
            "logger": SilentYtdlpLogger(),
        }

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
        input_path, title, source_url = self._download_audio(track, skip_event, stop_event)
        try:
            self._raise_if_cancelled(skip_event, stop_event)
            wav_path = self._convert_to_wav(input_path, skip_event, stop_event)
        finally:
            self._unlink_quietly(input_path)

        return PreparedTrack(
            track=track,
            title=title or track.display_name,
            source_url=source_url or track.source_url,
            path=wav_path,
        )

    def cleanup(self, prepared: PreparedTrack) -> None:
        self._unlink_quietly(prepared.path)

    def close(self) -> None:
        if self._owns_temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _download_audio(
        self,
        track: Track,
        skip_event: threading.Event,
        stop_event: threading.Event,
    ) -> tuple[Path, str, str]:
        try:
            import imageio_ffmpeg
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError(
                "Dependencias faltando. Rode: pip install -r requirements.txt"
            ) from exc

        downloaded_files: list[Path] = []
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        target = track.source_url or (
            track.query if is_http_url(track.query) else f"ytsearch1:{track.query}"
        )

        def progress_hook(data: dict) -> None:
            if skip_event.is_set() or stop_event.is_set():
                raise PlaybackSkipped()
            if data.get("status") == "finished" and data.get("filename"):
                downloaded_files.append(Path(data["filename"]))

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(self.temp_dir / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "noprogress": True,
            "quiet": True,
            "no_warnings": True,
            "default_search": "ytsearch1",
            "ffmpeg_location": ffmpeg_exe,
            "logger": SilentYtdlpLogger(),
            "progress_hooks": [progress_hook],
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target, download=True)
        except PlaybackSkipped:
            raise
        except Exception as exc:
            if skip_event.is_set() or stop_event.is_set():
                raise PlaybackSkipped() from exc
            raise RuntimeError(f"Nao consegui baixar '{track.display_name}': {exc}") from exc

        self._raise_if_cancelled(skip_event, stop_event)
        info = self._select_entry(info)
        title = str(info.get("title") or track.display_name)
        source_url = str(info.get("webpage_url") or info.get("original_url") or track.source_url)
        input_path = self._find_downloaded_file(downloaded_files, info)
        return input_path, title, source_url

    def _convert_to_wav(
        self,
        input_path: Path,
        skip_event: threading.Event,
        stop_event: threading.Event,
    ) -> Path:
        try:
            import imageio_ffmpeg
        except ImportError as exc:
            raise RuntimeError(
                "Dependencias faltando. Rode: pip install -r requirements.txt"
            ) from exc

        output_path = self.temp_dir / f"{uuid.uuid4().hex}.wav"
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(output_path),
        ]

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        try:
            while process.poll() is None:
                if skip_event.is_set() or stop_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise PlaybackSkipped()
                time.sleep(0.1)

            stdout, stderr = process.communicate()
            if process.returncode != 0:
                message = (stderr or stdout).decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"FFmpeg falhou ao converter o audio: {message}")
        except Exception:
            self._unlink_quietly(output_path)
            raise

        return output_path

    def _find_downloaded_file(self, downloaded_files: list[Path], info: dict) -> Path:
        existing = [path for path in downloaded_files if path.exists()]
        if existing:
            return existing[-1]

        requested_downloads = info.get("requested_downloads") or []
        for item in requested_downloads:
            filepath = item.get("filepath") or item.get("filename")
            if filepath and Path(filepath).exists():
                return Path(filepath)

        fallback = self.temp_dir / f"{info.get('id')}.{info.get('ext')}"
        if fallback.exists():
            return fallback

        raise RuntimeError("Download terminou, mas o arquivo de audio nao foi encontrado.")

    def _tracks_from_info(self, info: dict, query: str, playlist_allowed: bool) -> list[Track]:
        if "entries" not in info:
            return [self._track_from_entry(info, query)]

        entries = [entry for entry in info.get("entries") or [] if entry]
        if not entries:
            return []

        selected = entries if playlist_allowed else entries[:1]
        return [self._track_from_entry(entry, query) for entry in selected]

    def _track_from_entry(self, entry: dict, query: str) -> Track:
        title = str(entry.get("title") or query)
        source_url = self._entry_url(entry, query)
        return Track(query=query, title=title, source_url=source_url)

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

    def _raise_if_cancelled(
        self,
        skip_event: threading.Event,
        stop_event: threading.Event,
    ) -> None:
        if skip_event.is_set() or stop_event.is_set():
            raise PlaybackSkipped()

    def _unlink_quietly(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


class WinsoundAudioBackend:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def play(
        self,
        path: Path,
        stop_event: threading.Event,
        skip_event: threading.Event,
    ) -> None:
        try:
            import winsound
        except ImportError as exc:
            raise RuntimeError("Este backend de audio funciona no Windows.") from exc

        duration = self._wav_duration(path)
        with self._lock:
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)

        deadline = time.monotonic() + duration
        try:
            while time.monotonic() < deadline:
                if stop_event.is_set() or skip_event.is_set():
                    self.stop()
                    break
                time.sleep(0.05)
        finally:
            if stop_event.is_set() or skip_event.is_set():
                self.stop()

    def stop(self) -> None:
        try:
            import winsound
        except ImportError:
            return

        with self._lock:
            winsound.PlaySound(None, 0)

    def close(self) -> None:
        self.stop()

    def _wav_duration(self, path: Path) -> float:
        import wave

        with wave.open(str(path), "rb") as wav_file:
            frames = wav_file.getnframes()
            rate = wav_file.getframerate()
            if rate <= 0:
                raise RuntimeError("Arquivo WAV invalido.")
            return frames / float(rate)


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
            self.say("Download ainda encerrando; alguns temporarios podem ficar para limpeza do sistema.")

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
                    self.audio_backend.play(prepared.path, self._stop_event, self._skip_event)
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
                '  m!p <nome ou URL>    adiciona musica/video/playlist do YouTube',
                "  m!s                   para a musica atual e pula para a proxima",
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
    player = MusicQueuePlayer(YouTubeTrackResolver(), WinsoundAudioBackend())
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
