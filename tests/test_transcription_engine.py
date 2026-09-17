import shutil
import tempfile
import unittest
from pathlib import Path

from app.audio import split_into_chunks
from app.transcription_engine import MockEngine, merge_chunk_results
from tests.conftest_helpers import make_tone


class TestMockEngine(unittest.TestCase):
    def setUp(self):
        self.work_dir = Path(tempfile.mkdtemp(prefix="volga_test_"))

    def tearDown(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def test_mock_engine_is_deterministic(self):
        tone = make_tone(2, suffix=".wav")
        engine = MockEngine()
        r1 = engine.transcribe(tone)
        r2 = engine.transcribe(tone)
        self.assertEqual(r1.text, r2.text)
        self.assertEqual(len(r1.segments), len(r2.segments))

    def test_merge_chunk_results_dedupes_overlap_and_spans_full_duration(self):
        tone = make_tone(20, suffix=".wav")
        chunks = split_into_chunks(tone, self.work_dir / "chunks", chunk_length_seconds=8, overlap_seconds=2)
        engine = MockEngine()
        results = [(c, engine.transcribe(c.path)) for c in chunks]

        merged = merge_chunk_results(results, overlap_seconds=2)

        # No gaps and no backwards jumps in the merged timeline.
        for prev, cur in zip(merged.segments, merged.segments[1:]):
            self.assertLessEqual(prev.end, cur.start + 0.01)

        self.assertAlmostEqual(merged.segments[0].start, 0.0, delta=0.01)
        self.assertAlmostEqual(merged.segments[-1].end, 20.0, delta=0.5)

        # No segment should appear twice (merge must have de-duplicated the
        # overlap region between consecutive chunks).
        seen_windows = [(s.start, s.end) for s in merged.segments]
        self.assertEqual(len(seen_windows), len(set(seen_windows)))


if __name__ == "__main__":
    unittest.main()
