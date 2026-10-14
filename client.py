"""Speak text through the local service, or save it as a WAV file."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

BASE_URL = "http://127.0.0.1:8880"


def synthesize(text: str, voice: str = "af_heart", speed: float = 1.0) -> bytes:
    data = json.dumps({
        "model": "kokoro", "input": text, "voice": voice,
        "speed": speed, "response_format": "wav",
    }).encode("utf-8")
    request = Request(
        f"{BASE_URL}/v1/audio/speech", data=data,
        headers={"Content-Type": "application/json"},
    )
    with build_opener(ProxyHandler({})).open(request, timeout=600) as response:
        return response.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="*", help="Text to speak; omit to read stdin")
    parser.add_argument("-v", "--voice", default="af_heart", help="Voice ID (default: af_heart)")
    parser.add_argument("-s", "--speed", type=float, default=1.0, help="Speed, 0.5–2.0")
    parser.add_argument("-o", "--output", type=Path, help="Save WAV instead of playing")
    parser.add_argument("--voices", action="store_true", help="List available English voices")
    args = parser.parse_args()
    # Never send localhost speech requests through an environment-configured proxy.
    opener = build_opener(ProxyHandler({}))
    try:
        if args.voices:
            with opener.open(f"{BASE_URL}/v1/audio/voices", timeout=10) as response:
                for voice in json.load(response)["voices"]:
                    print(f'{voice["id"]:16} {voice["language"]}')
            return 0
        if not args.text and sys.stdin.isatty():
            parser.error("provide text or pipe it on stdin")
        text = " ".join(args.text) if args.text else sys.stdin.read()
        if not text.strip():
            parser.error("text must not be empty")
        audio = synthesize(text, args.voice, args.speed)
        if args.output:
            args.output.write_bytes(audio)
            print(f"Saved {args.output}", file=sys.stderr)
        else:
            with tempfile.NamedTemporaryFile(suffix=".wav") as output:
                output.write(audio)
                output.flush()
                subprocess.run(["pw-play", output.name], check=True)
        return 0
    except HTTPError as exc:
        print(f"local-say: HTTP {exc.code}: {exc.read().decode('utf-8')}", file=sys.stderr)
    except URLError as exc:
        print(
            f"local-say: {exc.reason}. Start the service with: "
            "systemctl --user start local-tts",
            file=sys.stderr,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"local-say: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
