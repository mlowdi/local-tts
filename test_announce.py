import io
import os
import tempfile
import unittest
from unittest import mock
from urllib.error import URLError
import wave

import announce
import media
from test_media import Player, StatefulBus


def speech_fixture():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x10\x00" * 2400)
    return output.getvalue()


class AnnouncementSafetyTests(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime.cleanup)
        self.enterContext(mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.runtime.name}))
        self.enterContext(mock.patch.object(announce.shutil, "which", return_value="/usr/bin/available"))
        self.name = "org.mpris.MediaPlayer2.movie"
        self.player = Player(":1.123")
        self.initially_paused = Player(":1.124", status="Paused")
        self.session = media.MediaSession(_bus=StatefulBus({
            self.name: self.player,
            "org.mpris.MediaPlayer2.music": self.initially_paused,
        }))
        self.enterContext(mock.patch.object(announce, "MediaSession", return_value=self.session))

    def test_unavailable_speech_does_not_interrupt_media(self):
        self.enterContext(mock.patch.object(announce, "synthesize", side_effect=URLError("offline")))
        result = announce.announce("Please take a look.")
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.player.status, "Playing")
        self.assertEqual(self.player.pause_count, 0)
        self.assertEqual(self.initially_paused.status, "Paused")

    def test_playback_failure_restores_only_media_we_paused(self):
        self.enterContext(mock.patch.object(announce, "synthesize", return_value=speech_fixture()))

        def fail_playback(_path):
            self.assertEqual(self.player.status, "Paused")
            raise RuntimeError("audio device disappeared")

        self.enterContext(mock.patch.object(announce, "play_audio", side_effect=fail_playback))
        result = announce.announce("Please take a look.")
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.player.status, "Playing")
        self.assertEqual(self.initially_paused.status, "Paused")
        self.assertEqual(result["resumed"], [self.name])

    def test_cancellation_waits_for_restoration(self):
        self.enterContext(mock.patch.object(announce, "synthesize", return_value=speech_fixture()))
        self.enterContext(mock.patch.object(announce, "play_audio", side_effect=announce.AnnouncementCancelled()))
        result = announce.announce("Please take a look.")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.player.status, "Playing")
        self.assertEqual(self.initially_paused.status, "Paused")
        self.assertEqual(result["resumed"], [self.name])

    def test_user_stopping_media_during_speech_is_respected(self):
        self.enterContext(mock.patch.object(announce, "synthesize", return_value=speech_fixture()))

        def user_stops(_path):
            self.player.status = "Stopped"

        self.enterContext(mock.patch.object(announce, "play_audio", side_effect=user_stops))
        result = announce.announce("Please take a look.")
        self.assertEqual(result["status"], "played")
        self.assertEqual(self.player.status, "Stopped")
        self.assertEqual(result["resumed"], [])
        self.assertIn(self.name, result["skipped"])

    def test_brief_message_boundary(self):
        self.assertEqual(announce.validate_message(" x "), "x")
        self.assertEqual(len(announce.validate_message("x" * 300)), 300)
        for message in (" \n ", "x" * 301):
            with self.subTest(message_length=len(message)), self.assertRaises(ValueError):
                announce.validate_message(message)


if __name__ == "__main__":
    unittest.main()
