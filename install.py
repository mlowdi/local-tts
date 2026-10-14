#!/usr/bin/env python3
"""Install the local TTS environment, models, commands, and user service."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen


COMMANDS = ("local-say", "local-audiobook", "local-tts-server", "local-announce")
MODEL_RELEASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.1"
)
MODEL_URLS = {
    "kokoro-v1.0.onnx": f"{MODEL_RELEASE}/kokoro-v1.0.onnx",
    "voices-v1.0.bin": f"{MODEL_RELEASE}/voices-v1.0.bin",
}
UNIT_NAME = "local-tts.service"
UNIT_MARKER = "# Managed by local-tts install.py"
PROJECT_ID_PREFIX = "# Project ID: "
HEALTH_URL = "http://127.0.0.1:8880/health"
HEALTH_TIMEOUT = 90.0


class InstallError(Exception):
    """An anticipated installation failure with a user-facing message."""


def project_id(root: Path) -> str:
    return hashlib.sha256(os.fsencode(str(root))).hexdigest()


def systemd_quote(value: str) -> str:
    """Quote a path as one systemd argument, including literal specifier signs."""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InstallError("systemd paths cannot contain control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InstallError("systemd paths must be valid UTF-8") from exc
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def render_unit(root: Path, template: str) -> str:
    replacements = {
        "{{PROJECT_ID}}": project_id(root),
        "{{SERVER_LAUNCHER}}": systemd_quote(str(root / "local-tts-server")),
    }
    for placeholder, value in replacements.items():
        if template.count(placeholder) != 1:
            raise InstallError(f"service template must contain exactly one {placeholder}")
        template = template.replace(placeholder, value)
    if "{{" in template or "}}" in template:
        raise InstallError("service template contains an unknown placeholder")
    return template


def load_checksums(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise InstallError(f"cannot read model checksum file {path}: {exc}") from exc

    checksums: dict[str, str] = {}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 2:
            raise InstallError(f"invalid checksum entry on {path}:{number}")
        digest, filename = fields
        filename = filename.removeprefix("*")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise InstallError(f"invalid SHA-256 digest on {path}:{number}")
        if filename in checksums:
            raise InstallError(f"duplicate checksum entry for {filename}")
        checksums[filename] = digest.lower()

    expected = set(MODEL_URLS)
    found = set(checksums)
    if found != expected:
        missing = ", ".join(sorted(expected - found)) or "none"
        unexpected = ", ".join(sorted(found - expected)) or "none"
        raise InstallError(
            f"model checksum manifest does not match the release files "
            f"(missing: {missing}; unexpected: {unexpected})"
        )
    return checksums


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def existing_model_is_valid(path: Path, expected: str) -> bool:
    try:
        return path.is_file() and sha256_file(path) == expected
    except OSError as exc:
        raise InstallError(f"cannot verify model file {path}: {exc}") from exc


def download_model(url: str, destination: Path, expected: str) -> None:
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".download", dir=destination.parent
        )
    except OSError as exc:
        raise InstallError(f"cannot create a temporary model file in {destination.parent}: {exc}") from exc

    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    try:
        request = Request(url, headers={"User-Agent": "local-tts-installer/1.0"})
        try:
            response = urlopen(request, timeout=60)
            with os.fdopen(file_descriptor, "wb") as output, response:
                file_descriptor = -1
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
        except (HTTPError, URLError, TimeoutError, http.client.HTTPException) as exc:
            raise InstallError(f"could not download {url}: {exc}") from exc
        except OSError as exc:
            raise InstallError(f"could not save {destination.name}: {exc}") from exc

        actual = digest.hexdigest()
        if actual != expected:
            raise InstallError(
                f"downloaded {destination.name} failed SHA-256 verification "
                f"(expected {expected}, got {actual}); the existing file was not changed"
            )
        try:
            os.chmod(temporary, 0o644)
            os.replace(temporary, destination)
        except OSError as exc:
            raise InstallError(f"cannot publish verified model {destination}: {exc}") from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_models(root: Path, checksums: dict[str, str]) -> None:
    model_directory = root / "models"
    for filename, url in MODEL_URLS.items():
        destination = model_directory / filename
        if existing_model_is_valid(destination, checksums[filename]):
            print(f"Model verified: {filename}")
            continue
        print(f"Downloading {filename} ...", flush=True)
        download_model(url, destination, checksums[filename])
        print(f"Model installed: {filename}")


def resolve_link(path: Path) -> Path | None:
    if not path.is_symlink():
        return None
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def check_link_collisions(bin_directory: Path, root: Path) -> None:
    if os.path.lexists(bin_directory) and not bin_directory.is_dir():
        raise InstallError(f"command directory exists but is not a directory: {bin_directory}")
    for command in COMMANDS:
        destination = bin_directory / command
        if not os.path.lexists(destination):
            continue
        source = (root / command).resolve()
        if resolve_link(destination) != source:
            raise InstallError(
                f"refusing to replace unrelated command {destination}; move or remove it first"
            )


def unit_is_owned_by(path: Path, expected_project_id: str) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    return UNIT_MARKER in lines and f"{PROJECT_ID_PREFIX}{expected_project_id}" in lines


def check_unit_collision(unit_path: Path, expected_project_id: str) -> None:
    if not os.path.lexists(unit_path):
        return
    if not unit_is_owned_by(unit_path, expected_project_id):
        raise InstallError(
            f"refusing to replace unrelated service unit {unit_path}; "
            "disable and move or remove it first"
        )


def require_program(name: str, purpose: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise InstallError(f"{name} is required {purpose}; install it and run this installer again")
    return executable


def preflight(root: Path, bin_directory: Path, install_service: bool) -> tuple[str, str, Path | None, str | None]:
    if not sys.platform.startswith("linux"):
        raise InstallError("this installer supports Linux only")

    required_files = [
        root / "pyproject.toml",
        root / "uv.lock",
        root / "models" / "SHA256SUMS",
        root / "deploy" / "local-tts.service.in",
        *(root / command for command in COMMANDS),
    ]
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise InstallError("the project checkout is incomplete; missing: " + ", ".join(missing))

    uv = require_program("uv", "to create the pinned Python 3.13 environment")
    require_program("ffmpeg", "to create audiobooks")
    require_program("ffprobe", "to validate audiobook audio")
    check_link_collisions(bin_directory, root)

    if not install_service:
        return uv, "", None, None

    systemctl = require_program("systemctl", "to install the user service")
    config_home_value = os.environ.get("XDG_CONFIG_HOME")
    if config_home_value:
        config_home = Path(config_home_value).expanduser()
        if not config_home.is_absolute():
            raise InstallError("XDG_CONFIG_HOME must be an absolute path")
    else:
        config_home = Path.home() / ".config"
    unit_path = config_home / "systemd" / "user" / UNIT_NAME
    identifier = project_id(root)
    check_unit_collision(unit_path, identifier)
    try:
        template = (root / "deploy" / "local-tts.service.in").read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"cannot read the service template: {exc}") from exc
    rendered = render_unit(root, template)
    return uv, systemctl, unit_path, rendered


def sync_dependencies(uv: str, root: Path) -> None:
    print("Installing the pinned Python 3.13 environment ...", flush=True)
    result = subprocess.run(
        [uv, "sync", "--frozen", "--python", "3.13"],
        cwd=root,
        text=True,
    )
    if result.returncode:
        raise InstallError(
            "uv could not install the locked environment; resolve the error above and run the installer again"
        )


def make_launchers_executable(root: Path) -> None:
    for command in COMMANDS:
        path = root / command
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError as exc:
            raise InstallError(f"cannot make launcher executable: {path}: {exc}") from exc


def install_command_links(bin_directory: Path, root: Path) -> None:
    try:
        bin_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallError(f"cannot create command directory {bin_directory}: {exc}") from exc

    for command in COMMANDS:
        source = (root / command).resolve()
        destination = bin_directory / command
        if os.path.lexists(destination):
            if resolve_link(destination) == source:
                continue
            raise InstallError(
                f"command collision appeared during installation: {destination}; it was not replaced"
            )
        relative_source = os.path.relpath(source, start=bin_directory)
        try:
            destination.symlink_to(relative_source)
        except FileExistsError as exc:
            raise InstallError(
                f"command collision appeared during installation: {destination}; it was not replaced"
            ) from exc
        except OSError as exc:
            raise InstallError(f"cannot install command link {destination}: {exc}") from exc
        print(f"Command installed: {destination}")


def write_service_unit(unit_path: Path, content: str, expected_project_id: str) -> None:
    try:
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{UNIT_NAME}.", dir=unit_path.parent, text=True
        )
    except OSError as exc:
        raise InstallError(f"cannot prepare service unit {unit_path}: {exc}") from exc

    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)

        if os.path.lexists(unit_path):
            check_unit_collision(unit_path, expected_project_id)
            os.replace(temporary, unit_path)
        else:
            try:
                os.link(temporary, unit_path)
            except FileExistsError as exc:
                raise InstallError(
                    f"service unit collision appeared during installation: {unit_path}; it was not replaced"
                ) from exc
            temporary.unlink()
    except InstallError:
        raise
    except OSError as exc:
        raise InstallError(f"cannot install service unit {unit_path}: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def run_systemctl(systemctl: str, *arguments: str) -> None:
    result = subprocess.run(
        [systemctl, "--user", *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        detail = result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise InstallError(f"systemctl --user {' '.join(arguments)} failed{suffix}")


def wait_for_health(timeout: float = HEALTH_TIMEOUT) -> None:
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last_problem = "no response"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            request = Request(HEALTH_URL, headers={"Accept": "application/json"})
            with opener.open(request, timeout=min(2.0, remaining)) as response:
                payload = json.loads(response.read(4096))
                if response.status == 200 and payload.get("status") == "ok":
                    return
                last_problem = f"unexpected health response: HTTP {response.status} {payload!r}"
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            last_problem = str(exc)
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    raise InstallError(
        f"the service did not report healthy at {HEALTH_URL} within {timeout:.0f} seconds "
        f"({last_problem}). Inspect it with: journalctl --user -u {UNIT_NAME}"
    )


def install_service(systemctl: str, unit_path: Path, content: str, root: Path) -> None:
    write_service_unit(unit_path, content, project_id(root))
    run_systemctl(systemctl, "daemon-reload")
    run_systemctl(systemctl, "enable", UNIT_NAME)
    run_systemctl(systemctl, "restart", UNIT_NAME)
    print("Waiting for the speech service to load its model ...", flush=True)
    wait_for_health()
    print(f"Service is healthy: {HEALTH_URL}")


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-service",
        action="store_true",
        help="install the environment, models, and commands without changing systemd",
    )
    parser.add_argument(
        "--bin-dir",
        type=Path,
        default=Path.home() / ".local" / "bin",
        metavar="PATH",
        help="command link directory (default: ~/.local/bin)",
    )
    return parser.parse_args(argv)


def install(arguments: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parent
    bin_directory = arguments.bin_dir.expanduser().resolve(strict=False)

    # All same-name command and unit collisions are rejected before uv, model,
    # launcher, link, or service state is modified.
    uv, systemctl, unit_path, rendered_unit = preflight(
        root, bin_directory, not arguments.no_service
    )
    checksums = load_checksums(root / "models" / "SHA256SUMS")

    sync_dependencies(uv, root)
    ensure_models(root, checksums)
    make_launchers_executable(root)
    install_command_links(bin_directory, root)

    if not arguments.no_service:
        assert unit_path is not None and rendered_unit is not None
        install_service(systemctl, unit_path, rendered_unit, root)

    if shutil.which("pw-play") is None:
        print(
            "Note: pw-play is not installed; use local-say --output FILE.wav "
            "or install PipeWire tools for direct playback.",
            file=sys.stderr,
        )
    print("Local TTS installation complete.")


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    try:
        install(arguments)
    except InstallError as exc:
        print(f"install.py: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("install.py: interrupted", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"install.py: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
