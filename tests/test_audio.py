import shutil
import tempfile
import unittest
from pathlib import Path

from app.audio import normalize_to_wav, probe_duration_seconds, split_into_chunks
from tests.conftest_helpers import make_tone


class TestAudioProcessing(unittest.TestCase):
    def setUp(self):
        self.work_dir = Path(tempfile.mkdtemp(prefix="volga_test_"))

    def tearDown(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def test_probe_duration(self):
        tone = make_tone(3, suffix=".mp3")
        duration = probe_duration_seconds(tone)
        self.assertAlmostEqual(duration, 3.0, delta=0.2)

    def test_normalize_produces_mono_16k_wav(self):
        tone = make_tone(2, suffix=".mp3")
        dst = self.work_dir / "norm.wav"
        normalize_to_wav(tone, dst)
        self.assertTrue(dst.exists())
        self.assertAlmostEqual(probe_duration_seconds(dst), 2.0, delta=0.2)

    def test_short_file_is_not_split(self):
        tone = make_tone(5, suffix=".wav")
        chunks = split_into_chunks(tone, self.work_dir / "chunks", chunk_length_seconds=20, overlap_seconds=2)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].start_offset, 0.0)

    def test_long_file_is_split_with_correct_offsets(self):
        tone = make_tone(20, suffix=".wav")
        chunks = split_into_chunks(tone, self.work_dir / "chunks", chunk_length_seconds=8, overlap_seconds=2)
        # stride = 8 - 2 = 6 -> starts at 0, 6, 12, 18
        self.assertEqual([c.start_offset for c in chunks], [0.0, 6.0, 12.0, 18.0])
        for c in chunks:
            self.assertTrue(c.path.exists())

    def test_chunk_durations_sum_covers_full_audio_with_overlap(self):
        tone = make_tone(20, suffix=".wav")
        chunks = split_into_chunks(tone, self.work_dir / "chunks", chunk_length_seconds=8, overlap_seconds=2)
        last_chunk_end = chunks[-1].start_offset + probe_duration_seconds(chunks[-1].path)
        self.assertGreaterEqual(last_chunk_end, 19.5)  # covers (close to) the full 20s


if __name__ == "__main__":
    unittest.main()
