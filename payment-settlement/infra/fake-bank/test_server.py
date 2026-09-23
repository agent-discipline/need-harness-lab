import concurrent.futures
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from server import Bank, make_server


class BankTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "bank.sqlite3")
        self.bank = Bank(self.path)
        self.payload = dict(paymentId="p1", userId=1, agencyId=1, amount=100, currency="KRW")

    def tearDown(self):
        self.directory.cleanup()

    def test_parallel_retry_has_one_effect_and_restart_recovers(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(lambda _: self.bank.transfer("k1", self.payload), range(100)))
        self.assertTrue(all(result == results[0] for result in results))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transfers").fetchone()[0], 1)
        restarted = Bank(self.path)
        self.assertEqual(restarted.lookup("k1"), results[0])
        self.assertEqual(restarted.transfer("k1", self.payload), results[0])
        self.assertEqual(restarted.transfer("k1", dict(self.payload, amount=200))[0], 409)

    def test_faults_and_uncertain_completion(self):
        for fault, status in [("decline", 422), ("5xx", 503), ("timeout-before", 503)]:
            with patch("server.time.sleep") as sleep:
                self.assertEqual(self.bank.transfer(fault, self.payload, fault)[0], status)
                if fault == "timeout-before":
                    sleep.assert_called_once_with(3)
            self.assertEqual(self.bank.lookup(fault)[0], 404)
        def inspect_during_timeout(seconds):
            self.assertEqual(seconds, 3)
            self.assertEqual(Bank(self.path).lookup("after")[0], 200)
        with patch("server.time.sleep", side_effect=inspect_during_timeout):
            result = self.bank.transfer("after", self.payload, "timeout-after")
        self.assertEqual(self.bank.transfer("after", self.payload, "decline"), result)

    def test_http_contract(self):
        server = make_server(self.bank, ("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        try:
            with urllib.request.urlopen(base + "/health") as response:
                self.assertEqual(json.load(response), {"status": "UP"})
            request = urllib.request.Request(base + "/transfers", json.dumps(self.payload).encode(),
                                             {"Idempotency-Key": "http-key", "Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response)["status"], "SUCCEEDED")
            with urllib.request.urlopen(base + "/transfers/http-key") as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(base + "/transfers", b"{}"))
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
