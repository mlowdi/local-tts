import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ebooklib import epub

from audiobook import assemble, prepare_output
from epub_reader import Book, Chapter, html_text, read_book, select_chapters, split_text


class EpubReadingTests(unittest.TestCase):
    def test_spine_order_navigation_and_duplicate_references(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.epub"
            book = epub.EpubBook()
            book.set_identifier("local-test")
            book.set_title("Reading order")
            book.set_language("en")
            book.add_author("Test Author")
            first = epub.EpubHtml(uid="first", file_name="first.xhtml", content="<h1>First</h1><p>The first section.</p>")
            second = epub.EpubHtml(uid="second", file_name="second.xhtml", content="<h1>Second</h1><p>The second section.</p>")
            appendix = epub.EpubHtml(uid="appendix", file_name="appendix.xhtml", content="<p>Nonlinear material.</p>")
            # Manifest order and ZIP order are deliberately not reading order.
            for item in (first, second, appendix, epub.EpubNcx(), epub.EpubNav()):
                book.add_item(item)
            book.toc = [epub.Link("second.xhtml#start", "Second in manifest, first in spine", "link"), first]
            book.spine = ["nav", second, first, first, (appendix, "no")]
            epub.write_epub(source, book)
            result = read_book(source)
            self.assertEqual([chapter.href for chapter in result.chapters], ["second.xhtml", "first.xhtml"])
            self.assertEqual(result.chapters[0].title, "Second in manifest, first in spine")
            self.assertEqual(result.chapters[1].text, "First\n\nThe first section.")
            self.assertEqual(result.author, "Test Author")

    def test_inline_punctuation_and_hidden_markup(self):
        content = b'''<body><h1>Heading</h1>
          <p>We <em>really</em> talk; this is <strong>fine</strong>.</p>
          <p>Second&nbsp;paragraph.</p>
          <nav>Do not narrate navigation.</nav><script>not speech</script>
          <div hidden><span hidden>hidden text</span></div>
          <div style="display: none"><span style="visibility:hidden">hidden child</span></div>
          </body>'''
        title, text = html_text(content)
        self.assertEqual(title, "Heading")
        self.assertEqual(text, "Heading\n\nWe really talk; this is fine.\n\nSecond paragraph.")

    def test_chunking_preserves_words_and_bounds_requests(self):
        text = ("A short sentence. Here is a longer sentence with punctuation, accents like café, and a hyphenated-word!\n\n" * 25) + ("Long " * 1000)
        chunks = split_text(text)
        self.assertEqual(" ".join(chunks), " ".join(text.split()))
        self.assertTrue(all(0 < len(chunk) <= 900 for chunk in chunks))
        # An oversized token must not silently disappear or exceed the API limit.
        token = "x" * 1901
        pieces = split_text(token)
        self.assertEqual("".join(pieces), token)
        self.assertTrue(all(len(piece) <= 900 for piece in pieces))

    def test_selection_keeps_reading_order_and_rejects_invalid_ranges(self):
        chapters = [Chapter(index, str(index), str(index), "Text") for index in range(1, 6)]
        self.assertEqual([chapter.number for chapter in select_chapters(chapters, "5,2-3,2")], [2, 3, 5])
        for selection in ("0", "6", "3-1", "1,,2", "", "-1"):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                select_chapters(chapters, selection)


class ResumeSafetyTests(unittest.TestCase):
    def test_resume_rejects_changed_settings_without_touching_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            plan = {"voice": "af_heart", "speed": 1.0, "source_sha256": "source-a"}
            prepare_output(output, plan)
            chapter = output / "chapter-0001.m4a"
            chapter.write_bytes(b"existing user audio")
            prepare_output(output, plan)
            with self.assertRaisesRegex(ValueError, "different book"):
                prepare_output(output, {**plan, "voice": "am_michael"})
            self.assertEqual(chapter.read_bytes(), b"existing user audio")
            self.assertEqual(json.loads((output / "manifest.json").read_text()), plan)

    def test_unrelated_output_directory_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "notes.txt").write_text("keep me")
            with self.assertRaisesRegex(ValueError, "not empty"):
                prepare_output(output, {"voice": "af_heart"})
            self.assertFalse((output / "manifest.json").exists())
            self.assertEqual((output / "notes.txt").read_text(), "keep me")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class AudiobookContainerTests(unittest.TestCase):
    def test_m4b_chapter_markers_preserve_titles_and_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            chapters = [Chapter(2, r"Prelude; #1 = A\B", "a", "First"), Chapter(7, "Next chapter", "b", "Second")]
            book = Book("A title; with #symbols", "Test Author", "en", chapters)
            for chapter, duration in zip(chapters, (0.8, 1.1)):
                subprocess.run([
                    "ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                    f"sine=frequency=440:sample_rate=24000:duration={duration}",
                    "-c:a", "aac", "-b:a", "64k", str(output / f"chapter-{chapter.number:04d}.m4a"),
                ], check=True)
            result = assemble(book, chapters, output)
            info = json.loads(subprocess.check_output([
                "ffprobe", "-v", "error", "-show_chapters", "-show_format", "-of", "json", str(result),
            ]))
            self.assertEqual([item["tags"]["title"] for item in info["chapters"]], [chapter.title for chapter in chapters])
            self.assertEqual(info["format"]["tags"]["title"], book.title)
            self.assertEqual(info["format"]["tags"]["artist"], book.author)
            self.assertAlmostEqual(float(info["chapters"][1]["start_time"]), 0.8, delta=0.05)
            self.assertAlmostEqual(float(info["chapters"][1]["end_time"]), 1.9, delta=0.05)
            self.assertAlmostEqual(float(info["format"]["duration"]), 1.9, delta=0.1)
            subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(result), "-f", "null", "-"], check=True)


if __name__ == "__main__":
    unittest.main()
