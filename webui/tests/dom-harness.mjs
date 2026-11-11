// Dependency-free mini DOM + host-page runtime used to exercise
// module-settings.js in Node without jsdom or any npm install.
//
// It intentionally mirrors only the DOM surface the extension is allowed to
// use (createElement/textContent/setAttribute/appendChild/addEventListener)
// plus the host page globals (`S`, `render`, `go`, `PAGE_META`) that the real
// maa-webui.py defines. `S` is declared with `let`, exactly like the real
// page, so `window.S` stays undefined and any code that reaches for it breaks.

import fs from 'node:fs';
import vm from 'node:vm';

class ClassList {
  constructor(element) { this.element = element; }

  _tokens() {
    return (this.element.attributes.class || '').split(/\s+/).filter(Boolean);
  }

  add(...names) {
    const tokens = new Set(this._tokens());
    names.forEach((name) => tokens.add(String(name)));
    this.element.attributes.class = [...tokens].join(' ');
  }

  remove(...names) {
    const tokens = new Set(this._tokens());
    names.forEach((name) => tokens.delete(String(name)));
    this.element.attributes.class = [...tokens].join(' ');
  }

  contains(name) { return this._tokens().includes(String(name)); }

  toString() { return this.element.attributes.class || ''; }
}

export class TextNode {
  constructor(text) {
    this.nodeType = 3;
    this.childNodes = [];
    this.parentNode = null;
    this.textContent = String(text);
  }
}

export class Element {
  constructor(tagName, ownerDocument) {
    this.nodeType = 1;
    this.localName = String(tagName).toLowerCase();
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument || null;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = Object.create(null);
    this.style = {};
    this.dataset = {};
    this.classList = new ClassList(this);
    this._listeners = Object.create(null);
    this._disabled = false;
  }

  get children() { return this.childNodes.filter((node) => node.nodeType === 1); }

  get firstChild() { return this.childNodes.length ? this.childNodes[0] : null; }

  get id() { return this.attributes.id || ''; }
  set id(value) { this.attributes.id = String(value); }

  get className() { return this.attributes.class || ''; }
  set className(value) { this.attributes.class = String(value); }

  // Reflected like the real DOM, so tests can assert hover explanations.
  get title() { return this.attributes.title || ''; }
  set title(value) { this.attributes.title = String(value); }

  get disabled() { return this._disabled; }
  set disabled(value) { this._disabled = Boolean(value); }

  get textContent() { return this.childNodes.map((node) => node.textContent).join(''); }

  set textContent(value) {
    this.childNodes = [];
    if (value !== null && value !== undefined && String(value) !== '') {
      this.appendChild(new TextNode(value));
    }
  }

  setAttribute(name, value) { this.attributes[String(name)] = String(value); }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }

  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name); }

  removeAttribute(name) { delete this.attributes[name]; }

  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  insertBefore(node, reference) {
    if (reference === null || reference === undefined) return this.appendChild(node);
    const index = this.childNodes.indexOf(reference);
    if (index < 0) return this.appendChild(node);
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.splice(index, 0, node);
    return node;
  }

  removeChild(node) {
    const index = this.childNodes.indexOf(node);
    if (index >= 0) {
      this.childNodes.splice(index, 1);
      node.parentNode = null;
    }
    return node;
  }

  remove() { if (this.parentNode) this.parentNode.removeChild(this); }

  addEventListener(type, handler) {
    (this._listeners[type] = this._listeners[type] || []).push(handler);
  }

  dispatchEvent(type, event = {}) {
    const payload = Object.assign({
      type,
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() {},
    }, event);
    for (const handler of (this._listeners[type] || []).slice()) handler.call(this, payload);
    return !payload.defaultPrevented;
  }

  click() { return this.dispatchEvent('click'); }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  querySelectorAll(selector) {
    const found = [];
    for (const element of descendants(this)) {
      if (matchesSelector(element, selector)) found.push(element);
    }
    return found;
  }
}

export class Document {
  constructor() {
    this.readyState = 'complete';
    this.title = '';
    this._listeners = Object.create(null);
    this.documentElement = new Element('html', this);
    this.head = new Element('head', this);
    this.body = new Element('body', this);
    this.documentElement.appendChild(this.head);
    this.documentElement.appendChild(this.body);
  }

  createElement(tagName) { return new Element(tagName, this); }

  createTextNode(text) { return new TextNode(text); }

  getElementById(id) { return this.querySelector('#' + id); }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  querySelectorAll(selector) {
    const found = [];
    for (const element of descendants(this.documentElement)) {
      if (matchesSelector(element, selector)) found.push(element);
    }
    return found;
  }

  addEventListener(type, handler) {
    (this._listeners[type] = this._listeners[type] || []).push(handler);
  }

  dispatchEvent(type, event = {}) {
    const payload = Object.assign({ type, target: this }, event);
    for (const handler of (this._listeners[type] || []).slice()) handler.call(this, payload);
  }
}

function descendants(root) {
  const out = [];
  const visit = (node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === 1) {
        out.push(child);
        visit(child);
      }
    }
  };
  visit(root);
  return out;
}

function matchesCompound(element, compound) {
  if (!compound) return false;
  const pattern = /([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  let matched = false;
  let index = 0;
  let token;
  while ((token = pattern.exec(compound)) !== null) {
    if (token.index !== index) return false;
    index = pattern.lastIndex;
    matched = true;
    if (token[1]) {
      if (element.localName !== token[1].toLowerCase()) return false;
    } else if (token[2]) {
      if (element.id !== token[2]) return false;
    } else if (token[3]) {
      if (!element.classList.contains(token[3])) return false;
    } else if (token[4]) {
      if (!element.hasAttribute(token[4])) return false;
      if (token[5] !== undefined && element.getAttribute(token[4]) !== token[5]) return false;
    }
  }
  return matched && index === compound.length;
}

export function matchesSelector(element, selector) {
  if (element.nodeType !== 1) return false;
  const parts = String(selector).trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return false;
  if (!matchesCompound(element, parts[parts.length - 1])) return false;
  let remaining = parts.length - 2;
  let node = element.parentNode;
  while (remaining >= 0 && node) {
    if (node.nodeType === 1 && matchesCompound(node, parts[remaining])) remaining -= 1;
    node = node.parentNode;
  }
  return remaining < 0;
}

export function findAll(root, predicate) {
  const out = [];
  const visit = (node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === 1) {
        if (predicate(child)) out.push(child);
        visit(child);
      }
    }
  };
  visit(root);
  return out;
}

export const NAV_PAGES = [
  ['home', '首页'],
  ['weekly', '周计划'],
  ['infrast-plans', '基建排班'],
  ['cultivation', '培养目标'],
  ['tasks', '任务配置'],
  ['logs', '日志'],
  ['cron', '定时'],
  ['conn', '设置'],
];

export const DEFAULT_STATUS = {
  manager_version: '0.0.1-dev',
  versions: {
    manager: { current: '0.0.1-dev', previous: '' },
    project: { current: '2026.09.12', previous: '2026.09.01' },
  },
  pending: null,
  configured: true,
  can_install: true,
  reason: '',
};

export const HOST_SCRIPT = `
let S = {
  page: 'tasks',
  runPending: false,
  tasks: [],
  dailyTasks: [],
  weeklyDay: 'mon',
  weeklyDirty: false,
  weeklyDraftOk: true,
};
const PAGE_META = { home: '首页', weekly: '周计划', tasks: '任务配置', conn: '设置' };
const HOST = { renderCalls: [], goCalls: [], draftCalls: 0, messages: [] };
function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function showMsg(cls, text) { HOST.messages.push({ cls, text }); }
// Mirrors the real weekly editor hook: leaving the weekly page is blocked until
// the pending draft is resolved, and the host returns early when the user cancels.
function resolveWeeklyDraft() { HOST.draftCalls += 1; return S.weeklyDraftOk !== false; }
function updateShellContext() {
  const title = PAGE_META[S.page] || 'MAA 控制台';
  const titleElement = document.getElementById('pageTitle');
  if (titleElement) titleElement.textContent = title;
  document.title = title + ' · MAA 控制台';
}
function render() {
  HOST.renderCalls.push({ page: S.page, source: 'host' });
  const main = document.getElementById('main');
  if (!main) return;
  if (S.page === 'home') main.textContent = 'HOST-HOME';
  else if (S.page === 'tasks') main.textContent = 'HOST-TASKS';
  else if (S.page === 'weekly') main.textContent = 'HOST-WEEKLY';
}
async function go(page) {
  HOST.goCalls.push(page);
  if (S.runPending) return;
  if (S.page === 'weekly' && !(await resolveWeeklyDraft())) return;
  S.page = page;
  S.tasks = page === 'weekly' ? S.dailyTasks : S.dailyTasks;
  document.querySelectorAll('.nav a').forEach((link) => link.classList.remove('on'));
  const link = document.getElementById('n_' + page);
  if (link) link.classList.add('on');
  updateShellContext();
  render();
}
`;

export function buildHostPage(document) {
  const shell = document.createElement('div');
  shell.className = 'app-shell';
  document.body.appendChild(shell);

  const sidebar = document.createElement('aside');
  sidebar.className = 'sidebar';
  shell.appendChild(sidebar);

  const nav = document.createElement('nav');
  nav.className = 'nav';
  nav.setAttribute('aria-label', '主导航');
  for (const [page, label] of NAV_PAGES) {
    const link = document.createElement('a');
    link.id = 'n_' + page;
    link.setAttribute('href', '#');
    link.setAttribute('onclick', `go('${page}');return false`);
    link.textContent = label;
    nav.appendChild(link);
  }
  sidebar.appendChild(nav);

  const workspace = document.createElement('section');
  workspace.className = 'workspace';
  shell.appendChild(workspace);

  const topbar = document.createElement('header');
  topbar.className = 'topbar';
  workspace.appendChild(topbar);
  const title = document.createElement('h1');
  title.id = 'pageTitle';
  title.textContent = '任务配置';
  topbar.appendChild(title);
  const dateContext = document.createElement('span');
  dateContext.id = 'dateContext';
  topbar.appendChild(dateContext);

  const main = document.createElement('main');
  main.id = 'main';
  main.className = 'wrap';
  workspace.appendChild(main);

  return { shell, nav, main };
}

export function createRuntime(options = {}) {
  const document = new Document();
  if (options.readyState) document.readyState = options.readyState;
  const page = buildHostPage(document);

  const calls = [];
  const intervalCalls = [];
  const clearedIntervals = [];
  const errors = [];
  const routes = new Map();
  let intervalSeq = 0;

  const setRoute = (path, handler) => routes.set(path, handler);
  setRoute('/api/v1/status', () => ({ status: 200, body: options.status || DEFAULT_STATUS }));
  setRoute('/api/v1/help', () => ({
    status: 200,
    text: options.help === undefined ? '# MAA 手机模块帮助\n\n这是一段离线教程。' : options.help,
  }));

  const fetchImpl = async (url, fetchOptions = {}) => {
    const record = { url, options: fetchOptions };
    calls.push(record);
    const handler = routes.get(url);
    if (!handler) {
      const error = new TypeError('Failed to fetch ' + url);
      throw error;
    }
    const result = handler(fetchOptions);
    if (result instanceof Error) throw result;
    record.response = result;
    const body = result.body;
    return {
      ok: result.status >= 200 && result.status < 300,
      status: result.status,
      headers: { get: (name) => (result.headers || {})[String(name).toLowerCase()] || null },
      json: async () => {
        if (body instanceof Error) throw body;
        return body;
      },
      text: async () => {
        if (body instanceof Error) throw body;
        return result.text !== undefined ? result.text : JSON.stringify(body);
      },
    };
  };

  const sandbox = {};
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.document = document;
  sandbox.fetch = fetchImpl;
  sandbox.console = console;
  sandbox.setInterval = (handler, delay) => {
    intervalSeq += 1;
    intervalCalls.push({ id: intervalSeq, handler, delay });
    return intervalSeq;
  };
  sandbox.clearInterval = (id) => { clearedIntervals.push(id); };
  sandbox.setTimeout = () => 0;
  sandbox.clearTimeout = () => {};

  vm.createContext(sandbox);
  vm.runInContext(HOST_SCRIPT, sandbox, { filename: 'host-page.js' });

  const runtime = {
    sandbox,
    document,
    page,
    calls,
    intervalCalls,
    clearedIntervals,
    errors,
    setRoute,
    fetchImpl,
    run(code, filename) { return vm.runInContext(code, sandbox, { filename: filename || 'inline.js' }); },
    load(filePath) {
      const source = fs.readFileSync(filePath, 'utf8');
      return vm.runInContext(source, sandbox, { filename: String(filePath) });
    },
    host(name) { return vm.runInContext(name, sandbox); },
    setPage(pageName) { vm.runInContext(`S.page = ${JSON.stringify(pageName)};`, sandbox); },
    async settle(rounds = 3) {
      // Drain microtasks queued by the extension at each macrotask turn.
      for (let index = 0; index < rounds; index += 1) {
        await new Promise((resolve) => setImmediate(resolve));
      }
    },
  };
  return runtime;
}
