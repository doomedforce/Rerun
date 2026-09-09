const http = require('node:http');
const fs = require('node:fs/promises');
const { spawn } = require('node:child_process');
const path = require('node:path');
const { URL } = require('node:url');

const ROOT = __dirname;
const DATA_DIR = path.join(ROOT, 'data');
const HOST = process.env.RERUN_HOST || '0.0.0.0';
const PORT = Number(process.env.RERUN_PORT || process.env.PORT || 4173);

const MIME_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.ico': 'image/x-icon'
};

const BLOCKED_STATIC = new Set(['/server.js', '/server.py', '/scraper.py', '/refresh.py', '/package.json', '/README.md', '/data/subscribers.json', '/data/sources.json', '/data/last-refresh.json']);
const AUTO_REFRESH = !['0', 'false', 'no'].includes(String(process.env.RERUN_AUTO_REFRESH || '1').toLowerCase());
const REFRESH_TTL_MS = Number(process.env.RERUN_REFRESH_TTL_SECONDS || 1800) * 1000;
const RETRY_BACKOFF_MS = Number(process.env.RERUN_REFRESH_RETRY_SECONDS || 600) * 1000;
const FEED_POLICY_VERSION = 12;
const OPTIONAL_SOURCE_IDS = new Set(['new-beverly', 'vidiots']);
const IMAGE_CACHE = new Map();
const IMAGE_CACHE_LIMIT = 64;
const IMAGE_MAX_BYTES = 8 * 1024 * 1024;
const IMAGE_ALLOWED_HOSTS = new Set([
  'americancinematheque.com', 'assets.americancinematheque.com',
  'thenewbev.com', 'www.thenewbev.com',
  'vistatheaterhollywood.com', 'www.vistatheaterhollywood.com',
  'vidiotsfoundation.org', 'www.vidiotsfoundation.org', 'la.teentix.org'
]);
let refreshInFlight = null;

const jsonHeaders = { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' };

async function readJson(fileName, fallback = []) {
  try {
    return JSON.parse(await fs.readFile(path.join(DATA_DIR, fileName), 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') return fallback;
    throw error;
  }
}

async function writeJson(fileName, value) {
  await fs.mkdir(DATA_DIR, { recursive: true });
  await fs.writeFile(path.join(DATA_DIR, fileName), `${JSON.stringify(value, null, 2)}\n`, 'utf8');
}

function sendJson(response, status, payload) {
  response.writeHead(status, jsonHeaders);
  response.end(JSON.stringify(payload));
}

function imageHostAllowed(hostname) {
  const host = String(hostname || '').toLowerCase().replace(/\.$/, '');
  return IMAGE_ALLOWED_HOSTS.has(host) || [...IMAGE_ALLOWED_HOSTS].some((domain) => host.endsWith(`.${domain}`));
}

async function handleImage(response, requestUrl) {
  const rawUrl = requestUrl.searchParams.get('url') || '';
  let target;
  try {
    target = new URL(rawUrl);
  } catch (error) {
    response.writeHead(400, { 'Content-Type': 'text/plain; charset=utf-8' });
    return response.end('Invalid image URL');
  }
  if (target.protocol !== 'https:' || !imageHostAllowed(target.hostname)) {
    response.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
    return response.end('Image source is not an approved official venue domain');
  }
  if (IMAGE_CACHE.has(target.href)) {
    const cached = IMAGE_CACHE.get(target.href);
    response.writeHead(200, { 'Content-Type': cached.contentType, 'Cache-Control': 'public, max-age=3600, stale-while-revalidate=86400', 'Content-Length': cached.body.length });
    return response.end(cached.body);
  }
  try {
    const upstream = await fetch(target, {
      headers: {
        Accept: 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        Referer: `${target.origin}/`,
        'User-Agent': 'Rerun/0.4 (+https://rerun.local; official venue artwork proxy)'
      },
      signal: AbortSignal.timeout(15000)
    });
    const finalUrl = new URL(upstream.url || target.href);
    if (!upstream.ok || finalUrl.protocol !== 'https:' || !imageHostAllowed(finalUrl.hostname)) {
      throw new Error(`upstream returned ${upstream.status}`);
    }
    const contentType = (upstream.headers.get('content-type') || '').split(';', 1)[0].toLowerCase();
    if (!contentType.startsWith('image/')) throw new Error('approved source did not return an image');
    const body = Buffer.from(await upstream.arrayBuffer());
    if (body.length > IMAGE_MAX_BYTES) throw new Error('image exceeds the size limit');
    if (IMAGE_CACHE.size >= IMAGE_CACHE_LIMIT) IMAGE_CACHE.delete(IMAGE_CACHE.keys().next().value);
    IMAGE_CACHE.set(target.href, { contentType, body });
    response.writeHead(200, { 'Content-Type': contentType, 'Cache-Control': 'public, max-age=3600, stale-while-revalidate=86400', 'Content-Length': body.length });
    return response.end(body);
  } catch (error) {
    response.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' });
    return response.end(`Image unavailable: ${error.message}`);
  }
}

function normalize(value) {
  return String(value || '').trim().toLowerCase();
}

function refreshIsStale(meta = {}, force = false) {
  if (force || !meta || Object.keys(meta).length === 0) return true;
  if (Number(meta.feedPolicyVersion) !== FEED_POLICY_VERSION) return true;
  if (meta.nextRetryAt && Date.parse(meta.nextRetryAt) > Date.now()) return false;
  if (meta.mode === 'seed' || ['seed', 'unknown'].includes(meta.status)) return true;
  const checked = Date.parse(meta.lastRefresh || meta.checkedAt || '');
  return !Number.isFinite(checked) || Date.now() - checked >= REFRESH_TTL_MS;
}

function sourceIsRequired(source = {}) {
  return source.required !== false && !OPTIONAL_SOURCE_IDS.has(source.id);
}

function feedIsComplete(refreshMeta = {}, sources = []) {
  const requiredSources = sources.filter(sourceIsRequired);
  const verifiedRequired = Number(refreshMeta.verifiedRequiredSourceCount)
    || requiredSources.filter((source) => source.status === 'verified').length;
  return requiredSources.length > 0
    && refreshMeta.status === 'ok'
    && verifiedRequired >= requiredSources.length
    && requiredSources.every((source) => source.status === 'verified');
}

function runPythonRefresh(force = false) {
  if (refreshInFlight) return refreshInFlight;
  const executable = process.env.RERUN_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  refreshInFlight = new Promise((resolve) => {
    const child = spawn(executable, [path.join(ROOT, 'refresh.py')], {
      cwd: ROOT,
      env: process.env,
      stdio: ['ignore', 'ignore', 'pipe']
    });
    let stderr = '';
    const timer = setTimeout(() => child.kill('SIGTERM'), Number(process.env.RERUN_REFRESH_TIMEOUT_MS || 120000));
    child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
    child.on('error', async (error) => {
      clearTimeout(timer);
      const prior = await readJson('last-refresh.json', {});
      await writeJson('last-refresh.json', { ...prior, status: 'error', stale: true, lastRefresh: new Date().toISOString(), errors: [String(error)], nextRetryAt: new Date(Date.now() + RETRY_BACKOFF_MS).toISOString() });
      resolve(false);
    });
    child.on('close', async (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        const prior = await readJson('last-refresh.json', {});
        await writeJson('last-refresh.json', { ...prior, status: 'error', stale: true, lastRefresh: new Date().toISOString(), errors: [stderr.trim() || `Refresh exited with code ${code}`], nextRetryAt: new Date(Date.now() + RETRY_BACKOFF_MS).toISOString() });
      }
      resolve(code === 0);
    });
  }).finally(() => { refreshInFlight = null; });
  return refreshInFlight;
}

async function maybeRefresh(force = false) {
  const prior = await readJson('last-refresh.json', {});
  if (!force && (!AUTO_REFRESH || !refreshIsStale(prior))) return prior;
  await runPythonRefresh(force);
  return readJson('last-refresh.json', {});
}

function feedMeta(city, data, venues, sources, refreshMeta) {
  return {
    count: data.length,
    city: city || null,
    updatedAt: new Date().toISOString(),
    lastRefresh: refreshMeta.lastRefresh || null,
    refreshStatus: refreshMeta.status || 'unknown',
    stale: Boolean(refreshMeta.stale ?? true),
    complete: feedIsComplete(refreshMeta, sources),
    source: 'Official venue feeds',
    sourceCount: sources.length,
    requiredSourceCount: Number(refreshMeta.requiredSourceCount) || sources.filter(sourceIsRequired).length,
    sources: sources.map((source) => ({
      id: source.id,
      name: source.name,
      url: source.url,
      partnerUrl: source.partnerUrl,
      status: source.status,
      required: sourceIsRequired(source),
      sourceMode: source.sourceMode || '',
      eventCount: source.eventCount || 0,
      lastChecked: source.lastChecked || null,
      errors: (source.errors || []).slice(0, 2)
    })),
    venueCount: venues.length
  };
}

function matchesQuery(item, query) {
  if (!query) return true;
  return [item.title, item.director, item.venue, item.genre, item.format, item.notes, item.sourceName]
    .filter(Boolean)
    .some((field) => normalize(field).includes(query));
}

function parseBody(request) {
  return new Promise((resolve, reject) => {
    let body = '';
    request.on('data', (chunk) => {
      body += chunk;
      if (body.length > 32_000) request.destroy(new Error('Payload too large'));
    });
    request.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (error) {
        reject(error);
      }
    });
    request.on('error', reject);
  });
}

async function handleApi(request, response, requestUrl) {
  const pathname = requestUrl.pathname;

  if (request.method === 'GET' && pathname === '/api/health') {
    const refreshMeta = await readJson('last-refresh.json', {});
    return sendJson(response, 200, {
      ok: true,
      service: 'rerun-api',
      version: '0.3.0',
      updatedAt: new Date().toISOString(),
      feed: {
        status: refreshMeta.status || 'unknown',
        lastRefresh: refreshMeta.lastRefresh || null,
        stale: Boolean(refreshMeta.stale ?? true)
      }
    });
  }

  if (request.method === 'GET' && pathname === '/api/image') {
    return handleImage(response, requestUrl);
  }

  if (request.method === 'GET' && pathname === '/api/screenings') {
    const city = normalize(requestUrl.searchParams.get('city'));
    const force = ['1', 'true', 'yes'].includes(String(requestUrl.searchParams.get('refresh') || '').toLowerCase());
    const refreshMeta = city === '' || city === 'los-angeles' ? await maybeRefresh(force) : await readJson('last-refresh.json', {});
    const [screenings, venues] = await Promise.all([readJson('screenings.json'), readJson('venues.json')]);
    const sources = await readJson('sources.json');
    if ((city === '' || city === 'los-angeles') && !feedIsComplete(refreshMeta, sources)) {
      const retryAfterSeconds = Math.max(5, Math.min(900, Number(refreshMeta.retryAfterSeconds || 30)));
      return sendJson(response, 503, {
        error: 'complete_live_index_unavailable',
        message: 'Rerun is waiting for every required official Los Angeles source to verify.',
        retryAfterSeconds,
        meta: feedMeta(city, [], venues, sources, refreshMeta),
        sources
      });
    }
    const date = normalize(requestUrl.searchParams.get('date'));
    const query = normalize(requestUrl.searchParams.get('q') || requestUrl.searchParams.get('query'));
    const format = normalize(requestUrl.searchParams.get('format'));
    const limit = Math.min(Math.max(Number(requestUrl.searchParams.get('limit') || 500), 1), 500);
    const data = screenings
      .filter((item) => !city || normalize(item.city) === city)
      .filter((item) => !Number.isFinite(Date.parse(item.startAt || '')) || Date.parse(item.startAt) >= Date.now())
      .filter((item) => !date || date === 'all' || normalize(item.dateGroup) === date)
      .filter((item) => !format || normalize(item.format).includes(format))
      .filter((item) => matchesQuery(item, query))
      .sort((a, b) => new Date(a.startAt) - new Date(b.startAt))
      .slice(0, limit);
    return sendJson(response, 200, {
      data,
      meta: feedMeta(city, data, venues, sources, refreshMeta)
    });
  }

  const screeningMatch = pathname.match(/^\/api\/screenings\/([^/]+)$/);
  if (request.method === 'GET' && screeningMatch) {
    const screenings = await readJson('screenings.json');
    const item = screenings.find((screening) => screening.id === decodeURIComponent(screeningMatch[1]));
    return item ? sendJson(response, 200, { data: item }) : sendJson(response, 404, { error: 'Screening not found' });
  }

  if (request.method === 'GET' && pathname === '/api/venues') {
    const venues = await readJson('venues.json');
    const city = normalize(requestUrl.searchParams.get('city'));
    const data = venues.filter((venue) => !city || normalize(venue.city) === city);
    return sendJson(response, 200, { data, meta: { count: data.length, updatedAt: new Date().toISOString() } });
  }

  if (request.method === 'GET' && pathname === '/api/sources') {
    const [sources, refreshMeta] = await Promise.all([readJson('sources.json'), readJson('last-refresh.json', {})]);
    return sendJson(response, 200, { data: sources, meta: refreshMeta });
  }

  if (request.method === 'POST' && pathname === '/api/refresh') {
    await maybeRefresh(true);
    const meta = await readJson('last-refresh.json', {});
    const sources = await readJson('sources.json');
    const ok = feedIsComplete(meta, sources);
    return sendJson(response, ok ? 200 : 503, { ok, completed: ok, meta, sources });
  }

  if (request.method === 'POST' && pathname === '/api/notify') {
    let payload;
    try {
      payload = await parseBody(request);
    } catch (error) {
      return sendJson(response, 400, { error: 'Send valid JSON.' });
    }
    const email = normalize(payload.email);
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      return sendJson(response, 422, { error: 'Enter a valid email address.' });
    }
    const subscribers = await readJson('subscribers.json');
    if (subscribers.some((subscriber) => subscriber.email === email)) {
      return sendJson(response, 409, { error: 'That email is already on the list.' });
    }
    subscribers.push({ email, createdAt: new Date().toISOString(), source: 'rerun-web' });
    await writeJson('subscribers.json', subscribers);
    return sendJson(response, 201, { ok: true, message: 'You’re on the list — see you at the movies.' });
  }

  return sendJson(response, 404, { error: 'API route not found' });
}

async function serveStatic(request, response, requestUrl) {
  const requested = requestUrl.pathname === '/' ? '/index.html' : requestUrl.pathname;
  if (BLOCKED_STATIC.has(requested) || requested.startsWith('/data/cache/')) {
    response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    return response.end('Not found');
  }
  const candidate = path.normalize(path.join(ROOT, requested));
  if (!candidate.startsWith(ROOT + path.sep) && candidate !== ROOT) {
    response.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
    return response.end('Forbidden');
  }
  try {
    const stats = await fs.stat(candidate);
    if (!stats.isFile()) throw Object.assign(new Error('Not a file'), { code: 'ENOENT' });
    const extension = path.extname(candidate).toLowerCase();
    const headers = { 'Content-Type': MIME_TYPES[extension] || 'application/octet-stream' };
    if (['.html', '.js', '.css', '.json', '.webmanifest'].includes(extension)) {
      headers['Cache-Control'] = 'no-cache, must-revalidate';
    }
    response.writeHead(200, headers);
    return response.end(await fs.readFile(candidate));
  } catch (error) {
    if (error.code !== 'ENOENT') {
      response.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' });
      return response.end('Server error');
    }
    response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    return response.end('Not found');
  }
}

const server = http.createServer(async (request, response) => {
  const requestUrl = new URL(request.url, `http://${request.headers.host || 'localhost'}`);
  try {
    if (requestUrl.pathname.startsWith('/api/')) return await handleApi(request, response, requestUrl);
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      response.writeHead(405, { Allow: 'GET, HEAD, POST' });
      return response.end();
    }
    return await serveStatic(request, response, requestUrl);
  } catch (error) {
    console.error(error);
    return sendJson(response, 500, { error: 'Unexpected server error' });
  }
});

if (require.main === module) {
  server.listen(PORT, HOST, () => {
    console.log(`Rerun is live at http://localhost:${PORT} (bound to ${HOST})`);
  });
}

module.exports = { server, handleApi, serveStatic };
