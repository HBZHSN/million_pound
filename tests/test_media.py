import asyncio
import functools
import json
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch

from million_pound import media


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_frame_cannot_reuse_previous_preview(self):
        with tempfile.TemporaryDirectory() as work:
            target = Path(work) / "preview.jpg"
            target.write_bytes(b"old-preview")
            with patch("million_pound.media.command", new_callable=AsyncMock):
                with self.assertRaises(media.MediaError):
                    await media.screenshot(Path(work) / "source.mkv", target, 1)
            self.assertFalse(target.exists())

    async def test_record_accelerate_and_three_original_frames(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source, raw, fast = root / "source.mp4", root / "raw.mkv", root / "fast.mp4"
            await media.command("ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi",
                                "-i", "testsrc2=size=320x240:rate=15", "-t", "3", "-c:v", "libx264",
                                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(source), timeout=15)
            server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=work))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                duration = await media.record(f"http://127.0.0.1:{server.server_port}/source.mp4", raw, 3)
                self.assertGreater(duration, 2)
                await media.accelerate(raw, fast, 2)
                output = await media.command("ffprobe", "-v", "error", "-show_entries", "format=duration",
                                             "-of", "json", str(fast), timeout=10)
                self.assertLess(float(json.loads(output)["format"]["duration"]), duration)
                for i, fraction in enumerate((.1, .5, .9)):
                    target = root / f"frame-{i}.jpg"
                    await media.screenshot(raw, target, duration * fraction)
                    self.assertEqual(target.read_bytes()[:2], b"\xff\xd8")
            finally:
                await asyncio.to_thread(server.shutdown)
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
