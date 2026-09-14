import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { loadCatalog } from './catalog.mjs';

const HEADERS = {
  'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
};
// Deliberately no filesystem-path request parameter, generic static server or runtime imports.
export function createViewerServer() {
  const catalog = loadCatalog();
  const assets = new Map([
    ['/', ['index.html', 'text/html; charset=utf-8']],
    ['/app.mjs', ['app.mjs', 'text/javascript; charset=utf-8']],
    ['/view-model.mjs', ['view-model.mjs', 'text/javascript; charset=utf-8']],
    ['/style.css', ['style.css', 'text/css; charset=utf-8']],
  ].map(([route, [file, mime]]) => [route, { body: readFileSync(new URL(file, import.meta.url)), mime }]));
  const server = createServer((req, res) => {
    const send = (code, value, mime = 'application/json; charset=utf-8') => {
      const body = Buffer.isBuffer(value) ? value : Buffer.from(JSON.stringify(value));
      res.writeHead(code, { ...HEADERS, 'Content-Type': mime, 'Content-Length': body.length });
      res.end(req.method === 'HEAD' ? undefined : body);
    };
    const port = server.address()?.port;
    const hosts = [`127.0.0.1:${port}`, `localhost:${port}`];
    if (!hosts.includes(req.headers.host)
      || (req.headers.origin && !hosts.some(host => req.headers.origin === `http://${host}`))) {
      req.resume(); return send(403, { error: 'local_origin_required' });
    }
    if (!['GET', 'HEAD'].includes(req.method)) {
      req.resume(); return send(405, { error: 'read_only' });
    }
    // Compare the raw URL exactly: encoded traversal, query paths and unknown IDs all reject.
    if (assets.has(req.url)) {
      const asset = assets.get(req.url); return send(200, asset.body, asset.mime);
    }
    if (req.url === '/api/runs') {
      return send(200, [...catalog.values()].map(({ id, title, kind, sourceLabel }) => ({ id, title, kind, sourceLabel })));
    }
    for (const [id, run] of catalog) {
      if (req.url === `/api/runs/${id}`) return send(200, run);
    }
    return send(404, { error: 'resource_not_allowlisted' });
  });
  server.requestTimeout = 5000;
  server.headersTimeout = 5000;
  return server;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  if (args.length && (args.length !== 2 || args[0] !== '--port' || !/^\d+$/.test(args[1]))) {
    console.error('Usage: node server.mjs [--port 18743]'); process.exitCode = 1;
  } else {
    const port = args.length ? Number(args[1]) : 18743;
    if (!Number.isInteger(port) || port < 1024 || port > 65535) {
      console.error('Port must be between 1024 and 65535'); process.exitCode = 1;
    } else {
      const server = createViewerServer();
      server.on('error', error => { console.error(`Preview failed: ${error.code}`); process.exitCode = 1; });
      server.listen(port, '127.0.0.1', () => console.log(`ResearchOps read-only preview http://127.0.0.1:${port}/ PID=${process.pid}`));
    }
  }
}
