// POST /api/notify — Cloudflare Pages Function replacing alerts.py's SQLite store.
// Signups live in the RERUN_SUBSCRIBERS KV namespace, keyed by email.
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const json = (status, body) => Response.json(body, { status, headers: { 'Cache-Control': 'no-store' } });

export async function onRequestPost({ request, env }) {
  if (!env.RERUN_SUBSCRIBERS) return json(503, { error: 'Signups are not set up yet. Try again soon.' });
  if (Number(request.headers.get('Content-Length') || 0) > 32000) return json(413, { error: 'Payload too large.' });
  let payload;
  try { payload = await request.json(); } catch { return json(400, { error: 'Send valid JSON.' }); }
  // Honeypot: bots fill the hidden "website" field; pretend success.
  if (payload.website) return json(201, { ok: true, message: 'You’re on the Rerun list.' });
  const email = String(payload.email || '').trim().toLowerCase();
  if (email.length > 254 || !EMAIL_PATTERN.test(email)) return json(422, { error: 'Enter a valid email address.' });
  if (await env.RERUN_SUBSCRIBERS.get(email)) return json(409, { error: 'That email is already on the list.' });
  await env.RERUN_SUBSCRIBERS.put(email, JSON.stringify({ createdAt: new Date().toISOString(), source: 'rerun-web' }));
  return json(201, { ok: true, message: 'You’re on the Rerun list.' });
}
