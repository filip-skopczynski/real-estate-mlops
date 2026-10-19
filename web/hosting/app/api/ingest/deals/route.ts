import { env } from 'cloudflare:workers';
import { ingestFeed } from '../../../../lib/feed.mjs';
export const dynamic = 'force-dynamic';
export async function POST(request: Request) { try { return await ingestFeed(request,env); } catch { return Response.json({error:'Publication temporarily unavailable'},{status:503}); } }
