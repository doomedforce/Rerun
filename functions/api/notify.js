// POST /api/notify — Cloudflare Pages Function replacing alerts.py's SQLite store.
// Signups live in the RERUN_SUBSCRIBERS KV namespace, keyed by email, and go on to Buttondown, which sends the emails.
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const json = (status, body) => Response.json(body, { status, headers: { 'Cache-Control': 'no-store' } });

// Buttondown sends its own confirmation email. A failure never loses the signup: KV keeps it, marked to retry.
async function addToButtondown(email, request, key) {
  if (!key) return false;
  try {
    const response = await fetch('https://api.buttondown.com/v1/subscribers', {
      method: 'POST',
      headers: { Authorization: `Token ${key}`, 'Content-Type': 'application/json', 'X-API-Version': '2026-04-01', 'X-Buttondown-Collision-Behavior': 'add' },
      body: JSON.stringify({ email_address: email, ip_address: request.headers.get('CF-Connecting-IP') || undefined })
    });
    return response.ok;
  } catch {
    return false;
  }
}

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
  const buttondown = await addToButtondown(email, request, env.BUTTONDOWN_API_KEY);
  await env.RERUN_SUBSCRIBERS.put(email, JSON.stringify({ createdAt: new Date().toISOString(), source: 'rerun-web', buttondown }));
  return json(201, { ok: true, message: buttondown ? 'Almost there: check your inbox to confirm.' : 'You’re on the Rerun list.' });
}
