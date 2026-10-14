# Third-party components and model provenance

The [MIT license](LICENSE) covers the original source code and documentation in
this repository. It does **not** relicense dependencies, model weights, voice
data, input books, or generated readings of copyrighted books.

This is **not an all-permissively licensed runtime distribution**. In particular,
EbookLib is AGPL-3.0-or-later, and phonemizer/eSpeak NG are GPL-3.0-or-later.
The source repository does not vendor these components; the installer obtains
Python packages separately using `uv.lock`. Installing or distributing a combined
runtime does not remove their license obligations. If you redistribute a bundled
application, container, binary, modified dependencies, or offer a network service,
review the applicable upstream licenses and their source-disclosure requirements.
Do not describe such a bundle as "MIT-only."

These notes identify important components, not a substitute for their full
license texts or an exhaustive legal audit of every wheel and system library.
The installed distributions include their own license files and notices. Retain
those when redistributing them, and consult qualified advice where necessary.

## Speech model and voices

- Model: [hexgrad/Kokoro-82M v1.0](https://huggingface.co/hexgrad/Kokoro-82M),
  Apache-2.0 model weights. The model card records architecture, training-data
  acknowledgements, and attribution information.
- Runtime/export project: [thewh1teagle/kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx), MIT code.
- Download release: [model-files-v1.1](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.1).
  The release tag is the asset release, not a claim that the downloaded model
  itself is Kokoro v1.1. The selected files are `kokoro-v1.0.onnx` and
  `voices-v1.0.bin`.
- [models/SHA256SUMS](models/SHA256SUMS) pins the exact release asset hashes.
  The installer verifies both existing files and new downloads against these
  hashes. A changed upstream asset must not silently replace the pinned model.
- The assets are downloaded during setup and are not committed to this source
  repository. Consult the upstream model/export notices before redistributing
  them; the repository's MIT license does not apply to them.

## Runtime dependencies

| Component | Upstream license / notes |
| --- | --- |
| [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) | MIT; model assets are separately licensed as above. |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | MIT, with bundled third-party notices. |
| [phonemizer](https://github.com/bootphon/phonemizer#licence) | GPL-3.0-or-later. |
| [espeakng-loader](https://github.com/thewh1teagle/espeakng-loader) | MIT loader; its bundled [eSpeak NG](https://github.com/espeak-ng/espeak-ng#license-information) library/data are GPL-3.0-or-later. The loader's license does not replace eSpeak NG's. |
| [EbookLib](https://github.com/aerkalov/ebooklib) | AGPL-3.0-or-later. Used to read EPUBs. |
| [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/bs4/) | MIT. |
| [lxml](https://github.com/lxml/lxml) | BSD-3-Clause; bundled XML libraries have their own notices. |
| [SoundFile](https://github.com/bastibe/python-soundfile) | BSD-3-Clause; [libsndfile](https://github.com/libsndfile/libsndfile) is separately LGPL-2.1-or-later. |
| [NumPy](https://numpy.org/) | BSD-3-Clause and additional bundled-component licenses; consult the installed wheel's notices. |
| [FastAPI](https://github.com/fastapi/fastapi) / [Pydantic](https://github.com/pydantic/pydantic) | MIT. |
| [Uvicorn](https://github.com/Kludex/uvicorn) / [Starlette](https://github.com/Kludex/starlette) | BSD-3-Clause. |

`uv.lock` records exact direct and transitive Python dependency versions and
artifact hashes. More packages and native components are present than the key
components listed here; their license files remain authoritative.

## External system tools

- [FFmpeg](https://ffmpeg.org/legal.html) is installed separately and invoked as
  a command-line program for AAC encoding and M4B assembly. Its license depends
  on how that particular build was configured (LGPL/GPL and external libraries).
  A build using nonfree components can have additional redistribution restrictions.
  Check the actual build and upstream legal documentation before bundling it.
- PipeWire's `pw-play`, systemd, Python, Git, and uv are independently installed
  tools; they are not relicensed or vendored by this repository.

## Books and output audio

No EPUBs, model binaries, sample books, or generated audiobooks are included in
Git. Local ignored files can still contain private or copyrighted material;
share a Git clone/archive rather than an unfiltered copy of an installed working
directory. The software's license does not grant rights to distribute a book or
its narration. Use material you have the necessary rights to process and share.
