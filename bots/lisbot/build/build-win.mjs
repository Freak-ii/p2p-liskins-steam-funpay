// Сборка Windows-приложения: портативный zip + установщик (NSIS).
// Работает на Linux/macOS/Windows. Нужны: Node 20+, zip (или PowerShell), makensis (для установщика).
//   npm run build:win
// Переменные: ELECTRON_ZIP — путь к уже скачанному electron-vX-win32-x64.zip (иначе скачается с GitHub).
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const BUILD = path.join(ROOT, 'build');
const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
const VERSION = pkg.version;
const ELECTRON = (pkg.devDependencies.electron || '').replace(/^[^\d]*/, '');
const STAGE = path.join(BUILD, '.stage');
const CACHE = path.join(BUILD, '.cache');
const DIST = path.join(ROOT, 'dist');
const APPDIR = path.join(DIST, 'LisBot-win-x64');
const sh = (cmd, args, opts = {}) => execFileSync(cmd, args, { stdio: 'inherit', ...opts });
const has = (cmd) => { try { execFileSync(process.platform === 'win32' ? 'where' : 'which', [cmd], { stdio: 'ignore' }); return true; } catch { return false; } };
const step = (s) => console.log(`\n▶ ${s}`);

async function download(url, dest) {
  const res = await fetch(url, { redirect: 'follow' });
  if (!res.ok) throw new Error(`Не удалось скачать ${url}: ${res.status}`);
  fs.writeFileSync(dest, Buffer.from(await res.arrayBuffer()));
}

// 1. Исходники приложения + только боевые зависимости
step('Подготовка файлов приложения');
fs.rmSync(STAGE, { recursive: true, force: true });
fs.mkdirSync(STAGE, { recursive: true });
for (const f of ['src', 'app', 'config.example.json', 'README.md', 'package-lock.json']) fs.cpSync(path.join(ROOT, f), path.join(STAGE, f), { recursive: true });
const appPkg = { name: pkg.name, productName: 'LisBot', version: VERSION, description: pkg.description, main: pkg.main, type: 'module', private: true, dependencies: pkg.dependencies };
fs.writeFileSync(path.join(STAGE, 'package.json'), JSON.stringify(appPkg, null, 2));
sh('npm', ['ci', '--omit=dev', '--ignore-scripts', '--no-audit', '--no-fund'], { cwd: STAGE, shell: process.platform === 'win32' });
fs.rmSync(path.join(STAGE, 'package-lock.json'));
// better-sqlite3: оставляем только Windows x64 бинарник (N-API, работает в Electron без пересборки)
const bs = path.join(STAGE, 'node_modules', 'better-sqlite3');
for (const d of ['deps', 'src', 'docs']) fs.rmSync(path.join(bs, d), { recursive: true, force: true });
for (const f of fs.readdirSync(path.join(bs, 'prebuilds'))) if (f !== 'win32-x64.node') fs.rmSync(path.join(bs, 'prebuilds', f));
if (!fs.existsSync(path.join(bs, 'prebuilds', 'win32-x64.node'))) throw new Error('Нет prebuilds/win32-x64.node у better-sqlite3');

// 2. Electron для Windows
step(`Electron ${ELECTRON} win32-x64`);
fs.mkdirSync(CACHE, { recursive: true });
const zipName = `electron-v${ELECTRON}-win32-x64.zip`;
const zip = process.env.ELECTRON_ZIP || path.join(CACHE, zipName);
if (!fs.existsSync(zip)) { console.log('скачиваю…'); await download(`https://github.com/electron/electron/releases/download/v${ELECTRON}/${zipName}`, zip); }
fs.rmSync(APPDIR, { recursive: true, force: true });
fs.mkdirSync(APPDIR, { recursive: true });
if (has('unzip')) sh('unzip', ['-q', zip, '-d', APPDIR]);
else sh('powershell', ['-NoProfile', '-Command', `Expand-Archive -Path '${zip}' -DestinationPath '${APPDIR}'`]);
fs.renameSync(path.join(APPDIR, 'electron.exe'), path.join(APPDIR, 'LisBot.exe'));
fs.rmSync(path.join(APPDIR, 'resources', 'default_app.asar'), { force: true });
fs.cpSync(STAGE, path.join(APPDIR, 'resources', 'app'), { recursive: true });
fs.rmSync(path.join(APPDIR, 'LICENSES.chromium.html'), { force: true }); // 9 МБ, лицензия Electron остаётся в LICENSE
// локализации Chromium: оставляем русскую и английскую (интерфейс приложения свой, на русском)
for (const f of fs.readdirSync(path.join(APPDIR, 'locales'))) if (!['ru.pak', 'en-US.pak'].includes(f)) fs.rmSync(path.join(APPDIR, 'locales', f));

// 3. Иконка и сведения о программе в LisBot.exe (без wine — библиотекой resedit)
step('Иконка и версия exe');
const ResEdit = await import('resedit');
const PELibrary = ResEdit; // NtExecutable реэкспортируется из resedit
const exePath = path.join(APPDIR, 'LisBot.exe');
const exe = PELibrary.NtExecutable.from(fs.readFileSync(exePath), { ignoreCert: true });
const res = PELibrary.NtExecutableResource.from(exe);
const iconFile = ResEdit.Data.IconFile.from(fs.readFileSync(path.join(ROOT, 'app', 'assets', 'icon.ico')));
for (const g of ResEdit.Resource.IconGroupEntry.fromEntries(res.entries)) {
  ResEdit.Resource.IconGroupEntry.replaceIconsForResource(res.entries, g.id, g.lang, iconFile.icons.map((i) => i.data));
}
const vi = ResEdit.Resource.VersionInfo.fromEntries(res.entries)[0];
const [ma, mi, pa] = VERSION.split('.').map(Number);
vi.setFileVersion(ma, mi, pa, 0, 1033);
vi.setProductVersion(ma, mi, pa, 0, 1033);
vi.setStringValues({ lang: 1033, codepage: 1200 }, {
  ProductName: 'LisBot', FileDescription: 'LisBot — LIS-SKINS → Steam Market', CompanyName: 'LisBot', OriginalFilename: 'LisBot.exe', InternalName: 'LisBot', LegalCopyright: '',
  ProductVersion: VERSION, FileVersion: VERSION,
});
vi.outputToResourceEntries(res.entries);
res.outputResource(exe);
fs.writeFileSync(exePath, Buffer.from(exe.generate()));

// 4. Портативный zip
step('Портативная версия');
const portableName = `LisBot-${VERSION}-win-x64-portable.zip`;
const portableDir = path.join(DIST, 'portable', 'LisBot');
fs.rmSync(path.join(DIST, 'portable'), { recursive: true, force: true });
fs.cpSync(APPDIR, portableDir, { recursive: true });
fs.writeFileSync(path.join(portableDir, 'portable.txt'), 'Портативный режим: настройки, ключи и база хранятся в папке data рядом с LisBot.exe.\r\nУдалите этот файл, чтобы хранить данные в %APPDATA%\\LisBot.\r\n');
fs.rmSync(path.join(DIST, portableName), { force: true });
if (has('zip')) sh('zip', ['-qr9', path.join(DIST, portableName), 'LisBot'], { cwd: path.join(DIST, 'portable') });
else sh('powershell', ['-NoProfile', '-Command', `Compress-Archive -Path '${portableDir}' -DestinationPath '${path.join(DIST, portableName)}'`]);
fs.rmSync(path.join(DIST, 'portable'), { recursive: true, force: true });

// 5. Установщик
if (has('makensis')) {
  step('Установщик (NSIS)');
  sh('makensis', ['-V2', `-DVERSION=${VERSION}`, `-DSRCDIR=${APPDIR}`, `-DOUTFILE=${path.join(DIST, `LisBot-Setup-${VERSION}.exe`)}`, `-DICON=${path.join(ROOT, 'app', 'assets', 'icon.ico')}`, path.join(BUILD, 'installer.nsi')]);
} else {
  console.log('\nmakensis не найден — установщик пропущен (есть портативная версия). Установите NSIS: https://nsis.sourceforge.io');
}

// 6. Части по 29 МБ для передачи через мессенджеры/чаты с лимитом размера + сборщик для Windows
const setup = path.join(DIST, `LisBot-Setup-${VERSION}.exe`);
if (fs.existsSync(setup)) {
  step('Части установщика (≤29 МБ)');
  const PARTS = path.join(DIST, 'parts');
  fs.rmSync(PARTS, { recursive: true, force: true });
  fs.mkdirSync(PARTS);
  const buf = fs.readFileSync(setup);
  const size = 29 * 1024 * 1024;
  const names = [];
  for (let i = 0, n = 1; i < buf.length; i += size, n++) {
    const name = `LisBot-Setup-${VERSION}.exe.part${n}`;
    fs.writeFileSync(path.join(PARTS, name), buf.subarray(i, i + size));
    names.push(name);
  }
  const sha = (await import('node:crypto')).createHash('sha256').update(buf).digest('hex').toUpperCase();
  const bat = [
    '@echo off', 'chcp 65001 >nul', 'cd /d "%~dp0"',
    `echo Сборка LisBot-Setup-${VERSION}.exe из ${names.length} частей...`,
    ...names.map((n) => `if not exist "${n}" (echo Нет файла ${n} — скачайте все части в одну папку & pause & exit /b 1)`),
    `copy /b ${names.map((n) => `"${n}"`).join('+')} "LisBot-Setup-${VERSION}.exe" >nul`,
    `for /f "delims=" %%h in ('certutil -hashfile "LisBot-Setup-${VERSION}.exe" SHA256 ^| findstr /v ":"') do (set "H=%%h" & goto :got)`,
    ':got', 'set "H=%H: =%"',
    `if /i "%H%"=="${sha}" (echo Контрольная сумма совпала. Запускаю установщик... & start "" "LisBot-Setup-${VERSION}.exe") else (echo ОШИБКА: контрольная сумма не совпала — перекачайте части. & pause & exit /b 1)`,
  ].join('\r\n') + '\r\n';
  fs.writeFileSync(path.join(PARTS, 'join-installer.bat'), bat);
  fs.writeFileSync(path.join(PARTS, 'SHA256.txt'), `${sha}  LisBot-Setup-${VERSION}.exe\r\n`);
  console.log(`  ${names.length} частей + join-installer.bat в dist/parts`);
}

step('Готово');
for (const f of fs.readdirSync(DIST).filter((f) => /\.(zip|exe)$/.test(f))) console.log(`  dist/${f}  ${(fs.statSync(path.join(DIST, f)).size / 1048576).toFixed(1)} МБ`);
