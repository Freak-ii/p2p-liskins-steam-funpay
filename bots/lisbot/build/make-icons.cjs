// Генерация иконок приложения (запускать при изменении дизайна): node build/make-icons.cjs
// Требует playwright (Chromium) — рендерит SVG в PNG, затем собирает ICO.
const fs = require('fs');
const path = require('path');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');
const OUT = path.join(__dirname, '..', 'app', 'assets');

const icon = (dot) => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3b6fe0"/><stop offset="1" stop-color="#1f3a8a"/></linearGradient></defs>
<rect x="8" y="8" width="240" height="240" rx="56" fill="url(#g)"/>
<g fill="#fff">
  <rect x="58" y="120" width="26" height="70" rx="6" opacity=".55"/><rect x="68" y="104" width="6" height="100" rx="3" opacity=".55"/>
  <rect x="115" y="90" width="26" height="80" rx="6" opacity=".8"/><rect x="125" y="72" width="6" height="116" rx="3" opacity=".8"/>
  <rect x="172" y="60" width="26" height="86" rx="6"/><rect x="182" y="44" width="6" height="118" rx="3"/>
</g>
<path d="M52 206 L118 152 L150 172 L206 118" stroke="#7ee2a8" stroke-width="14" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
${dot ? `<circle cx="200" cy="200" r="50" fill="#0f1115"/><circle cx="200" cy="200" r="38" fill="${dot}"/>` : ''}
</svg>`;

function ico(pngs) { // ICO с PNG-кадрами (поддерживается Windows Vista+)
  const header = Buffer.alloc(6); header.writeUInt16LE(0, 0); header.writeUInt16LE(1, 2); header.writeUInt16LE(pngs.length, 4);
  const dir = Buffer.alloc(16 * pngs.length);
  let offset = 6 + dir.length;
  pngs.forEach(([size, buf], i) => {
    const o = i * 16;
    dir.writeUInt8(size >= 256 ? 0 : size, o); dir.writeUInt8(size >= 256 ? 0 : size, o + 1);
    dir.writeUInt16LE(1, o + 4); dir.writeUInt16LE(32, o + 6); dir.writeUInt32LE(buf.length, o + 8); dir.writeUInt32LE(offset, o + 12);
    offset += buf.length;
  });
  return Buffer.concat([header, dir, ...pngs.map(([, b]) => b)]);
}

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  const render = async (svg, size) => {
    await p.setViewportSize({ width: size, height: size });
    await p.setContent(`<html><body style="margin:0;background:transparent">${svg.replace('<svg ', `<svg width="${size}" height="${size}" `)}</body></html>`);
    return p.screenshot({ omitBackground: true, clip: { x: 0, y: 0, width: size, height: size } });
  };
  const sizes = [16, 24, 32, 48, 64, 128, 256];
  const frames = [];
  for (const s of sizes) frames.push([s, await render(icon(null), s)]);
  fs.writeFileSync(path.join(OUT, 'icon.ico'), ico(frames));
  fs.writeFileSync(path.join(OUT, 'icon.png'), await render(icon(null), 512));
  for (const [name, color] of [['green', '#2fbf71'], ['yellow', '#e0a526'], ['red', '#e5484d'], ['grey', '#8b93a3']]) {
    fs.writeFileSync(path.join(OUT, `tray-${name}.png`), await render(icon(color), 32));
    fs.writeFileSync(path.join(OUT, `tray-${name}@2x.png`), await render(icon(color), 64));
  }
  fs.writeFileSync(path.join(OUT, 'icon.svg'), icon(null));
  await b.close();
  console.log('иконки готовы:', fs.readdirSync(OUT).join(', '));
})();
