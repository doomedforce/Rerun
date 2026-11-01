// Runs the Pages Functions with stand-ins for Cloudflare: node tests/test_functions.mjs
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const load = async (path) => import(`data:text/javascript,${encodeURIComponent(await readFile(new URL(path, import.meta.url), 'utf8'))}`);
const event = await load('../functions/api/event.js');
const notify = await load('../functions/api/notify.js');
const post = (body, origin = 'https://1rerun.com') => new Request('https://1rerun.com/api/x', { method: 'POST', headers: { Origin: origin, 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

const writes = [];
const db = { prepare: () => ({ bind: (...values) => ({ run: async () => writes.push(values) }) }) };
assert.equal((await event.onRequestPost({ request: post({ event: 'ticket_click', source: 'reddit', venue: 'new-beverly-cinema' }), env: { RERUN_EVENTS: db } })).status, 204);
assert.deepEqual(writes[0].slice(1), ['ticket_click', 'reddit', 'new-beverly-cinema']);
await event.onRequestPost({ request: post({ event: 'visit', source: 'Bad Value!', venue: 7 }), env: { RERUN_EVENTS: db } });
assert.deepEqual(writes[1].slice(1), ['visit', 'other', '']);
assert.equal((await event.onRequestPost({ request: post({ event: 'hack' }), env: { RERUN_EVENTS: db } })).status, 422);
assert.equal((await event.onRequestPost({ request: post({ event: 'visit' }, 'https://evil.example'), env: { RERUN_EVENTS: db } })).status, 403);
assert.equal(writes.length, 2);

const kv = () => { const m = new Map(); return { m, get: async (k) => m.get(k) ?? null, put: async (k, v) => m.set(k, v) }; };
const realFetch = globalThis.fetch;

// With a key: the address goes to Buttondown and the reply says to confirm.
let sent;
globalThis.fetch = async (url, init) => { sent = { url, init }; return new Response('{}', { status: 201 }); };
let store = kv();
let res = await notify.onRequestPost({ request: post({ email: 'A@Example.com' }), env: { RERUN_SUBSCRIBERS: store, BUTTONDOWN_API_KEY: 'test' } });
assert.equal(res.status, 201);
assert.match((await res.json()).message, /confirm/i);
assert.equal(JSON.parse(sent.init.body).email_address, 'a@example.com');
assert.equal(JSON.parse(store.m.get('a@example.com')).buttondown, true);

// Buttondown down: the signup is kept, marked to send later.
globalThis.fetch = async () => { throw new Error('offline'); };
store = kv();
res = await notify.onRequestPost({ request: post({ email: 'b@example.com' }), env: { RERUN_SUBSCRIBERS: store, BUTTONDOWN_API_KEY: 'test' } });
assert.equal(res.status, 201);
assert.equal(JSON.parse(store.m.get('b@example.com')).buttondown, false);

// No key yet: signups still save, and nothing is called.
globalThis.fetch = async () => { throw new Error('should not be called'); };
store = kv();
res = await notify.onRequestPost({ request: post({ email: 'c@example.com' }), env: { RERUN_SUBSCRIBERS: store } });
assert.equal(res.status, 201);
assert.equal(JSON.parse(store.m.get('c@example.com')).buttondown, false);
globalThis.fetch = realFetch;
console.log('functions ok');
