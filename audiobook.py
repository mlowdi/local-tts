"""Turn a DRM-free English EPUB into a chaptered, local Kokoro audiobook."""

import argparse
from decimal import Decimal
import fcntl
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener
import wave
import zipfile

from ebooklib import epub
from lxml.etree import XMLSyntaxError

from client import BASE_URL, synthesize
from epub_reader import Book, Chapter, read_book, select_chapters, split_text

SAMPLE_RATE = 24000


def encode_chapter(chapter: Chapter, book: Book, destination: Path, voice: str, speed: float):
    chunks = split_text(chapter.text)
    partial = destination.with_suffix(".part.m4a")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
        "-c:a", "aac", "-b:a", "64k", "-metadata", f"title={chapter.title}",
        "-metadata", f"album={book.title}", "-metadata", f"artist={book.author}",
        "-metadata", f"track={chapter.number}", "-f", "ipod", str(partial),
    ]
    started = time.monotonic()
    frames = 0
    try:
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen(command, stdin=subprocess.PIPE, stderr=errors) as encoder:
                try:
                    for index, text in enumerate(chunks, 1):
                        print(f"  Chunk {index}/{len(chunks)} ({len(text)} characters)", flush=True)
                        audio = synthesize(text, voice, speed)
                        with wave.open(io.BytesIO(audio)) as source:
                            if (source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getcomptype()) != (SAMPLE_RATE, 1, 2, "NONE"):
                                raise ValueError("Speech server returned an unexpected WAV format")
                            count = source.getnframes()
                            pcm = source.readframes(count)
                            if count == 0 or len(pcm) != count * 2:
                                raise ValueError("Speech server returned empty or truncated audio")
                        encoder.stdin.write(pcm)
                        # A short breath between requests; half a second between chapters.
                        pause_frames = SAMPLE_RATE // (4 if index < len(chunks) else 2)
                        encoder.stdin.write(b"\0\0" * pause_frames)
                        frames += count + pause_frames
                    encoder.stdin.close()
                    returncode = encoder.wait()
                except BaseException:
                    encoder.terminate()
                    raise
            if returncode:
                errors.seek(0)
                raise RuntimeError(f"Audio encoding failed: {errors.read().decode(errors='replace').strip()}")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    print(f"  Saved {frames / SAMPLE_RATE / 60:.1f} minutes of audio in {time.monotonic() - started:.1f}s", flush=True)


def audio_duration(path: Path) -> Decimal:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name:format=duration", "-of", "json", str(path),
    ], capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    if not data.get("streams") or data["streams"][0]["codec_name"] != "aac":
        raise ValueError(f"Not a valid cached AAC chapter: {path}")
    duration = Decimal(data["format"]["duration"])
    if not duration.is_finite() or duration <= 0:
        raise ValueError(f"Invalid audio duration: {path}")
    return duration


def _metadata(value: str) -> str:
    value = " ".join(value.splitlines()).replace("\\", "\\\\")
    for char in "=;#":
        value = value.replace(char, "\\" + char)
    return value


def assemble(book: Book, chapters: list[Chapter], directory: Path) -> Path:
    lines = ["ffconcat version 1.0"]
    metadata = [";FFMETADATA1", f"title={_metadata(book.title)}", f"artist={_metadata(book.author)}", "genre=Audiobook", "media_type=2"]
    elapsed = Decimal(0)
    for chapter in chapters:
        name = f"chapter-{chapter.number:04d}.m4a"
        duration = audio_duration(directory / name)
        lines.append(f"file '{name}'")
        metadata.extend([
            "[CHAPTER]", "TIMEBASE=1/1000",
            f"START={round(elapsed * 1000)}",
            f"END={round((elapsed + duration) * 1000)}",
            f"title={_metadata(chapter.title)}",
        ])
        elapsed += duration
    playlist = directory / "chapters.ffconcat"
    tags = directory / "chapters.ffmetadata"
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tags.write_text("\n".join(metadata) + "\n", encoding="utf-8")
    partial = directory / "audiobook.part.m4b"
    output = directory / "audiobook.m4b"
    try:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "concat", "-safe", "1", "-i", str(playlist),
            "-f", "ffmetadata", "-i", str(tags),
            "-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1",
            "-c:a", "copy", "-movflags", "+faststart", "-f", "ipod", str(partial),
        ], check=True)
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)
    print(f"Audiobook: {output} ({elapsed / 60:.1f} minutes)", flush=True)
    return output


def conversion_plan(source: Path, book: Book, chapters: list[Chapter], voice: str, speed: float) -> dict:
    with source.open("rb") as stream:
        source_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "version": 1, "source_sha256": source_hash, "title": book.title,
        "author": book.author, "model": "kokoro", "voice": voice, "speed": speed,
        "chapters": [{
            "number": chapter.number, "title": chapter.title, "href": chapter.href,
            "text_sha256": hashlib.sha256(chapter.text.encode()).hexdigest(),
        } for chapter in chapters],
    }


def prepare_output(directory: Path, plan: dict):
    manifest = directory / "manifest.json"
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != plan:
            raise ValueError("Output belongs to a different book, chapter selection, voice, or speed; choose another --output directory")
    else:
        if any(path.name != ".lock" for path in directory.iterdir()):
            raise ValueError("Output directory is not empty and has no audiobook manifest; choose an empty directory")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            try:
                json.dump(plan, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                temporary_path.replace(manifest)
            finally:
                temporary_path.unlink(missing_ok=True)


def generate(source: Path, directory: Path, book: Book, chapters: list[Chapter], voice: str, speed: float):
    for executable in ("ffmpeg", "ffprobe"):
        if shutil.which(executable) is None:
            raise ValueError(f"Required executable not found: {executable}")
    plan = conversion_plan(source, book, chapters, voice, speed)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another conversion is using this output directory") from exc
        # Check the endpoint before committing a new conversion's settings.
        if any(not (directory / f"chapter-{chapter.number:04d}.m4a").exists() for chapter in chapters):
            with build_opener(ProxyHandler({})).open(f"{BASE_URL}/v1/audio/voices", timeout=10) as response:
                voices = {item["id"] for item in json.load(response)["voices"]}
            if voice not in voices:
                raise ValueError(f"Unknown voice {voice!r}; use local-say --voices")
        prepare_output(directory, plan)
        for index, chapter in enumerate(chapters, 1):
            destination = directory / f"chapter-{chapter.number:04d}.m4a"
            if destination.exists():
                audio_duration(destination)
                print(f"Reusing [{index}/{len(chapters)}] {chapter.title}", flush=True)
                continue
            print(f"Generating [{index}/{len(chapters)}] {chapter.title}", flush=True)
            encode_chapter(chapter, book, destination, voice, speed)
        return assemble(book, chapters, directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", type=Path, help="Input EPUB (never modified)")
    parser.add_argument("-o", "--output", type=Path, help="Output directory (default: beside EPUB, <stem>-audiobook)")
    parser.add_argument("-v", "--voice", default="af_heart")
    parser.add_argument("-s", "--speed", type=float, default=1.0)
    parser.add_argument("--list-chapters", action="store_true", help="List section indices/titles without generating audio")
    parser.add_argument("--chapters", help="Indices from --list-chapters, e.g. 7 or 7-9,12; default: all")
    args = parser.parse_args()
    if not math.isfinite(args.speed) or not 0.5 <= args.speed <= 2.0:
        parser.error("--speed must be between 0.5 and 2.0")
    try:
        source = args.epub.expanduser().resolve()
        book = read_book(source)
        chapters = select_chapters(book.chapters, args.chapters)
        print(f"{book.title} — {book.author}", flush=True)
        if args.list_chapters:
            for chapter in chapters:
                print(f"{chapter.number:4d}  {chapter.title}  ({len(chapter.text):,} characters)")
            return 0
        if book.language and any(not language.strip().lower().startswith("en") for language in book.language.split(";")):
            raise ValueError(f"This speech setup supports English, but the EPUB declares {book.language!r}")
        directory = (args.output.expanduser() if args.output else source.with_name(source.stem + "-audiobook")).resolve()
        generate(source, directory, book, chapters, args.voice, args.speed)
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted. Rerun the same command to reuse completed chapters; the unfinished chapter restarts.", file=sys.stderr)
        return 130
    except HTTPError as exc:
        print(f"local-audiobook: HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}", file=sys.stderr)
    except URLError as exc:
        print(f"local-audiobook: {exc.reason}. Start speech with: systemctl --user start local-tts", file=sys.stderr)
    except (OSError, ValueError, RuntimeError, KeyError, epub.EpubException, XMLSyntaxError, zipfile.BadZipFile, wave.Error, subprocess.CalledProcessError) as exc:
        print(f"local-audiobook: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
