import http from 'node:http';
import { WebSocketServer } from 'ws';

/**
 * Эмулятор API LIS-SKINS (https://api.lis-skins.com/v1) и его WebSocket (Centrifugo, JSON-протокол)
 * поверх DemoWorld. Бот ходит в него своим обычным LisClient и LisWs — меняется только адрес.
 */
export function startLisEmulator(world, { host = '127.0.0.1', port = 0 } = {}) {
  const sockets = new Set();
  const json = (res, status, body) => {
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(body));
  };
  const listingView = (l) => ({ id: Number(l.id), name: l.name, price: l.price, unlock_at: null, item_float: null, created_at: new Date(l.created).toISOString() });

  const server = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (c) => { raw += c; });
    req.on('end', () => {
      const u = new URL(req.url, 'http://lis');
      const p = u.pathname.replace(/^\/v1/, '');
      const q = u.searchParams;
      const list = (k) => q.getAll(`${k}[]`).concat(q.getAll(k));
      const num = (k) => (q.has(k) ? Number(q.get(k)) : undefined);
      if (!/^Bearer \S+/.test(req.headers.authorization || '')) return json(res, 401, { message: 'Unauthenticated.' });
      let body = {};
      try { body = raw ? JSON.parse(raw) : {}; } catch { return json(res, 400, { message: 'bad json' }); }
      try {
        if (req.method === 'GET' && p === '/user/balance') return json(res, 200, { data: { balance: world.balance } });
        if (req.method === 'GET' && p === '/user/get-ws-token') return json(res, 200, { data: { token: 'demo-ws-token' } });
        if (req.method === 'GET' && p === '/market/search') {
          const all = world.search({ names: list('names'), priceFrom: num('price_from'), priceTo: num('price_to'), sortBy: q.get('sort_by') || undefined });
          const start = Number(q.get('cursor') || 0);
          const page = all.slice(start, start + 50);
          return json(res, 200, { data: page.map(listingView), meta: { next_cursor: start + 50 < all.length ? String(start + 50) : null } });
        }
        if (req.method === 'POST' && p === '/market/buy') {
          const r = world.buy({ ids: body.ids || [], maxPrice: body.max_price, customId: body.custom_id, skipUnavailable: !!body.skip_unavailable });
          return json(res, 200, { data: world.purchaseView(r) });
        }
        if (req.method === 'GET' && p === '/market/info') {
          return json(res, 200, { data: world.info({ customIds: list('custom_ids'), purchaseIds: list('purchase_ids') }) });
        }
        if (req.method === 'GET' && p === '/market/check-availability') {
          return json(res, 200, { data: list('ids').map((id) => ({ id: Number(id), available: world.listings.has(String(id)) })) });
        }
        if (req.method === 'POST' && p === '/market/withdraw') {
          const pur = world.purchases.get(body.custom_id) || [...world.purchases.values()].find((x) => String(x.purchase_id) === String(body.purchase_id));
          if (!pur) return json(res, 404, { message: 'Purchase not found' });
          world.sendTrade(pur);
          return json(res, 200, { data: { ok: true } });
        }
        if (req.method === 'GET' && p === '/market/history') return json(res, 200, { data: [], meta: { last_page: 1 } });
        return json(res, 404, { message: `Not found: ${p}` });
      } catch (e) {
        return json(res, e.status || 500, { message: e.message });
      }
    });
  });

  // ---- WebSocket в стиле Centrifugo: connect -> subscribe -> push.pub
  const wss = new WebSocketServer({ server, path: '/connection/websocket' });
  wss.on('connection', (ws) => {
    ws.channels = new Set();
    sockets.add(ws);
    ws.on('close', () => sockets.delete(ws));
    ws.on('message', (buf) => {
      for (const line of buf.toString().split('\n')) {
        if (!line.trim()) continue;
        let m;
        try { m = JSON.parse(line); } catch { continue; }
        if (m.connect) ws.send(JSON.stringify({ id: m.id, connect: { client: `demo-${m.id}`, version: '0.0.0-emu' } }));
        else if (m.subscribe) { ws.channels.add(m.subscribe.channel); ws.send(JSON.stringify({ id: m.id, subscribe: {} })); }
      }
    });
  });
  const publish = (event, payload) => {
    for (const ws of sockets) {
      for (const channel of ws.channels) {
        if (ws.readyState === 1) ws.send(JSON.stringify({ push: { channel, pub: { data: { event, payload: [payload] } } } }));
      }
    }
  };
  const onListing = (l) => publish('obtained_skin_added', listingView(l));
  const onRemoved = ({ id }) => publish('obtained_skin_deleted', { id: Number(id) });
  world.on('lis.listing', onListing);
  world.on('lis.removed', onRemoved);

  return new Promise((resolve) => {
    server.listen(port, host, () => {
      const { port: real } = server.address();
      resolve({
        baseUrl: `http://${host}:${real}/v1`,
        wsUrl: `ws://${host}:${real}/connection/websocket`,
        close: () => new Promise((r) => {
          world.off('lis.listing', onListing);
          world.off('lis.removed', onRemoved);
          for (const ws of sockets) ws.terminate();
          wss.close();
          server.close(() => r());
          server.closeAllConnections?.();
        }),
      });
    });
  });
}
