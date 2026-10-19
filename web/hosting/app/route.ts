import html from '../site-assets/index.html?raw';
export const dynamic = 'force-dynamic';
export function GET() {
  return new Response(html, {headers:{
    'Content-Type':'text/html; charset=utf-8', 'Cache-Control':'private, no-store',
    'X-Content-Type-Options':'nosniff', 'Referrer-Policy':'no-referrer',
    'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'",
  }});
}
