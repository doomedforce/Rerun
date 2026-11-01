// POST /api/event — counts what people do on Rerun, by day, source and venue.
// Counts only: no cookies, no IP addresses, nothing that identifies a person.
const EVENTS = new Set(['visit', 'ticket_click', 'save', 'calendar_add', 'share', 'signup']);
const slug = (value) => (typeof value === 'string' && /^[a-z0-9-]{1,48}$/.test(value) ? value : '');
const ALLOWED_ORIGINS = new Set(['https://1rerun.com', 'https://www.1rerun.com']);

export async function onRequestPost({ request, env }) {
  if (!env.RERUN_EVENTS) return new Response(null, { status: 204 });
  if (!ALLOWED_ORIGINS.has(request.headers.get('Origin'))) return new Response(null, { status: 403 });
  if (Number(request.headers.get('Content-Length') || 0) > 1000) return new Response(null, { status: 413 });
  let payload;
  try { payload = await request.json(); } catch { return new Response(null, { status: 400 }); }
  if (!EVENTS.has(payload.event)) return new Response(null, { status: 422 });
  const source = slug(payload.source) || 'other';
  const venue = slug(payload.venue);
  const day = new Date().toLocaleDateString('en-CA', { timeZone: 'America/Los_Angeles' });
  await env.RERUN_EVENTS.prepare(
    `INSERT INTO events (day, event, source, venue, count) VALUES (?, ?, ?, ?, 1)
     ON CONFLICT (day, event, source, venue) DO UPDATE SET count = count + 1`
  ).bind(day, payload.event, source, venue).run();
  return new Response(null, { status: 204, headers: { 'Cache-Control': 'no-store' } });
}
