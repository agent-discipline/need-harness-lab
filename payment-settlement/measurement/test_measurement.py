import unittest
from normalize import normalize, unique
from summarize import summarize


class MeasurementTests(unittest.TestCase):
    def norm(self, e, provider="codex"):
        if provider == "codex":
            e.setdefault("usage_semantics", "delta")
        return normalize(e, provider, "run1", "session1", "implementer", "gpt-sol-high")

    def test_codex_cache_included_and_missing_unknown(self):
        r = self.norm(dict(type="turn.completed", event_id="t1", usage=dict(input_tokens=100, output_tokens=20, cached_input_tokens=70)))
        self.assertEqual(r["total_tokens"], 120)
        self.assertEqual(r["cached_input_tokens"], 70)
        r = self.norm(dict(type="turn.completed", event_id="t1", usage=dict(input_tokens=100, output_tokens=20)))
        self.assertIsNone(r["cached_input_tokens"])
        self.assertIsNone(summarize([r])["actual_cost_usd"])

    def test_codex_write_and_reasoning(self):
        r = self.norm(dict(type="turn.completed", event_id="t1", usage=dict(input_tokens=100, output_tokens=30, cached_input_tokens=80, cache_write_input_tokens=20, reasoning_output_tokens=10)))
        self.assertEqual(r["cache_write_input_tokens"], 20)
        self.assertEqual(r["reasoning_tokens"], 10)
        self.assertEqual(r["total_tokens"], 130)
        with self.assertRaises(ValueError):
            self.norm(dict(type="turn.completed", event_id="t2", usage_semantics="unknown", usage=dict(input_tokens=100, output_tokens=30)))

    def test_anthropic_cache_is_added(self):
        r = self.norm(dict(id="m1", usage=dict(input_tokens=10, cache_read_input_tokens=50, cache_creation_input_tokens=20, output_tokens=5)), "anthropic")
        self.assertEqual(r["input_tokens"], 80)
        self.assertEqual(r["total_tokens"], 85)

    def test_reasoning_is_not_added_twice(self):
        r = self.norm(dict(id="resp1", usage=dict(input_tokens=100, output_tokens=30, input_tokens_details=dict(cached_tokens=50), output_tokens_details=dict(reasoning_tokens=20))), "openai")
        self.assertEqual(summarize([r])["total_tokens"], 130)
        self.assertEqual(r["reasoning_tokens"], 20)

    def test_dedup_across_files_and_conflict(self):
        r = self.norm(dict(type="turn.completed", event_id="t1", usage=dict(input_tokens=100, output_tokens=20)))
        self.assertEqual(summarize([r, r])["event_count"], 1)
        with self.assertRaises(ValueError):
            list(unique([r, dict(r, output_tokens=21)]))
        with self.assertRaises(ValueError):
            summarize([dict(r, usage_scope="includes_children")])

    def test_fail_closed(self):
        base = dict(type="turn.completed", event_id="t1", usage=dict(input_tokens=100, output_tokens=20))
        for e in (dict(base, cumulative=True), dict(base, event_id=None), dict(base, usage=None)):
            with self.assertRaises(ValueError):
                self.norm(e)
        with self.assertRaises(ValueError):
            self.norm(dict(id="m1", usage=dict(input_tokens=10, output_tokens=2)), "anthropic")

    def test_children_and_costs(self):
        r = self.norm(dict(type="turn.completed", event_id="t1", provider_reported_cost_usd=0.01, usage=dict(input_tokens=100, output_tokens=20)))
        child = dict(r, session_id="child1", role="worker")
        result = summarize([r, child])
        self.assertEqual(result["total_tokens"], 240)
        self.assertAlmostEqual(result["actual_cost_usd"], 0.02)


if __name__ == "__main__":
    unittest.main()
