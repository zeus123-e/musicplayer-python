# Console Music Player

A Windows console music player written in Python. It searches YouTube, resolves the best matching song title, manages a playback queue, and uses simple `m!` commands inspired by chat music bots.

## Features

- Search YouTube by song name or play from a direct URL.
- Shows the resolved YouTube title before adding a track to the queue.
- Queue support: add multiple songs and play them in order.
- Skip the current track with `m!s`.
- Quiet download/conversion output.
- Clean interactive prompt powered by `prompt-toolkit`.
- No `pygame` dependency.

## Platform Support

Playback currently works on **Windows only** because the audio backend uses Python's built-in `winsound` module.

The YouTube search/download parts are cross-platform, but Linux/macOS playback would need a different audio backend such as `simpleaudio`, `sounddevice`, or `miniaudio`.

## Requirements

- Windows 10/11
- Python 3.11+
- Git
- Internet connection

Python dependencies:

- `yt-dlp`
- `imageio-ffmpeg`
- `prompt-toolkit`

## Download

Clone the repository:

```powershell
git clone https://github.com/zeus123-e/musicplayer-python.git
cd musicplayer-python
```

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Usage

```powershell
python main.py
```

Then type commands in the console:

```text
m!p love me not
```

Example output:

```text
Ravyn Lenae - Love Me Not (Official Music Video) adicionado a fila (posicao 1)
Tocando: Ravyn Lenae - Love Me Not (Official Music Video)
```

You can also use a direct YouTube URL:

```text
m!p https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

## Commands

| Command | Description |
| --- | --- |
| `m!p <song name or URL>` | Search YouTube and add the resolved track to the queue. |
| `m!s` | Stop the current track and skip to the next one. |
| `m!fila` | Show the current track and queued songs. |
| `m!limpar` | Clear the pending queue without stopping the current track. |
| `m!help` | Show available commands. |
| `m!q` | Quit the player. |

## How It Works

1. `yt-dlp` searches YouTube and resolves the real video title.
2. The selected audio is downloaded quietly.
3. `imageio-ffmpeg` converts the audio to a temporary WAV file.
4. `winsound` plays the WAV file.
5. The temporary file is removed after playback.

## Basic Check

```powershell
python -m py_compile main.py
```

For a manual smoke test, start the player and run:

```text
m!help
m!q
```

## Notes

This project depends on YouTube extraction through `yt-dlp`, so availability can change if YouTube changes its site behavior. Keeping `yt-dlp` updated is recommended.

```powershell
pip install --upgrade yt-dlp
```
