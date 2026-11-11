// Real-browser verification of the injected extension against the *actual*
// generated MAA WebUI page (produced by packaging/webui_overlay.py).
//
// It serves the page plus a stub of the fixed manager API over real HTTP, so
// fetch headers, methods and bodies are observed server-side exactly as the
// phone will see them. No phone, no deployment, no network access beyond
// 127.0.0.1. Skips cleanly when no cached Playwright is available.
//
// Run: node webui/tests/module-settings.browser.mjs

import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { pathToFileURL, fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEBUI_DIR = path.resolve(HERE, '..');
const PACKAGING_DIR = path.resolve(WEBUI_DIR, '..', 'packaging');
const OVERLAY = path.join(PACKAGING_DIR, 'webui_overlay.py');

function repoRoot() {
  let dir = WEBUI_DIR;
  for (let i = 0; i < 6; i += 1) {
    if (fs.existsSync(path.join(dir, 'packaging', 'project', 'source', 'maa-webui.py'))) return dir;
    dir = path.dirname(dir);
  }
  return path.dirname(WEBUI_DIR);
}

async function loadPlaywright() {
  const candidates = [process.env.PLAYWRIGHT_MODULE, 'playwright'].filter(Boolean);
  const npxRoot = path.join(
    process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'),
    'npm-cache',
    '_npx',
  );
  if (fs.existsSync(npxRoot)) {
    for (const entry of fs.readdirSync(npxRoot)) {
      // ESM cannot import a package directory, so point at its entry file.
      const candidate = path.join(npxRoot, entry, 'node_modules', 'playwright', 'index.mjs');
      if (fs.existsSync(candidate)) candidates.push(candidate);
    }
  }
  for (const candidate of candidates) {
    try {
      const specifier = candidate === 'playwright' ? candidate : pathToFileURL(candidate).href;
      const module = await import(specifier);
      return module.chromium ? module : module.default;
    } catch (_) { /* try the next candidate */ }
  }
  return null;
}

const state = {
  status: {
    manager_version: '0.4.0-test',
    versions: {
      manager: { current: '0.4.0-test', previous: '0.3.9-test' },
      project: { current: '2026.09.12', previous: '2026.09.01' },
    },
    pending: null,
    configured: true,
    can_install: true,
    reason: '',
  },
  applyResponse: { ok: true, message: '已提交下载任务' },
  requests: [],
};

const HOST_APIS = {
  '/api/config': { tasks: [], version: 'test-host', adb: false, common_stages: [] },
  '/api/status': { active: false, task: '', start_time: '', output: '', startup_health: { status: 'ok' }, skip_next_daily: null },
  '/api/weekly-plan': { enabled: false, initialized: false, days: [], current_weekday: 'mon', selection: {} },
  '/api/infrast/plans': { plans: [] },
  '/api/logs': { name: '', log: '', core_log: '' },
  '/api/crontab': { content: '' },
  '/api/files': { profile: '', cli: '' },
};

function startServer(htmlPath) {
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => {
      const body = Buffer.concat(chunks).toString('utf8');
      const url = new URL(request.url, 'http://127.0.0.1');
      const pathname = url.pathname;
      state.requests.push({
        path: pathname,
        method: request.method,
        headers: request.headers,
        body,
      });

      const send = (code, payload, type) => {
        const text = typeof payload === 'string' ? payload : JSON.stringify(payload);
        response.writeHead(code, { 'Content-Type': type || 'application/json; charset=utf-8' });
        response.end(text);
      };

      if (pathname === '/' || pathname === '/index.html') {
        send(200, fs.readFileSync(htmlPath, 'utf8'), 'text/html; charset=utf-8');
        return;
      }
      // Production serves these from /module-assets/; /static/ is kept as an alias
      // so the check also covers an overlay generated with custom paths.
      if (pathname === '/module-assets/module-settings.js' || pathname === '/static/module-settings.js') {
        send(200, fs.readFileSync(path.join(WEBUI_DIR, 'module-settings.js'), 'utf8'), 'text/javascript; charset=utf-8');
        return;
      }
      if (pathname === '/module-assets/module-settings.css' || pathname === '/static/module-settings.css') {
        send(200, fs.readFileSync(path.join(WEBUI_DIR, 'module-settings.css'), 'utf8'), 'text/css; charset=utf-8');
        return;
      }
      if (pathname === '/api/v1/status') { send(200, state.status); return; }
      if (pathname === '/api/v1/help') {
        send(200, '# 关于与模块设置\n\n- 本页只显示后台真实状态。\n- 安装/更新/回退由 maa-managerd 执行。', 'text/markdown; charset=utf-8');
        return;
      }
      if (pathname === '/api/v1/update/check') { send(200, { ok: true, message: '已是最新版本' }); return; }
      if (pathname === '/api/v1/update/apply') { send(200, state.applyResponse); return; }
      if (pathname === '/api/v1/rollback') { send(200, { ok: true, message: '已提交回退任务' }); return; }
      if (HOST_APIS[pathname]) { send(200, HOST_APIS[pathname]); return; }
      send(404, { error: 'not found' });
    });
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

const playwright = await loadPlaywright();
if (!playwright) {
  console.log('SKIP - no Playwright module found; set PLAYWRIGHT_MODULE to run the browser check.');
  process.exit(0);
}

const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'maa-module-settings-'));
const pagePath = path.join(workDir, 'webui.html');

function findChromiumExecutable() {
  if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE) return process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH
    || path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'ms-playwright');
  if (!fs.existsSync(root)) return '';
  const candidates = [
    path.join('chrome-win64', 'chrome.exe'),
    path.join('chrome-win', 'chrome.exe'),
    path.join('chrome-headless-shell-win64', 'chrome-headless-shell.exe'),
  ];
  const dirs = fs.readdirSync(root)
    .filter((entry) => entry.startsWith('chromium'))
    .sort((left, right) => (parseInt(right.replace(/\D/g, ''), 10) || 0) - (parseInt(left.replace(/\D/g, ''), 10) || 0));
  for (const dir of dirs) {
    for (const relative of candidates) {
      const full = path.join(root, dir, relative);
      if (fs.existsSync(full)) return full;
    }
  }
  return '';
}

function argValue(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : '';
}

// The exact fragment the Go reverse proxy inserts before </body>.
function productionFragment() {
  const serverGo = path.join(WEBUI_DIR, '..', 'manager', 'internal', 'api', 'server.go');
  try {
    const source = fs.readFileSync(serverGo, 'utf8');
    const match = source.match(/extra\s*:=\s*`([^`]*<\/body>)`/);
    if (match) return { fragment: match[1], source: serverGo };
  } catch (_) { /* fall through to the recorded shape */ }
  return { fragment: '', source: serverGo };
}

const RECORDED_FRAGMENT = '<link rel="stylesheet" href="/module-assets/module-settings.css"><script src="/module-assets/module-settings.js"></script></body>';
const artifactsDir = path.join(repoRoot(), '.superpowers', 'phone-distribution', 'artifacts');
const rawFixture = argValue('--raw') || process.env.MAA_WEBUI_RAW || path.join(artifactsDir, 'webui-raw.html');
const overlayFixture = argValue('--overlay') || process.env.MAA_WEBUI_FIXTURE || path.join(artifactsDir, 'webui-module-settings.html');

function generate(command, target) {
  const emit = spawnSync('python', [OVERLAY, command, target], { encoding: 'utf8' });
  return emit.status === 0 && fs.existsSync(target) ? target : '';
}

const PROXY_MARKER = 'data-maa-overlay="module-settings"';

// Mirrors the proxy rule: skip a page that already carries the marker, otherwise
// replace the last closing body tag with the literal (the literal carries its
// own), or append it when the page has no closing body tag at all.
function guardedInject(html, fragment) {
  if (html.includes(PROXY_MARKER)) return html;
  const close = '</body>';
  const index = html.lastIndexOf(close);
  if (index < 0) return html + fragment;
  return html.slice(0, index) + fragment + html.slice(index + close.length);
}

function buildProductionPage() {
  let raw = fs.existsSync(rawFixture) ? fs.readFileSync(rawFixture, 'utf8') : '';
  if (!raw) {
    const generated = generate('--emit-raw', path.join(workDir, 'webui-raw.html'));
    if (generated) raw = fs.readFileSync(generated, 'utf8');
  }
  if (!raw || !raw.includes('</body>')) return null;
  const { fragment } = productionFragment();
  const used = fragment || RECORDED_FRAGMENT;
  return { html: guardedInject(raw, used), raw, fragment: used, fragmentFromSource: Boolean(fragment) };
}

function buildOverlayPage() {
  const fixture = fs.existsSync(overlayFixture)
    ? overlayFixture
    : generate('--emit', path.join(workDir, 'webui-overlay.html'));
  if (!fixture) return null;
  return { html: fs.readFileSync(fixture, 'utf8'), raw: '', fragment: '', fragmentFromSource: false };
}

const shape = argValue('--shape') || (fs.existsSync(rawFixture) ? 'production' : 'overlay');
const built = shape === 'overlay' ? buildOverlayPage() : buildProductionPage();
if (!built) {
  console.log('SKIP - cannot build the real WebUI fixture for shape ' + shape + '.');
  console.log('       generate it with: python "' + OVERLAY + '" --emit-raw "' + rawFixture + '"');
  console.log('       or pass --raw <path> / --overlay <path> / --shape overlay.');
  process.exit(0);
}
fs.writeFileSync(pagePath, built.html, 'utf8');
const pagePathActual = pagePath;

const checks = [];
function check(name, condition, detail) {
  checks.push({ name, ok: Boolean(condition), detail: detail || '' });
  console.log(`${condition ? 'ok  ' : 'FAIL'} - ${name}${condition ? '' : ' :: ' + detail}`);
}

if (shape === 'production') {
  check('fixture is the untouched generated page', built.raw.includes('id="n_home"') && built.raw.includes('async function go(page)') && built.raw.includes('function render()'));
  check('production fragment came from the Go proxy source', built.fragmentFromSource, built.fragment);
  check('production fragment references both module-assets files', built.fragment.includes('/module-assets/module-settings.css') && built.fragment.includes('/module-assets/module-settings.js'), built.fragment);
  check('production fragment carries the idempotency marker on both tags', (built.fragment.match(/data-maa-overlay="module-settings"/g) || []).length === 2, built.fragment);
  check('production fragment defers the extension script, as the proxy sends it', /<script[^>]*module-settings\.js[^>]*defer/.test(built.fragment), built.fragment);
  check('each asset is referenced exactly once', (built.html.match(/module-settings\.js/g) || []).length === 1 && (built.html.match(/module-settings\.css/g) || []).length === 1);
  check('the marker guard makes a second proxy pass a no-op', guardedInject(built.html, built.fragment) === built.html, 'second pass changed the served page');
  check('the served page keeps exactly one closing body tag', (built.html.match(/<\/body>/g) || []).length === (built.raw.match(/<\/body>/g) || []).length, 'closing body tags changed');
  check('a page without a closing body tag still gains the assets', guardedInject('<html><body>x', built.fragment).endsWith(built.fragment), 'mirror of the proxy append branch');
  if (fs.existsSync(overlayFixture)) {
    const overlayHtml = fs.readFileSync(overlayFixture, 'utf8');
    check('the proxy rule never double-injects a reference-overlay page', guardedInject(overlayHtml, built.fragment) === overlayHtml, 'proxy would duplicate the overlay');
    check('proxy output equals the reference overlay byte for byte', overlayHtml === built.html, 'reference overlay and proxy output diverged');
  }
}

const server = await startServer(pagePathActual);
const origin = `http://127.0.0.1:${server.address().port}`;
const executablePath = findChromiumExecutable();
const browser = await playwright.chromium.launch(executablePath ? { executablePath } : {});
const context = await browser.newContext();
const page = await context.newPage();
const failures = [];
page.on('pageerror', (error) => failures.push(String(error && error.message ? error.message : error)));

try {
  await page.goto(origin + '/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#n_module-settings', { timeout: 15000 });

  const navLabels = await page.$$eval('.nav a', (links) => links.map((link) => link.textContent.trim()));
  check('exactly one nav entry is added', navLabels.length === 9, JSON.stringify(navLabels));
  check('nav entry keeps its Chinese label', navLabels[navLabels.length - 1] === '关于与模块设置', navLabels.join('|'));
  check('existing nav entries are untouched', navLabels.slice(0, 8).join(',') === '首页,周计划,基建排班,培养目标,任务配置,日志,定时,设置', navLabels.join('|'));
  check('the settings section loads as a real stylesheet and script', true);

  await page.click('#n_module-settings');
  await page.waitForSelector('[data-ms="help"]');
  const title = await page.textContent('#pageTitle');
  check('shell title follows the new page', title === '关于与模块设置', title);
  const body = await page.textContent('#main');
  check('manager version is shown', body.includes('0.4.0-test'), body.slice(0, 200));
  check('component versions are shown', body.includes('2026.09.12') && body.includes('2026.09.01'));
  check('idle state is honest', (await page.textContent('[data-ms="pending"]')).trim() === '空闲');
  check('help markdown is rendered as visible text', body.includes('本页只显示后台真实状态'));

  const disabled = await page.$$eval('button[data-ms-action]', (buttons) => buttons.map((button) => ({ action: button.dataset.msAction, disabled: button.disabled })));
  check('operations are enabled only when allowed', disabled.every((item) => item.disabled === false), JSON.stringify(disabled));

  const isCsrfSafe = state.requests
    .filter((item) => item.path.startsWith('/api/v1/'))
    .every((item) => item.headers['x-maa-request'] === '1');
  check('every manager request carries the CSRF header', isCsrfSafe);

  const statusOnOpen = state.requests.filter((item) => item.path === '/api/v1/status').length;
  await page.waitForTimeout(2600);
  const statusPolled = state.requests.filter((item) => item.path === '/api/v1/status').length;
  check('status is polled while the settings page is open', statusPolled > statusOnOpen, `${statusOnOpen} -> ${statusPolled}`);

  await page.click('button[data-ms-action="check"]');
  await page.waitForFunction(() => document.getElementById('main').textContent.includes('已是最新版本'));
  const checkRequest = state.requests.find((item) => item.path === '/api/v1/update/check');
  check('check update posts an empty JSON object', checkRequest && checkRequest.method === 'POST' && checkRequest.body === '{}', JSON.stringify(checkRequest && checkRequest.body));

  page.once('dialog', (dialog) => dialog.accept());
  await page.click('button[data-ms-action="apply"]');
  await page.waitForFunction(() => document.getElementById('main').textContent.includes('已提交下载任务'));
  const applyRequest = state.requests.find((item) => item.path === '/api/v1/update/apply');
  check('apply posts {confirm:true} as JSON', applyRequest && applyRequest.body === '{"confirm":true}', JSON.stringify(applyRequest && applyRequest.body));
  check('apply sends the JSON content type', applyRequest && String(applyRequest.headers['content-type']).includes('application/json'));

  state.status = Object.assign({}, state.status, { pending: { stage: 'downloading' }, can_install: false, reason: 'MAA 任务运行中' });
  await page.waitForFunction(() => document.getElementById('main').textContent.includes('downloading'), null, { timeout: 12000 });
  const pendingText = (await page.textContent('[data-ms="pending"]')).trim();
  check('pending progress is polled and shown', pendingText.includes('downloading'), pendingText);
  const disabledWhileBusy = await page.$$eval('button[data-ms-action]', (buttons) => buttons.map((button) => button.disabled));
  check('every operation is disabled while the manager cannot install', disabledWhileBusy.every(Boolean), JSON.stringify(disabledWhileBusy));
  check('the blocking reason is shown', (await page.textContent('#main')).includes('MAA 任务运行中'));

  state.applyResponse = { ok: false, message: '清单签名无效，已取消' };
  state.status = Object.assign({}, state.status, { pending: null, can_install: true, reason: '' });
  await page.waitForFunction(() => !document.querySelector('button[data-ms-action="apply"]').disabled, null, { timeout: 12000 });
  page.once('dialog', (dialog) => dialog.accept());
  await page.click('button[data-ms-action="apply"]');
  await page.waitForFunction(() => document.getElementById('main').textContent.includes('清单签名无效，已取消'));
  const messageClass = await page.getAttribute('[data-ms="message"]', 'class');
  check('failure message is styled as an error', String(messageClass).includes('ms-err'), messageClass);

  await page.click('#n_home');
  await page.waitForFunction(() => !document.querySelector('[data-ms="help"]'));
  const statusAfterLeave = state.requests.filter((item) => item.path === '/api/v1/status').length;
  await page.waitForTimeout(2600);
  const statusLater = state.requests.filter((item) => item.path === '/api/v1/status').length;
  check('polling stops when the settings page closes', statusLater === statusAfterLeave, `before=${statusAfterLeave} after=${statusLater}`);

  const storageKeys = await page.evaluate(() => Object.keys(window.localStorage));
  check('no credentials are kept in localStorage', storageKeys.every((key) => !/token|auth|secret/i.test(key)), JSON.stringify(storageKeys));

  const moduleErrors = failures.filter((message) => !message.includes('Failed to fetch'));
  check('the extension throws no page errors', moduleErrors.length === 0, moduleErrors.join(' | '));

  const shotDir = path.join(repoRoot(), 'test');
  fs.mkdirSync(shotDir, { recursive: true });

  // ── clean-state evidence ──
  // Everything above pushed the page through unusual states (disabled controls,
  // pending updates, reload-free navigation). Evidence screenshots are taken
  // from a fresh load at scroll top instead, and the geometry is asserted so a
  // sticky-sidebar artefact cannot be mistaken for a real layout bug.
  async function captureClean(width, height, file) {
    await page.setViewportSize({ width, height });
    await page.goto(origin + '/', { waitUntil: 'load' });
    await page.waitForSelector('#n_module-settings');
    await page.click('#n_module-settings');
    await page.waitForSelector('[data-ms="help"]');
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: file });
    return page.evaluate(() => {
      const nav = document.querySelector('.nav');
      const card = document.querySelector('.ms-card');
      const navRect = nav.getBoundingClientRect();
      const cardRect = card.getBoundingClientRect();
      const navSpansWidth = navRect.width > window.innerWidth * 0.8;
      return {
        scrollY: window.scrollY,
        selected: document.getElementById('n_module-settings').classList.contains('on'),
        homeSelected: document.getElementById('n_home').classList.contains('on'),
        cardTop: Math.round(cardRect.top),
        navBottom: navSpansWidth ? Math.round(navRect.bottom) : 0,
        viewportHeight: window.innerHeight,
        overflow: document.documentElement.scrollWidth - window.innerWidth,
        cardBackground: getComputedStyle(card).backgroundColor,
        checkButtonWidth: Math.round(document.querySelector('button[data-ms-action="check"]').getBoundingClientRect().width),
      };
    });
  }

  const desktopShot = path.join(shotDir, '20260912-module-settings-browser.png');
  const desktopState = await captureClean(1280, 900, desktopShot);
  console.log('screenshot: ' + desktopShot);
  check('module settings is the selected nav entry (desktop)', desktopState.selected && !desktopState.homeSelected, JSON.stringify(desktopState));
  check('desktop screenshot is at scroll top', desktopState.scrollY === 0, String(desktopState.scrollY));
  check('first settings card starts inside the viewport (desktop)', desktopState.cardTop >= 0 && desktopState.cardTop < desktopState.viewportHeight, JSON.stringify(desktopState));
  check('first settings card is not covered by the nav (desktop)', desktopState.cardTop >= desktopState.navBottom, JSON.stringify(desktopState));
  check('the extension stylesheet is applied', desktopState.cardBackground !== 'rgba(0, 0, 0, 0)' && desktopState.cardBackground !== 'transparent', desktopState.cardBackground);
  check('no horizontal overflow at desktop width', desktopState.overflow <= 1, String(desktopState.overflow));

  const mobileShot = path.join(shotDir, '20260912-module-settings-browser-mobile.png');
  const mobileState = await captureClean(390, 844, mobileShot);
  console.log('screenshot: ' + mobileShot);
  check('module settings is the selected nav entry (phone)', mobileState.selected && !mobileState.homeSelected, JSON.stringify(mobileState));
  check('phone screenshot is at scroll top', mobileState.scrollY === 0, String(mobileState.scrollY));
  check('first settings card is not covered by the sticky nav (phone)', mobileState.cardTop >= mobileState.navBottom, JSON.stringify(mobileState));
  check('first settings card starts inside the viewport (phone)', mobileState.cardTop >= 0 && mobileState.cardTop < mobileState.viewportHeight, JSON.stringify(mobileState));
  check('actions stay tappable at phone width', mobileState.checkButtonWidth >= 60, String(mobileState.checkButtonWidth));
  check('no horizontal overflow at phone width', mobileState.overflow <= 1, String(mobileState.overflow));

  await page.setViewportSize({ width: 1280, height: 900 });
  const lateErrors = failures.filter((message) => !message.includes('Failed to fetch'));
  check('no page errors after a clean reload', lateErrors.length === 0, lateErrors.join(' | '));
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
  fs.rmSync(workDir, { recursive: true, force: true });
}

const failed = checks.filter((item) => !item.ok);
console.log(`\nbrowser module-settings: ${checks.length - failed.length}/${checks.length} passing`);
if (failed.length) process.exitCode = 1;
