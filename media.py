"""Conservative pause and restore support for local MPRIS media players."""

from __future__ import annotations

from dataclasses import dataclass
import json
import subprocess
import time
from typing import Any


_DBUS_DESTINATION = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"
_DBUS_INTERFACE = "org.freedesktop.DBus"
_PLAYER_PATH = "/org/mpris/MediaPlayer2"
_PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player"
_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
_MPRIS_PREFIX = "org.mpris.MediaPlayer2."
_POSITION_TOLERANCE_US = 1_500_000
_STATUS_SETTLE_SECONDS = 1.0
_STATUS_POLL_SECONDS = 0.05


class _BusError(Exception):
    pass


@dataclass(frozen=True)
class _Snapshot:
    status: str
    can_control: bool
    can_pause: bool
    can_play: bool
    position: int
    metadata: str


@dataclass
class _Target:
    name: str
    owner: str
    before: _Snapshot
    paused_at: int | None = None
    done: bool = False


class _Bus:
    def __init__(self, timeout: float = 2.0):
        self.timeout = timeout

    def call(self, destination: str, path: str, interface: str, method: str, *arguments: str) -> dict[str, Any]:
        command = [
            "busctl", "--user", "--json=short", "call",
            destination, path, interface, method, *arguments,
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise _BusError("D-Bus call failed") from exc
        # busctl emits no JSON for successful void methods such as Pause/Play.
        if not completed.stdout.strip():
            return {"type": "", "data": []}
        try:
            value = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise _BusError("D-Bus returned invalid JSON") from exc
        if not isinstance(value, dict) or not isinstance(value.get("type"), str) or "data" not in value:
            raise _BusError("D-Bus returned an invalid result")
        return value

    def names(self) -> list[str]:
        result = self.call(_DBUS_DESTINATION, _DBUS_PATH, _DBUS_INTERFACE, "ListNames")
        data = result["data"]
        if (
            result["type"] != "as"
            or not isinstance(data, list)
            or len(data) != 1
            or not isinstance(data[0], list)
            or any(not isinstance(item, str) for item in data[0])
        ):
            raise _BusError("ListNames returned an invalid result")
        return data[0]

    def owner(self, name: str) -> str:
        result = self.call(
            _DBUS_DESTINATION, _DBUS_PATH, _DBUS_INTERFACE,
            "GetNameOwner", "s", name,
        )
        data = result["data"]
        if result["type"] != "s" or not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], str):
            raise _BusError("GetNameOwner returned an invalid result")
        if not data[0].startswith(":"):
            raise _BusError("GetNameOwner returned a non-unique name")
        return data[0]

    def properties(self, owner: str) -> _Snapshot:
        result = self.call(
            owner, _PLAYER_PATH, _PROPERTIES_INTERFACE,
            "GetAll", "s", _PLAYER_INTERFACE,
        )
        data = result["data"]
        if result["type"] != "a{sv}" or not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise _BusError("GetAll returned an invalid result")
        properties = data[0]
        status = _variant(properties, "PlaybackStatus", "s", str)
        can_control = _variant(properties, "CanControl", "b", bool)
        can_pause = _variant(properties, "CanPause", "b", bool)
        can_play = _variant(properties, "CanPlay", "b", bool)
        position = _variant(properties, "Position", "x", int)
        metadata = _variant(properties, "Metadata", "a{sv}", dict)
        if isinstance(position, bool) or position < 0:
            raise _BusError("Position returned an invalid result")
        try:
            metadata_identity = json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise _BusError("Metadata returned an invalid result") from exc
        return _Snapshot(status, can_control, can_pause, can_play, position, metadata_identity)

    def pause(self, owner: str) -> None:
        result = self.call(owner, _PLAYER_PATH, _PLAYER_INTERFACE, "Pause")
        if result["type"] != "" or result["data"] != []:
            raise _BusError("Pause returned an invalid result")

    def play(self, owner: str) -> None:
        result = self.call(owner, _PLAYER_PATH, _PLAYER_INTERFACE, "Play")
        if result["type"] != "" or result["data"] != []:
            raise _BusError("Play returned an invalid result")


def _variant(properties: dict[str, Any], key: str, expected_type: str, expected_python_type: type) -> Any:
    value = properties.get(key)
    if not isinstance(value, dict) or value.get("type") != expected_type or "data" not in value:
        raise _BusError(f"{key} returned an invalid result")
    data = value["data"]
    if not isinstance(data, expected_python_type):
        raise _BusError(f"{key} returned an invalid result")
    return data


def _excluded(name: str) -> bool:
    suffix = name[len(_MPRIS_PREFIX):]
    return suffix == "playerctld" or suffix.startswith("kdeconnect.")


def _same_media(expected: _Snapshot, current: _Snapshot, expected_position: int) -> bool:
    return (
        current.metadata == expected.metadata
        and abs(current.position - expected_position) <= _POSITION_TOLERANCE_US
    )


class MediaSession:
    """Pause eligible local MPRIS players and safely restore only those players."""

    def __init__(self, *, _bus: _Bus | None = None):
        self.paused: list[str] = []
        self.resumed: list[str] = []
        self.skipped: list[str] = []
        self.warnings: list[str] = []
        self._bus = _bus or _Bus()
        self._targets: list[_Target] = []

    def pause_playing(self) -> None:
        """Pause each currently playing, controllable local MPRIS player."""
        try:
            names = self._bus.names()
        except _BusError:
            self.warnings.append("Could not discover MPRIS media players")
            return

        candidates = sorted({name for name in names if name.startswith(_MPRIS_PREFIX) and not _excluded(name)})
        for name in candidates:
            try:
                owner = self._bus.owner(name)
                before = self._bus.properties(owner)
            except _BusError:
                self._skip(name)
                self.warnings.append(f"Could not inspect media player {name}")
                continue
            if before.status != "Playing" or not before.can_control or not before.can_pause or not before.can_play:
                self._skip(name)
                continue

            # Register the intent before Pause: a signal may interrupt immediately
            # after the remote side has acted but before this process sees success.
            target = _Target(name, owner, before)
            self._targets.append(target)
            try:
                self._bus.pause(owner)
            except _BusError:
                self.warnings.append(f"Could not confirm pause for media player {name}")
                self._reconcile_pause(target)
                continue

            self._append_once(self.paused, name)
            try:
                after = self._settled_properties(target, "Paused")
            except _BusError:
                # A successful method return means Pause was accepted. Retain the
                # target so cleanup can inspect it again rather than abandoning it.
                self.warnings.append(f"Could not verify pause for media player {name}")
                continue
            if after.status == "Paused" and _same_media(before, after, before.position):
                target.paused_at = after.position
            elif after.status == "Playing" and _same_media(before, after, before.position):
                # An accepted Pause may publish its state later. Keep the intent
                # for cleanup, but do not claim a confirmed pause yet.
                self._remove(self.paused, name)
                self.warnings.append(f"Pause not yet confirmed for media player {name}")
            else:
                # It changed or stopped after our request. It is never safe to play.
                target.done = True
                self._remove(self.paused, name)
                self._skip(name)
                self.warnings.append(f"Media changed while pausing {name}")

    def resume_paused(self) -> None:
        """Restore eligible players paused by this session; safe to call repeatedly."""
        for target in self._targets:
            if target.done:
                continue
            try:
                owner = self._bus.owner(target.name)
            except _BusError:
                target.done = True
                self._skip(target.name)
                self.warnings.append(f"Media player {target.name} is no longer available")
                continue
            if owner != target.owner:
                target.done = True
                self._skip(target.name)
                self.warnings.append(f"Media player {target.name} was replaced")
                continue
            try:
                current = (
                    self._settled_properties(target, "Paused")
                    if target.paused_at is None
                    else self._bus.properties(target.owner)
                )
            except _BusError:
                target.done = True
                self._skip(target.name)
                self.warnings.append(f"Could not inspect media player {target.name} for restoration")
                continue

            expected_position = target.paused_at if target.paused_at is not None else target.before.position
            if current.status != "Paused":
                target.done = True
                self._skip(target.name)
                if current.status != "Playing":
                    self.warnings.append(f"Media player {target.name} is no longer paused")
                continue
            if not current.can_play:
                target.done = True
                self._skip(target.name)
                self.warnings.append(f"Media player {target.name} can no longer play")
                continue
            if not _same_media(target.before, current, expected_position):
                target.done = True
                self._skip(target.name)
                self.warnings.append(f"Media changed while {target.name} was paused")
                continue

            # This also accounts for a Pause that completed just before a
            # cancellation interrupted the local call.
            self._append_once(self.paused, target.name)
            try:
                self._bus.play(target.owner)
            except _BusError:
                self.warnings.append(f"Could not resume media player {target.name}")
                self._reconcile_play(target)
                continue
            self._reconcile_play(target)
            if not target.done:
                self.warnings.append(f"Media player {target.name} did not confirm resumed playback")

    def _settled_properties(self, target: _Target, status: str) -> _Snapshot:
        """Observe delayed state publication without repeating control commands."""
        deadline = time.monotonic() + _STATUS_SETTLE_SECONDS
        position = target.paused_at if target.paused_at is not None else target.before.position
        while True:
            current = self._bus.properties(target.owner)
            if (
                current.status == status
                or current.status not in ("Playing", "Paused")
                or not _same_media(target.before, current, position)
            ):
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return current
            time.sleep(min(_STATUS_POLL_SECONDS, remaining))

    def _reconcile_pause(self, target: _Target) -> None:
        try:
            current = self._settled_properties(target, "Paused")
        except _BusError:
            return
        if current.status == "Paused" and _same_media(target.before, current, target.before.position):
            target.paused_at = current.position
            self._append_once(self.paused, target.name)
            return
        if current.status == "Playing" and _same_media(target.before, current, target.before.position):
            return
        target.done = True
        self._skip(target.name)

    def _reconcile_play(self, target: _Target) -> None:
        try:
            current = self._settled_properties(target, "Playing")
        except _BusError:
            return
        if current.status == "Playing" and current.metadata == target.before.metadata:
            target.done = True
            self._append_once(self.resumed, target.name)

    def _skip(self, name: str) -> None:
        self._append_once(self.skipped, name)

    @staticmethod
    def _append_once(items: list[str], name: str) -> None:
        if name not in items:
            items.append(name)

    @staticmethod
    def _remove(items: list[str], name: str) -> None:
        try:
            items.remove(name)
        except ValueError:
            pass
