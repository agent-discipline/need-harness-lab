ALTER TABLE users ADD COLUMN reserved_minor BIGINT NOT NULL DEFAULT 0;
ALTER TABLE users ADD CONSTRAINT users_available_check CHECK (balance_minor >= 0 AND reserved_minor >= 0 AND reserved_minor <= balance_minor);

CREATE TABLE payments (
    id UUID PRIMARY KEY,
    agency_id BIGINT NOT NULL REFERENCES agencies(id),
    user_id BIGINT NOT NULL REFERENCES users(id),
    idempotency_key VARCHAR(128) NOT NULL,
    bank_key VARCHAR(200) NOT NULL UNIQUE,
    amount_minor BIGINT NOT NULL CHECK (amount_minor BETWEEN 1 AND 1000000),
    currency VARCHAR(3) NOT NULL CHECK (currency = 'KRW'),
    fee_bps INTEGER NOT NULL CHECK (fee_bps BETWEEN 0 AND 10000),
    fee_minor BIGINT NOT NULL CHECK (fee_minor >= 0),
    status VARCHAR(16) NOT NULL CHECK (status IN ('PENDING','SUCCEEDED','FAILED')),
    failure_code VARCHAR(40),
    bank_transfer_id VARCHAR(100),
    succeeded_at TIMESTAMPTZ,
    settlement_id UUID,
    processing_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (agency_id, idempotency_key)
);
CREATE INDEX payments_recovery_idx ON payments(processing_until) WHERE status = 'PENDING';
CREATE INDEX payments_settlement_idx ON payments(agency_id, succeeded_at) WHERE status = 'SUCCEEDED' AND settlement_id IS NULL;

CREATE TABLE ledger (
    id BIGSERIAL PRIMARY KEY,
    payment_id UUID NOT NULL UNIQUE REFERENCES payments(id),
    user_id BIGINT NOT NULL REFERENCES users(id),
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE settlements (
    id UUID PRIMARY KEY,
    agency_id BIGINT NOT NULL REFERENCES agencies(id),
    idempotency_key VARCHAR(128) NOT NULL,
    business_date DATE NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status = 'SUCCEEDED'),
    cutoff_at TIMESTAMPTZ NOT NULL,
    gross_amount BIGINT NOT NULL DEFAULT 0,
    fee_amount BIGINT NOT NULL DEFAULT 0,
    net_amount BIGINT NOT NULL DEFAULT 0,
    payment_count BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (agency_id, idempotency_key)
);
ALTER TABLE payments ADD CONSTRAINT payments_settlement_fk FOREIGN KEY (settlement_id) REFERENCES settlements(id);
CREATE TABLE settlement_items (
    settlement_id UUID NOT NULL REFERENCES settlements(id),
    payment_id UUID NOT NULL UNIQUE REFERENCES payments(id),
    amount_minor BIGINT NOT NULL,
    fee_minor BIGINT NOT NULL,
    PRIMARY KEY (settlement_id, payment_id)
);
