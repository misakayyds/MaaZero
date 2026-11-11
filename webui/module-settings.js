/*! MAA phone-manager WebUI extension: 关于与模块设置
 *
 * Adds exactly one navigation entry to the existing MAA WebUI page and renders
 * the manager settings section for it. The host page (maa-webui.py) is never
 * edited: this file is injected as an external script by the packaging overlay
 * (see maa-phone-manager/packaging/webui_overlay.py) and pointed at the fixed
 * local API implemented by maa-managerd.
 *
 * Rules kept deliberately:
 * - No localStorage / cookie / inline auth. State is in-memory for this page.
 * - All server strings are written with textContent; innerHTML is never used.
 * - Operations are disabled (with the server-provided reason) instead of
 *   pretending to work when the environment is not configured or not installable.
 * - Failures from the network or from {ok:false, message} responses are shown as
 *   failures; success is only shown for {ok:true}.
 * - Status is polled only while this section is open.
 */
(function () {
  'use strict';

  if (typeof window === 'undefined' || typeof document === 'undefined') return;
  if (window.__maaModuleSettingsLoaded) return;
  window.__maaModuleSettingsLoaded = true;

  var PAGE = 'module-settings';
  var NAV_LABEL = '关于与模块设置';
  var POLL_MS = 2000;
  var API = {
    status: '/api/v1/status',
    check: '/api/v1/update/check',
    apply: '/api/v1/update/apply',
    rollback: '/api/v1/rollback',
    help: '/api/v1/help'
  };
  var CSRF_HEADER = 'X-MAA-Request';
  var STAGE_LABELS = {
    checking: '检查清单',
    downloading: '下载组件',
    verifying: '校验签名',
    staging: '准备新版本',
    waiting_idle: '等待设备空闲',
    switching: '切换版本',
    health_check: '健康检查',
    rolling_back: '回退上一版'
  };
  // Fixed request bodies. The manager API never accepts free-form input from the
  // page: reads are GET, every mutation is POST with an explicit confirm flag.
  var ACTIONS = {
    check: { path: API.check, needsInstall: false, label: '检查更新', body: {} },
    apply: {
      path: API.apply,
      needsInstall: true,
      label: '应用更新',
      body: { confirm: true },
      confirm: '应用更新会下载并切换程序版本，期间将等待 MAA 任务空闲。是否继续？'
    },
    rollback: {
      path: API.rollback,
      needsInstall: true,
      label: '回退上一版',
      body: { confirm: true },
      confirm: '回退会把程序版本切回上一版本，用户数据不会删除。是否继续？'
    }
  };

  var state = {
    status: null,
    statusLoaded: false,
    statusError: '',
    help: '',
    helpLoading: false,
    helpError: '',
    message: null,
    busy: false,
    pollTimer: null,
    statusSeq: 0
  };

  // ─── host page integration ───

  function pageState() {
    // The host page declares `let S = {...}` (a global lexical binding, NOT
    // window.S), so the bare identifier is the only correct access path.
    try {
      if (typeof S !== 'undefined' && S && typeof S.page === 'string') return S.page;
    } catch (_) { /* ignore */ }
    return '';
  }

  function installPageMeta() {
    try {
      if (typeof PAGE_META !== 'undefined' && PAGE_META) PAGE_META[PAGE] = NAV_LABEL;
    } catch (_) { /* optional */ }
  }

  function installNavEntry() {
    if (document.getElementById('n_' + PAGE)) return;
    var nav = document.querySelector('.nav') || document.querySelector('nav');
    if (!nav) return;
    var link = document.createElement('a');
    link.id = 'n_' + PAGE;
    link.setAttribute('href', '#');
    link.className = 'ms-nav-link';
    link.textContent = NAV_LABEL;
    link.addEventListener('click', function (event) {
      if (event && typeof event.preventDefault === 'function') event.preventDefault();
      try {
        if (typeof window.go === 'function') window.go(PAGE);
      } catch (_) { /* keep the page usable */ }
    });
    nav.appendChild(link);
  }

  function installRenderHook() {
    var hostRender = window.render;
    if (typeof hostRender !== 'function') return;
    window.render = function () {
      if (pageState() === PAGE) {
        startPolling();
        renderSettings();
        return undefined;
      }
      stopPolling();
      return hostRender.apply(this, arguments);
    };
  }

  function init() {
    installPageMeta();
    installNavEntry();
    installRenderHook();
  }

  // ─── API access ───

  function apiGet(path, accept) {
    return window.fetch(path, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { 'Accept': accept || 'application/json', 'X-MAA-Request': '1' }
    }).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return accept === 'text/markdown' ? response.text() : response.json();
    });
  }

  function apiPost(path, body) {
    return window.fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-MAA-Request': '1'
      },
      body: JSON.stringify(body || {})
    }).then(function (response) {
      return response.json().then(function (payload) {
        return { transportOk: response.ok, payload: payload };
      }, function () {
        throw new Error('响应不是有效的 JSON（HTTP ' + response.status + '）');
      });
    }).then(function (result) {
      var payload = result.payload && typeof result.payload === 'object' ? result.payload : {};
      var ok = typeof payload.ok === 'boolean' ? payload.ok : result.transportOk;
      return {
        ok: ok === true,
        message: typeof payload.message === 'string' ? payload.message : ''
      };
    });
  }

  function describeError(error) {
    if (!error) return '未知错误';
    if (typeof error.message === 'string' && error.message) return error.message;
    return String(error);
  }

  function refreshStatus() {
    state.statusSeq += 1;
    var seq = state.statusSeq;
    return apiGet(API.status).then(function (payload) {
      if (seq !== state.statusSeq) return;
      state.status = payload && typeof payload === 'object' ? payload : null;
      state.statusLoaded = true;
      state.statusError = '';
      rerenderIfOpen();
    }, function (error) {
      if (seq !== state.statusSeq) return;
      state.statusLoaded = true;
      state.statusError = describeError(error);
      rerenderIfOpen();
    });
  }

  function loadHelp() {
    if (state.helpLoading || state.help) return Promise.resolve();
    state.helpLoading = true;
    return apiGet(API.help, 'text/markdown').then(function (text) {
      state.helpLoading = false;
      state.helpError = '';
      state.help = typeof text === 'string' ? text : '';
      rerenderIfOpen();
    }, function (error) {
      state.helpLoading = false;
      state.helpError = describeError(error);
      rerenderIfOpen();
    });
  }

  // ─── polling ───

  function startPolling() {
    if (state.pollTimer !== null) return;
    refreshStatus();
    loadHelp();
    state.pollTimer = window.setInterval(refreshStatus, POLL_MS);
  }

  function stopPolling() {
    if (state.pollTimer === null) return;
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  // ─── actions ───

  function statusValue(name, fallback) {
    var status = state.status || {};
    return status[name] === undefined || status[name] === null ? fallback : status[name];
  }

  function reasonText(kind) {
    var reason = statusValue('reason', '');
    if (typeof reason === 'string' && reason) return reason;
    return kind === 'install'
      ? '后台管理程序当前不允许安装或回退'
      : '后台管理程序尚未就绪，暂时无法执行操作';
  }

  // Every operation is disabled unless the manager reports both configured and
  // can_install. The CLI reports can_install=false until a safe runtime driver
  // exists, so the page must not offer a button that cannot work.
  function disabledReason(action) {
    if (!state.statusLoaded || !state.status) return '正在读取后台状态…';
    if (statusValue('configured', false) !== true) return reasonText('status');
    if (statusValue('can_install', false) !== true) return reasonText('install');
    if (state.busy) return '上一个操作仍在进行';
    return '';
  }

  function runAction(action) {
    var config = ACTIONS[action];
    if (!config || state.busy) return;
    var blocked = disabledReason(action);
    if (blocked) {
      setMessage(false, blocked);
      return;
    }
    if (config.confirm) {
      var confirmed = false;
      try { confirmed = window.confirm(config.confirm) === true; } catch (_) { confirmed = false; }
      if (!confirmed) {
        // A real browser always has window.confirm; if it is missing we must not
        // silently "succeed", so say plainly that nothing happened.
        if (typeof window.confirm !== 'function') setMessage(false, '无法弹出确认框，已取消操作');
        return;
      }
    }

    state.busy = true;
    renderSettings();
    apiPost(config.path, config.body).then(function (result) {
      state.busy = false;
      if (result.ok) {
        setMessage(true, result.message || '操作已提交，等待后台处理');
      } else {
        setMessage(false, result.message || '操作失败，后台未接受该请求');
      }
      return refreshStatus();
    }, function (error) {
      state.busy = false;
      setMessage(false, '操作失败：' + describeError(error));
    });
  }

  function setMessage(ok, text) {
    state.message = { ok: ok === true, text: String(text === undefined || text === null ? '' : text) };
    rerenderIfOpen();
  }

  // ─── rendering ───

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function field(parent, label, value) {
    var row = el('p', 'ms-field');
    row.appendChild(el('span', 'ms-field-label', label));
    row.appendChild(el('span', 'ms-field-value', value));
    parent.appendChild(row);
    return row;
  }

  function rerenderIfOpen() {
    if (pageState() === PAGE) renderSettings();
  }

  function renderOverviewCard() {
    var card = el('section', 'ms-card');
    card.appendChild(el('h2', null, '关于与模块设置'));

    if (!state.statusLoaded) {
      card.appendChild(el('p', 'ms-hint', '正在读取后台状态…'));
    } else if (!state.status) {
      card.appendChild(el('p', 'ms-hint ms-err', '无法读取后台状态：' + (state.statusError || '未知错误')));
    } else {
      field(card, '后台管理程序版本', state.status.manager_version || '未知');
      field(card, '环境状态', statusValue('configured', false) === true ? '已配置' : '未配置');
      field(card, '更新能力', statusValue('can_install', false) === true ? '空闲且允许安装' : '当前不允许安装');
      var reason = statusValue('reason', '');
      if (typeof reason === 'string' && reason) field(card, '原因', reason);
      if (state.statusError) card.appendChild(el('p', 'ms-hint ms-err', '状态刷新失败：' + state.statusError));
    }
    return card;
  }

  function renderVersionsCard() {
    var card = el('section', 'ms-card');
    card.appendChild(el('h2', null, '版本'));
    var versions = (state.status && state.status.versions) || null;
    var components = versions && typeof versions === 'object' ? Object.keys(versions) : [];
    if (!components.length) {
      card.appendChild(el('p', 'ms-hint', '暂无版本记录'));
      return card;
    }
    var table = el('table', 'ms-table');
    table.setAttribute('data-ms', 'versions');
    var head = el('tr');
    head.appendChild(el('th', null, '组件'));
    head.appendChild(el('th', null, '当前版本'));
    head.appendChild(el('th', null, '上一版本'));
    table.appendChild(head);
    components.forEach(function (component) {
      var entry = versions[component] && typeof versions[component] === 'object' ? versions[component] : {};
      var row = el('tr');
      row.appendChild(el('td', null, component));
      row.appendChild(el('td', null, entry.current || '—'));
      row.appendChild(el('td', null, entry.previous || '—'));
      table.appendChild(row);
    });
    card.appendChild(table);
    return card;
  }

  function renderProgressCard() {
    var card = el('section', 'ms-card');
    card.appendChild(el('h2', null, '更新进度'));
    var pending = state.status ? state.status.pending : null;
    if (pending && typeof pending === 'object' && pending.stage) {
      var stage = String(pending.stage);
      var label = STAGE_LABELS[stage] ? STAGE_LABELS[stage] + '（' + stage + '）' : stage;
      var line = el('p', 'ms-pending');
      line.setAttribute('data-ms', 'pending');
      line.textContent = '正在进行：' + label;
      card.appendChild(line);
      card.appendChild(el('p', 'ms-hint', '更新在后台异步执行，完成后状态会自动刷新。'));
    } else {
      var idle = el('p', 'ms-idle');
      idle.setAttribute('data-ms', 'pending');
      idle.textContent = '空闲';
      card.appendChild(idle);
    }
    return card;
  }

  function renderActionsCard() {
    var card = el('section', 'ms-card');
    card.appendChild(el('h2', null, '操作'));
    card.appendChild(el('p', 'ms-hint', '控制通道与版本事务由 maa-managerd 执行；本页面只提交请求并显示结果。'));

    var bar = el('div', 'ms-actions');
    ['check', 'apply', 'rollback'].forEach(function (action) {
      var button = el('button', 'ms-button' + (action === 'check' ? ' ms-button-primary' : ''), ACTIONS[action].label);
      button.setAttribute('type', 'button');
      button.setAttribute('data-ms-action', action);
      var why = disabledReason(action);
      button.disabled = Boolean(why);
      if (why) button.title = why;
      button.addEventListener('click', function () { runAction(action); });
      bar.appendChild(button);
    });
    card.appendChild(bar);

    if (state.statusLoaded && state.status) {
      var configured = statusValue('configured', false) === true;
      var installable = statusValue('can_install', false) === true;
      if (!configured) {
        card.appendChild(el('p', 'ms-hint ms-err', '环境未配置，操作已禁用：' + reasonText('status')));
      } else if (!installable) {
        card.appendChild(el('p', 'ms-hint ms-warn', '安装与回退已禁用：' + reasonText('install')));
      }
    }

    var message = el('p', 'ms-message', state.message ? state.message.text : '尚未执行操作');
    message.setAttribute('data-ms', 'message');
    if (state.message) message.className = 'ms-message ' + (state.message.ok ? 'ms-ok' : 'ms-err');
    card.appendChild(message);
    return card;
  }

  function renderHelpCard() {
    var card = el('section', 'ms-card');
    card.appendChild(el('h2', null, '帮助'));
    var body = '';
    if (state.help) body = state.help;
    else if (state.helpError) body = '帮助加载失败：' + state.helpError;
    else body = '正在加载帮助…';
    var pre = el('pre', 'ms-help', body);
    pre.setAttribute('data-ms', 'help');
    card.appendChild(pre);
    return card;
  }

  function renderSettings() {
    var main = document.getElementById('main');
    if (!main) return;
    main.textContent = '';
    var root = el('div', 'ms-root');
    root.appendChild(renderOverviewCard());
    root.appendChild(renderVersionsCard());
    root.appendChild(renderProgressCard());
    root.appendChild(renderActionsCard());
    root.appendChild(renderHelpCard());
    main.appendChild(root);
  }

  window.MAA_MODULE_SETTINGS = {
    page: PAGE,
    navLabel: NAV_LABEL,
    api: API,
    render: renderSettings,
    refreshStatus: refreshStatus,
    loadHelp: loadHelp
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
