import { env } from 'cloudflare:workers';
import { readFeed } from '../../../lib/feed.mjs';
export const dynamic = 'force-dynamic';
export async function GET() { try { return await readFeed(env); } catch { return Response.json({error:'Ranking temporarily unavailable'},{status:503,headers:{'Cache-Control':'private, no-store'}}); } }
