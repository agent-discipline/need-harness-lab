import json
from pathlib import Path
import tempfile
import unittest
from record import init_run, record_turn, read_events


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "core1"
        self.prompt = self.root / "prompt.txt"
        self.analysis = self.root / "analysis.txt"
        self.prompt.write_text("요구사항 전문\n```shell\nhello\n```\n")
        self.analysis.write_text("검증 결과 전체\n실패 하나\n")
        init_run(self.run)

    def add(self, **kwargs):
        return record_turn(self.run, self.prompt, self.analysis, "implementer", "session1", "NEEDS_FIX", **kwargs)

    def test_two_turn_full_text_and_response(self):
        first = self.prompt.read_text()
        response = self.root / "response.txt"
        response.write_text("응답 전문\n`````\n")
        self.add(response_file=response)
        self.prompt.write_text("두 번째 요구사항\n누락 없이")
        self.analysis.write_text("최종 검증 통과")
        self.add()
        events = read_events(self.run / "events.jsonl")
        self.assertEqual(len(events), 3)
        self.assertEqual(events[1]["files"]["prompt"]["content"], first)
        self.assertEqual(events[2]["files"]["prompt"]["content"], self.prompt.read_text())
        text = (self.run / "transcript.md").read_text()
        self.assertIn("## 1. 프롬프트 전문", text)
        self.assertIn("## 2. 결과물 분석", text)
        self.assertIn("``````text", text)
        self.assertIn(response.read_text(), text)

    def test_duplicate_does_not_modify_history(self):
        self.add()
        before = (self.run / "events.jsonl").read_bytes()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.add()
        self.assertEqual((self.run / "events.jsonl").read_bytes(), before)

    def test_journal_tamper_detected(self):
        self.add()
        path = self.run / "events.jsonl"
        path.write_text(path.read_text().replace("요구사항", "조작사항"))
        with self.assertRaisesRegex(ValueError, "modified"):
            init_run(self.run)

    def test_transcript_tamper_detected(self):
        (self.run / "transcript.md").write_text("manual overwrite")
        with self.assertRaisesRegex(ValueError, "modified"):
            self.add()

    def test_init_idempotent_not_started(self):
        before = (self.run / "events.jsonl").read_bytes()
        result = init_run(self.run)
        self.assertEqual(result["status"], "NOT_STARTED")
        self.assertEqual((self.run / "events.jsonl").read_bytes(), before)

    def test_worker_and_binary_evidence_preserved(self):
        proof = self.root / "evidence.bin"
        proof.write_bytes(bytes([0, 255, 128]))
        event = record_turn(self.run, self.prompt, self.analysis, "worker", "child1", "DONE", evidence_files=[proof])
        self.assertEqual(event["role"], "worker")
        self.assertEqual(event["evidence"][0]["encoding"], "base64")
        self.assertEqual(read_events(self.run / "events.jsonl")[-1], event)


if __name__ == "__main__":
    unittest.main()
