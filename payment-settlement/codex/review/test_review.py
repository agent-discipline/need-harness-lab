import json
import unittest
from run import assess, CRITERIA

MODEL = 'claude-fable-5-1'

class ReviewGateTests(unittest.TestCase):
    def events(self):
        review = {'criteria':[{'name':n, 'score':2, 'evidence':'source.kt:1'} for n in CRITERIA],
                  'total_score':12, 'findings':[], 'unverified_runtime_requirements':['runtime not executed'], 'verdict':'PASS'}
        return [{'type':'assistant', 'message':{'model':MODEL}},
                {'type':'result', 'is_error':False, 'result':json.dumps(review),
                 'modelUsage':{MODEL:{}}, 'usage':{'input_tokens':1, 'cache_creation_input_tokens':2,
                 'cache_read_input_tokens':3, 'output_tokens':4}}]

    def check_result(self, events):
        return assess(events, 0, MODEL, 'test', 'a'*64)

    def test_valid_static_only_and_single_aggregate(self):
        summary, _, ledger = self.check_result(self.events())
        self.assertEqual(summary['exit_code'], 0)
        self.assertTrue(summary['quality_passed'])
        self.assertFalse(summary['runtime_acceptance_proven'])
        self.assertEqual(ledger['total_tokens'], 10)
        self.assertEqual(ledger['usage_scope'], 'own_cli_call_aggregate')

    def test_malformed(self):
        events = self.events(); events[-1]['result'] = 'not json'
        self.assertFalse(self.check_result(events)[0]['review_completed'])

    def test_no_result(self):
        self.assertEqual(self.check_result([])[0]['exit_code'], 2)

    def test_wrong_model(self):
        events = self.events(); events[0]['message']['model'] = 'other'
        self.assertEqual(self.check_result(events)[0]['exit_code'], 2)

    def test_score_mismatch(self):
        events = self.events(); review = json.loads(events[-1]['result']); review['total_score'] = 11
        events[-1]['result'] = json.dumps(review)
        self.assertEqual(self.check_result(events)[0]['exit_code'], 2)

    def test_unresolved_correctness_blocks_pass(self):
        events = self.events(); review = json.loads(events[-1]['result'])
        review['findings'] = [{'severity':'high','line':1,'file':'x.kt','cause':'x','impact':'x',
                              'remediation':'x','correctness':True,'resolved':False}]
        events[-1]['result'] = json.dumps(review)
        self.assertEqual(self.check_result(events)[0]['exit_code'], 2)

    def test_zero_blocks_pass(self):
        events = self.events(); review = json.loads(events[-1]['result'])
        review['criteria'][0]['score'] = 0; review['total_score'] = 10
        events[-1]['result'] = json.dumps(review)
        self.assertEqual(self.check_result(events)[0]['exit_code'], 2)

    def test_needs_work_valid_review_not_quality_pass(self):
        events = self.events(); review = json.loads(events[-1]['result']); review['verdict'] = 'NEEDS_WORK'
        events[-1]['result'] = json.dumps(review)
        summary, _, _ = self.check_result(events)
        self.assertTrue(summary['review_completed'])
        self.assertFalse(summary['quality_passed'])

if __name__ == '__main__':
    unittest.main()
