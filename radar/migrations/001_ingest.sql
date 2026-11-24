CREATE TABLE IF NOT EXISTS source_cursor (
  source_id text PRIMARY KEY, health jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS source_item (
  item_id text PRIMARY KEY, source_id text NOT NULL, payload jsonb NOT NULL,
  target_url text NOT NULL, version text NOT NULL, first_seen timestamptz NOT NULL, last_seen timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS document (
  url text PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS discovery_edge (
  item_id text REFERENCES source_item, url text REFERENCES document,
  PRIMARY KEY (item_id,url)
);
CREATE TABLE IF NOT EXISTS ingest_event (
  event_id text PRIMARY KEY, payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS fetch_job (
  url text REFERENCES document, version text NOT NULL,
  item_id text NOT NULL REFERENCES source_item, input jsonb NOT NULL, excerpt text,
  status text NOT NULL DEFAULT 'pending', attempts integer NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL DEFAULT now(), token uuid,
  PRIMARY KEY(url,version)
);
