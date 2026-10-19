import { env } from 'cloudflare:workers';
import { ingestModel } from '../../../../lib/feed.mjs';
export const dynamic = 'force-dynamic';
export async function PUT(request: Request) { try { return await ingestModel(request,env); } catch { return Response.json({error:'Model storage temporarily unavailable'},{status:503}); } }
