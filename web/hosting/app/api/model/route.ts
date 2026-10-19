import { env } from 'cloudflare:workers';
import { readModel } from '../../../lib/feed.mjs';
export const dynamic = 'force-dynamic';
export async function GET(request: Request) { try { return await readModel(request,env); } catch { return Response.json({error:'Model temporarily unavailable'},{status:503}); } }
