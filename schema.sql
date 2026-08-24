-- Reference schema. The application applies this schema automatically through SQLAlchemy at startup.
CREATE TABLE watch_items (
  id INTEGER PRIMARY KEY,
  name VARCHAR(240) NOT NULL,
  min_price DECIMAL(12,2),
  max_price DECIMAL(12,2),
  region VARCHAR(160) NOT NULL,
  currency VARCHAR(8) NOT NULL DEFAULT 'USD',
  notification_method VARCHAR(12) NOT NULL,
  notification_target VARCHAR(320) NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT 1,
  last_checked_at TIMESTAMP,
  last_status VARCHAR(24) NOT NULL DEFAULT 'never',
  last_error TEXT,
  last_notified_price DECIMAL(12,2),
  current_price DECIMAL(12,2),
  current_deal_url TEXT,
  current_retailer VARCHAR(240),
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);
CREATE TABLE price_history (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES watch_items(id) ON DELETE CASCADE,
  title VARCHAR(500) NOT NULL,
  retailer VARCHAR(240) NOT NULL,
  price DECIMAL(12,2) NOT NULL,
  currency VARCHAR(8) NOT NULL,
  deal_url TEXT NOT NULL,
  found_at TIMESTAMP NOT NULL
);
CREATE INDEX ix_price_history_item_id ON price_history(item_id);
CREATE INDEX ix_price_history_found_at ON price_history(found_at);
