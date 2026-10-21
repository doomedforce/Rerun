/* eslint-disable no-console */
// Lightweight production-server regression test. No worker or live scraping is
// started when server.js is imported, so this uses a self-contained data path.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const dataDir = fs.mkdtempSync(path.join(__dirname, 'rerun-node-test-'));
const source = { id: 'test-source', name: 'Test source', required: true, status: 'error', errors: ['timeout'] };
const meta = {
  status: 'error',
  stale: true,
  lastRefresh: '2026-09-10T00:00:00Z',
  lastVerifiedAt: '2026-09-09T00:00:00Z'
};
const event = { id: 'verified-event', city: 'los-angeles', title: 'Verified Film', startAt: '2099-09-10T20:00:00-07:00' };
for (const [name, value] of Object.entries({
  'snapshot.json': { screenings: [event], venues: [], sources: [source], meta },
  'sources.json': [source],
  'last-refresh.json': meta
})) {
  fs.writeFileSync(path.join(dataDir, name), JSON.stringify(value));
}
process.env.RERUN_DATA_DIR = dataDir;
const { server } = require('../server.js');

function request(port, pathname, options = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: pathname, method: options.method || 'GET', headers: options.headers || {} }, (response) => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { body += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body }));
    });
    req.on('error', reject);
    if (options.body) req.write(options.body);
    req.end();
  });
}

async function run() {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  try {
    const screenings = await request(port, '/api/screenings?city=los-angeles');
    assert.equal(screenings.status, 200);
    assert.equal(JSON.parse(screenings.body).meta.servingLastVerifiedSnapshot, true);

    const rawCatalog = await request(port, '/data/screenings.json');
    assert.equal(rawCatalog.status, 404);

    const health = await request(port, '/api/health');
    assert.equal(health.status, 200);
    const healthBody = JSON.parse(health.body);
    assert.equal(healthBody.storage.persistentStorageConfigured, true);
    assert.equal(healthBody.feed.servingLastVerifiedSnapshot, true);
    assert.equal(health.headers['x-content-type-options'], 'nosniff');

    const honeypot = await request(port, '/api/notify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: 'bot@example.com', website: 'filled' })
    });
    assert.equal(honeypot.status, 201);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(dataDir, { recursive: true, force: true });
  }
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
