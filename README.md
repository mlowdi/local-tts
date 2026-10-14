# Local TTS

Local TTS is a Linux, CPU-only speech service built around
[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M). It provides:

- an OpenAI-style local HTTP speech endpoint;
- `local-say` for playback or 24 kHz mono PCM16 WAV output; and
- `local-audiobook` for converting a DRM-free English EPUB into a chaptered
  M4B plus individually playable M4A chapters, with safe chapter-level resume.

The service uses four CPU inference threads, keeps the model loaded while it is
running, and needs no GPU, cloud account, or API key. After setup, synthesis and
EPUB processing are local and require no network connection.

## Requirements

- Linux with a user systemd session (unless installing with `--no-service`)
- Python 3.10 or newer (`python3`) for the installer; the application uses a uv-managed Python
  3.13 environment
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- FFmpeg and ffprobe for audiobook output
- `pw-play` only if `local-say` should play through PipeWire; saving WAV files
  does not require it

Install the system packages on Fedora:

```bash
sudo dnf install python3 curl ffmpeg-free
sudo dnf install pipewire-utils  # optional: provides pw-play
```

Fedora's distro `ffmpeg-free` package is codec-limited, but current builds
provide the native `aac` encoder used here. If using a different or older
FFmpeg build, confirm that its encoder list contains `aac`; having an AAC
decoder alone is not sufficient.

On Debian or Ubuntu:

```bash
sudo apt update
sudo apt install python3 curl ffmpeg
sudo apt install pipewire-bin  # optional: provides pw-play
```

Install uv using the [official installation
instructions](https://docs.astral.sh/uv/getting-started/installation/). Their
Unix installer command is:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Expect roughly 0.5 GB of installed model and Python environment data, in
addition to generated audio and temporary download space. A loaded service has
used roughly 1 GB of RAM on one system. These are planning baselines, not
promises: package versions, allocator behavior, input, and platform can change
both figures. Keeping the combined M4B and its per-chapter M4A files uses about
60 MB per finished hour at the configured bitrate.

## Install

From the repository root:

```bash
python3 install.py
```

The installer:

1. runs `uv sync --frozen --python 3.13` against the pinned lockfile;
2. verifies every model file against `models/SHA256SUMS`, reusing verified
   files and atomically downloading and checking missing or invalid files;
3. installs `local-say`, `local-audiobook`, and `local-tts-server` command
   links; and
4. installs, enables, and starts the `local-tts.service` user unit.

It refuses to replace unrelated command links or service units. Rerunning it
for this same checkout is safe.

To prepare the environment, models, and commands without touching systemd:

```bash
python3 install.py --no-service
```

Commands go to `~/.local/bin` by default. Choose another directory with:

```bash
python3 install.py --bin-dir "$HOME/bin"
```

Ensure the selected directory is on `PATH`. For the default, add this to the
appropriate shell startup file if needed, then start a new shell:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

The installed links and service refer back to this checkout, and model assets
remain under its `models/` directory. Keep the checkout at a stable path. If it
must be moved, follow the Uninstall section **before moving it** to remove the
old checkout's service and command links without deleting data. After moving,
recreate the path-dependent virtual environment with `uv venv --clear --python 3.13`
and rerun `python3 install.py` from the new root. The installer deliberately
refuses to take over links or a unit owned by another checkout path.

### Foreground server

With a `--no-service` installation, or after stopping the service, run:

```bash
local-tts-server
```

It stays in the foreground until Ctrl-C and listens on `127.0.0.1:8880`.
Additional arguments are passed to Uvicorn; for example, a temporary alternate
port can be selected with:

```bash
local-tts-server --port 18880
```

`local-say` and `local-audiobook` always use port 8880, so an alternate port is
primarily useful for direct API testing.

## Speak or save text

The service must be running before using `local-say`.

```bash
local-say "Hello! This sentence is generated on this computer."
local-say --voices
local-say --voice am_michael "This uses another American English voice."
local-say --voice bf_emma --speed 0.9 "This British English voice is slightly slower."
printf '%s\n' "Text can also arrive on standard input." | local-say
local-say --output reply.wav "Save this speech instead of playing it."
pw-play reply.wav
```

The default voice is `af_heart`. `--voices` reports the exact available US and
British English voice IDs. `--speed` must be a finite value from **0.5 through
2.0**, inclusive. Without `--output`, `local-say` uses `pw-play` and removes its
temporary WAV afterward. With `--output`, it writes a 24 kHz, mono, PCM16 WAV.

Text is spoken literally. Plain prose generally works better than Markdown,
source code, or markup. This project does not perform voice cloning, speech
recognition, or automatic narration of another application's replies.

## EPUB audiobooks

First inspect the sections that the EPUB declares in linear reading order:

```bash
local-audiobook /path/to/book.epub --list-chapters
```

Convert the complete book (the value of `--output` is a directory, not a file):

```bash
local-audiobook /path/to/book.epub --output /path/to/My-Audiobook
```

Select sections and narration settings:

```bash
local-audiobook /path/to/book.epub --chapters 2-4,7 \
  --voice bm_george --speed 0.95 --output /path/to/My-Audiobook-preview
```

Audiobooks use the same voice IDs as `local-say`, and `--speed` has the same
finite **0.5–2.0** inclusive bound. Without `--output`, the directory is
`<EPUB stem>-audiobook` beside the EPUB. The input EPUB is never modified. Output has this shape:

```text
My-Audiobook/
├── audiobook.m4b
├── chapter-0001.m4a
├── chapter-0002.m4a
├── manifest.json
├── chapters.ffconcat
├── chapters.ffmetadata
└── .lock
```

- `audiobook.m4b` combines the audio with title/author metadata and named
  chapter markers.
- `chapter-NNNN.m4a` files are playable mono AAC chapters and the resume cache.
- `manifest.json` records the source and extracted-text hashes, selected
  sections, model, voice, and speed.
- `chapters.ffconcat` and `chapters.ffmetadata` describe the final remux.
- `.lock` prevents two conversions from using one output directory at once.

Chapter audio is encoded as 24 kHz mono AAC targeting 64 kbit/s. The final M4B
copies that AAC without another lossy encode. Speech is requested in chunks of
at most 900 characters and streamed into FFmpeg, rather than retaining an
entire book's PCM audio in memory.

The numbers accepted by `--chapters` are the indices printed by
`--list-chapters`: readable, linear EPUB spine sections in their reading order.
They are not necessarily printed chapter numbers, TOC anchor numbers, manifest
order, or ZIP filenames. A publisher may split one printed chapter across
multiple spine files or place several chapters in one file. Inspect the list
before a long conversion. Selection preserves book reading order.

Press Ctrl-C to stop cleanly, then rerun the **same command**. Completed M4A
chapters are validated and reused; the interrupted chapter restarts from its
beginning. The manifest prevents accidental mixing: changing the EPUB contents,
section selection, voice, or speed requires a new output directory. A nonempty
unrelated directory is not overwritten. Keep the manifest and M4A files if
resume or later remuxing is desired.

### EPUB limitations

- Input must be a DRM-free, text-based English EPUB. DRM removal and OCR are not
  provided. A declared non-English language is rejected.
- Reading follows linear spine documents. Navigation, scripts, hidden content,
  image-only sections, and duplicate/nonlinear spine entries are skipped.
- Front matter, ordinary footnotes, and afterwords remain unless excluded with
  `--chapters`.
- The reader does not render external CSS or split one spine document at
  internal TOC anchors.
- Extracted prose is normalized for narration but is not rewritten, summarized,
  or sent to an LLM.
- The single speech engine serializes requests, so `local-say` may wait while an
  audiobook chunk is being generated. There are no automatic network retries
  or sub-chapter resume points.

No copyrighted book or sample passage is included; provide content you are
permitted to process.

## HTTP API

The service binds only to `127.0.0.1:8880`, has no authentication, and disables
HTTP access logging. Other processes running as local users may still submit
speech. Do not expose it on a network without adding authentication and
appropriate transport security.

Create a WAV with the OpenAI-style speech endpoint:

```bash
curl --fail --silent --show-error http://127.0.0.1:8880/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"kokoro","input":"Hello from the local speech service.","voice":"af_heart","response_format":"wav","speed":1.0}' \
  --output reply.wav
pw-play reply.wav
```

Inspect service metadata:

```bash
curl --fail http://127.0.0.1:8880/health
curl --fail http://127.0.0.1:8880/v1/models
curl --fail http://127.0.0.1:8880/v1/audio/voices
```

For compatible clients, use base URL `http://127.0.0.1:8880/v1`, model
`kokoro`, and format `wav`; there is no API key. This is deliberately only a
small subset of the OpenAI audio API:

- `model` must be `kokoro`.
- `input` is required after trimming and may contain at most **10,000
  characters**.
- `voice` must be one of `GET /v1/audio/voices`; the default is `af_heart`.
- `speed` must be finite and within **0.5–2.0**, inclusive.
- `response_format` must be `wav`.
- The endpoint returns one complete 24 kHz mono PCM16 WAV. It does not stream,
  return MP3/AAC, or accept voice instructions. Audiobook AAC/M4B encoding is a
  separate CLI feature.
- Synthesis requests are serialized and the CLI waits up to ten minutes. For
  lower conversational latency, send sentence-sized requests.

The API itself neither plays nor writes audio files.

## Service management

The standard installation enables the user service and starts it immediately.
It starts again at user login, not at system boot independently of a login;
the installer does not enable lingering.

```bash
systemctl --user status local-tts.service
systemctl --user start local-tts.service
systemctl --user stop local-tts.service       # also releases model memory
systemctl --user restart local-tts.service
systemctl --user enable --now local-tts.service
systemctl --user disable --now local-tts.service
journalctl --user -u local-tts.service -n 50 --no-pager
journalctl --user -u local-tts.service -f
```

Restart after changing server code or replacing model assets.

## Models, checksums, and network privacy

Kokoro-82M is distributed by its authors at
[hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M). This project
uses the full-precision ONNX model and voice data published with the
[kokoro-onnx `model-files-v1.1`
release](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.1).
The Kokoro model repository publishes its model under Apache-2.0. The expected
SHA-256 digests for the two downloaded assets are committed in
`models/SHA256SUMS`.

Every installer run checks existing assets. Verified assets are reused; a
needed download is written to a temporary file and is published only after its
checksum matches. Setup may contact the Python package source used by uv and
the GitHub release above. Normal server, speech, and audiobook operation makes
no external request; EPUB text and generated audio remain on the machine.

## Tests

After installation, run the unit tests from the repository root:

```bash
.venv/bin/python -m unittest discover -v
```

They use generated fixtures and do not require model files, a running service,
or network access. The FFmpeg container test runs when FFmpeg and ffprobe are
available and is otherwise skipped. The generated-unit parser regression runs
when `systemd-analyze` is available; it does not install or start a service.

The initial Linux integration check passed all 14 tests and exercised a fresh
source-only checkout in a path containing spaces, `%`, and a backslash. It
installed the locked environment, downloaded and verified both assets, started
a foreground server, generated WAV speech and a two-chapter M4B, and repeated
`--no-service` installation with networking disabled. The user-service install
was also exercised separately. These are verification results, not a claim of
testing every Linux distribution or architecture.

## Uninstall

From the repository root, first disable and remove only this project's generated
user unit, then remove only command links that resolve to this checkout. The
commands below intentionally leave the checkout, model assets, virtual
environment, EPUBs, and generated audio in place:

```bash
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
unit="$config_home/systemd/user/local-tts.service"
project_id=$(python3 -c 'import hashlib, os, pathlib; print(hashlib.sha256(os.fsencode(str(pathlib.Path.cwd().resolve()))).hexdigest())')

if [ -f "$unit" ] && [ ! -L "$unit" ] \
   && grep -Fqx -- '# Managed by local-tts install.py' "$unit" \
   && grep -Fqx -- "# Project ID: $project_id" "$unit"; then
  systemctl --user disable --now local-tts.service
  rm -- "$unit"
  systemctl --user daemon-reload
else
  printf '%s\n' "Refusing to remove an unrecognized or unrelated unit: $unit" >&2
fi

for command in local-say local-audiobook local-tts-server; do
  link="$HOME/.local/bin/$command"
  if [ -L "$link" ] && [ "$(readlink -f "$link")" = "$(pwd -P)/$command" ]; then
    rm -- "$link"
  fi
done
```

If installation used `--bin-dir`, replace `$HOME/.local/bin` with that same
absolute directory. Before removing the unit, inspect it if there is any doubt
that it was generated by this checkout; do not remove an unrelated unit with
the same name. After uninstalling, delete the checkout or generated audiobook
directories only if their retained data is no longer wanted.

## License

The project's original source code is licensed under the [MIT License](LICENSE).
Bundled/downloaded models, Python packages, and system tools retain their own
licenses; the complete installed runtime is **not** uniformly permissive.
Notably, [EbookLib](https://github.com/aerkalov/ebooklib) is licensed under the
GNU AGPL. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for provenance
and third-party license details.
