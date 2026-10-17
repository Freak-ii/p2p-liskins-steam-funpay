// LisBot — десктоп-приложение: окно с панелью, мастер настройки, трей, автозапуск, системные уведомления.
import { app, BrowserWindow, Tray, Menu, Notification, ipcMain, dialog, safeStorage, shell, nativeImage, powerSaveBlocker, session, clipboard } from 'electron';
import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { loadConfig, validateConfig, parseTradeUrl } from '../src/config.js';
import { Bot } from '../src/index.js';
import { LisClient } from '../src/lis/client.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ASSETS = path.join(HERE, 'assets');
app.setName('LisBot');
// портативный режим: файл portable.txt рядом с LisBot.exe -> данные в папке data рядом с программой
const PORTABLE_FLAG = path.join(path.dirname(process.execPath), 'portable.txt');
if (process.env.LISBOT_HOME) app.setPath('userData', path.resolve(process.env.LISBOT_HOME));
else if (app.isPackaged && fs.existsSync(PORTABLE_FLAG)) app.setPath('userData', path.join(path.dirname(process.execPath), 'data'));
if (process.platform === 'win32') app.setAppUserModelId('com.lisbot.app');

const USER = app.getPath('userData');
const DATA_DIR = path.join(USER, 'data');
const CONFIG_FILE = path.join(USER, 'config.json');
const SECRETS_FILE = path.join(USER, 'secrets.bin');
const SECRET_KEYS = ['LIS_API_KEY', 'STEAM_ACCOUNT_NAME', 'STEAM_PASSWORD', 'STEAM_SHARED_SECRET', 'STEAM_IDENTITY_SECRET', 'STEAM_TRADE_URL', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'DASHBOARD_TOKEN'];
const REQUIRED = ['LIS_API_KEY', 'STEAM_ACCOUNT_NAME', 'STEAM_PASSWORD', 'STEAM_SHARED_SECRET', 'STEAM_IDENTITY_SECRET', 'STEAM_TRADE_URL'];

const MAIN_LOG = path.join(USER, 'main.log');
function logMain(msg) {
  try { fs.mkdirSync(USER, { recursive: true }); fs.appendFileSync(MAIN_LOG, `${new Date().toISOString()} ${msg}\n`); } catch { /* noop */ }
}

let win = null;
let tray = null;
let bot = null;
let botState = 'stopped'; // stopped | starting | running | paused | halted
let quitting = false;
let psbId = null;
let setupErrors = [];
let hiddenHintShown = false;

// ---------- секреты: шифруются средствами ОС (Windows DPAPI / Keychain / libsecret) ----------
function loadSecrets() {
  if (!fs.existsSync(SECRETS_FILE)) return {};
  const buf = fs.readFileSync(SECRETS_FILE);
  try {
    if (buf.subarray(0, 6).toString() === 'PLAIN:') return JSON.parse(buf.subarray(6).toString());
    return JSON.parse(safeStorage.decryptString(buf));
  } catch (e) {
    dialog.showErrorBox('LisBot', `Не удалось расшифровать сохранённые ключи (${e.message}). Введите их заново.`);
    return {};
  }
}
function saveSecrets(obj) {
  const json = JSON.stringify(Object.fromEntries(SECRET_KEYS.map((k) => [k, obj[k] || ''])));
  fs.mkdirSync(USER, { recursive: true });
  if (safeStorage.isEncryptionAvailable()) fs.writeFileSync(SECRETS_FILE, safeStorage.encryptString(json), { mode: 0o600 });
  else fs.writeFileSync(SECRETS_FILE, 'PLAIN:' + json, { mode: 0o600 });
}
const isConfigured = (s) => REQUIRED.every((k) => s[k]);

function readUserConfig() { try { return JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')); } catch { return {}; } }
function writeUserConfig(patch) {
  const c = readUserConfig();
  const merge = (a, b) => { for (const [k, v] of Object.entries(b)) a[k] = v && typeof v === 'object' && !Array.isArray(v) ? merge(a[k] || {}, v) : v; return a; };
  fs.mkdirSync(USER, { recursive: true });
  fs.writeFileSync(CONFIG_FILE, JSON.stringify(merge(c, patch), null, 2));
}

function freePort(preferred) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once('error', () => { const s2 = net.createServer(); s2.listen(0, '127.0.0.1', () => { const p = s2.address().port; s2.close(() => resolve(p)); }); });
    srv.listen(preferred, '127.0.0.1', () => srv.close(() => resolve(preferred)));
  });
}
async function waitPort(port, ms = 15000) {
  const t = Date.now();
  while (Date.now() - t < ms) {
    const ok = await new Promise((r) => { const s = net.connect(port, '127.0.0.1', () => { s.end(); r(true); }); s.on('error', () => r(false)); });
    if (ok) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

// ---------- окно ----------
function createWindow() {
  win = new BrowserWindow({
    width: 1320, height: 880, minWidth: 380, minHeight: 500, show: false, title: 'LisBot', backgroundColor: '#0f1115',
    icon: path.join(ASSETS, process.platform === 'win32' ? 'icon.ico' : 'icon.png'), autoHideMenuBar: true,
    webPreferences: { preload: path.join(HERE, 'preload.cjs'), contextIsolation: true, sandbox: true, nodeIntegration: false, spellcheck: false },
  });
  win.once('ready-to-show', () => { if (!process.argv.includes('--hidden')) win.show(); });
  win.on('close', (e) => {
    if (quitting) return;
    e.preventDefault();
    win.hide();
    if (!hiddenHintShown && bot) { hiddenHintShown = true; notifyOs('info', 'LisBot работает в фоне', 'Окно свёрнуто в трей. Бот продолжает работу. Выход — через меню трея.'); }
  });
  // внешние ссылки — в браузер, навигация только внутри приложения
  win.webContents.setWindowOpenHandler(({ url }) => { if (/^https:\/\//.test(url)) shell.openExternal(url); return { action: 'deny' }; });
  win.webContents.on('will-navigate', (e, url) => {
    const allowed = url.startsWith('file://') || (bot && url.startsWith(`http://127.0.0.1:${bot.cfg.dashboard.port}`));
    if (!allowed) { e.preventDefault(); if (/^https:\/\//.test(url)) shell.openExternal(url); }
  });
  buildAppMenu();
}

function showWindow() { if (!win) createWindow(); win.show(); win.focus(); }
function showSetup(errors = []) { setupErrors = errors; showWindow(); win.loadFile(path.join(HERE, 'setup.html')); }

async function showDashboard() {
  if (!bot) return showSetup();
  const { port } = bot.cfg.dashboard;
  await waitPort(port);
  await session.defaultSession.cookies.set({ url: `http://127.0.0.1:${port}`, name: 'lsb_token', value: encodeURIComponent(bot.cfg.secrets.dashboardToken), httpOnly: true, sameSite: 'strict' });
  if (!win) createWindow();
  await win.loadURL(`http://127.0.0.1:${port}/`);
}

// ---------- бот ----------
const CONNECTION_HALTS = ['STEAM_START', 'STEAM_LOGIN', 'STEAM_GUARD', 'LIS_AUTH', 'START'];

async function startBot(reason = '') {
  const secrets = loadSecrets();
  if (!isConfigured(secrets)) { logMain('ключи не заданы — открываю мастер настройки'); return showSetup(); }
  if (!secrets.DASHBOARD_TOKEN) { secrets.DASHBOARD_TOKEN = crypto.randomBytes(24).toString('hex'); saveSecrets(secrets); }
  const cfg = loadConfig({ configPath: CONFIG_FILE, env: secrets, dataDir: DATA_DIR });
  cfg.telegram.enabled = !!(secrets.TELEGRAM_BOT_TOKEN && secrets.TELEGRAM_CHAT_ID);
  cfg.dashboard.enabled = true;
  cfg.dashboard.host = '127.0.0.1';
  cfg.dashboard.port = await freePort(cfg.dashboard.port || 8787);
  logMain(`конфиг загружен (dryRun=${cfg.dryRun}), порт панели ${cfg.dashboard.port}`);
  const errs = validateConfig(cfg);
  if (errs.length) { logMain(`конфигурация: ${errs.join('; ')}`); return showSetup(errs); }

  setState('starting');
  bot = new Bot(cfg, { appMode: true, version: app.getVersion(), onRestartRequest: (why) => restartBot(why) });
  bot.notify.subscribe((level, text) => onBotNotify(level, text));
  bot.guard.on('change', () => setState());
  // остановка из-за неверных ключей снимается, когда пользователь исправил подключения
  if (reason === 'изменены подключения' && CONNECTION_HALTS.includes(bot.guard.state.haltCode)) bot.guard.resume('исправлены подключения');
  const started = bot.start().catch((e) => { bot?.guard.critical('START', `Сбой запуска: ${e.message}`); });
  await showDashboard();
  await started;
  setState();
  if (psbId === null) psbId = powerSaveBlocker.start('prevent-app-suspension');
}

async function stopBot(reason) {
  if (!bot) return;
  const b = bot;
  bot = null;
  setState('stopped');
  try { await b.stop(reason); } catch { /* уже остановлен */ }
  if (psbId !== null) { powerSaveBlocker.stop(psbId); psbId = null; }
}

async function restartBot(reason = 'перезапуск') {
  await stopBot(reason);
  await startBot(reason);
}

function setState(forced) {
  if (forced) botState = forced;
  else if (!bot) botState = 'stopped';
  else if (bot.guard.state.halted) botState = 'halted';
  else if (bot.guard.state.paused) botState = 'paused';
  else botState = 'running';
  updateTray();
}

// ---------- уведомления ОС ----------
const TITLES = { critical: '🚨 LisBot: КРИТИЧНО — бот остановлен', warn: '⚠️ LisBot: внимание', money: '💰 LisBot', report: '📊 LisBot: отчёт', ok: '✅ LisBot', info: 'LisBot' };
function notifyOs(level, title, body) {
  if (!Notification.isSupported()) return;
  const n = new Notification({ title: title || TITLES[level], body: String(body).slice(0, 300), icon: path.join(ASSETS, 'icon.png'), urgency: level === 'critical' ? 'critical' : 'normal', silent: level === 'info' });
  n.on('click', () => showWindow());
  n.show();
}
function onBotNotify(level, text) {
  if (['critical', 'warn', 'money', 'report'].includes(level) || (level === 'ok' && /возобновл|восстановлен/i.test(text))) notifyOs(level, null, text);
  if (level === 'critical') { if (win && !win.isVisible()) win.showInactive(); win?.flashFrame(true); }
  if (level === 'info' && /Куплено|🛒/.test(text) && Notification.isSupported()) notifyOs('info', '🛒 LisBot: покупка', text);
}

// ---------- трей и меню ----------
const STATE_RU = { stopped: 'Не запущен', starting: 'Запуск…', running: 'Работает', paused: 'Пауза', halted: 'ОСТАНОВЛЕН (авария)' };
const TRAY_ICON = { stopped: 'tray-grey', starting: 'tray-yellow', running: 'tray-green', paused: 'tray-yellow', halted: 'tray-red' };

function updateTray() {
  if (!tray) return;
  tray.setImage(nativeImage.createFromPath(path.join(ASSETS, `${TRAY_ICON[botState]}.png`)));
  const bal = bot?.balanceRef?.value;
  const mode = bot?.cfg?.dryRun ? ' · DRY-RUN' : '';
  tray.setToolTip(`LisBot — ${STATE_RU[botState]}${mode}${bal != null ? ` · баланс $${bal.toFixed(2)}` : ''}`);
  const login = app.getLoginItemSettings();
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Открыть LisBot', click: () => (bot ? (showWindow(), showDashboard()) : showSetup()) },
    { label: `Статус: ${STATE_RU[botState]}${mode}`, enabled: false },
    ...(bot?.guard.state.halted ? [{ label: `Причина: ${String(bot.guard.state.haltReason || '').slice(0, 70)}`, enabled: false }] : []),
    { type: 'separator' },
    { label: 'Пауза', enabled: botState === 'running', click: () => bot?.guard.pause('трей') },
    { label: 'Продолжить / снять остановку', enabled: ['paused', 'halted'].includes(botState), click: () => confirmResume() },
    { label: 'Аварийный стоп', enabled: ['running', 'paused'].includes(botState), click: () => bot?.guard.critical('MANUAL_STOP', 'Аварийная остановка из трея') },
    { label: 'Перезапустить бота', enabled: !!bot, click: () => restartBot('перезапуск из трея') },
    { type: 'separator' },
    { label: 'Подключения и ключи…', click: () => showSetup() },
    { label: 'Открыть папку данных и логов', click: () => shell.openPath(USER) },
    { label: 'Открыть панель в браузере', enabled: !!bot, click: () => shell.openExternal(`http://127.0.0.1:${bot.cfg.dashboard.port}/`) },
    { label: 'Скопировать токен панели', enabled: !!bot, click: () => clipboard.writeText(bot.cfg.secrets.dashboardToken) },
    { label: 'Запускать вместе с системой', type: 'checkbox', checked: login.openAtLogin, click: (i) => app.setLoginItemSettings({ openAtLogin: i.checked, args: ['--hidden'] }) },
    { type: 'separator' },
    { label: 'Выход', click: () => quit() },
  ]));
}

async function confirmResume() {
  if (!bot) return;
  if (bot.guard.state.halted) {
    const r = await dialog.showMessageBox(win, { type: 'warning', buttons: ['Снять остановку', 'Отмена'], defaultId: 1, cancelId: 1, title: 'LisBot', message: 'Бот был аварийно остановлен', detail: `Причина: ${bot.guard.state.haltReason}\n\nСнимайте остановку только после того, как разобрались с причиной.` });
    if (r.response !== 0) return;
  }
  bot.guard.resume('приложение');
}

function buildAppMenu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: 'Бот', submenu: [
      { label: 'Панель', accelerator: 'CmdOrCtrl+1', click: () => showDashboard() },
      { label: 'Подключения и ключи…', accelerator: 'CmdOrCtrl+,', click: () => showSetup() },
      { label: 'Папка данных и логов', click: () => shell.openPath(USER) },
      { type: 'separator' },
      { label: 'Перезапустить бота', click: () => restartBot('перезапуск из меню') },
      { label: 'Выход', accelerator: 'CmdOrCtrl+Q', click: () => quit() },
    ] },
    { label: 'Вид', submenu: [{ role: 'reload', label: 'Обновить' }, { role: 'resetZoom', label: 'Масштаб 100%' }, { role: 'zoomIn', label: 'Крупнее' }, { role: 'zoomOut', label: 'Мельче' }, { role: 'togglefullscreen', label: 'Во весь экран' }, { type: 'separator' }, { role: 'toggleDevTools', label: 'Инструменты разработчика' }] },
  ]));
}

async function quit() {
  if (bot && !bot.cfg.dryRun) {
    const r = await dialog.showMessageBox(win?.isVisible() ? win : null, { type: 'question', buttons: ['Выйти', 'Свернуть в трей', 'Отмена'], defaultId: 1, cancelId: 2, title: 'LisBot', message: 'Закрыть LisBot?', detail: 'Бот перестанет покупать, принимать трейды и продавать, пока приложение не будет запущено снова.' });
    if (r.response === 1) { win?.hide(); return; }
    if (r.response === 2) return;
  }
  quitting = true;
  await stopBot('выход из приложения');
  app.quit();
}

// ---------- IPC мастера настройки (только со страницы setup.html) ----------
const fromSetup = (e) => e.senderFrame?.url?.startsWith('file://') && e.senderFrame.url.endsWith('setup.html');
function handle(ch, fn) { ipcMain.handle(ch, async (e, ...a) => { if (!fromSetup(e)) throw new Error('forbidden'); return fn(...a); }); }

handle('setup:get', () => {
  const s = loadSecrets();
  const c = readUserConfig();
  const masked = Object.fromEntries(SECRET_KEYS.filter((k) => k !== 'DASHBOARD_TOKEN').map((k) => [k, k === 'STEAM_PASSWORD' ? '' : s[k] || '']));
  return {
    secrets: masked, hasPassword: !!s.STEAM_PASSWORD, configured: isConfigured(s), errors: setupErrors,
    dryRun: c.dryRun ?? true, walletCurrency: c.steam?.walletCurrency ?? 5, walletCurrencyCode: c.steam?.walletCurrencyCode ?? 'RUB',
    autostart: app.getLoginItemSettings().openAtLogin, encrypted: safeStorage.isEncryptionAvailable(), botRunning: !!bot, version: app.getVersion(),
  };
});

handle('setup:save', async (data) => {
  const old = loadSecrets();
  const s = { ...old };
  for (const k of SECRET_KEYS) if (k !== 'DASHBOARD_TOKEN' && typeof data.secrets?.[k] === 'string') s[k] = data.secrets[k].trim();
  if (!data.secrets?.STEAM_PASSWORD) s.STEAM_PASSWORD = old.STEAM_PASSWORD || '';
  const errs = [];
  for (const k of REQUIRED) if (!s[k]) errs.push(`Не заполнено: ${k}`);
  if (s.STEAM_TRADE_URL && !parseTradeUrl(s.STEAM_TRADE_URL)) errs.push('Трейд-ссылка некорректна (нужны partner и token)');
  for (const k of ['STEAM_SHARED_SECRET', 'STEAM_IDENTITY_SECRET']) if (s[k] && !/^[A-Za-z0-9+/]{27}=$/.test(s[k])) errs.push(`${k} не похож на секрет из maFile (28 символов base64)`);
  if (!!s.TELEGRAM_BOT_TOKEN !== !!s.TELEGRAM_CHAT_ID) errs.push('Для Telegram нужны и токен, и chat id (или оставьте оба пустыми)');
  if (errs.length) return { ok: false, errors: errs };
  saveSecrets(s);
  writeUserConfig({ dryRun: !!data.dryRun, steam: { walletCurrency: Number(data.walletCurrency) || 5, walletCurrencyCode: data.walletCurrencyCode || 'RUB' } });
  app.setLoginItemSettings({ openAtLogin: !!data.autostart, args: ['--hidden'] });
  setupErrors = [];
  setTimeout(() => restartBot('изменены подключения'), 100);
  return { ok: true };
});

handle('setup:cancel', () => (bot ? showDashboard() : null));

handle('setup:testLis', async (key) => {
  const k = key || loadSecrets().LIS_API_KEY;
  if (!k) return { ok: false, error: 'Введите ключ' };
  const base = readUserConfig().lis?.baseUrl || JSON.parse(fs.readFileSync(path.join(HERE, '..', 'config.example.json'), 'utf8')).lis.baseUrl;
  const c = new LisClient({ apiKey: k, baseUrl: base, log: { warn() {}, info() {}, error() {} } });
  try { const b = await c.balance(); return { ok: true, balance: b }; } catch (e) { return { ok: false, error: e.message }; } finally { c.sched.stop(); }
});

async function tg(token, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${token}/${method}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}), signal: AbortSignal.timeout(15000) });
  const j = await r.json().catch(() => ({}));
  if (!j.ok) throw new Error(j.description || `HTTP ${r.status}`);
  return j.result;
}
handle('setup:tgFindChat', async (token) => {
  try {
    const ups = await tg(token, 'getUpdates', { timeout: 0 });
    const m = [...ups].reverse().find((u) => u.message?.chat?.id);
    return m ? { ok: true, chatId: String(m.message.chat.id), name: m.message.chat.first_name || m.message.chat.title || '' } : { ok: false, error: 'Напишите своему боту в Telegram любое сообщение (например /start) и нажмите ещё раз' };
  } catch (e) { return { ok: false, error: e.message }; }
});
handle('setup:tgTest', async (token, chatId) => {
  try { await tg(token, 'sendMessage', { chat_id: chatId, text: '✅ LisBot: уведомления работают' }); return { ok: true }; } catch (e) { return { ok: false, error: e.message }; }
});

handle('setup:importMaFile', async () => {
  const r = await dialog.showOpenDialog(win, { title: 'Выберите maFile (Steam Desktop Authenticator)', properties: ['openFile'], filters: [{ name: 'maFile', extensions: ['maFile', 'json'] }, { name: 'Все файлы', extensions: ['*'] }] });
  if (r.canceled || !r.filePaths[0]) return { ok: false };
  try {
    const j = JSON.parse(fs.readFileSync(r.filePaths[0], 'utf8'));
    if (!j.shared_secret || !j.identity_secret) return { ok: false, error: 'В файле нет shared_secret/identity_secret' };
    return { ok: true, sharedSecret: j.shared_secret, identitySecret: j.identity_secret, accountName: j.account_name || '' };
  } catch {
    return { ok: false, error: 'Файл зашифрован или повреждён. В Steam Desktop Authenticator уберите шифрование (Manage Encryption → пустой пароль) и попробуйте снова.' };
  }
});

handle('app:openExternal', (url) => { if (/^https:\/\/[a-z0-9.-]+\//i.test(url)) shell.openExternal(url); });

// ---------- жизненный цикл ----------
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => showWindow());
  app.whenReady().then(async () => {
    logMain(`запуск v${app.getVersion()}, userData=${USER}`);
    tray = new Tray(nativeImage.createFromPath(path.join(ASSETS, 'tray-grey.png')));
    tray.on('click', () => showWindow());
    tray.on('double-click', () => showWindow());
    updateTray();
    createWindow();
    await startBot();
    logMain(bot ? `бот запущен, панель на порту ${bot.cfg.dashboard.port}` : 'бот не запущен (нужна настройка)');
  }).catch((e) => {
    logMain(`ОШИБКА запуска: ${e.stack || e}`);
    dialog.showErrorBox('LisBot — ошибка запуска', `${e.message}\n\nПодробности: ${MAIN_LOG}`);
  });
  app.on('window-all-closed', (e) => e.preventDefault?.());
  app.on('before-quit', (e) => { if (!quitting) { e.preventDefault(); quit(); } });
  process.on('uncaughtException', (e) => { logMain(`uncaughtException: ${e.stack || e}`); try { bot?.log.crit(`uncaughtException: ${e.message}`); bot?.guard.critical('CRASH', `Необработанная ошибка: ${e.message}`); } catch { /* noop */ } });
  process.on('unhandledRejection', (e) => { try { bot?.log.error(`unhandledRejection: ${e?.message || e}`); } catch { /* noop */ } });
}
