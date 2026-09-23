-- Shared fixture only; payment/ledger schema belongs to the experimental implementation.
CREATE TABLE users (
    id BIGINT PRIMARY KEY,
    balance_minor BIGINT NOT NULL CHECK (balance_minor >= 0)
);
CREATE TABLE agencies (
    id BIGINT PRIMARY KEY,
    fee_bps INTEGER NOT NULL CHECK (fee_bps BETWEEN 0 AND 10000),
    settlement_balance_minor BIGINT NOT NULL CHECK (settlement_balance_minor >= 0)
);
INSERT INTO users SELECT id, 1000000 FROM generate_series(1, 1000000) AS id;
INSERT INTO agencies SELECT id, 100, 0 FROM generate_series(1, 1000) AS id;
ANALYZE users;
ANALYZE agencies;
