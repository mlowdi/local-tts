"""Brief opt-in announcements: prepare speech, pause media, chime, speak, restore."""

import argparse
from array import array
from contextlib import contextmanager
import fcntl
import io
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
import wave

from client import synthesize
from media import MediaSession

MAX_MESSAGE_CHARS = 300
SAMPLE_RATE = 24000


class AnnouncementCancelled(BaseException):
    """Cancellation must unwind media restoration, not be treated as a bus error."""


def validate_message(message: str) -> str:
    message = message.strip()
    if not message:
        raise ValueError("announcement message must not be empty")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"keep announcements to {MAX_MESSAGE_CHARS} characters or fewer")
    return message


@contextmanager
def finish_cleanup():
    """Defer repeated cancellation until children and paused media are restored."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


@contextmanager
def announcement_lock():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    directory = Path(runtime) if runtime else Path.home() / ".cache" / "local-tts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory / "local-tts-announcement.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def chime_pcm() -> bytes:
    """An original, soft four-note bell arpeggio; no external sound recording."""
    notes = (523.25, 659.25, 783.99, 1046.50)
    spacing, tail = 0.22, 0.62
    samples = array("h")
    for index in range(round((spacing * (len(notes) - 1) + tail) * SAMPLE_RATE)):
        time = index / SAMPLE_RATE
        value = 0.0
        for number, frequency in enumerate(notes):
            age = time - number * spacing
            if 0 <= age < tail:
                envelope = (1 - math.exp(-age * 180)) * math.exp(-age * 8)
                envelope *= min(1.0, (tail - age) / 0.03)
                bell = math.sin(2 * math.pi * frequency * age)
                bell += 0.25 * math.sin(2 * math.pi * frequency * 2.01 * age)
                value += 0.16 * envelope * bell
        samples.append(round(max(-1.0, min(1.0, value)) * 32767))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def prepare_audio(speech: bytes, destination: Path) -> float:
    with wave.open(io.BytesIO(speech)) as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getcomptype()) != (SAMPLE_RATE, 1, 2, "NONE"):
            raise ValueError("speech server returned an unexpected WAV format")
        frames = source.getnframes()
        pcm = source.readframes(frames)
        if frames == 0 or len(pcm) != frames * 2:
            raise ValueError("speech server returned empty or truncated audio")
    chime = chime_pcm()
    pause = b"\0\0" * round(0.18 * SAMPLE_RATE)
    with wave.open(str(destination), "wb") as output:
        output.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        output.writeframes(chime)
        output.writeframes(pause)
        output.writeframes(pcm)
    return (len(chime) + len(pause) + len(pcm)) / (2 * SAMPLE_RATE)


def play_audio(path: Path) -> None:
    player = subprocess.Popen(["pw-play", str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, error = player.communicate()
    except BaseException:
        with finish_cleanup():
            if player.poll() is None:
                player.terminate()
            try:
                player.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                player.kill()
                player.communicate()
        raise
    if player.returncode:
        raise RuntimeError(f"audio playback failed: {error.strip() or f'pw-play exited {player.returncode}'}")


def announce(message: str) -> dict:
    media = MediaSession()
    result = {"status": "error", "paused": media.paused, "resumed": media.resumed, "skipped": media.skipped, "warnings": media.warnings}
    try:
        message = validate_message(message)
        for executable in ("pw-play", "busctl"):
            if shutil.which(executable) is None:
                raise RuntimeError(f"{executable} is required for desktop announcements; see the README")
        with announcement_lock():
            # A slow/unavailable TTS server never leaves the user's movie paused.
            speech = synthesize(message)
            with tempfile.TemporaryDirectory(prefix="local-announce-") as directory:
                audio = Path(directory) / "announcement.wav"
                result["audio_seconds"] = prepare_audio(speech, audio)
                try:
                    media.pause_playing()
                    play_audio(audio)
                    result["status"] = "played"
                finally:
                    with finish_cleanup():
                        media.resume_paused()
    except AnnouncementCancelled:
        result["status"] = "cancelled"
        result["error"] = "Announcement cancelled; media restoration was attempted. Check resumed, skipped, and warnings."
    except HTTPError as exc:
        result["error"] = f"speech service returned HTTP {exc.code}"
    except URLError as exc:
        result["error"] = f"speech service unavailable: {exc.reason}; run systemctl --user start local-tts"
    except (OSError, ValueError, RuntimeError, wave.Error) as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(prog="local-announce", description=__doc__)
    parser.add_argument("text", nargs="*", help="Brief message; omit to read stdin (maximum 300 characters)")
    parser.add_argument("--json", action="store_true", help="Return machine-readable status after media restoration")
    args = parser.parse_args()

    def cancel(_signum, _frame):
        raise AnnouncementCancelled()

    previous = {kind: signal.signal(kind, cancel) for kind in (signal.SIGINT, signal.SIGTERM)}
    try:
        if not args.text and sys.stdin.isatty():
            parser.error("provide a message or pipe it on stdin")
        message = " ".join(args.text) if args.text else sys.stdin.read()
        result = announce(message)
    except AnnouncementCancelled:
        result = {"status": "cancelled", "paused": [], "resumed": [], "skipped": [], "warnings": [], "error": "Announcement cancelled before playback."}
    finally:
        for kind, handler in previous.items():
            signal.signal(kind, handler)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result["status"] == "played":
            print(f"Announcement played; resumed {len(result['resumed'])} media player(s).")
        else:
            print(f"local-announce: {result.get('error', result['status'])}", file=sys.stderr)
        for warning in result["warnings"]:
            print(f"local-announce: {warning}", file=sys.stderr)
    return {"played": 0, "cancelled": 130, "error": 1}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
