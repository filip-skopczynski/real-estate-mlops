declare namespace Cloudflare {
  interface Env {
    DB?: D1Database;
    BUCKET?: R2Bucket;
    WEBSITE_INGEST_TOKEN?: string;
  }
}
