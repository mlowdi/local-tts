# Local TTS

Local TTS is a Linux, CPU-only speech service based on [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M). It provides:

- An OpenAI-style local HTTP speech endpoint.
- `local-say` for playback or 24 kHz mono PCM16 WAV output.
- `local-audiobook` for conversion of a DRM-free English EPUB into a chaptered M4B and individually playable M4A chapters, with chapter-level resume.
- `local-announce` and an optional tool for the OMP coding-agent host, for brief spoken announcements with a chime and guarded media pause and resume.

The service uses four CPU inference threads and keeps the model loaded while running. It requires no GPU, cloud account, or API key. Speech generation and EPUB processing run locally. No external network connection is required after setup.

## Requirements

- Linux with a user systemd session, unless installation uses `--no-service`.
- Python 3.10 or newer (`python3`) for the installer. The application uses a uv-managed Python 3.13 environment.
- [uv](https://docs.astral.sh/uv/getting-started/installation/).
- FFmpeg and ffprobe for audiobook output.
- `pw-play` for direct speech playback and announcements. WAV file output does not require it.
- `busctl` and access to the desktop user D-Bus session for optional announcements.

The system packages on Fedora are:

```bash
sudo dnf install python3 curl ffmpeg-free
sudo dnf install pipewire-utils  # optional: provides pw-play
```

Fedora's `ffmpeg-free` package has a limited codec set. Current builds provide the native `aac` encoder required for audiobook output. Other or older FFmpeg builds must include `aac` in their encoder list. An AAC decoder alone is insufficient.

The system packages on Debian or Ubuntu are:

```bash
sudo apt update
sudo apt install python3 curl ffmpeg
sudo apt install pipewire-bin  # optional: provides pw-play
```

The [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/) describe uv setup. The Unix installer command is:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

The model files and Python environment require approximately 0.5 GB, excluding generated audio and temporary download space. A loaded service used approximately 1 GB of RAM on one system. These figures are planning estimates. Package versions, allocator behavior, input, and platform affect disk and memory use. Retaining the combined M4B and its per-chapter M4A files requires about 60 MB per finished hour at the configured bitrate.

## Install

Installation runs from the repository root:

```bash
python3 install.py
```

The installer performs these operations:

1. Runs `uv sync --frozen --python 3.13` against the pinned lockfile.
2. Verifies each model file against `models/SHA256SUMS`. It reuses verified files and downloads and checks missing or invalid files before atomic installation.
3. Installs command links for `local-say`, `local-audiobook`, `local-tts-server`, and `local-announce`.
4. Installs, enables, and starts the `local-tts.service` user unit.

The installer refuses to replace unrelated command links or service units. Repeated installation from the same checkout is safe.

The `--no-service` option installs the environment, models, and commands without modifying systemd:

```bash
python3 install.py --no-service
```

The default command directory is `~/.local/bin`. The `--bin-dir` option selects another directory:

```bash
python3 install.py --bin-dir "$HOME/bin"
```

The command directory must be on `PATH`. For the default directory, the following line can be added to the appropriate shell startup file. A new shell loads the updated setting.

```bash
export PATH="$HOME/.local/bin:$PATH"
```

The command links and service refer to the checkout. Model files remain in its `models/` directory, so the checkout must stay at a stable path. Before a checkout move, the user must follow the Uninstall procedure to remove its service and command links without deleting data. After the move, `uv venv --clear --python 3.13` recreates the path-dependent virtual environment. Installation then runs with `python3 install.py` from the new repository root. The installer refuses to replace links or a unit owned by another checkout path.

### Foreground server

A `--no-service` installation uses the foreground server. The same command can run after the user stops the installed service:

```bash
local-tts-server
```

The server runs in the foreground until Ctrl-C and listens on `127.0.0.1:8880`. Additional arguments pass to Uvicorn. For example, this command selects a temporary alternate port:

```bash
local-tts-server --port 18880
```

`local-say`, `local-audiobook`, and `local-announce` always use port 8880. An alternate port is primarily useful for direct API testing.

## Speak or save text

`local-say` requires a running service.

```bash
local-say "Hello! This sentence is generated on this computer."
local-say --voices
local-say --voice am_michael "This uses another American English voice."
local-say --voice bf_emma --speed 0.9 "This British English voice is slightly slower."
printf '%s\n' "Text can also arrive on standard input." | local-say
local-say --output reply.wav "Save this speech instead of playing it."
pw-play reply.wav
```

The default voice is `af_heart`. `--voices` lists the available US and British English voice IDs. `--speed` must be finite and between 0.5 and 2.0, inclusive. Without `--output`, `local-say` plays the audio through `pw-play` and removes its temporary WAV afterward. With `--output`, it writes a 24 kHz mono PCM16 WAV.

The service speaks text literally. Plain prose generally produces better results than Markdown, source code, or markup. Local TTS does not provide voice cloning, speech recognition, or automatic narration of another application's replies.

## OMP announcements (optional)

The extension in `omp-extension/` adds the `announce` tool to the OMP coding-agent host. Explicit agent calls invoke the tool. The extension has no reply, completion, or automatic speech hooks. The tool description specifies sparing use for requests for user attention or completion of important work. It discourages routine progress narration. Messages must be nonempty after trimming and contain at most 300 characters.

The extension requires OMP, the running local speech service, `local-announce` on OMP's `PATH`, `pw-play`, and `busctl` from systemd. OMP must run as the desktop user with access to the same PipeWire server and user D-Bus session as the media players. `playerctl` is not required. The extension was tested with OMP 18.9.1.

After Local TTS installation, the following command links the extension from the repository root:

```bash
omp plugin link "$PWD/omp-extension"
```

OMP loads the linked extension after a restart. The following command loads the extension for one session without linking it:

```bash
omp -e "$PWD/omp-extension/index.ts"
```

The `announce` tool accepts a `message` field:

```json
{"message":"The conversion is complete. Would you like me to start the next book?"}
```

`local-announce` runs the same sequence directly:

```bash
local-announce "Your attention, please. The audiobook is ready."
printf '%s\n' "I need your decision before continuing." | local-announce --json
```

The helper generates speech before pausing media. Speech generation delays or an unavailable speech server therefore do not leave a movie paused. The helper controls local media through the Media Player Remote Interfacing Specification (MPRIS). It pauses eligible playing media, plays an original four-note bell chime followed by the message, and restores eligible players during cleanup.

Speech uses `af_heart` at normal speed. Announcements in the same desktop session run serially, including speech generation, playback, and restoration. The helper removes temporary audio afterward and does not change output volume.

Media restoration follows these rules:

- Already-paused media stays paused. A player must support control, pause, and play before the helper pauses it.
- MPRIS status can lag a successful `Pause` or `Play` request. The helper briefly waits for status publication without repeating control commands. It retains unconfirmed pause requests and rechecks them during cleanup. A stale `Playing` reply does not discard the request. A player never observed paused is not sent `Play`.
- Only players paused by the current invocation are eligible for resume. Their D-Bus owner, metadata, playback state, and position must still match. Position has a 1.5-second tolerance. The helper skips restoration after a track change, a seek beyond that tolerance, a stop, or a player replacement.
- KDE Connect remote-player proxies and the `playerctld` proxy are excluded.
- Players without usable MPRIS support cannot be paused and may overlap the announcement. Results include discovery and control failures. MPRIS cannot distinguish every user action.

Ctrl-C, SIGTERM, and OMP cancellation stop playback and attempt restoration before returning. The OMP tool permits 120 seconds in total, including queue time. It then allows up to 20 seconds for cleanup before forced termination. SIGKILL, crashes, or a lost desktop session can prevent restoration. Affected media may require manual resume. Results include `paused`, `resumed`, `skipped`, and `warnings` lists. They distinguish playback success, cancellation, and errors.

Announcements use PipeWire audio rather than desktop notifications. Full-screen notification suppression does not suppress the tool. A muted or unavailable audio output still prevents hearing the announcement. Speech generation runs locally. OMP's chat-model connection depends on the OMP configuration.

The following command disables the optional tool. The change takes effect after an OMP restart.

```bash
omp plugin disable omp-local-announce
```

## EPUB audiobooks

The `--list-chapters` option lists readable sections from the EPUB spine, which defines the book's reading order:

```bash
local-audiobook /path/to/book.epub --list-chapters
```

The following command converts the complete book. The `--output` value is a directory, not a file.

```bash
local-audiobook /path/to/book.epub --output /path/to/My-Audiobook
```

The `--chapters`, `--voice`, and `--speed` options select sections and narration settings:

```bash
local-audiobook /path/to/book.epub --chapters 2-4,7 \
  --voice bm_george --speed 0.95 --output /path/to/My-Audiobook-preview
```

Audiobooks use the same voice IDs as `local-say`. The `--speed` value must be finite and between 0.5 and 2.0, inclusive. Without `--output`, the output directory is `<EPUB stem>-audiobook` beside the EPUB. The input EPUB is never modified. The output directory contains:

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

- `audiobook.m4b` combines the audio with title and author metadata and named chapter markers.
- `chapter-NNNN.m4a` files contain playable mono AAC chapters and serve as the resume cache.
- `manifest.json` records source and extracted-text hashes, selected sections, model, voice, and speed.
- `chapters.ffconcat` and `chapters.ffmetadata` describe the final remux.
- `.lock` prevents concurrent conversions from using the same output directory.

Chapter audio uses 24 kHz mono AAC with a target bitrate of 64 kbit/s. The final M4B copies the AAC audio without another lossy encode. Speech requests contain at most 900 characters. The command streams the resulting audio into FFmpeg instead of retaining the entire book's PCM audio in memory.

`--chapters` accepts the indices printed by `--list-chapters`: readable, linear EPUB spine sections in reading order. These indices can differ from printed chapter numbers, table of contents (TOC) anchor numbers, manifest order, or ZIP filenames. A publisher can split a printed chapter across several spine files or place several chapters in one file. The section list therefore needs inspection before a long conversion. Section selection preserves book reading order.

Ctrl-C stops conversion cleanly. Rerunning the same command validates and reuses completed M4A chapters. The interrupted chapter restarts from its beginning. The manifest prevents accidental mixing of source content and narration settings. Changes to EPUB contents, section selection, voice, or speed require a new output directory. The command does not overwrite a nonempty unrelated directory. Resume and later remuxing require the manifest and M4A files.

### EPUB limitations

- Input must be a DRM-free, text-based English EPUB. Local TTS does not provide DRM removal or OCR. It rejects a declared non-English language.
- Reading follows linear spine documents. The reader skips navigation, scripts, hidden content, image-only sections, and duplicate or nonlinear spine entries.
- Front matter, ordinary footnotes, and afterwords remain unless excluded with `--chapters`.
- The reader does not render external CSS or split a spine document at internal TOC anchors.
- The reader normalizes extracted prose for narration. It does not rewrite, summarize, or send the prose to an LLM.
- The speech engine serializes requests. `local-say` may therefore wait while an audiobook chunk is being generated. There are no automatic network retries or sub-chapter resume points.

The repository includes no copyrighted book or sample passage. The user must have permission to process the input content.

## HTTP API

The service binds only to `127.0.0.1:8880`, requires no authentication, and disables HTTP access logging. Other processes running as local users can still submit speech requests. Network exposure requires authentication and appropriate transport security.

The OpenAI-style speech endpoint returns a WAV:

```bash
curl --fail --silent --show-error http://127.0.0.1:8880/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"kokoro","input":"Hello from the local speech service.","voice":"af_heart","response_format":"wav","speed":1.0}' \
  --output reply.wav
pw-play reply.wav
```

The service exposes readiness, model, and voice metadata at these endpoints:

```bash
curl --fail http://127.0.0.1:8880/health
curl --fail http://127.0.0.1:8880/v1/models
curl --fail http://127.0.0.1:8880/v1/audio/voices
```

Compatible clients use the base URL `http://127.0.0.1:8880/v1`, model `kokoro`, and format `wav`. No API key is required. The service implements a limited subset of the OpenAI audio API:

- `model` must be `kokoro`.
- `input` must be nonempty after trimming and contain at most 10,000 characters.
- `voice` must be listed by `GET /v1/audio/voices`. The default is `af_heart`.
- `speed` must be finite and between 0.5 and 2.0, inclusive.
- `response_format` must be `wav`.
- The endpoint returns one complete 24 kHz mono PCM16 WAV. It does not stream, return MP3 or AAC, or accept voice instructions. Audiobook AAC and M4B encoding is a separate CLI feature.
- The service serializes speech generation requests. The CLI waits up to ten minutes for a response. Sentence-sized requests reduce conversational latency.

The API does not play audio or write audio files.

## Service management

Standard installation enables and starts the user service. The service starts again at user login. It does not start at system boot independently of a login. The installer does not enable lingering.

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

Server code changes and model file replacements require a service restart.

## Models, checksums, and network privacy

The model authors distribute Kokoro-82M at [hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M). Local TTS uses the full-precision ONNX model and voice data from the [kokoro-onnx `model-files-v1.1` release](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.1). The Kokoro model repository publishes the model under Apache-2.0. `models/SHA256SUMS` contains the committed SHA-256 digests for the downloaded model and voice data files.

Each installer run verifies existing model files. The installer reuses valid files. It writes required downloads to temporary files and publishes them only after their checksums match. Setup can contact the Python package source configured for uv and the GitHub release above. Normal server, speech, and audiobook operation makes no external request. EPUB text and generated audio remain on the machine.

## Tests

The unit tests run from the repository root after installation:

```bash
.venv/bin/python -m unittest discover -v
```

The tests use generated fixtures and require no model files, running service, or network access. The FFmpeg container test runs when FFmpeg and ffprobe are available and is otherwise skipped. The generated-unit parser regression runs when `systemd-analyze` is available. It does not install or start a service.

### Verification history

The initial Linux integration check passed all 14 tests. It used a fresh source-only checkout in a path containing spaces, `%`, and a backslash. The check installed the locked environment, downloaded and verified both model files, and started a foreground server. It generated WAV speech and a two-chapter M4B. It also repeated `--no-service` installation with networking disabled. The user-service installation was tested separately. These results do not establish compatibility with every Linux distribution or architecture.

The announcement addition passed the 24-test Python suite and an installed OMP tool invocation through PipeWire. An isolated D-Bus scenario tested normal playback, SIGTERM during `pw-play`, and an unavailable playback device. Normal playback, cancellation, and device failure each restored the eligible playing fixture. Initially paused and KDE Connect fixtures remained unchanged. Extension AbortSignal propagation was tested against a real helper waiting on the announcement lock. A pre-aborted invocation was also tested. Audible quality was not independently assessed.

The delayed-MPRIS-state fix passed all 28 Python tests. A private D-Bus runtime scenario tested the CLI with delayed `Pause` and `Play` status publication. It also tested cancellation before the paused status appeared and a pause published after the initial observation window. Each scenario restored the playing fixture and left an already-paused fixture unchanged. These checks used isolated MPRIS fixtures rather than a live Spotify session.

## Uninstall

The following command removes the optional OMP extension link if it was installed. OMP must restart to unload the extension.

```bash
omp plugin uninstall omp-local-announce
```

The following commands run from the repository root. They disable and remove only the user unit generated for this checkout, then remove only command links that resolve to it. The checkout, model files, virtual environment, EPUBs, and generated audio remain in place.

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

for command in local-say local-audiobook local-tts-server local-announce; do
  link="$HOME/.local/bin/$command"
  if [ -L "$link" ] && [ "$(readlink -f "$link")" = "$(pwd -P)/$command" ]; then
    rm -- "$link"
  fi
done
```

If installation used `--bin-dir`, that same absolute directory must replace `$HOME/.local/bin` in the removal commands. The user must verify the unit's origin before removal if its ownership is uncertain. An unrelated unit with the same name must remain in place. After uninstalling, the checkout and generated audiobook directories can be deleted if their retained data is no longer required.

## License

The [MIT License](LICENSE) covers the project's original source code. Bundled or downloaded models, Python packages, and system tools retain their own licenses. The complete installed runtime is not uniformly permissive. [EbookLib](https://github.com/aerkalov/ebooklib) uses the GNU AGPL. [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) records component provenance and third-party license details.
