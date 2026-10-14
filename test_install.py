import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import install


class SystemdRenderingTests(unittest.TestCase):
    def test_quote_preserves_spaces_percent_backslashes_and_quotes(self):
        value = '/home/Test User/100%/back\\slash/"quoted"'
        self.assertEqual(
            install.systemd_quote(value),
            '"/home/Test User/100%%/back\\\\slash/\\"quoted\\""',
        )

    def test_control_characters_are_rejected(self):
        with self.assertRaisesRegex(install.InstallError, "control"):
            install.systemd_quote("/tmp/line\nbreak")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is required")
    def test_systemd_accepts_rendered_unit_from_nontrivial_path(self):
        with tempfile.TemporaryDirectory(prefix="local tts unit ") as directory:
            root = Path(directory) / r'100% and back\slash'
            root.mkdir()
            launcher = root / "local-tts-server"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            launcher.chmod(0o755)
            template = (Path(__file__).parent / "deploy/local-tts.service.in").read_text()
            unit = Path(directory) / "portable-speech.service"
            unit.write_text(install.render_unit(root, template))
            result = subprocess.run(
                ["systemd-analyze", "--user", "verify", str(unit)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


class CollisionTests(unittest.TestCase):
    def test_unrelated_command_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            bin_directory = base / "bin"
            root.mkdir()
            bin_directory.mkdir()
            for command in install.COMMANDS:
                (root / command).write_text("launcher")
            (bin_directory / "local-say").write_text("unrelated")
            with self.assertRaisesRegex(install.InstallError, "unrelated command"):
                install.check_link_collisions(bin_directory, root)
            self.assertEqual((bin_directory / "local-say").read_text(), "unrelated")

    def test_only_this_clones_managed_unit_is_replaceable(self):
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / install.UNIT_NAME
            root = Path(directory) / "clone"
            identifier = install.project_id(root)
            unit.write_text(
                f"{install.UNIT_MARKER}\n{install.PROJECT_ID_PREFIX}{identifier}\n"
            )
            install.check_unit_collision(unit, identifier)
            with self.assertRaisesRegex(install.InstallError, "unrelated service"):
                install.check_unit_collision(unit, install.project_id(root / "other"))
            unit.write_text("[Unit]\nDescription=Someone else's service\n")
            with self.assertRaisesRegex(install.InstallError, "unrelated service"):
                install.check_unit_collision(unit, identifier)


class ModelSafetyTests(unittest.TestCase):
    def test_hash_mismatch_keeps_existing_model_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            model_directory = Path(directory)
            source = model_directory / "download-source"
            source.write_bytes(b"bad model")
            destination = model_directory / "model.bin"
            destination.write_bytes(b"existing model")

            with self.assertRaisesRegex(install.InstallError, "failed SHA-256"):
                install.download_model(source.as_uri(), destination, "0" * 64)

            self.assertEqual(destination.read_bytes(), b"existing model")
            self.assertEqual(
                sorted(path.name for path in model_directory.iterdir()),
                ["download-source", "model.bin"],
            )

    def test_valid_existing_models_are_not_downloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models = root / "models"
            models.mkdir()
            checksums = {}
            for filename in install.MODEL_URLS:
                content = filename.encode("ascii")
                (models / filename).write_bytes(content)
                checksums[filename] = hashlib.sha256(content).hexdigest()

            with mock.patch.object(install, "download_model") as download:
                install.ensure_models(root, checksums)
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
