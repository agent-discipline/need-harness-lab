"""Deterministic bank simulator. Monetary values are integer KRW minor units."""
import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit


class Bank:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS transfers (key TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def transfer(self, key, payload, fault=None):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload, result FROM transfers WHERE key=?", (key,)).fetchone()
            if row:
                return (200, json.loads(row[1])) if row[0] == encoded else (409, {"error": "IDEMPOTENCY_CONFLICT"})
            if fault == "decline":
                return 422, {"error": "BANK_DECLINED"}
            if fault in ("5xx", "timeout-before"):
                result = (503, {"error": "BANK_UNAVAILABLE"})
            else:
                import uuid
                result = (200, {"bankTransferId": str(uuid.uuid5(uuid.NAMESPACE_URL, key)), "status": "SUCCEEDED"})
                db.execute("INSERT INTO transfers VALUES (?, ?, ?)", (key, encoded, json.dumps(result[1])))
        if fault in ("timeout-before", "timeout-after"):
            time.sleep(3)
        return result

    def lookup(self, key):
        with self.connect() as db:
            row = db.execute("SELECT result FROM transfers WHERE key=?", (key,)).fetchone()
        return (200, json.loads(row[0])) if row else (404, {"error": "TRANSFER_NOT_FOUND"})


def make_server(bank, address=("0.0.0.0", 8081)):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, body):
            data = json.dumps(body).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                self.reply(200, {"status": "UP"})
            elif path.startswith("/transfers/"):
                self.reply(*bank.lookup(unquote(path[len("/transfers/"):])))
            else:
                self.reply(404, {"error": "NOT_FOUND"})

        def do_POST(self):
            if urlsplit(self.path).path != "/transfers":
                self.reply(404, {"error": "NOT_FOUND"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError()
                payload = json.loads(self.rfile.read(length))
                key = self.headers.get("Idempotency-Key", "").strip()
                if not key or len(key) > 200 or not isinstance(payload, dict):
                    raise ValueError()
                if set(payload) != {"paymentId", "userId", "agencyId", "amount", "currency"}:
                    raise ValueError()
                if not isinstance(payload["paymentId"], str) or not payload["paymentId"]:
                    raise ValueError()
                if any(type(payload[x]) is not int or payload[x] <= 0 for x in ("userId", "agencyId", "amount")):
                    raise ValueError()
                if payload["currency"] != "KRW":
                    raise ValueError()
                fault = self.headers.get("X-Fault")
                if fault not in (None, "decline", "timeout-before", "timeout-after", "5xx"):
                    raise ValueError()
            except (ValueError, TypeError, json.JSONDecodeError):
                self.reply(400, {"error": "INVALID_REQUEST"})
                return
            self.reply(*bank.transfer(key, payload, fault))

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(address, Handler)


if __name__ == "__main__":
    journal = os.environ.get("BANK_JOURNAL", "/data/bank.sqlite3")
    os.makedirs(os.path.dirname(os.path.abspath(journal)), exist_ok=True)
    make_server(Bank(journal), ("0.0.0.0", int(os.environ.get("PORT", "8081")))).serve_forever()
