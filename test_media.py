from dataclasses import replace
import unittest
from unittest import mock

import media


class SimulatedSignal(BaseException):
    pass


class Player:
    def __init__(self, owner, *, status="Playing", position=1_000_000, metadata="track-1",
                 can_control=True, can_pause=True, can_play=True):
        self.owner = owner
        self.status = status
        self.position = position
        self.metadata = metadata
        self.can_control = can_control
        self.can_pause = can_pause
        self.can_play = can_play
        self.pause_outcome = None
        self.play_outcome = None
        self.pause_count = 0
        self.play_count = 0
        self.pause_status_reads = []
        self.play_status_reads = []
        self.status_reads = []

    def snapshot(self):
        return media._Snapshot(
            self.status,
            self.can_control,
            self.can_pause,
            self.can_play,
            self.position,
            self.metadata,
        )


class StatefulBus:
    def __init__(self, players):
        self.players = players

    def names(self):
        return list(self.players)

    def owner(self, name):
        player = self.players.get(name)
        if player is None:
            raise media._BusError("gone")
        return player.owner

    def properties(self, owner):
        for player in self.players.values():
            if player.owner == owner:
                snapshot = player.snapshot()
                if player.status_reads:
                    return replace(snapshot, status=player.status_reads.pop(0))
                return snapshot
        raise media._BusError("gone")

    def pause(self, owner):
        player = self._player(owner)
        player.pause_count += 1
        outcome, player.pause_outcome = player.pause_outcome, None
        if outcome == "error-before":
            raise media._BusError("pause failed")
        if outcome == "ignored":
            return
        player.status = "Paused"
        player.status_reads = list(player.pause_status_reads)
        if outcome == "error-after":
            raise media._BusError("reply lost")
        if outcome == "cancel-after":
            raise SimulatedSignal()

    def play(self, owner):
        player = self._player(owner)
        player.play_count += 1
        outcome, player.play_outcome = player.play_outcome, None
        if outcome == "error-before":
            raise media._BusError("play failed")
        player.status = "Playing"
        player.status_reads = list(player.play_status_reads)
        if outcome == "error-after":
            raise media._BusError("reply lost")

    def _player(self, owner):
        for player in self.players.values():
            if player.owner == owner:
                return player
        raise media._BusError("gone")


class MediaSessionTests(unittest.TestCase):
    prefix = "org.mpris.MediaPlayer2."

    def setUp(self):
        self.now = 0.0
        self.enterContext(mock.patch.object(media.time, "monotonic", side_effect=lambda: self.now))
        self.enterContext(mock.patch.object(media.time, "sleep", side_effect=self.advance))

    def advance(self, seconds):
        self.now += seconds

    def test_initially_paused_and_excluded_players_are_untouched(self):
        playing = Player(":1.1")
        already_paused = Player(":1.2", status="Paused")
        remote = Player(":1.3")
        proxy = Player(":1.4")
        players = {
            self.prefix + "browser": playing,
            self.prefix + "music": already_paused,
            self.prefix + "kdeconnect.phone": remote,
            self.prefix + "playerctld": proxy,
        }
        session = media.MediaSession(_bus=StatefulBus(players))

        session.pause_playing()
        self.assertEqual(playing.status, "Paused")
        self.assertEqual(already_paused.status, "Paused")
        self.assertEqual(remote.status, "Playing")
        self.assertEqual(proxy.status, "Playing")
        self.assertEqual(session.paused, [self.prefix + "browser"])
        self.assertEqual(session.skipped, [self.prefix + "music"])

        session.resume_paused()
        self.assertEqual(playing.status, "Playing")
        self.assertEqual(already_paused.status, "Paused")
        self.assertEqual(playing.play_count, 1)
        self.assertEqual(already_paused.play_count, 0)
        self.assertEqual(session.resumed, [self.prefix + "browser"])

    def test_track_seek_and_owner_changes_are_never_resumed(self):
        names = [self.prefix + suffix for suffix in ("track", "seek", "owner")]
        players = {name: Player(f":1.{index}") for index, name in enumerate(names, 1)}
        session = media.MediaSession(_bus=StatefulBus(players))
        session.pause_playing()

        players[names[0]].metadata = "a different private title"
        players[names[1]].position += 3_000_000
        players[names[2]].owner = ":1.99"
        session.resume_paused()

        self.assertEqual(session.paused, sorted(names))
        self.assertEqual(session.resumed, [])
        self.assertEqual(session.skipped, sorted(names))
        self.assertTrue(all(player.status == "Paused" for player in players.values()))
        self.assertTrue(all(player.play_count == 0 for player in players.values()))
        self.assertNotIn("different private title", " ".join(session.warnings))

    def test_partial_method_failures_are_reconciled_and_cleanup_is_idempotent(self):
        names = [self.prefix + suffix for suffix in ("reply-lost", "retry", "not-paused")]
        players = {name: Player(f":1.{index}") for index, name in enumerate(names, 1)}
        players[names[0]].pause_outcome = "error-after"
        players[names[1]].play_outcome = "error-before"
        players[names[2]].pause_outcome = "error-before"
        session = media.MediaSession(_bus=StatefulBus(players))

        session.pause_playing()
        self.assertEqual(session.paused, names[:2])
        self.assertEqual(players[names[2]].status, "Playing")

        players[names[0]].play_outcome = "error-after"
        session.resume_paused()
        self.assertEqual(session.resumed, [names[0]])
        self.assertEqual(players[names[1]].status, "Paused")
        self.assertIn(names[2], session.skipped)

        session.resume_paused()
        session.resume_paused()
        self.assertEqual(session.resumed, names[:2])
        self.assertEqual(players[names[0]].play_count, 1)
        self.assertEqual(players[names[1]].play_count, 2)
        self.assertEqual(players[names[2]].play_count, 0)

    def test_cleanup_restores_pause_completed_during_cancellation(self):
        name = self.prefix + "movie"
        player = Player(":1.1")
        player.pause_outcome = "cancel-after"
        session = media.MediaSession(_bus=StatefulBus({name: player}))

        with self.assertRaises(SimulatedSignal):
            session.pause_playing()
        self.assertEqual(player.status, "Paused")

        session.resume_paused()
        self.assertEqual(player.status, "Playing")
        self.assertEqual(session.paused, [name])
        self.assertEqual(session.resumed, [name])

    def test_delayed_pause_and_play_statuses_restore_without_false_warnings(self):
        name = self.prefix + "spotify"
        player = Player(":1.1")
        player.pause_status_reads = ["Playing", "Playing"]
        player.play_status_reads = ["Paused", "Paused"]
        session = media.MediaSession(_bus=StatefulBus({name: player}))

        session.pause_playing()
        self.assertEqual(player.status, "Paused")
        session.resume_paused()

        self.assertEqual(player.status, "Playing")
        self.assertEqual(session.paused, [name])
        self.assertEqual(session.resumed, [name])
        self.assertEqual(session.skipped, [])
        self.assertEqual(session.warnings, [])
        self.assertEqual((player.pause_count, player.play_count), (1, 1))

    def test_pause_unconfirmed_before_speech_remains_eligible_for_cleanup(self):
        name = self.prefix + "slow-player"
        player = Player(":1.1")
        player.pause_status_reads = ["Playing"] * 100
        session = media.MediaSession(_bus=StatefulBus({name: player}))

        session.pause_playing()
        self.assertEqual(player.status, "Paused")
        # The delayed status becomes visible while the announcement plays.
        player.status_reads.clear()
        session.resume_paused()

        self.assertEqual(player.status, "Playing")
        self.assertEqual(session.paused, [name])
        self.assertEqual(session.resumed, [name])
        self.assertEqual(session.skipped, [])
        self.assertEqual(player.play_count, 1)

    def test_cancellation_while_waiting_for_pause_still_restores_media(self):
        name = self.prefix + "movie"
        player = Player(":1.1")
        player.pause_status_reads = ["Playing", "Playing"]
        session = media.MediaSession(_bus=StatefulBus({name: player}))

        with mock.patch.object(media.time, "sleep", side_effect=SimulatedSignal()):
            with self.assertRaises(SimulatedSignal):
                session.pause_playing()
        session.resume_paused()

        self.assertEqual(player.status, "Playing")
        self.assertEqual(session.resumed, [name])
        self.assertEqual(player.play_count, 1)

    def test_ignored_pause_is_not_claimed_or_followed_by_play(self):
        name = self.prefix + "ignores-pause"
        player = Player(":1.1")
        player.pause_outcome = "ignored"
        session = media.MediaSession(_bus=StatefulBus({name: player}))

        session.pause_playing()
        session.resume_paused()

        self.assertEqual(player.status, "Playing")
        self.assertEqual(session.paused, [])
        self.assertEqual(session.resumed, [])
        self.assertEqual(session.skipped, [name])
        self.assertEqual(player.play_count, 0)

    def test_missing_control_pause_or_play_capability_is_skipped(self):
        name = self.prefix + "read-only"
        for capability in ("can_control", "can_pause", "can_play"):
            with self.subTest(capability=capability):
                player = Player(":1.1", **{capability: False})
                session = media.MediaSession(_bus=StatefulBus({name: player}))
                session.pause_playing()
                session.resume_paused()
                self.assertEqual(player.status, "Playing")
                self.assertEqual(player.pause_count, 0)
                self.assertEqual(player.play_count, 0)
                self.assertEqual(session.skipped, [name])


if __name__ == "__main__":
    unittest.main()
