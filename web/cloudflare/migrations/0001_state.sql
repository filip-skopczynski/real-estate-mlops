CREATE TABLE website_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    model_base64 TEXT,
    model_sha256 TEXT,
    feed_json TEXT,
    feed_generated_ms INTEGER,
    CHECK ((model_base64 IS NULL) = (model_sha256 IS NULL)),
    CHECK ((feed_json IS NULL) = (feed_generated_ms IS NULL)),
    CHECK (model_base64 IS NULL OR length(model_base64) <= 1398104),
    CHECK (model_sha256 IS NULL OR
        (length(model_sha256) = 64 AND model_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK (feed_json IS NULL OR length(CAST(feed_json AS BLOB)) <= 524288),
    CHECK (feed_json IS NULL OR model_sha256 IS NOT NULL),
    CHECK (feed_generated_ms IS NULL OR feed_generated_ms >= 0)
);

INSERT INTO website_state (singleton) VALUES (1);
