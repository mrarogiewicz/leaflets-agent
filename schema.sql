CREATE TABLE IF NOT EXISTS flyers (
  flyer_id   TEXT PRIMARY KEY,
  store      TEXT NOT NULL,
  region     TEXT,
  title      TEXT,
  valid_from TEXT,
  valid_to   TEXT,
  pdf_url    TEXT,
  n_offers   INTEGER,
  fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS offers (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  flyer_id        TEXT NOT NULL,
  store           TEXT NOT NULL,
  valid_from      TEXT,
  valid_to        TEXT,
  page            INTEGER,
  name            TEXT NOT NULL,
  details         TEXT,
  quantity        REAL,
  quantity_unit   TEXT,
  price           REAL NOT NULL,
  old_price       REAL,
  discount_pct    INTEGER,
  unit_price      REAL,
  unit_price_unit TEXT,
  flags           TEXT
);

CREATE INDEX IF NOT EXISTS idx_offers_flyer ON offers (flyer_id);
CREATE INDEX IF NOT EXISTS idx_offers_valid ON offers (valid_to, store);
CREATE INDEX IF NOT EXISTS idx_offers_name ON offers (name);

CREATE VIEW IF NOT EXISTS current_offers AS
  SELECT * FROM offers WHERE valid_to >= date('now') AND valid_from <= date('now');
