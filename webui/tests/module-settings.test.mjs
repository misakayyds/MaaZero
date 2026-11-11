// Dependency-free behaviour tests for module-settings.js.
// Run: node maa-phone-manager/webui/tests/module-settings.test.mjs

import fs from 'node:fs';
import { createRuntime, findAll, NAV_PAGES, DEFAULT_STATUS } from './dom-harness.mjs';

const MODULE_PATH = new URL('../module-settings.js', import.meta.url);
const MODULE_SOURCE = () => fs.readFileSync(MODULE_PATH, 'utf8');
const PAGE = 'module-settings';
const NAV_LABEL = '关于与模块设置';

const tests = [];
function test(name, fn) { tests.push({ name, fn }); }

function assert(condition, message) {
  if (!condition) throw new Error(message || 'assertion failed');
}

function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message || 'assertEqual failed'}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

function assertIncludes(haystack, needle, message) {
  if (!String(haystack).includes(needle)) {
    throw new Error(`${message || 'assertIncludes failed'}: ${JSON.stringify(needle)} not found in ${JSON.stringify(String(haystack).slice(0, 400))}`);
  }
}

function navLink(rt, id) {
  return rt.document.getElementById(id);
}

function stripComments(source) {
  return String(source)
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/(^|[^:\\])\/\/[^\n]*/g, '$1 ');
}

function buttons(rt) {
  if (!rt.page.main) return [];
  return findAll(rt.page.main, (element) => element.localName === 'button' && element.getAttribute('data-ms-action'));
}

function button(rt, action) {
  const found = buttons(rt).find((element) => element.getAttribute('data-ms-action') === action);
  assert(found, `button [data-ms-action=${action}] missing`);
  return found;
}

function nodeText(rt, selector) {
  const found = rt.page.main.querySelector(selector);
  return found ? found.textContent : '';
}

async function openSettings(rt) {
  await rt.host('go')(PAGE);
  await rt.settle();
}

test('adds exactly one nav entry labelled 关于与模块设置 inside the existing nav', async () => {
  const rt = createRuntime();
  rt.load(MODULE_PATH);

  const nav = rt.page.nav;
  const entries = findAll(nav, (element) => element.localName === 'a');
  assertEqual(entries.length, NAV_PAGES.length + 1, 'nav entry count');
  const added = navLink(rt, 'n_' + PAGE);
  assert(added, 'nav link #n_module-settings missing');
  assertEqual(added.textContent, NAV_LABEL, 'nav label');
  assertEqual(added.parentNode, nav, 'nav link must live in the existing .nav element');
  assertEqual(entries[entries.length - 1], added, 'nav link appended after existing entries');
});

test('is idempotent when the extension script is evaluated twice', async () => {
  const rt = createRuntime();
  rt.load(MODULE_PATH);
  rt.load(MODULE_PATH);

  const entries = findAll(rt.page.nav, (element) => element.localName === 'a');
  assertEqual(entries.length, NAV_PAGES.length + 1, 'duplicate nav entries after double load');

  rt.setPage(PAGE);
  rt.host('render')();
  await rt.settle();
  assertEqual(rt.intervalCalls.length, 1, 'double load must not register two poll timers');
});

test('keeps the host go() guard and host rendering for other pages', async () => {
  const rt = createRuntime();
  rt.load(MODULE_PATH);

  rt.run('S.runPending = true;');
  await rt.host('go')(PAGE);
  assertEqual(rt.host('S.page'), 'tasks', 'host guard must still block navigation');
  assert(!rt.page.main.textContent.includes('后台管理程序版本'), 'settings UI must not render while the host guard blocks navigation');

  rt.run('S.runPending = false;');
  await rt.host('go')('home');
  assertEqual(rt.page.main.textContent, 'HOST-HOME', 'host page rendering must be preserved');
  assertEqual(rt.document.title, '首页 · MAA 控制台', 'host shell context must be preserved');
});

test('adds PAGE_META entry so the shell title is meaningful', async () => {
  const rt = createRuntime();
  rt.load(MODULE_PATH);
  await openSettings(rt);
  assertEqual(rt.host('PAGE_META')[PAGE], NAV_LABEL, 'PAGE_META entry');
  assertEqual(rt.document.title, NAV_LABEL + ' · MAA 控制台', 'document title');
  assertEqual(rt.document.getElementById('pageTitle').textContent, NAV_LABEL, 'page title element');
});

test('renders manager version, component versions and idle state from /api/v1/status', async () => {
  const rt = createRuntime({
    status: {
      manager_version: '1.4.2',
      versions: { manager: { current: '1.4.2', previous: '1.4.0' }, project: { current: '2026.09.12', previous: '' } },
      pending: null,
      configured: true,
      can_install: true,
      reason: '',
    },
  });
  rt.load(MODULE_PATH);
  await openSettings(rt);

  const text = rt.page.main.textContent;
  assertIncludes(text, '1.4.2', 'manager version');
  assertIncludes(text, '2026.09.12', 'project version');
  assertIncludes(text, '1.4.0', 'previous version');
  assertEqual(rt.page.main.querySelector('[data-ms="pending"]').textContent, '空闲', 'idle pending state');

  const statusCall = rt.calls.find((call) => call.url === '/api/v1/status');
  assert(statusCall, 'status endpoint must be requested');
  assertEqual(statusCall.options.method, 'GET', 'status method');
  assertEqual(statusCall.options.credentials, 'same-origin', 'status credentials');
  assertEqual(statusCall.options.headers['X-MAA-Request'], '1', 'status CSRF header');
});

test('shows the current pending stage instead of claiming everything is done', async () => {
  const rt = createRuntime({
    status: Object.assign({}, DEFAULT_STATUS, { pending: { stage: 'downloading' } }),
  });
  rt.load(MODULE_PATH);
  await openSettings(rt);

  const pending = rt.page.main.querySelector('[data-ms="pending"]').textContent;
  assertIncludes(pending, 'downloading', 'raw stage must be visible');
  assert(!pending.includes('空闲'), 'must not also show an idle state');
});

test('disables every operation and shows the reason when the manager is not configured', async () => {
  const rt = createRuntime({
    status: Object.assign({}, DEFAULT_STATUS, {
      configured: false,
      can_install: false,
      reason: '未检测到 maa-managerd 运行环境',
    }),
  });
  rt.load(MODULE_PATH);
  await openSettings(rt);

  for (const action of ['check', 'apply', 'rollback']) {
    assertEqual(button(rt, action).disabled, true, `${action} must be disabled when not configured`);
  }
  assertIncludes(rt.page.main.textContent, '未检测到 maa-managerd 运行环境', 'reason must be shown');
});

test('disables every operation and shows the server reason when can_install is false', async () => {
  const rt = createRuntime({
    status: Object.assign({}, DEFAULT_STATUS, { configured: true, can_install: false, reason: 'MAA 任务运行中' }),
  });
  rt.load(MODULE_PATH);
  await openSettings(rt);

  assertEqual(button(rt, 'check').disabled, true, 'check disabled while installs are impossible');
  assertEqual(button(rt, 'apply').disabled, true, 'apply disabled');
  assertEqual(button(rt, 'rollback').disabled, true, 'rollback disabled');
  assertIncludes(rt.page.main.textContent, 'MAA 任务运行中', 'reason must be shown');
  assert(button(rt, 'apply').getAttribute('title'), 'a disabled operation should explain itself on hover');
});

test('explains an empty reason honestly instead of leaving a silent dead button', async () => {
  const notInstallable = createRuntime({
    status: Object.assign({}, DEFAULT_STATUS, { configured: true, can_install: false, reason: '' }),
  });
  notInstallable.load(MODULE_PATH);
  await openSettings(notInstallable);
  assertIncludes(notInstallable.page.main.textContent, '不允许安装或回退', 'install fallback reason');

  const notConfigured = createRuntime({
    status: Object.assign({}, DEFAULT_STATUS, { configured: false, can_install: false, reason: '' }),
  });
  notConfigured.load(MODULE_PATH);
  await openSettings(notConfigured);
  assertIncludes(notConfigured.page.main.textContent, '尚未就绪', 'status fallback reason');
});

test('blocks navigation into the settings page while the weekly draft guard says no', async () => {
  const rt = createRuntime();
  rt.load(MODULE_PATH);
  rt.run("S.page = 'weekly'; S.weeklyDirty = true; S.weeklyDraftOk = false;");
  rt.host('render')();

  await rt.host('go')(PAGE);
  assertEqual(rt.host('S.page'), 'weekly', 'host weekly guard must block the navigation');
  assertEqual(rt.host('HOST').draftCalls, 1, 'the weekly draft must be resolved exactly once');
  assert(!rt.page.main.textContent.includes('后台管理程序版本'), 'settings UI must not render when the draft was not resolved');
  assertEqual(rt.intervalCalls.length, 0, 'no polling must start behind the weekly guard');

  rt.run('S.weeklyDraftOk = true;');
  await rt.host('go')(PAGE);
  await rt.settle();
  assertEqual(rt.host('S.page'), PAGE, 'navigation proceeds once the draft is resolved');
  assert(rt.page.main.textContent.includes('后台管理程序版本'), 'settings UI renders after the guard passes');
});

test('never mutates the weekly draft state and routes nav clicks through the host go()', async () => {
  const rt = createRuntime();
  rt.load(MODULE_PATH);
  rt.run("S.page = 'weekly'; S.weeklyDirty = true; S.weeklyDay = 'mon'; S.dailyTasks = [1, 2, 3];");

  const draftBefore = rt.host('S').weeklyDirty;
  navLink(rt, 'n_' + PAGE).click();
  await rt.settle();

  assertEqual(rt.host('S').page, PAGE, 'clicking the nav entry opens the settings page');
  assertEqual(rt.host('HOST').goCalls.filter((entry) => entry === PAGE).length, 1, 'exactly one host go() call per click');
  assertEqual(rt.host('S').weeklyDirty, draftBefore, 'weekly dirty flag must be untouched');
  assertEqual(rt.host('S').weeklyDay, 'mon', 'weekly day must be untouched');
  assertEqual(rt.host('S').dailyTasks.length, 3, 'daily tasks must be untouched');

  const statusBeforeBlockedClick = rt.calls.filter((item) => item.url === '/api/v1/status').length;
  rt.run('S.runPending = true;');
  navLink(rt, 'n_' + PAGE).click();
  await rt.settle();
  assertEqual(rt.host('S').page, PAGE, 'the runPending guard must not navigate away or break the page');
  assertEqual(
    rt.calls.filter((item) => item.url === '/api/v1/status').length,
    statusBeforeBlockedClick,
    'a click blocked by runPending must not trigger any work'
  );
});

test('describes the rollback scope as the program version, not a component it cannot control', async () => {
  const rt = createRuntime();
  const confirmations = [];
  rt.sandbox.confirm = (message) => { confirmations.push(message); return false; };
  rt.load(MODULE_PATH);
  await openSettings(rt);

  button(rt, 'rollback').click();
  await rt.settle();
  button(rt, 'apply').click();
  await rt.settle();

  assertEqual(confirmations.length, 2, 'both mutating actions ask for confirmation');
  for (const message of confirmations) {
    assertIncludes(message, '程序版本', 'confirmation must talk about the program version');
    assert(!message.includes('后台程序'), 'confirmation must not claim to switch the daemon itself: ' + message);
    assert(!message.includes('项目包'), 'confirmation must not claim a component scope the driver does not own: ' + message);
  }
});

test('request bodies stay strictly empty / confirm-only for the API JSON decoder', async () => {
  const rt = createRuntime();
  rt.sandbox.confirm = () => true;
  rt.load(MODULE_PATH);
  await openSettings(rt);

  button(rt, 'check').click();
  await rt.settle();
  button(rt, 'apply').click();
  await rt.settle();
  button(rt, 'rollback').click();
  await rt.settle();

  const bodies = {
    '/api/v1/update/check': rt.calls.find((item) => item.url === '/api/v1/update/check'),
    '/api/v1/update/apply': rt.calls.find((item) => item.url === '/api/v1/update/apply'),
    '/api/v1/rollback': rt.calls.find((item) => item.url === '/api/v1/rollback'),
  };
  assertEqual(bodies['/api/v1/update/check'].options.body, '{}', 'check body must be an empty object');
  for (const path of ['/api/v1/update/apply', '/api/v1/rollback']) {
    const parsed = JSON.parse(bodies[path].options.body);
    assertEqual(Object.keys(parsed).join(','), 'confirm', `${path} may only send confirm`);
    assertEqual(parsed.confirm, true, `${path} confirm value`);
  }
  assertEqual(Object.keys(JSON.parse(bodies['/api/v1/update/check'].options.body)).length, 0, 'check must not add extra keys');
  assertEqual(bodies['/api/v1/update/check'].options.headers['Content-Type'], 'application/json', 'exact media type without charset');
});

test('surfaces a strict-decoder rejection as a failure, never as success', async () => {
  const rt = createRuntime();
  rt.sandbox.confirm = () => true;
  rt.setRoute('/api/v1/update/apply', () => ({ status: 400, body: { ok: false, message: '请求格式错误' } }));
  rt.load(MODULE_PATH);
  await openSettings(rt);

  button(rt, 'apply').click();
  await rt.settle();

  assertIncludes(rt.page.main.textContent, '请求格式错误', 'server rejection must be visible');
  assertEqual(rt.page.main.querySelector('[data-ms="message"]').className.includes('ms-err'), true, 'rejection must be styled as an error');
});

test('posts update check with JSON body, same-origin credentials and CSRF header', async () => {
  const rt = createRuntime();
  rt.load(MODULE_PATH);
  await openSettings(rt);

  button(rt, 'check').click();
  await rt.settle();

  const call = rt.calls.find((item) => item.url === '/api/v1/update/check');
  assert(call, 'check endpoint must be requested');
  assertEqual(call.options.method, 'POST', 'check method');
  assertEqual(call.options.credentials, 'same-origin', 'check credentials');
  assertEqual(call.options.headers['Content-Type'], 'application/json', 'check content type');
  assertEqual(call.options.headers['X-MAA-Request'], '1', 'check CSRF header');
  assertEqual(call.options.body, '{}', 'check body');
});

test('apply and rollback send {confirm:true} only after the operator confirms', async () => {
  const rt = createRuntime();
  const confirmations = [];
  rt.sandbox.confirm = (message) => { confirmations.push(message); return true; };
  rt.load(MODULE_PATH);
  await openSettings(rt);

  button(rt, 'apply').click();
  await rt.settle();
  button(rt, 'rollback').click();
  await rt.settle();

  const apply = rt.calls.find((item) => item.url === '/api/v1/update/apply');
  const rollback = rt.calls.find((item) => item.url === '/api/v1/rollback');
  assert(apply && rollback, 'apply and rollback endpoints must be requested');
  assertEqual(apply.options.body, JSON.stringify({ confirm: true }), 'apply body');
  assertEqual(rollback.options.body, JSON.stringify({ confirm: true }), 'rollback body');
  assertEqual(apply.options.headers['Content-Type'], 'application/json', 'apply content type');
  assertEqual(rollback.options.headers['X-MAA-Request'], '1', 'rollback CSRF header');
  assertEqual(confirmations.length, 2, 'both mutating actions must ask for confirmation');
});

test('cancelling the confirmation never calls the mutating endpoint', async () => {
  const rt = createRuntime();
  rt.sandbox.confirm = () => false;
  rt.load(MODULE_PATH);
  await openSettings(rt);

  button(rt, 'apply').click();
  await rt.settle();
  assert(!rt.calls.some((item) => item.url === '/api/v1/update/apply'), 'apply must not be requested after cancel');
});

test('reports failures honestly instead of faking success', async () => {
  const rt = createRuntime();
  rt.sandbox.confirm = () => true;
  rt.setRoute('/api/v1/update/apply', () => ({ status: 200, body: { ok: false, message: '清单签名无效，已取消' } }));
  rt.load(MODULE_PATH);
  await openSettings(rt);

  button(rt, 'apply').click();
  await rt.settle();

  const text = rt.page.main.textContent;
  assertIncludes(text, '清单签名无效，已取消', 'server message must be shown');
  assert(!text.includes('已开始'), 'must not fabricate a success state');
});

test('turns transport failures into a visible error, not a success banner', async () => {
  const rt = createRuntime();
  rt.setRoute('/api/v1/update/check', () => { throw new TypeError('Failed to fetch'); });
  rt.load(MODULE_PATH);
  await openSettings(rt);

  button(rt, 'check').click();
  await rt.settle();

  const text = rt.page.main.textContent;
  assert(text.includes('失败') || text.includes('无法'), 'a failure message must be rendered');
  assert(!text.includes('检查完成，发现'), 'no fabricated success text');
});

test('renders /api/v1/help as text, never as parsed markup', async () => {
  const help = '# 帮助标题\n\n<script>window.__pwned = true;</script>\n\n- 第一条';
  const rt = createRuntime({ help });
  rt.load(MODULE_PATH);
  await openSettings(rt);

  const call = rt.calls.find((item) => item.url === '/api/v1/help');
  assert(call, 'help endpoint must be requested');
  assertEqual(call.options.method, 'GET', 'help method');
  assertIncludes(call.options.headers.Accept, 'text/markdown', 'help accepts markdown text');

  const helpNode = rt.page.main.querySelector('[data-ms="help"]');
  assert(helpNode, 'help node missing');
  assertIncludes(helpNode.textContent, '<script>window.__pwned = true;</script>', 'server text must be kept verbatim as text');
  const injected = findAll(rt.page.main, (element) => element.localName === 'script');
  assertEqual(injected.length, 0, 'no script element may be created from server text');
  assert(!rt.host('window.__pwned'), 'script text must never execute');
});

test('polls status only while the settings page is open', async () => {
  const rt = createRuntime();
  rt.load(MODULE_PATH);

  const before = rt.calls.filter((item) => item.url === '/api/v1/status').length;
  await openSettings(rt);
  assertEqual(rt.intervalCalls.length, 1, 'one poll timer while open');
  const statusAfterOpen = rt.calls.filter((item) => item.url === '/api/v1/status').length;
  assert(statusAfterOpen > before, 'status must be fetched on entering the page');

  await rt.host('go')('home');
  assert(rt.clearedIntervals.includes(rt.intervalCalls[0].id), 'poll timer must be cleared when leaving');
  const afterLeave = rt.calls.filter((item) => item.url === '/api/v1/status').length;
  await rt.settle();
  assertEqual(rt.calls.filter((item) => item.url === '/api/v1/status').length, afterLeave, 'no polling after leaving the page');
});

test('registers after DOMContentLoaded when the document is still loading', async () => {
  const rt = createRuntime({ readyState: 'loading' });
  rt.load(MODULE_PATH);
  assert(!navLink(rt, 'n_' + PAGE), 'must wait for DOMContentLoaded');

  rt.document.readyState = 'interactive';
  rt.document.dispatchEvent('DOMContentLoaded');
  assert(navLink(rt, 'n_' + PAGE), 'nav entry added after DOMContentLoaded');
});

test('exposes a small integration surface for host pages and packaging checks', async () => {
  const rt = createRuntime();
  rt.load(MODULE_PATH);
  const api = rt.sandbox.MAA_MODULE_SETTINGS;
  assert(api, 'window.MAA_MODULE_SETTINGS missing');
  assertEqual(api.page, PAGE, 'exposed page id');
  assertEqual(typeof api.render, 'function', 'exposed render function');
  assertEqual(typeof api.refreshStatus, 'function', 'exposed refreshStatus function');
});

test('never uses innerHTML, localStorage or window.S', () => {
  const code = stripComments(MODULE_SOURCE());
  assert(!/\.innerHTML\b/.test(code), 'innerHTML must not be used');
  assert(!/localStorage\s*\./.test(code), 'localStorage must never store auth or state');
  assert(!/sessionStorage\s*\./.test(code), 'sessionStorage must never store auth or state');
  assert(!/window\.S\b/.test(code), 'window.S is undefined for the real page; read the lexical S binding');
  assert(!/document\.cookie/.test(code), 'document.cookie must not be used');
  assert(!/\binsertAdjacentHTML\b/.test(code), 'insertAdjacentHTML must not be used');
});

let passed = 0;
for (const { name, fn } of tests) {
  try {
    await fn();
    passed += 1;
    console.log('ok   - ' + name);
  } catch (error) {
    console.log('FAIL - ' + name);
    console.log('       ' + (error && error.stack ? error.stack.split('\n').slice(0, 4).join('\n       ') : String(error)));
  }
}
console.log(`\nmodule-settings: ${passed}/${tests.length} passing`);
if (passed !== tests.length) process.exitCode = 1;
