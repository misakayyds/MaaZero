// Cultivation goals: searchable operator picker, training levels, material gap
// estimate and manual craft hints. This file never upgrades or crafts anything.

var CULTIVATION_DEFAULT_MAX_LEVELS = {
  6: [50, 80, 90],
  5: [50, 70, 80],
  4: [45, 60, 70],
  3: [40, 55],
  2: [30],
  1: [30],
};

var CULTIVATION_RUNTIME_LABELS = {
  idle: '空闲',
  scanning: '库存识别中',
  running: '培养刷取中',
  done: '已完成',
  error: '异常',
};

// Virtual plan keys are game concepts, not depot items.
var CULTIVATION_VIRTUAL_ITEM_NAMES = {EXP: '经验值'};

function cultivationState() {
  if (!S.cultivation) S.cultivation = {};
  var state = S.cultivation;
  if (state.goalBusyId === undefined) state.goalBusyId = '';
  if (state.fetchSeq === undefined) state.fetchSeq = 0;
  if (state.pollFingerprint === undefined) state.pollFingerprint = '';
  if (state.balanceDirty === undefined) state.balanceDirty = false;
  if (state.balanceSaving === undefined) state.balanceSaving = false;
  if (state.masteryDraft === undefined) state.masteryDraft = null;
  if (state.masteryQuery === undefined) state.masteryQuery = '';
  if (state.masterySaving === undefined) state.masterySaving = false;
  if (state.masteryBusyId === undefined) state.masteryBusyId = '';
  if (state.masteryError === undefined) state.masteryError = '';
  if (state.masteryNotice === undefined) state.masteryNotice = '';
  if (state.activeSearchId === undefined) state.activeSearchId = '';
  return state;
}

function cultivationData() {
  return cultivationState().data || {};
}

function cultivationMastery() {
  var mastery = cultivationData().mastery || {};
  return {
    enabled: !!mastery.enabled,
    plans: Array.isArray(mastery.plans) ? mastery.plans : [],
    runtime: mastery.runtime || {status: 'idle', message: '', updated_at: ''},
  };
}

function cultivationSettings() {
  var settings = cultivationData().settings || {};
  return {
    enabled: !!settings.enabled,
    fallback_stage: String(settings.fallback_stage || '1-7'),
  };
}

function cultivationRuntime() {
  return cultivationData().runtime || {};
}

function cultivationInventoryVerified() {
  var inventory = cultivationData().inventory || {};
  var status = cultivationInventoryScan();
  return !!inventory.available && status.stale !== true && !status.last_error;
}

function cultivationCurrency() {
  var currency = cultivationData().currency || {};
  return {
    available: !!currency.available,
    balance: Number(currency.balance) || 0,
    source: String(currency.source || (currency.available ? 'manual' : 'unknown')),
    confirmed_at: String(currency.confirmed_at || ''),
    updated_at: String(currency.updated_at || ''),
  };
}

function cultivationCurrencySourceLabel(currency) {
  if (currency.source === 'depot') return '仓库识别';
  if (currency.source === 'manual') return '用户手动临时校正，下次成功仓库识别会覆盖';
  return '未知，请先识别仓库';
}

function cultivationBalanceInputValue() {
  var draft = cultivationState().balanceDraft;
  return draft === undefined || draft === null ? '' : String(draft);
}

function cultivationSetBalance(value) {
  var state = cultivationState();
  state.balanceDraft = String(value === undefined || value === null ? '' : value);
  state.balanceDirty = true;
  if (!cultivationSyncSettingsChrome()) cultivationRenderIfVisible();
  return state.balanceDraft;
}

function cultivationBalanceValue() {
  var raw = cultivationBalanceInputValue().trim();
  if (!raw) return {ok: false, error: '请填写龙门币余额'};
  if (!/^[0-9]+$/.test(raw)) return {ok: false, error: '龙门币余额必须是非负整数'};
  var value = Number(raw);
  if (!isFinite(value) || value < 0 || value > 1000000000000) {
    return {ok: false, error: '龙门币余额超出合理范围'};
  }
  return {ok: true, value: value};
}

function cultivationCatalog() {
  var catalog = cultivationData().catalog || {};
  return {
    operators: catalog.operators || {},
    items: catalog.items || {},
    recipes: catalog.recipes || {},
    stages: catalog.stages || [],
  };
}

function cultivationAvatarHtml(operatorId, name) {
  var id = String(operatorId || '');
  var label = String(name || id);
  return '<img class="cult-avatar" src="/api/cultivation/avatar/' + esc(id) +
    '" alt="' + esc(label) + '" loading="lazy">';
}

function cultivationOperatorEntry(operatorId) {
  return cultivationCatalog().operators[operatorId] || null;
}

function cultivationItemName(itemId) {
  var key = String(itemId === undefined || itemId === null ? '' : itemId);
  var entry = cultivationCatalog().items[key];
  if (entry && entry.name) return entry.name;
  if (CULTIVATION_VIRTUAL_ITEM_NAMES[key]) return CULTIVATION_VIRTUAL_ITEM_NAMES[key];
  return key || '未知材料';
}

function cultivationOperatorName(operatorId, planEntry) {
  var entry = cultivationOperatorEntry(operatorId);
  if (entry && entry.name) return String(entry.name);
  if (planEntry && planEntry.name) return String(planEntry.name);
  return String(operatorId || '未知干员');
}

function cultivationSnapshot() {
  var snapshot = cultivationData().operator_snapshot || {};
  if (Array.isArray(snapshot.own_opers)) return snapshot;
  if (Array.isArray(snapshot.operators)) return snapshot;
  if (snapshot.details && Array.isArray(snapshot.details.own_opers)) {
    return Object.assign({}, snapshot.details, {scanned_at: snapshot.scanned_at || ''});
  }
  return {};
}

function cultivationSnapshotEntry(operatorId) {
  var owners = cultivationSnapshot().own_opers || cultivationSnapshot().operators || [];
  for (var i = 0; i < owners.length; i += 1) {
    if (owners[i] && owners[i].id === operatorId) return owners[i];
  }
  return null;
}

function cultivationMaxLevels(operatorId) {
  var entry = cultivationOperatorEntry(operatorId);
  if (entry && Array.isArray(entry.max_levels) && entry.max_levels.length) {
    return entry.max_levels.map(function(value) { return Number(value); });
  }
  var byRarity = CULTIVATION_DEFAULT_MAX_LEVELS[entry ? Number(entry.rarity) : 0];
  return byRarity || [50, 80, 90];
}

function cultivationLevelCap(operatorId, elite) {
  var levels = cultivationMaxLevels(operatorId);
  var index = Math.max(0, Math.min(Number(elite) || 0, levels.length - 1));
  return levels[index];
}

function cultivationEliteName(elite) {
  return '精英' + (Number(elite) || 0);
}

function cultivationValidateDraft(draft) {
  if (!draft || !draft.operator_id) return ['请选择干员'];
  var errors = [];
  var levels = cultivationMaxLevels(draft.operator_id);
  var currentElite = Number(draft.current_elite);
  var targetElite = Number(draft.target_elite);
  var currentLevel = Number(draft.current_level);
  var targetLevel = Number(draft.target_level);
  if (!Number.isInteger(currentElite) || currentElite < 0 || currentElite >= levels.length) {
    errors.push('当前精英等级无效');
  }
  if (!Number.isInteger(targetElite) || targetElite < 0 || targetElite >= levels.length) {
    errors.push('目标精英等级无效');
  }
  if (!Number.isInteger(currentLevel) || currentLevel < 1) errors.push('当前等级无效');
  if (!Number.isInteger(targetLevel) || targetLevel < 1) errors.push('目标等级无效');
  if (errors.length) return errors;
  if (currentLevel > levels[currentElite]) {
    errors.push('当前等级超过' + cultivationEliteName(currentElite) + '上限 ' + levels[currentElite]);
  }
  if (targetLevel > levels[targetElite]) {
    errors.push('目标等级超过' + cultivationEliteName(targetElite) + '上限 ' + levels[targetElite]);
  }
  if (errors.length) return errors;
  if (targetElite * 1000 + targetLevel <= currentElite * 1000 + currentLevel) {
    errors.push('目标必须高于当前练度');
  }
  return errors;
}

function cultivationSetDraft(draft) {
  cultivationState().draft = draft ? Object.assign({}, draft) : null;
  cultivationState().error = '';
  return cultivationState().draft;
}

function cultivationSetSettings(patch) {
  var state = cultivationState();
  var base = state.settingsDraft || cultivationSettings();
  state.settingsDraft = Object.assign({}, base, patch || {});
  state.settingsDirty = true;
  // Update the unsaved hint in place: a full repaint would drop input focus.
  if (!cultivationSyncSettingsChrome()) cultivationRenderIfVisible();
  return state.settingsDraft;
}

function cultivationSettingsChrome(state) {
  var dirty = !!state.settingsDirty;
  var balanceDirty = !!state.balanceDirty;
  return {
    settingsTag: dirty ? '设置未保存' : '',
    settingsTagAttr: dirty ? '' : ' style="display:none"',
    settingsNoteAttr: dirty ? '' : ' style="display:none"',
    buttonLabel: dirty ? '保存开关与备用关卡（未保存）' : '保存开关与备用关卡',
    buttonClass: dirty ? 'btn btn-p' : 'btn',
    balanceTag: balanceDirty ? '余额未保存' : '',
    balanceTagAttr: balanceDirty ? '' : ' style="display:none"',
  };
}

function cultivationSyncSettingsChrome() {
  if (typeof document === 'undefined' || typeof document.getElementById !== 'function') {
    return false;
  }
  var chrome = cultivationSettingsChrome(cultivationState());
  var button = document.getElementById('cultSettingsSave');
  var tag = document.getElementById('cultSettingsDirtyTag');
  var note = document.getElementById('cultSettingsNote');
  var balanceTag = document.getElementById('cultBalanceDirtyTag');
  if (button) {
    button.textContent = chrome.buttonLabel;
    button.className = chrome.buttonClass;
  }
  if (tag) {
    tag.textContent = chrome.settingsTag;
    if (tag.style) tag.style.display = chrome.settingsTagAttr ? 'none' : '';
  }
  if (note && note.style) note.style.display = chrome.settingsNoteAttr ? 'none' : '';
  if (balanceTag) {
    balanceTag.textContent = chrome.balanceTag;
    if (balanceTag.style) balanceTag.style.display = chrome.balanceTagAttr ? 'none' : '';
  }
  return !!(button || tag || note || balanceTag);
}

function cultivationPickOperator(operatorId) {
  var state = cultivationState();
  var entry = cultivationOperatorEntry(operatorId) || {};
  var snapshot = cultivationSnapshotEntry(operatorId);
  var levels = cultivationMaxLevels(operatorId);
  var currentElite = snapshot ? Number(snapshot.elite) || 0 : 0;
  var currentLevel = snapshot ? Number(snapshot.level) || 1 : 1;
  var targetElite = Math.min(currentElite + 1, levels.length - 1);
  state.draft = {
    id: '',
    operator_id: operatorId,
    operator_name: entry.name || (snapshot && snapshot.name) || operatorId,
    rarity: entry.rarity || (snapshot && snapshot.rarity) || 0,
    current_elite: currentElite,
    current_level: currentLevel,
    target_elite: targetElite,
    target_level: levels[targetElite],
    snapshot_at: snapshot ? String(cultivationSnapshot().scanned_at || '') : '',
    from_snapshot: !!snapshot,
  };
  state.operatorOpen = false;
  state.operatorQuery = '';
  state.error = '';
  // The draft form replaces the picker, so the page must repaint here.
  cultivationRenderIfVisible();
  return state.draft;
}

function cultivationEditGoal(goalId) {
  var goals = cultivationData().goals || [];
  for (var i = 0; i < goals.length; i += 1) {
    if (goals[i] && goals[i].id === goalId) {
      cultivationSetDraft(Object.assign({}, goals[i], {snapshot_at: '', from_snapshot: false}));
      if (S.page === 'cultivation') render();
      return cultivationState().draft;
    }
  }
  return null;
}

function cultivationApplySnapshot() {
  var state = cultivationState();
  if (!state.draft || !state.draft.operator_id) return false;
  var snapshot = cultivationSnapshotEntry(state.draft.operator_id);
  if (!snapshot) {
    state.error = '最近一次干员扫描里没有这名干员，请手动填写当前练度';
    return false;
  }
  state.draft.current_elite = Number(snapshot.elite) || 0;
  state.draft.current_level = Number(snapshot.level) || 1;
  state.draft.snapshot_at = String(cultivationSnapshot().scanned_at || '');
  state.draft.from_snapshot = true;
  state.error = '';
  if (S.page === 'cultivation') render();
  return true;
}

function cultivationDraftField(field, value) {
  var state = cultivationState();
  if (!state.draft) return;
  var numeric = ['current_elite', 'current_level', 'target_elite', 'target_level'];
  state.draft[field] = numeric.indexOf(field) >= 0 ? Number(value) : value;
  if (field === 'target_elite') {
    var cap = cultivationLevelCap(state.draft.operator_id, state.draft.target_elite);
    if (!(Number(state.draft.target_level) > 0) || Number(state.draft.target_level) > cap) {
      state.draft.target_level = cap;
    }
  }
  // Level text inputs keep focus: only elite changes rebuild the form.
  if ((field === 'current_elite' || field === 'target_elite') && S.page === 'cultivation') render();
}

function cultivationCancelDraft() {
  cultivationSetDraft(null);
  if (S.page === 'cultivation') render();
}

function cultivationIsRealDomNode(node) {
  return !!(node && typeof node.nodeType === 'number' && node.nodeType === 1);
}

function cultivationSyncOperatorPickerDom() {
  if (typeof document === 'undefined' || typeof document.getElementById !== 'function') {
    return false;
  }
  var mount = document.getElementById('cultOperatorPickerMount');
  if (!cultivationIsRealDomNode(mount)) return false;
  mount.innerHTML = cultivationPickerList(cultivationOperatorScan());
  return true;
}

function cultivationSearchOperators(query) {
  var state = cultivationState();
  state.operatorQuery = String(query || '');
  state.operatorOpen = !!state.operatorQuery.trim();
  state.activeSearchId = state.operatorOpen ? 'cultOperatorSearch' : '';
  if (S.page === 'cultivation') {
    if (!cultivationSyncOperatorPickerDom()) cultivationRenderIfVisible();
  }
}

// Game-facing profession labels; the catalog only stores the enum.
var CULTIVATION_PROFESSIONS = [
  ['PIONEER', '先锋'], ['WARRIOR', '近卫'], ['TANK', '重装'], ['SNIPER', '狙击'],
  ['CASTER', '术师'], ['MEDIC', '医疗'], ['SUPPORT', '辅助'], ['SPECIAL', '特种'],
];

function cultivationProfessionLabel(code) {
  var key = String(code || '');
  for (var i = 0; i < CULTIVATION_PROFESSIONS.length; i += 1) {
    if (CULTIVATION_PROFESSIONS[i][0] === key) return CULTIVATION_PROFESSIONS[i][1];
  }
  return key || '未知职业';
}

function cultivationPicker() {
  var state = cultivationState();
  if (!state.picker) {
    state.picker = {profession: '', rarity: '', elite: '', hideMaxed: false, showAll: false};
  }
  return state.picker;
}

function cultivationSetFilters(patch) {
  var state = cultivationState();
  var picker = cultivationPicker();
  Object.keys(patch || {}).forEach(function(key) {
    if (key === 'query') {
      state.operatorQuery = String(patch.query === undefined || patch.query === null ? '' : patch.query);
      return;
    }
    picker[key] = patch[key];
  });
  if (Object.prototype.hasOwnProperty.call(patch || {}, 'query')) {
    state.operatorOpen = !!String(state.operatorQuery || '').trim();
    state.activeSearchId = state.operatorOpen ? 'cultOperatorSearch' : '';
  }
  if (!cultivationSyncOperatorPickerDom()) cultivationRenderIfVisible();
  return picker;
}

// snapshot_status is authoritative when present; otherwise fall back to the raw
// snapshot fields and say the freshness is unconfirmed instead of claiming 最新.
function cultivationOperatorScan() {
  var status = (cultivationData().snapshot_status || {}).operators || null;
  var snapshot = cultivationData().operator_snapshot || {};
  var owned = Array.isArray(snapshot.own_opers) ? snapshot.own_opers : null;
  if (status) {
    return {
      available: !!status.available,
      scanned_at: String(status.scanned_at || ''),
      stale: !!status.stale,
      last_error: String(status.last_error || ''),
      known: true,
      owned: owned,
    };
  }
  return {
    available: !!owned && !!snapshot.scanned_at,
    scanned_at: String(snapshot.scanned_at || ''),
    stale: null,
    last_error: '',
    known: false,
    owned: owned,
  };
}

function cultivationInventoryScan() {
  var status = (cultivationData().snapshot_status || {}).inventory || null;
  var inventory = cultivationData().inventory || {};
  if (status) {
    return {
      available: !!status.available,
      scanned_at: String(status.scanned_at || ''),
      stale: !!status.stale,
      last_error: String(status.last_error || ''),
      known: true,
    };
  }
  return {
    available: !!inventory.available,
    scanned_at: String(inventory.scanned_at || ''),
    stale: null,
    last_error: '',
    known: false,
  };
}

function cultivationScanFreshness(info) {
  if (info.stale === true) return '已过期';
  if (info.stale === false) return '有效';
  return '时效未确认';
}

function cultivationOwnedEntries(scan) {
  var catalog = cultivationCatalog().operators;
  return (scan.owned || []).map(function(entry) {
    var meta = catalog[entry.id] || {};
    var levels = cultivationMaxLevels(entry.id);
    var elite = Number(entry.elite) || 0;
    var level = Number(entry.level) || 0;
    return {
      id: entry.id,
      name: meta.name || entry.name || entry.id,
      rarity: Number(meta.rarity || entry.rarity) || 0,
      profession: String(meta.profession || ''),
      elite: elite,
      level: level,
      levels: levels,
      maxed: elite >= levels.length - 1 && level >= levels[levels.length - 1],
      phaseMaxed: level >= levels[Math.min(elite, levels.length - 1)],
      owned: true,
    };
  });
}

function cultivationIndexEntries(scan) {
  var operators = cultivationCatalog().operators;
  var owned = {};
  (scan.owned || []).forEach(function(entry) { if (entry && entry.id) owned[entry.id] = entry; });
  return Object.keys(operators).map(function(operatorId) {
    var meta = operators[operatorId] || {};
    var own = owned[operatorId];
    var levels = cultivationMaxLevels(operatorId);
    var elite = own ? Number(own.elite) || 0 : null;
    var level = own ? Number(own.level) || 0 : null;
    return {
      id: operatorId,
      name: meta.name || operatorId,
      rarity: Number(meta.rarity) || 0,
      profession: String(meta.profession || ''),
      elite: elite,
      level: level,
      levels: levels,
      maxed: own ? (elite >= levels.length - 1 && level >= levels[levels.length - 1]) : false,
      phaseMaxed: own ? (level >= levels[Math.min(elite, levels.length - 1)]) : false,
      owned: !!own,
      ownershipKnown: !!scan.available,
    };
  });
}

function cultivationFilterEntries(entries) {
  var state = cultivationState();
  var picker = cultivationPicker();
  var query = String(state.operatorQuery || '').trim();
  return entries.filter(function(entry) {
    if (query && entry.name.indexOf(query) < 0 && entry.id.indexOf(query) < 0) return false;
    if (picker.profession && entry.profession !== picker.profession) return false;
    if (picker.rarity && String(entry.rarity) !== String(picker.rarity)) return false;
    if (picker.elite !== '' && String(entry.elite) !== String(picker.elite)) return false;
    if (picker.hideMaxed && entry.maxed) return false;
    return true;
  }).sort(function(left, right) {
    return right.rarity - left.rarity || String(left.name).localeCompare(String(right.name), 'zh-Hans-CN');
  });
}

function cultivationPickerCard(entry) {
  var tags = '';
  if (entry.maxed) tags += '<span class="cult-tag is-ready">已满级</span>';
  if (entry.phaseMaxed && !entry.maxed) tags += '<span class="cult-tag">本阶段满级</span>';
  if (!entry.owned && entry.ownershipKnown) tags += '<span class="cult-tag">未拥有</span>';
  if (!entry.owned && entry.ownershipKnown === false) {
    tags += '<span class="cult-tag">未确认是否拥有</span>';
  }
  var levelText = entry.elite === null
    ? '未扫描，无练度数据'
    : '精英' + entry.elite + ' ' + entry.level + '级';
  var pickable = entry.owned || entry.ownershipKnown === false;
  var action = pickable
    ? ' onclick="cultivationPickOperator(\'' + esc(entry.id) + '\')"'
    : ' disabled title="未拥有，无法选择"';
  return '<button class="cult-pick-card"' + action + '>' +
      '<div class="cult-pick-head">' +
        cultivationAvatarHtml(entry.id, entry.name) +
        '<div><span class="cult-pick-name">' + esc(entry.name) + '</span>' + tags +
        '<div class="cult-pick-meta">' + esc(entry.rarity + '★ · ' + cultivationProfessionLabel(entry.profession)) + '</div>' +
        '<div class="cult-pick-meta">' + esc(levelText) + '</div></div>' +
      '</div>' +
    '</button>';
}

function cultivationPickerControls(scan) {
  var state = cultivationState();
  var picker = cultivationPicker();
  var present = {};
  Object.keys(cultivationCatalog().operators).forEach(function(operatorId) {
    var code = (cultivationCatalog().operators[operatorId] || {}).profession;
    if (code) present[code] = true;
  });
  var professionOptions = ['<option value=""' + (picker.profession ? '' : ' selected') + '>全部职业</option>'];
  CULTIVATION_PROFESSIONS.forEach(function(pair) {
    if (!present[pair[0]] && picker.profession !== pair[0]) return;
    professionOptions.push('<option value="' + esc(pair[0]) + '"' +
      (picker.profession === pair[0] ? ' selected' : '') + '>' + esc(pair[1]) + '</option>');
  });
  var rarityOptions = ['<option value=""' + (picker.rarity ? '' : ' selected') + '>全部星级</option>'];
  [6, 5, 4, 3, 2, 1].forEach(function(rarity) {
    rarityOptions.push('<option value="' + rarity + '"' +
      (String(picker.rarity) === String(rarity) ? ' selected' : '') + '>' + rarity + '★</option>');
  });
  var eliteOptions = ['<option value=""' + (picker.elite === '' ? ' selected' : '') + '>全部精英阶段</option>'];
  [0, 1, 2].forEach(function(elite) {
    eliteOptions.push('<option value="' + elite + '"' +
      (String(picker.elite) === String(elite) ? ' selected' : '') + '>' + cultivationEliteName(elite) + '</option>');
  });
  return '<div class="cult-toolbar">' +
      '<div class="cult-field"><label>搜索名称</label>' +
        '<input id="cultOperatorSearch" type="search" value="' + esc(state.operatorQuery || '') + '" ' +
        'placeholder="输入干员名称" oninput="cultivationSearchOperators(this.value)"></div>' +
      '<div class="cult-field"><label>职业</label>' +
        '<select id="cultProfession" onchange="cultivationSetFilters({profession:this.value})">' +
        professionOptions.join('') + '</select></div>' +
      '<div class="cult-field"><label>星级</label>' +
        '<select id="cultRarity" onchange="cultivationSetFilters({rarity:this.value})">' +
        rarityOptions.join('') + '</select></div>' +
      '<div class="cult-field"><label>精英阶段</label>' +
        '<select id="cultElite" onchange="cultivationSetFilters({elite:this.value})">' +
        eliteOptions.join('') + '</select></div>' +
      '<div class="cult-switch"><input type="checkbox" id="cultHideMaxed" ' +
        (picker.hideMaxed ? 'checked' : '') +
        ' onchange="cultivationSetFilters({hideMaxed:this.checked})"><span>隐藏已满级</span></div>' +
      '<div class="cult-switch"><input type="checkbox" id="cultShowAll" ' +
        (picker.showAll ? 'checked' : '') +
        ' onchange="cultivationSetFilters({showAll:this.checked})"><span>显示全图鉴（含未拥有）</span></div>' +
    '</div>';
}

function cultivationPickerList(scan) {
  var picker = cultivationPicker();
  if (!scan.available && !picker.showAll) {
    return '<div class="cult-empty">尚未进行干员扫描：开始刷取前会先复核实际练度和库存。' +
      '你可以打开“显示全图鉴（含未拥有）”先添加目标；未确认是否拥有或未拥有的目标不会盲刷。</div>';
  }
  var entries = cultivationFilterEntries(
    picker.showAll ? cultivationIndexEntries(scan) : cultivationOwnedEntries(scan)
  );
  if (!entries.length) {
    return '<div class="cult-empty">没有符合筛选条件的干员。</div>';
  }
  return '<div class="cult-picker">' + entries.map(cultivationPickerCard).join('') + '</div>';
}

function cultivationPlanGoalEntry(goalId) {
  var plan = cultivationData().plan || {};
  var entries = Array.isArray(plan.goals) ? plan.goals : [];
  for (var i = 0; i < entries.length; i += 1) {
    if (entries[i] && entries[i].id === goalId) return entries[i];
  }
  return null;
}

function cultivationCraftName(craft) {
  craft = craft || {};
  return craft.name || cultivationItemName(craft.item_id || craft.id);
}

function cultivationCraftAmount(craft) {
  craft = craft || {};
  var candidates = [craft.quantity, craft.produced, craft.count];
  for (var i = 0; i < candidates.length; i += 1) {
    var value = Number(candidates[i]);
    if (isFinite(value) && value > 0) return value;
  }
  var crafts = Number(craft.craft_count);
  if (isFinite(crafts) && crafts > 0) {
    var output = Number(craft.output_count);
    return crafts * (isFinite(output) && output > 0 ? output : 1);
  }
  return 0;
}

function cultivationCraftStation(craft) {
  var station = (craft || {}).station;
  if (!station) return '';
  if (typeof station === 'string') return station;
  return String(station.name || station.label || station.station || '');
}

function cultivationCraftInputs(craft) {
  var inputs = (craft || {}).inputs || {};
  var batches = Number((craft || {}).craft_count);
  if (!isFinite(batches) || batches < 1) batches = 1;
  var parts = [];
  Object.keys(inputs).forEach(function(itemId) {
    var perCraft = Number(inputs[itemId]) || 0;
    if (perCraft > 0) {
      // inputs are per craft; show the whole-plan consumption.
      parts.push(cultivationItemName(itemId) + ' ×' + (perCraft * batches));
    }
  });
  return parts;
}

function cultivationCraftBatches(craft) {
  var batches = Number((craft || {}).craft_count);
  return isFinite(batches) && batches > 0 ? batches : 0;
}

function cultivationGoalMissing(goalId) {
  var plan = cultivationData().plan || {};
  var goal = (cultivationData().goals || []).find(function(item) {
    return item && item.id === goalId;
  });
  if (goal && goal.completed) return [];
  var entry = cultivationPlanGoalEntry(goalId);
  var missing = entry ? (entry.missing || {}) : (plan.missing || {});
  return Object.keys(missing).map(function(itemId) {
    return {name: cultivationItemName(itemId), count: Number(missing[itemId]) || 0};
  }).filter(function(entry) { return entry.count > 0; });
}

function cultivationGoalCrafts(goalId) {
  var plan = cultivationData().plan || {};
  var entry = cultivationPlanGoalEntry(goalId);
  var crafts = entry ? (entry.crafts || []) : (plan.crafts || []);
  var ready = entry ? (entry.ready === true || entry.status === 'ready') : !!plan.ready;
  var blocked = entry ? (entry.blocked || []) : (plan.blocked || []);
  return {
    ready: ready,
    items: crafts.map(function(craft) {
      return {
        name: cultivationCraftName(craft),
        count: cultivationCraftAmount(craft),
        station: cultivationCraftStation(craft),
        batches: cultivationCraftBatches(craft),
        inputs: cultivationCraftInputs(craft),
      };
    }).filter(function(item) { return item.count > 0; }),
    blocked: blocked.map(function(item) {
      item = item || {};
      return {
        item_id: String(item.item_id || ''),
        reason: String(item.reason || ''),
        count: Number(item.count) || 0,
        name: item.name || cultivationItemName(item.item_id),
        detail: String(item.detail || ''),
      };
    }),
  };
}

function cultivationAggregate() {
  var plan = cultivationData().plan || {};
  var missing = plan.missing || {};
  var crafts = plan.crafts || [];
  return {
    missing: Object.keys(missing).map(function(itemId) {
      return {name: cultivationItemName(itemId), count: Number(missing[itemId]) || 0};
    }).filter(function(entry) { return entry.count > 0; }),
    crafts: crafts.map(function(craft) {
      return {
        name: cultivationCraftName(craft),
        count: cultivationCraftAmount(craft),
      };
    }).filter(function(item) { return item.count > 0; }),
    blocked: (plan.blocked || []).map(function(item) {
      item = item || {};
      return {
        item_id: String(item.item_id || ''),
        reason: String(item.reason || ''),
        count: Number(item.count) || 0,
        name: item.name || cultivationItemName(item.item_id),
        detail: String(item.detail || ''),
      };
    }),
    ready: !!plan.ready,
  };
}

function cultivationGoalStatus(goal) {
  if (goal.completed) return '已完成';
  if (goal.enabled === false) return '已暂停';
  if (!cultivationInventoryVerified()) return '库存待确认';
  var planEntry = cultivationPlanGoalEntry(goal.id);
  if (!planEntry) return '待计算';
  if (planEntry.status === 'blocked') {
    var manual = (planEntry.blocked || []).some(function(item) {
      return item && item.reason === 'manual_exchange';
    });
    return manual ? '人工兑换阻塞' : '无法自动获取';
  }
  if (planEntry.status === 'missing') return '材料不足';
  if (planEntry.status === 'ready') return '原料齐备，待手动合成/培养';
  return '状态待确认';
}

// currency_unknown is an unconfirmed balance, not a missing material, so it
// must never read as "缺 0 份" or expose the raw item id.
function cultivationBlockedHtml(entries, prefix) {
  if (!entries || !entries.length) return '';
  var currencyParts = [];
  var manualParts = [];
  var otherParts = [];
  entries.forEach(function(entry) {
    entry = entry || {};
    var detail = entry.detail ? '（' + esc(entry.detail) + '）' : '';
    if (entry.reason === 'currency_unknown') {
      currencyParts.push('龙门币余额未知（请先识别仓库确认龙门币余额）');
      return;
    }
    if (entry.reason === 'manual_exchange') {
      manualParts.push(esc(entry.name || cultivationItemName(entry.item_id)) + detail);
      return;
    }
    otherParts.push(esc(entry.name || cultivationItemName(entry.item_id)) + detail);
  });
  var parts = [];
  if (currencyParts.length) parts.push(currencyParts.join('、'));
  if (manualParts.length) parts.push('需手动兑换：' + manualParts.join('、'));
  if (otherParts.length) parts.push(prefix + otherParts.join('、'));
  return parts.length ? '<div class="cult-error">' + parts.join('；') + '</div>' : '';
}

function cultivationErrorText(data) {
  if (!data) return '';
  if (data.error && typeof data.error === 'string') return data.error;
  if (data.error && data.error.message) return String(data.error.message);
  return '';
}

function cultivationErrorCode(data) {
  return (data && data.error && data.error.code) ? String(data.error.code) : '';
}

function cultivationStageOptions(current) {
  var stages = (cultivationCatalog().stages || []).map(function(entry) {
    return typeof entry === 'string' ? entry : String(entry.stage || '');
  }).filter(function(stage) { return !!stage; });
  var value = String(current || '');
  if (value && stages.indexOf(value) < 0) stages.unshift(value);
  if (!stages.length) stages.push('1-7');
  return stages.map(function(stage) {
    return '<option value="' + esc(stage) + '"' + (stage === value ? ' selected' : '') + '>' +
      esc(stage) + '</option>';
  }).join('');
}

function applyCultivationPayload(payload, options) {
  var state = cultivationState();
  var preserveDraft = !!(options && options.preserveDraft);
  var keepMessages = !!(options && options.keepMessages);
  state.data = payload || {};
  state.loaded = true;
  state.loading = false;
  if (!preserveDraft) state.draft = null;
  if (!state.settingsDirty || !preserveDraft) {
    state.settingsDraft = cultivationSettings();
    state.settingsDirty = false;
  }
  if (!state.balanceDirty || !preserveDraft) {
    var currency = cultivationCurrency();
    state.balanceDraft = currency.available ? String(currency.balance) : '';
    state.balanceDirty = false;
  }
  if (!keepMessages) state.error = '';
  return state;
}

// Single GET path with a sequence guard: a newer write invalidates any
// in-flight read so a stale payload can never overwrite fresher state.
async function cultivationFetch() {
  var state = cultivationState();
  state.fetchSeq += 1;
  var seq = state.fetchSeq;
  var response = await apiResponse('cultivation');
  if (seq !== cultivationState().fetchSeq) return {stale: true};
  if (!response.ok || !response.data) {
    return {error: cultivationErrorText(response.data) || '读取培养数据失败'};
  }
  return {data: response.data};
}

async function loadCultivation(options) {
  var state = cultivationState();
  var preserveDraft = !(options && options.clearDraft);
  if (state.loading) return state.data;
  state.loading = true;
  try {
    var result = await cultivationFetch();
    if (result.error) {
      state.error = result.error;
      return state.data;
    }
    if (!result.data) return state.data;
    applyCultivationPayload(result.data, {preserveDraft: preserveDraft});
    state.pollFingerprint = cultivationFingerprint(result.data);
    return state.data;
  } finally {
    state.loading = false;
    cultivationRenderIfVisible();
  }
}

function cultivationRenderIfVisible() {
  if (S.page !== 'cultivation') return;
  var hasScroll = typeof window !== 'undefined' && typeof window.scrollY === 'number';
  var prevScrollY = hasScroll ? window.scrollY : null;
  render();
  if (prevScrollY !== null && typeof window.scrollTo === 'function') {
    try { window.scrollTo(0, prevScrollY); } catch (_) {}
  }
}

// Every write re-reads the persisted payload with a real GET. The POST response
// is only used to learn the new goal id, never as proof of what was stored.
async function cultivationReadBack() {
  var result = await cultivationFetch();
  return result.data || null;
}

function cultivationGoalSaved(goals, draft, savedId) {
  if (!Array.isArray(goals) || !draft) return false;
  var expected = {
    operator_id: draft.operator_id,
    current_elite: Number(draft.current_elite),
    current_level: Number(draft.current_level),
    target_elite: Number(draft.target_elite),
    target_level: Number(draft.target_level),
  };
  return goals.some(function(goal) {
    if (!goal) return false;
    if (savedId && goal.id !== savedId) return false;
    if (!savedId && goal.operator_id !== expected.operator_id) return false;
    return Number(goal.current_elite) === expected.current_elite &&
      Number(goal.current_level) === expected.current_level &&
      Number(goal.target_elite) === expected.target_elite &&
      Number(goal.target_level) === expected.target_level;
  });
}

// ─── Background refresh (page has to show the runner's inventory/runtime) ───
var cultivationPollTimer = null;
var CULTIVATION_POLL_MS = 5000;

function cultivationFingerprint(data) {
  if (!data) return '';
  return cultivationFingerprintValue({
    revision: data.revision,
    settings: data.settings,
    goals: data.goals,
    mastery: data.mastery,
    inventory: data.inventory,
    currency: data.currency,
    runtime: data.runtime,
    plan: data.plan,
    snapshot_status: cultivationFingerprintSnapshotStatus(data.snapshot_status),
    operator_snapshot: data.operator_snapshot,
    notifications: data.notifications,
  });
}

function cultivationFingerprintSnapshotStatus(status) {
  var value = status || {};
  var result = {};
  Object.keys(value).sort().forEach(function(key) {
    if (key !== 'inventory' && key !== 'operators') {
      result[key] = value[key];
      return;
    }
    var scan = value[key] || {};
    result[key] = {
      available: !!scan.available,
      scanned_at: String(scan.scanned_at || ''),
      stale: scan.stale,
      last_error: String(scan.last_error || ''),
    };
  });
  return result;
}

function cultivationFingerprintValue(value) {
  if (value === undefined) return 'undefined';
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) {
    return '[' + value.map(cultivationFingerprintValue).join(',') + ']';
  }
  return '{' + Object.keys(value).sort().map(function(key) {
    return JSON.stringify(key) + ':' + cultivationFingerprintValue(value[key]);
  }).join(',') + '}';
}

function cultivationCanRepaint() {
  var state = cultivationState();
  return !state.draft && !state.settingsDirty && !state.operatorOpen &&
    !state.balanceDirty && !state.saving && !state.settingsSaving &&
    !state.balanceSaving && !state.goalBusyId && !state.runPending &&
    !state.masteryDraft && !state.masteryQuery && !state.masterySaving && !state.masteryBusyId;
}

async function confirmCultivationBalance() {
  var state = cultivationState();
  if (state.balanceSaving) return false;
  var parsed = cultivationBalanceValue();
  state.error = '';
  if (!parsed.ok) {
    state.error = parsed.error;
    cultivationRenderIfVisible();
    return false;
  }
  state.balanceSaving = true;
  state.fetchSeq += 1; // invalidate any in-flight read before writing
  cultivationRenderIfVisible();
  try {
    var response = await apiResponse('cultivation/balance', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({balance: parsed.value, revision: (state.data || {}).revision}),
    });
    if (!response.ok) {
      state.error = cultivationErrorText(response.data) || '余额确认失败';
      return false;
    }
    var readData = await cultivationReadBack();
    if (!readData) {
      state.error = '余额读取失败，请刷新后重试';
      return false;
    }
    var currency = readData.currency || {};
    if (!currency.available || Number(currency.balance) !== parsed.value) {
      state.error = '余额读回与提交不一致，请刷新后重试';
      return false;
    }
    state.balanceDirty = false;
    applyCultivationPayload(readData, {preserveDraft: true, keepMessages: true});
    state.notice = '龙门币余额已确认';
    return true;
  } finally {
    state.balanceSaving = false;
    cultivationRenderIfVisible();
  }
}

async function refreshCultivation() {
  var state = cultivationState();
  if (state.loading) return false;
  state.loading = true;
  cultivationRenderIfVisible();
  try {
    var result = await cultivationFetch();
    if (result.error) {
      state.error = result.error;
      return false;
    }
    if (!result.data) return false;
    applyCultivationPayload(result.data, {preserveDraft: true, keepMessages: true});
    state.pollFingerprint = cultivationFingerprint(result.data);
    return true;
  } finally {
    state.loading = false;
    cultivationRenderIfVisible();
  }
}

async function cultivationPoll() {
  var state = cultivationState();
  if (S.page !== 'cultivation') return false;
  if (typeof document !== 'undefined' && document.hidden) return false;
  if (state.loading || state.saving || state.settingsSaving || state.goalBusyId || state.runPending) {
    return false;
  }
  var result = await cultivationFetch();
  if (!result.data) return false;
  var fingerprint = cultivationFingerprint(result.data);
  var changed = fingerprint !== state.pollFingerprint;
  state.pollFingerprint = fingerprint;
  // Keep the DOM (and the user's input focus) while a draft or settings edit
  // is open; the state still refreshes so a later repaint shows fresh data.
  var holdDom = !cultivationCanRepaint();
  applyCultivationPayload(result.data, {preserveDraft: true, keepMessages: true});
  if (changed && !holdDom) cultivationRenderIfVisible();
  return changed;
}

function cultivationStartPolling() {
  cultivationStopPolling();
  if (typeof setInterval !== 'function') return;
  cultivationPollTimer = setInterval(cultivationPoll, CULTIVATION_POLL_MS);
}

function cultivationStopPolling() {
  if (cultivationPollTimer === null) return;
  if (typeof clearInterval === 'function') clearInterval(cultivationPollTimer);
  cultivationPollTimer = null;
}

async function saveCultivationGoal() {
  var state = cultivationState();
  if (state.saving) return false;
  if (!state.draft) return false;
  var draft = state.draft;
  state.error = '';
  var errors = cultivationValidateDraft(draft);
  if (errors.length) {
    state.error = errors.join('；');
    cultivationRenderIfVisible();
    return false;
  }
  state.saving = true;
  state.fetchSeq += 1; // invalidate any in-flight read before writing
  cultivationRenderIfVisible();
  try {
    var body = {
      operator_id: draft.operator_id,
      current_elite: Number(draft.current_elite),
      current_level: Number(draft.current_level),
      target_elite: Number(draft.target_elite),
      target_level: Number(draft.target_level),
      revision: (state.data || {}).revision,
    };
    if (draft.id) body.id = draft.id;
    if (draft.completed) {
      // Editing an auto-completed goal with a higher target resumes it.
      // The service still guards current >= target and will answer with a
      // ValueError if the submitted target is not actually higher.
      body.completed = false;
      body.enabled = true;
    }
    var response = await apiResponse('cultivation/goal', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      state.error = cultivationErrorText(response.data) || '保存失败';
      return false;
    }
    var savedId = (response.data && response.data.goal && response.data.goal.id) || draft.id || '';
    var readData = await cultivationReadBack();
    if (!readData) {
      state.error = '保存结果读取失败，草稿已保留';
      return false;
    }
    if (!cultivationGoalSaved(readData.goals, draft, savedId)) {
      state.error = '保存后读回与提交内容不一致，草稿已保留';
      return false;
    }
    // Only the goal draft is consumed here: unsaved settings/balance edits
    // must survive a goal save and never be written implicitly.
    applyCultivationPayload(readData, {preserveDraft: true});
    state.draft = null;
    state.notice = '目标已保存，可继续添加下一位';
    return true;
  } finally {
    state.saving = false;
    cultivationRenderIfVisible();
  }
}

async function setCultivationGoalState(goalId, patch) {
  var state = cultivationState();
  if (state.goalBusyId) return false;
  state.goalBusyId = goalId;
  state.error = '';
  state.fetchSeq += 1;
  cultivationRenderIfVisible();
  try {
    var body = Object.assign({id: goalId, revision: (state.data || {}).revision}, patch || {});
    var response = await apiResponse('cultivation/goal-state', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      state.error = cultivationErrorText(response.data) || '目标状态更新失败';
      return false;
    }
    var readData = await cultivationReadBack();
    if (!readData) {
      state.error = '目标状态读取失败，请刷新页面确认';
      return false;
    }
    var savedGoal = null;
    (readData.goals || []).forEach(function(goal) {
      if (goal && goal.id === goalId) savedGoal = goal;
    });
    var mismatch = !savedGoal;
    if (savedGoal && patch) {
      Object.keys(patch).forEach(function(key) {
        if (key === 'move') return;
        if (savedGoal[key] !== patch[key]) mismatch = true;
      });
    }
    if (mismatch) {
      state.error = '目标状态读回不一致，请刷新页面后重试';
      return false;
    }
    applyCultivationPayload(readData, {preserveDraft: true});
    state.notice = patch && patch.move ? '目标优先级顺序已更新'
      : (patch && patch.completed ? '目标已标记完成'
      : (patch && patch.enabled === false ? '目标已暂停' : '目标已恢复'));
    return true;
  } finally {
    state.goalBusyId = '';
    cultivationRenderIfVisible();
  }
}

async function saveCultivationSettings() {
  var state = cultivationState();
  if (state.settingsSaving) return false;
  var draft = state.settingsDraft || cultivationSettings();
  var current = cultivationSettings();
  // Only send what changed so an unrelated stale fallback value cannot fail the save.
  var payload = {revision: (state.data || {}).revision};
  if (!!draft.enabled !== !!current.enabled) payload.enabled = !!draft.enabled;
  if (String(draft.fallback_stage || '') !== String(current.fallback_stage || '')) {
    payload.fallback_stage = String(draft.fallback_stage || '');
  }
  if (payload.enabled === undefined && payload.fallback_stage === undefined) {
    payload.enabled = !!draft.enabled;
  }
  state.settingsSaving = true;
  state.error = '';
  state.fetchSeq += 1;
  cultivationRenderIfVisible();
  try {
    var response = await apiResponse('cultivation/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      state.error = cultivationErrorText(response.data) || '设置保存失败';
      return false;
    }
    var readData = await cultivationReadBack();
    if (!readData) {
      state.error = '设置读取失败，草稿已保留';
      return false;
    }
    var saved = readData.settings || {};
    var mismatch = false;
    if ('enabled' in payload && !!saved.enabled !== !!payload.enabled) mismatch = true;
    if ('fallback_stage' in payload &&
        String(saved.fallback_stage || '') !== String(payload.fallback_stage)) mismatch = true;
    if (mismatch) {
      state.error = '设置保存后读回不一致，草稿已保留';
      return false;
    }
    state.settingsDirty = false;
    applyCultivationPayload(readData, {preserveDraft: true});
    state.notice = '设置已保存';
    return true;
  } finally {
    state.settingsSaving = false;
    cultivationRenderIfVisible();
  }
}

async function startCultivationRun(mode) {
  var state = cultivationState();
  if (state.runPending) return false;
  if (mode !== 'scan' && mode !== 'run') return false;
  state.runPending = true;
  state.error = '';
  state.fetchSeq += 1;
  cultivationRenderIfVisible();
  try {
    var response = await apiResponse('cultivation/' + mode, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}',
    });
    if (!response.ok || !response.data || response.data.ok === false) {
      state.error = cultivationErrorText(response.data) || '任务未能启动';
      return false;
    }
    state.notice = mode === 'scan' ? '库存识别已开始' : '培养刷取已开始';
    await loadCultivation({preserveDraft: true});
    return true;
  } finally {
    state.runPending = false;
    cultivationRenderIfVisible();
  }
}

function renderCultivation() {
  var state = cultivationState();
  if (!state.data) {
    return '<section class="card"><div class="section-title">干员培养目标</div>' +
      '<div class="cult-empty">' + esc(state.error || '正在读取培养数据…') + '</div></section>';
  }
  var settings = state.settingsDraft || cultivationSettings();
  var chrome = cultivationSettingsChrome(state);
  var runtime = cultivationRuntime();
  var inventory = cultivationData().inventory || {};
  var inventoryVerified = cultivationInventoryVerified();
  var currency = cultivationCurrency();
  var operatorScan = cultivationOperatorScan();
  var inventoryScan = cultivationInventoryScan();
  var aggregate = cultivationAggregate();
  var goals = cultivationData().goals || [];
  var runtimeLabel = CULTIVATION_RUNTIME_LABELS[runtime.status] || (runtime.status || '空闲');
  var runtimeLine = '运行状态：' + runtimeLabel +
    (runtime.message ? ' · ' + runtime.message : '') +
    (runtime.updated_at ? ' · 更新于 ' + runtime.updated_at : '');
  var operatorLine = operatorScan.available
    ? '干员扫描时间：' + (operatorScan.scanned_at || '未知') + ' · ' + cultivationScanFreshness(operatorScan) +
      (operatorScan.last_error ? '（上次扫描失败）' : '')
    : '干员扫描：未做干员扫描（请手动执行一次）';
  var inventoryLine = inventoryScan.available
    ? '库存识别时间：' + (inventoryScan.scanned_at || '未识别') + ' · ' + cultivationScanFreshness(inventoryScan) +
      (inventoryScan.last_error ? '（上次识别失败）' : '')
    : '库存未确认 · 上次识别时间：' + (inventoryScan.scanned_at || '无') + '（旧快照，仅供参考）';
  var currencyLine = currency.available
    ? '龙门币余额（余额来源：' + cultivationCurrencySourceLabel(currency) + '）：' + currency.balance +
      (currency.confirmed_at ? '（确认于 ' + currency.confirmed_at + '）' : '') +
      ' · 源石补货等可能花钱的操作及手动培养后，需重新确认'
    : '龙门币余额未知（余额来源：未知；请先识别仓库确认龙门币余额；手动填写仅作临时校正），暂不按缺口自动刷钱（仍会执行你指定的备用关卡）';

  var goalCards = goals.map(function(goal, index) {
    return renderCultivationGoal(goal, index, goals.length);
  }).join('');

  return '<section class="card"><div class="section-title">干员培养目标</div>' +
    '<div class="cult-toolbar">' +
      '<div class="cult-field"><label>启用培养刷取</label>' +
        '<div class="cult-switch"><input type="checkbox" id="cultEnabled" ' + (settings.enabled ? 'checked' : '') +
        ' onchange="cultivationSetSettings({enabled:this.checked})">' +
        '<span>保存启用后，日常优先刷培养缺口；齐料或本未开放时刷下方备用关卡。关闭后按周计划执行。</span></div>' +
        '<span class="cult-tag" id="cultSettingsDirtyTag"' + chrome.settingsTagAttr + '>' +
          esc(chrome.settingsTag) + '</span></div>' +
      '<div class="cult-field"><label>备用关卡</label>' +
        '<select id="cultFallback" onchange="cultivationSetSettings({fallback_stage:this.value})">' +
        cultivationStageOptions(settings.fallback_stage) + '</select></div>' +
      '<div class="cult-field"><label>龙门币余额（临时校正）</label>' +
        '<div class="cult-actions" style="margin-top:0">' +
          '<input id="cultBalance" type="text" inputmode="numeric" style="min-width:150px" value="' +
            esc(cultivationBalanceInputValue()) + '" placeholder="未知" ' +
            'oninput="cultivationSetBalance(this.value)">' +
          '<button class="btn" onclick="confirmCultivationBalance()" ' +
            (state.balanceSaving ? 'disabled' : '') + '>确认余额</button>' +
        '</div>' +
        '<span class="cult-note">成功仓库识别会覆盖此处的临时校正。</span>' +
        '<span class="cult-tag" id="cultBalanceDirtyTag"' + chrome.balanceTagAttr + '>' +
          esc(chrome.balanceTag) + '</span></div>' +
      '<div class="cult-field"><label>设置</label><div class="cult-actions">' +
        '<button class="' + chrome.buttonClass + '" id="cultSettingsSave" onclick="saveCultivationSettings()" ' +
        (state.settingsSaving ? 'disabled' : '') + '>' + esc(chrome.buttonLabel) + '</button>' +
        '<button class="btn" onclick="refreshCultivation()" ' + (state.loading ? 'disabled' : '') +
        '>刷新状态</button>' +
      '</div>' +
      '<div class="cult-note" id="cultSettingsNote"' + chrome.settingsNoteAttr + '>' +
        '开关/备用关卡改动尚未保存；保存目标不会一并保存设置。</div></div>' +
      '<div class="cult-field"><label>运行</label><div class="cult-actions">' +
        '<button class="btn" onclick="startCultivationRun(\'scan\')" ' + (state.runPending ? 'disabled' : '') +
        '>库存识别</button>' +
        '<button class="btn btn-p" onclick="startCultivationRun(\'run\')" ' + (state.runPending ? 'disabled' : '') +
        '>立即运行</button>' +
      '</div>' +
      '<div class="cult-note">开始前复核实际练度和库存，按最新缺口刷取；任务结束刷新。</div></div>' +
    '</div>' +
    '<div class="cult-note" style="margin-top:12px">' + esc(runtimeLine) + '<br>' + esc(operatorLine) + '<br>' + esc(inventoryLine) +
      ' · 备用关卡：目标关卡未开放或目标已齐备时，使用备用关卡。<br>' + esc(currencyLine) + '</div>' +
    (state.error ? '<div class="cult-error">' + esc(state.error) + '</div>' : '') +
    (state.notice ? '<div class="cult-result">' + esc(state.notice) + '</div>' : '') +
    (inventoryVerified ? '' :
      '<div class="cult-warn"><b>库存未确认</b>：下面的缺口和齐料判断来自上一次库存快照，不能作为齐料依据。' +
        '请先点“库存识别”，识别完成后再看缺口与通知。</div>') +
    (!operatorScan.available ?
      '<div class="cult-warn"><b>尚未进行干员扫描</b>：可以先从全图鉴添加目标；开始刷取前会先复核实际练度与库存，' +
        '不拥有或未确认的目标不会盲刷。</div>' : '') +
    (operatorScan.available && operatorScan.stale === true ?
      '<div class="cult-warn">干员扫描已过期：开始刷取前会重新复核实际练度，已达到目标的目标会自动标记完成。</div>' : '') +
    (currency.available ? '' :
      '<div class="cult-warn"><b>龙门币余额未知</b>：请先识别仓库确认龙门币；手动填写只作临时校正，下一次成功仓库识别会覆盖。' +
        '确认之前不会按缺口自动刷钱——该限制只针对缺口规划，仍会执行你指定的备用关卡。' +
        '资源齐料判断与齐料通知需要可信的仓库余额；目标已实际达成的通知不受余额影响。' +
        '源石补货等可能花钱的操作及手动培养后，请重新识别仓库。</div>') +
    '<div class="cult-warn">原料齐全不等同于已经升级。首版只消耗自然理智：不吃理智药、不使用源石。' +
      '刷齐材料后由系统通知你，升级与合成需要你手动在游戏内完成。</div>' +
    '<div class="cult-grid" style="margin-top:18px">' +
      '<div class="cult-card">' + renderCultivationDraft() + '</div>' +
      '<div class="cult-card">' + renderCultivationPlan(aggregate) + '</div>' +
    '</div>' +
    '<div class="cult-card" style="margin-top:18px"><div class="cult-section-title">目标列表（按优先级顺序 · 可自由排序）</div>' +
      (goalCards || '<div class="cult-empty">还没有培养目标，先在上方选择干员。</div>') +
    '</div></section>' +
    renderCultivationMasteryWindow();
}

function renderCultivationDraft() {
  var state = cultivationState();
  var scan = cultivationOperatorScan();
  if (!state.draft) {
    return '<div class="cult-section-title">选择干员</div>' +
      cultivationPickerControls(scan) +
      '<div class="cult-note" style="margin-top:12px">默认只列出已拥有的干员；点击卡片会带入当前精英/等级，' +
      '你只需要设置目标练度。全图鉴是显式切换，未拥有干员不可选。</div>' +
      '<div id="cultOperatorPickerMount">' + cultivationPickerList(scan) + '</div>';
  }
  var draft = state.draft;
  var levels = cultivationMaxLevels(draft.operator_id);
  var eliteOptions = levels.map(function(_cap, elite) {
    return '<option value="' + elite + '"' + (Number(draft.current_elite) === elite ? ' selected' : '') + '>' +
      cultivationEliteName(elite) + '</option>';
  }).join('');
  var targetEliteOptions = levels.map(function(_cap, elite) {
    return '<option value="' + elite + '"' + (Number(draft.target_elite) === elite ? ' selected' : '') + '>' +
      cultivationEliteName(elite) + '（上限 ' + cultivationLevelCap(draft.operator_id, elite) + '）</option>';
  }).join('');
  var errors = cultivationValidateDraft(draft);
  var completedNote = draft.completed
    ? '<div class="cult-note">该目标已达成：会先通知并继续用备用关卡；编辑更高目标只影响后续继续培养，' +
      '保存后按新目标接续。</div>'
    : '';
  var snapshotNote = draft.from_snapshot && draft.snapshot_at
    ? '已带入 ' + esc(draft.snapshot_at) + ' 的干员扫描结果'
    : '未带入扫描结果，请手动填写当前练度';
  return '<div class="cult-section-title">' + (draft.id ? '编辑目标' : '新增目标') +
      ' · ' + esc(draft.operator_name ||
        cultivationOperatorName(draft.operator_id, cultivationPlanGoalEntry(draft.id))) + '</div>' +
    '<div class="cult-toolbar" style="margin-top:12px">' +
      '<div class="cult-field"><label>当前精英</label>' +
        '<select id="cultCurrentElite" onchange="cultivationDraftField(\'current_elite\', this.value)">' + eliteOptions + '</select></div>' +
      '<div class="cult-field"><label>当前等级</label>' +
        '<input type="text" id="cultCurrentLevel" value="' + esc(draft.current_level) + '" ' +
        'oninput="cultivationDraftField(\'current_level\', this.value)"></div>' +
      '<div class="cult-field"><label>目标精英</label>' +
        '<select id="cultTargetElite" onchange="cultivationDraftField(\'target_elite\', this.value)">' + targetEliteOptions + '</select></div>' +
      '<div class="cult-field"><label>目标等级</label>' +
        '<input type="text" id="cultTargetLevel" value="' + esc(draft.target_level) + '" ' +
        'oninput="cultivationDraftField(\'target_level\', this.value)"></div>' +
    '</div>' +
    '<div class="cult-note" style="margin-top:10px">' + snapshotNote + '</div>' +
    completedNote +
    (errors.length ? '<div class="cult-error">' + esc(errors.join('；')) + '</div>' : '') +
    '<div class="cult-actions">' +
      '<button class="btn" onclick="cultivationApplySnapshot()">带入扫描练度</button>' +
      '<button class="btn btn-p" onclick="saveCultivationGoal()" ' + (state.saving ? 'disabled' : '') + '>保存目标</button>' +
      '<button class="btn" onclick="cultivationCancelDraft()">重新选择干员</button>' +
    '</div>';
}

function renderCultivationGoalDirectMaterials(planEntry) {
  if (!planEntry || !planEntry.requirements || typeof planEntry.requirements !== 'object') return '';
  var reqs = planEntry.requirements;
  var missingMap = planEntry.missing || {};
  var craftsList = Array.isArray(planEntry.crafts) ? planEntry.crafts : [];
  var craftById = {};
  craftsList.forEach(function(c) {
    if (c && c.item_id) craftById[String(c.item_id)] = c;
  });
  function collectMissingInputs(itemId, seen) {
    seen = seen || {};
    if (seen[itemId]) return [];
    seen[itemId] = true;
    var found = [];
    var c = craftById[itemId];
    if (!c || !c.inputs) return found;
    Object.keys(c.inputs).forEach(function(inId) {
      if (Number(missingMap[inId]) > 0) {
        found.push(cultivationItemName(inId) + ' ×' + Number(missingMap[inId]));
      }
      collectMissingInputs(inId, seen).forEach(function(sub) {
        if (found.indexOf(sub) < 0) found.push(sub);
      });
    });
    return found;
  }
  var pills = [];
  var order = {lack: 0, craft: 1, ok: 2};
  var items = [];
  Object.keys(reqs).forEach(function(itemId) {
    if (itemId === '4001' || itemId === 'EXP') return;
    var need = Number(reqs[itemId]) || 0;
    if (need <= 0) return;
    var name = cultivationItemName(itemId);
    var directMiss = Number(missingMap[itemId]) || 0;
    var craftEntry = craftById[itemId];
    if (directMiss > 0) {
      items.push({
        kind: 'lack',
        html: '<span class="cult-lack-pill">❌ ' + esc(name) + ' ×' + need + '（缺 ' + directMiss + '）</span>',
      });
      return;
    }
    if (craftEntry) {
      var craftQty = Number(craftEntry.quantity || craftEntry.produced) || 0;
      var haveStock = Math.max(0, need - craftQty);
      var missSubs = collectMissingInputs(itemId, {});
      if (missSubs.length > 0) {
        items.push({
          kind: 'lack',
          html: '<span class="cult-lack-pill">❌ ' + esc(name) + ' ×' + need +
            '（缺下位：' + esc(missSubs.join('、')) + '）</span>',
        });
      } else {
        items.push({
          kind: 'craft',
          html: '<span class="cult-craft-pill">🔧 ' + esc(name) + ' ×' + need +
            '（现成 ' + haveStock + ' + 可合 ' + craftQty + ' · 合成后充足）</span>',
        });
      }
      return;
    }
    items.push({
      kind: 'ok',
      html: '<span class="cult-ok-pill">✅ ' + esc(name) + ' ×' + need + '（现成充足）</span>',
    });
  });
  if (!items.length) return '';
  items.sort(function(a, b) { return order[a.kind] - order[b.kind]; });
  pills = items.map(function(it) { return it.html; });
  return '<div class="cult-goal-meta">目标材料状态：' + pills.join('') + '</div>';
}

function renderCultivationGoal(goal, index, totalCount) {
  var total = typeof totalCount === 'number' ? totalCount : ((cultivationData().goals || []).length || 1);
  var planEntry = cultivationPlanGoalEntry(goal.id);
  var goalName = goal.operator_name || cultivationOperatorName(goal.operator_id, planEntry);
  var crafts = cultivationGoalCrafts(goal.id);
  var missing = cultivationGoalMissing(goal.id);
  var classes = 'cult-goal' + (goal.completed ? ' is-completed' : '');
  var statusText = cultivationGoalStatus(goal);
  var statusTagClass = 'cult-tag';
  if (!goal.completed && planEntry) {
    if (planEntry.status === 'missing' || planEntry.status === 'blocked') statusTagClass += ' is-lack';
    else if (planEntry.status === 'ready') statusTagClass += ' is-ready';
  }
  var tags = '<span class="' + statusTagClass + '">' + statusText + '</span>' +
    (crafts.ready && !goal.completed && cultivationInventoryVerified()
      ? '<span class="cult-tag is-ready">' + (crafts.items.length ? '材料已齐 · 合成后充足' : '材料已齐') + '</span>' : '');
  var missingLine = goal.completed
    ? '无需处理'
    : !cultivationInventoryVerified()
      ? '库存待确认'
      : missing.length
        ? missing.map(function(entry) {
            return '<span class="cult-lack-pill">❌ 缺 ' + esc(entry.name) + ' ×' + entry.count + '</span>';
          }).join(' ')
        : (planEntry && planEntry.status === 'ready'
          ? '<span class="cult-ok-pill">✅ 无材料缺口（原料齐备，合成后充足，待手动合成/培养）</span>'
          : planEntry && planEntry.status === 'blocked'
            ? '<span class="cult-lack-pill">⚠️ 存在人工或其他获取阻塞</span>'
            : '暂未计算缺口');
  var directMaterialsLine = (!goal.completed && cultivationInventoryVerified())
    ? renderCultivationGoalDirectMaterials(planEntry)
    : '';
  var craftLine = crafts.items.length
    ? crafts.items.map(function(entry) {
        var detail = [];
        if (entry.station) detail.push(entry.station);
        if (entry.batches > 1) detail.push('合成 ' + entry.batches + ' 次');
        if (entry.inputs && entry.inputs.length) detail.push('需 ' + entry.inputs.join(' + '));
        return esc(entry.name) + ' ×' + entry.count +
          (detail.length ? '（' + detail.map(function(value) { return esc(value); }).join('，') + '）' : '');
      }).join('、')
    : '无';
  var blockedLine = cultivationBlockedHtml(crafts.blocked, '无法自动获取：');
  var completedNote = goal.completed
    ? '<div class="cult-note">目标已达成，将通知并继续备用关卡；如需继续培养，可编辑更高目标。</div>'
    : '';
  var busy = cultivationState().goalBusyId === goal.id ? ' disabled' : '';
  var topDisabled = (busy || index === 0) ? ' disabled' : '';
  var upDisabled = (busy || index === 0) ? ' disabled' : '';
  var downDisabled = (busy || index >= total - 1) ? ' disabled' : '';
  var actions = '<button class="btn" title="置顶最高优先级" onclick="setCultivationGoalState(\'' + esc(goal.id) + '\', {move:\'top\'})"' + topDisabled + '>⏫ 置顶</button>' +
    '<button class="btn" title="上移一位" onclick="setCultivationGoalState(\'' + esc(goal.id) + '\', {move:\'up\'})"' + upDisabled + '>⬆️ 上移</button>' +
    '<button class="btn" title="下移一位" onclick="setCultivationGoalState(\'' + esc(goal.id) + '\', {move:\'down\'})"' + downDisabled + '>⬇️ 下移</button>' +
    '<button class="btn" onclick="cultivationEditGoal(\'' + esc(goal.id) + '\')"' + busy + '>编辑目标</button>';
  if (goal.completed) {
    // No blind resume: the service rejects current >= target, so the goal must
    // be edited to a higher target instead.
    actions += '';
  } else {
    actions += goal.enabled === false
      ? '<button class="btn" onclick="setCultivationGoalState(\'' + esc(goal.id) + '\', {enabled:true})"' + busy + '>恢复</button>'
      : '<button class="btn" onclick="setCultivationGoalState(\'' + esc(goal.id) + '\', {enabled:false})"' + busy + '>暂停</button>';
    actions += '<button class="btn" onclick="setCultivationGoalState(\'' + esc(goal.id) + '\', {completed:true})"' + busy + '>完成</button>';
  }
  return '<div class="' + classes + '">' +
    '<div class="cult-goal-head"><div>' +
      '<div class="cult-goal-name">' + (index + 1) + '. ' + esc(goalName) + tags + '</div>' +
      '<div class="cult-goal-meta">当前 ' + esc(cultivationEliteName(goal.current_elite) + ' ' + goal.current_level + '级') +
        ' → 目标 ' + esc(cultivationEliteName(goal.target_elite) + ' ' + goal.target_level + '级') +
        ' · 优先级 ' + (index + 1) + '（支持置顶/上下调整顺序）</div>' +
      directMaterialsLine +
      '<div class="cult-goal-meta">缺口：' + missingLine + '</div>' +
      '<div class="cult-goal-meta">手动合成清单：' + craftLine + '</div>' +
      blockedLine +
      completedNote +
    '</div></div>' +
    '<div class="cult-goal-actions">' + actions + '</div>' +
  '</div>';
}

function renderCultivationPlan(aggregate) {
  var missing = aggregate.missing.length
    ? '<ul class="cult-list">' + aggregate.missing.map(function(entry) {
        return '<li><span class="cult-lack-pill">❌ 缺 ' + esc(entry.name) + ' ×' + entry.count + '</span></li>';
      }).join('') + '</ul>'
    : !cultivationInventoryVerified()
      ? '<div class="cult-note">库存未确认，暂不能判定缺口。</div>'
      : aggregate.ready
        ? '<div class="cult-note"><span class="cult-ok-pill">✅ 无材料缺口，下位原料齐备（合成后充足），等待手动合成并培养。</span></div>'
        : aggregate.blocked.length
          ? '<div class="cult-note">当前没有可刷取缺口，但存在人工或其他获取阻塞。</div>'
          : '<div class="cult-note">无材料缺口。</div>';
  var crafts = aggregate.crafts.length
    ? '<ul class="cult-list">' + aggregate.crafts.map(function(entry) {
        return '<li><span class="cult-craft-pill">🔧 ' + esc(entry.name) + ' ×' + entry.count + '</span></li>';
      }).join('') + '</ul>'
    : '<div class="cult-note">暂不需要手动合成。</div>';
  var blocked = cultivationBlockedHtml(aggregate.blocked, '无法自动获取，暂不能自动刷取：');
  return '<div class="cult-section-title">缺口与手动合成清单</div>' +
    '<div class="cult-note">按当前目标顺序汇总的原料缺口：</div>' + missing +
    '<div class="cult-note" style="margin-top:12px">材料齐了之后需要你手动合成的部分：</div>' + crafts +
    blocked +
    '<div class="cult-note" style="margin-top:12px">' +
      (!cultivationInventoryVerified()
        ? '库存未确认，暂不能判定原料是否齐备；请先做一次库存识别。'
        : (aggregate.ready ? '当前目标原料已齐，等待你在游戏内手动合成并培养。' : '仍有缺口，刷取完成后会通过 Telegram 通知你。')) +
    '</div>';
}

function bindCultivationEvents() {
  // Handlers are inline; re-rendering rebuilds the DOM, so restore the search
  // caret instead of losing focus while typing, without scrolling the page.
  var state = cultivationState();
  // Also self-heals if the page was opened before the navigation hook existed.
  cultivationStartPolling();
  var targetId = state.activeSearchId || (state.operatorOpen ? 'cultOperatorSearch' : '');
  if (targetId && document && typeof document.getElementById === 'function') {
    var input = document.getElementById(targetId);
    if (input && typeof input.focus === 'function') {
      try {
        input.focus({preventScroll: true});
      } catch (_) {
        input.focus();
      }
      if (typeof input.setSelectionRange === 'function') {
        var end = String(input.value || '').length;
        try { input.setSelectionRange(end, end); } catch (_) {}
      }
    }
  }
  return true;
}

// ─── Skill Mastery Window (OperProgress, independent from Elite/Level goals) ───

var CULTIVATION_MASTERY_RUNTIME_LABELS = {
  idle: '空闲',
  running: '专精执行中',
  succeeded: '已完成',
  failed: '异常',
};

function cultivationMasteryPicker() {
  var state = cultivationState();
  if (!state.masteryPicker) {
    state.masteryPicker = {profession: '', rarity: '', ownedOnly: true};
  }
  return state.masteryPicker;
}

function cultivationSyncMasteryDraftForOperator(draft, operatorId, resetSkillsWhenNew) {
  var pickedId = String(operatorId || '');
  draft.operator_id = pickedId;
  var existingPlan = null;
  cultivationMastery().plans.forEach(function(plan) {
    if (plan && plan.operator_id === pickedId) existingPlan = plan;
  });
  draft.id = existingPlan ? String(existingPlan.id || '') : '';
  if (existingPlan && Array.isArray(existingPlan.skill_mastery) && existingPlan.skill_mastery.length === 3) {
    draft.skill_mastery = existingPlan.skill_mastery.map(function(v) { return Number(v) || 0; });
  } else if (resetSkillsWhenNew || !Array.isArray(draft.skill_mastery) || draft.skill_mastery.length !== 3) {
    draft.skill_mastery = [0, 0, 3];
  }
  draft.current_mastery = existingPlan && Array.isArray(existingPlan.current_mastery) && existingPlan.current_mastery.length === 3
    ? existingPlan.current_mastery.map(function(v) { return Number(v) || 0; })
    : [0, 0, 0];
  draft.auto_skill_level_7 = existingPlan ? existingPlan.auto_skill_level_7 !== false : (draft.auto_skill_level_7 !== false);
  draft.completed = existingPlan ? !!existingPlan.completed : false;
  return draft;
}

function cultivationMasteryPickOperator(operatorId) {
  var state = cultivationState();
  var pickedId = String(operatorId || '');
  if (!pickedId) return null;
  if (!state.masteryDraft) {
    return cultivationOpenMasteryDraft(pickedId);
  }
  var hadExistingId = !!state.masteryDraft.id;
  cultivationSyncMasteryDraftForOperator(state.masteryDraft, pickedId, hadExistingId);
  state.masteryError = '';
  if (!cultivationSyncMasteryPickerDom()) cultivationRenderIfVisible();
  return state.masteryDraft;
}

function cultivationOpenMasteryDraft(operatorId) {
  var state = cultivationState();
  var candidates = cultivationMasteryOperatorCandidates();
  var pickedId = String(operatorId || '');
  if (!pickedId && candidates.length) {
    var firstUnqueued = candidates.find(function(c) { return !c.masteryPlan || c.masteryPlan.completed; });
    pickedId = (firstUnqueued || candidates[0]).id;
  }
  state.masteryDraft = cultivationSyncMasteryDraftForOperator({
    id: '',
    operator_id: pickedId,
    skill_mastery: [0, 0, 3],
    current_mastery: [0, 0, 0],
    auto_skill_level_7: true,
    completed: false,
  }, pickedId, true);
  state.masteryError = '';
  state.masteryNotice = '';
  cultivationRenderIfVisible();
  return state.masteryDraft;
}

function cultivationEditMasteryPlan(planId) {
  var state = cultivationState();
  var mastery = cultivationMastery();
  var found = null;
  mastery.plans.forEach(function(plan) {
    if (plan && plan.id === planId) found = plan;
  });
  if (!found) return null;
  var targets = Array.isArray(found.skill_mastery) && found.skill_mastery.length === 3
    ? found.skill_mastery.map(function(v) { return Number(v) || 0; })
    : [0, 0, 3];
  var current = Array.isArray(found.current_mastery) && found.current_mastery.length === 3
    ? found.current_mastery.map(function(v) { return Number(v) || 0; })
    : [0, 0, 0];
  state.masteryDraft = {
    id: String(found.id || ''),
    operator_id: String(found.operator_id || ''),
    skill_mastery: targets,
    current_mastery: current,
    auto_skill_level_7: found.auto_skill_level_7 !== false,
    completed: !!found.completed,
  };
  state.masteryError = '';
  state.masteryNotice = '';
  cultivationRenderIfVisible();
  return state.masteryDraft;
}

function cultivationCloseMasteryDraft() {
  var state = cultivationState();
  state.masteryDraft = null;
  state.masteryError = '';
  cultivationRenderIfVisible();
}

function cultivationMasteryDraftField(field, value) {
  var state = cultivationState();
  if (!state.masteryDraft) cultivationOpenMasteryDraft('');
  var draft = state.masteryDraft;
  if (field === 'operator_id') {
    var hadExistingId = !!draft.id;
    cultivationSyncMasteryDraftForOperator(draft, value, hadExistingId);
  } else if (field === 'skill_0' || field === 'skill_1' || field === 'skill_2') {
    var idx = Number(field.split('_')[1]);
    var num = Math.max(0, Math.min(3, parseInt(value, 10) || 0));
    draft.skill_mastery[idx] = num;
  } else if (field === 'auto_skill_level_7') {
    draft.auto_skill_level_7 = !!value;
  }
  state.masteryError = '';
  if (!cultivationSyncMasteryPickerDom()) cultivationRenderIfVisible();
  return draft;
}

function cultivationSyncMasteryPickerDom() {
  if (typeof document === 'undefined' || typeof document.getElementById !== 'function') {
    return false;
  }
  var pickerMount = document.getElementById('cultMasteryPickerMount');
  if (!cultivationIsRealDomNode(pickerMount)) return false;
  var state = cultivationState();
  var candidates = cultivationMasteryOperatorCandidates();
  pickerMount.innerHTML = renderCultivationMasteryAvatarGrid(candidates);
  var draftMount = document.getElementById('cultMasteryDraftMount');
  if (cultivationIsRealDomNode(draftMount)) {
    draftMount.innerHTML = state.masteryDraft
      ? renderCultivationMasteryDraftForm(state.masteryDraft, candidates)
      : '';
  }
  return true;
}

function cultivationSetMasterySearch(query) {
  var state = cultivationState();
  state.masteryQuery = String(query || '');
  state.activeSearchId = state.masteryQuery.trim() ? 'cultMasterySearch' : '';
  var candidates = cultivationMasteryOperatorCandidates();
  if (state.masteryDraft && candidates.length) {
    var exists = candidates.some(function(item) { return item.id === state.masteryDraft.operator_id; });
    if (!exists) cultivationSyncMasteryDraftForOperator(state.masteryDraft, candidates[0].id, !!state.masteryDraft.id);
  }
  if (!cultivationSyncMasteryPickerDom()) cultivationRenderIfVisible();
}

function cultivationSetMasteryFilters(patch) {
  var picker = cultivationMasteryPicker();
  Object.keys(patch || {}).forEach(function(key) {
    picker[key] = patch[key];
  });
  var state = cultivationState();
  var candidates = cultivationMasteryOperatorCandidates();
  if (state.masteryDraft && candidates.length) {
    var exists = candidates.some(function(item) { return item.id === state.masteryDraft.operator_id; });
    if (!exists) cultivationSyncMasteryDraftForOperator(state.masteryDraft, candidates[0].id, !!state.masteryDraft.id);
  }
  if (!cultivationSyncMasteryPickerDom()) cultivationRenderIfVisible();
  return picker;
}

function cultivationMasteryOperatorCandidates() {
  var state = cultivationState();
  var picker = cultivationMasteryPicker();
  var query = String(state.masteryQuery || '').trim().toLowerCase();
  var scan = cultivationOperatorScan();
  var ownedById = {};
  (scan.owned || []).forEach(function(entry) {
    if (entry && entry.id) ownedById[entry.id] = entry;
  });
  var planByOpId = {};
  (cultivationMastery().plans || []).forEach(function(plan, idx) {
    if (!plan || !plan.operator_id) return;
    if (!planByOpId[plan.operator_id] || (!plan.completed && planByOpId[plan.operator_id].completed)) {
      var isTraining = !plan.completed && plan.enabled !== false && (
        plan.status === 'training' || Number(plan.training_skill) > 0
      );
      var isPartial = !plan.completed && plan.enabled !== false && !isTraining && (
        plan.status === 'partial' ||
        (Array.isArray(plan.current_mastery) && plan.current_mastery.some(function(v) { return Number(v) > 0; }))
      );
      planByOpId[plan.operator_id] = {
        planId: String(plan.id || ''),
        queueIndex: idx,
        isTraining: isTraining,
        isPartial: isPartial,
        completed: !!plan.completed,
        enabled: plan.enabled !== false,
        statusLabel: String(plan.status_label || ''),
      };
    }
  });
  var operators = cultivationCatalog().operators || {};
  var seen = {};
  var allIds = Object.keys(operators).slice();
  Object.keys(ownedById).forEach(function(id) {
    if (!operators[id]) allIds.push(id);
  });
  var list = [];
  allIds.forEach(function(operatorId) {
    if (seen[operatorId]) return;
    seen[operatorId] = true;
    var meta = operators[operatorId] || {};
    var snap = ownedById[operatorId] || null;
    var rarity = Number(meta.rarity || (snap && snap.rarity)) || 6;
    var caps = Array.isArray(meta.max_levels) ? meta.max_levels : (CULTIVATION_DEFAULT_MAX_LEVELS[rarity] || [50, 80, 90]);
    if (!caps || caps.length < 3) return;
    var owned = !!snap;
    var masteryPlan = planByOpId[operatorId] || null;
    if (scan.available && picker.ownedOnly && !owned && !(masteryPlan && !masteryPlan.completed)) return;
    var profession = String(meta.profession || '');
    if (picker.profession && profession !== picker.profession) return;
    if (picker.rarity && String(rarity) !== String(picker.rarity)) return;
    var name = String(meta.name || (snap && snap.name) || operatorId);
    var profLabel = cultivationProfessionLabel(profession);
    if (query &&
        name.toLowerCase().indexOf(query) < 0 &&
        operatorId.toLowerCase().indexOf(query) < 0 &&
        profLabel.toLowerCase().indexOf(query) < 0) {
      return;
    }
    list.push({
      id: operatorId,
      name: name,
      rarity: rarity,
      profession: profession,
      owned: owned,
      elite: snap ? Number(snap.elite) : null,
      level: snap ? Number(snap.level) : null,
      masteryPlan: masteryPlan,
    });
  });
  list.sort(function(a, b) {
    var aActive = !!(a.masteryPlan && !a.masteryPlan.completed);
    var bActive = !!(b.masteryPlan && !b.masteryPlan.completed);
    if (aActive !== bActive) return aActive ? -1 : 1;
    if (aActive && bActive) {
      if (a.masteryPlan.isTraining !== b.masteryPlan.isTraining) {
        return a.masteryPlan.isTraining ? -1 : 1;
      }
      if (a.masteryPlan.queueIndex !== b.masteryPlan.queueIndex) {
        return a.masteryPlan.queueIndex - b.masteryPlan.queueIndex;
      }
    }
    if (a.owned !== b.owned) return a.owned ? -1 : 1;
    var aE2 = a.elite === 2 ? 1 : 0;
    var bE2 = b.elite === 2 ? 1 : 0;
    if (aE2 !== bE2) return bE2 - aE2;
    if (a.rarity !== b.rarity) return b.rarity - a.rarity;
    var aLv = a.level || 0;
    var bLv = b.level || 0;
    if (aLv !== bLv) return bLv - aLv;
    return a.name.localeCompare(b.name, 'zh-Hans-CN');
  });
  return list;
}

// ─── Mastery Material Sufficiency Prediction (using upper-layer inventory & recipes) ───

function cultivationFreshInventoryPool() {
  var raw = (cultivationData().inventory || {}).items || {};
  var pool = {};
  Object.keys(raw).forEach(function(itemId) {
    var count = Number(raw[itemId]) || 0;
    if (count > 0) pool[String(itemId)] = count;
  });
  return pool;
}

function cultivationWorkshopRecipeInputs(recipe) {
  if (!recipe) return null;
  if (recipe.station && recipe.station.kind && recipe.station.kind !== 'workshop') return null;
  var inputs = recipe.inputs || recipe.costs || null;
  if (!inputs || typeof inputs !== 'object' || !Object.keys(inputs).length) return null;
  return inputs;
}

function cultivationCopyPool(src) {
  var out = {};
  Object.keys(src || {}).forEach(function(k) {
    var v = Number(src[k]) || 0;
    if (v > 0) out[k] = v;
  });
  return out;
}

function cultivationAssignPool(target, src) {
  Object.keys(target).forEach(function(k) { delete target[k]; });
  Object.keys(src || {}).forEach(function(k) {
    var v = Number(src[k]) || 0;
    if (v > 0) target[k] = v;
  });
}

function cultivationTryAcquireWorkshopQty(targetId, qty, tempPool, recipes, craftBatches, depth) {
  if (qty <= 0) return true;
  if (depth > 12) return false;
  if (depth > 0) {
    var avail = Number(tempPool[targetId]) || 0;
    if (avail > 0) {
      var take = Math.min(avail, qty);
      tempPool[targetId] = avail - take;
      qty -= take;
    }
    if (qty <= 0) return true;
  }
  var recipe = recipes[targetId];
  var inputs = cultivationWorkshopRecipeInputs(recipe);
  if (!inputs) return false;
  var outCount = Math.max(1, Number(recipe.output_count || recipe.count) || 1);
  var batches = Math.ceil(qty / outCount);
  var produced = batches * outCount;
  var surplus = produced - qty;
  var inKeys = Object.keys(inputs);
  for (var i = 0; i < inKeys.length; i += 1) {
    var inId = inKeys[i];
    var inNeed = (Number(inputs[inId]) || 0) * batches;
    if (inNeed > 0 && !cultivationTryAcquireWorkshopQty(inId, inNeed, tempPool, recipes, craftBatches, depth + 1)) {
      return false;
    }
  }
  if (surplus > 0) {
    tempPool[targetId] = (Number(tempPool[targetId]) || 0) + surplus;
  }
  craftBatches[targetId] = (craftBatches[targetId] || 0) + batches;
  return true;
}

function cultivationExpandMissingSubs(targetId, qty, pool, recipes, missingMap, craftBatches, depth) {
  if (qty <= 0 || depth > 12) return;
  if (depth > 0) {
    var avail = Number(pool[targetId]) || 0;
    if (avail > 0) {
      var take = Math.min(avail, qty);
      pool[targetId] = avail - take;
      qty -= take;
    }
    if (qty <= 0) return;
  }
  var recipe = recipes[targetId];
  var inputs = cultivationWorkshopRecipeInputs(recipe);
  if (!inputs) {
    missingMap[targetId] = (missingMap[targetId] || 0) + qty;
    return;
  }
  var outCount = Math.max(1, Number(recipe.output_count || recipe.count) || 1);
  var batches = Math.ceil(qty / outCount);
  craftBatches[targetId] = (craftBatches[targetId] || 0) + batches;
  Object.keys(inputs).forEach(function(inId) {
    var inNeed = (Number(inputs[inId]) || 0) * batches;
    if (inNeed > 0) {
      cultivationExpandMissingSubs(inId, inNeed, pool, recipes, missingMap, craftBatches, depth + 1);
    }
  });
}

function cultivationPredictMasteryMaterials(operatorId, targetMastery, currentMastery, sharedPool) {
  if (operatorId && typeof operatorId === 'object') {
    var draftObj = operatorId;
    sharedPool = targetMastery || null;
    currentMastery = draftObj.current_mastery || [0, 0, 0];
    targetMastery = draftObj.skill_mastery || [0, 0, 0];
    operatorId = draftObj.operator_id || '';
  }
  var meta = cultivationOperatorEntry(operatorId) || {};
  var masteryMeta = meta.mastery || {};
  var skillsMeta = Array.isArray(masteryMeta.skills) ? masteryMeta.skills : [];
  var targets = Array.isArray(targetMastery) ? targetMastery : [0, 0, 0];
  var current = Array.isArray(currentMastery) ? currentMastery : [0, 0, 0];
  var hasTarget = targets.some(function(v, i) { return (Number(v) || 0) > (Number(current[i]) || 0); });
  if (!hasTarget) {
    return {status: 'satisfied', direct: [], crafts: [], missing: [], stepLabels: [], steps: []};
  }
  if (!skillsMeta.length) {
    return {status: 'no_data', direct: [], crafts: [], missing: [], stepLabels: [], steps: []};
  }
  var directNeed = {};
  var stepLabels = [];
  var rawSteps = [];
  for (var i = 0; i < 3; i += 1) {
    var cur = Math.max(0, Math.min(3, Number(current[i]) || 0));
    var tgt = Math.max(0, Math.min(3, Number(targets[i]) || 0));
    if (tgt <= cur) continue;
    var sk = skillsMeta[i] || {};
    var skName = sk.name ? ((i + 1) + '技能「' + sk.name + '」') : ((i + 1) + '技能');
    stepLabels.push(skName + ' 专' + cur + '→专' + tgt);
    var costsByLv = sk.costs || {};
    for (var lv = cur + 1; lv <= tgt; lv += 1) {
      var lvCosts = costsByLv[String(lv)] || {};
      var stepCostMap = {};
      Object.keys(lvCosts).forEach(function(itemId) {
        var c = Number(lvCosts[itemId]) || 0;
        if (c > 0) {
          directNeed[itemId] = (directNeed[itemId] || 0) + c;
          stepCostMap[itemId] = c;
        }
      });
      rawSteps.push({
        label: (i + 1) + '技能 专' + lv,
        fullLabel: skName + ' 专' + lv,
        costs: stepCostMap,
      });
    }
  }
  var needKeys = Object.keys(directNeed);
  if (!needKeys.length) {
    return {status: 'no_data', direct: [], crafts: [], missing: [], stepLabels: stepLabels, steps: []};
  }

  var pool = sharedPool || cultivationFreshInventoryPool();
  var recipes = cultivationCatalog().recipes || {};

  // Step-by-step sequential readiness preview on a snapshot of pool
  var stepPool = cultivationCopyPool(pool);
  var steps = rawSteps.map(function(st) {
    var usedCraft = false;
    var lackItems = [];
    Object.keys(st.costs).forEach(function(itemId) {
      var need = st.costs[itemId];
      var stock = Number(stepPool[itemId]) || 0;
      var take = Math.min(stock, need);
      stepPool[itemId] = stock - take;
      var rem = need - take;
      if (rem <= 0) return;
      var crafted = 0;
      while (crafted < rem) {
        var tmp = cultivationCopyPool(stepPool);
        var dummyCrafts = {};
        if (cultivationTryAcquireWorkshopQty(itemId, 1, tmp, recipes, dummyCrafts, 0)) {
          cultivationAssignPool(stepPool, tmp);
          var r = recipes[itemId];
          var outC = Math.max(1, Number((r && (r.output_count || r.count)) || 1));
          crafted += Math.min(outC, rem - crafted);
          usedCraft = true;
        } else {
          break;
        }
      }
      if (crafted < rem) {
        lackItems.push(cultivationItemName(itemId) + '×' + (rem - crafted));
      }
    });
    return {
      label: st.label,
      fullLabel: st.fullLabel,
      status: lackItems.length ? 'insufficient' : (usedCraft ? 'craftable' : 'ready'),
      lackText: lackItems.join('、'),
    };
  });

  // Pass 1: Deduct direct stock for all direct items first
  var directList = needKeys.map(function(itemId) {
    var need = directNeed[itemId];
    var stock = Number(pool[itemId]) || 0;
    var stockUsed = Math.min(stock, need);
    pool[itemId] = stock - stockUsed;
    var rem = need - stockUsed;
    return {
      id: itemId,
      name: cultivationItemName(itemId),
      need: need,
      stock: stock,
      stockUsed: stockUsed,
      rem: rem,
      crafted: 0,
      shortfall: rem,
      status: rem === 0 ? 'ready' : 'insufficient',
      inputsText: '',
      missingSubs: [],
    };
  });

  // Pass 2: For items with rem > 0, synthesize unit-by-unit from lower-tier stock
  var globalCraftBatches = {};
  var globalMissingMap = {};
  directList.forEach(function(item) {
    if (item.rem <= 0) return;
    var recipe = recipes[item.id];
    var rInputs = cultivationWorkshopRecipeInputs(recipe);
    if (!rInputs) {
      item.crafted = 0;
      item.shortfall = item.rem;
      item.status = 'insufficient';
      item.missingSubs = [{id: item.id, name: item.name, count: item.rem}];
      globalMissingMap[item.id] = (globalMissingMap[item.id] || 0) + item.rem;
      return;
    }
    var outCount = Math.max(1, Number(recipe.output_count || recipe.count) || 1);
    var itemBatches = 0;
    var itemSubCrafts = {};
    while (item.crafted < item.rem) {
      var tempPool = cultivationCopyPool(pool);
      var unitCrafts = {};
      if (cultivationTryAcquireWorkshopQty(item.id, 1, tempPool, recipes, unitCrafts, 0)) {
        cultivationAssignPool(pool, tempPool);
        var gained = Math.min(outCount, item.rem - item.crafted);
        item.crafted += gained;
        itemBatches += 1;
        Object.keys(unitCrafts).forEach(function(cid) {
          globalCraftBatches[cid] = (globalCraftBatches[cid] || 0) + unitCrafts[cid];
          if (cid !== item.id) {
            itemSubCrafts[cid] = (itemSubCrafts[cid] || 0) + unitCrafts[cid];
          }
        });
      } else {
        break;
      }
    }
    item.shortfall = item.rem - item.crafted;
    if (itemBatches > 0) {
      var parts = Object.keys(rInputs).map(function(inId) {
        return cultivationItemName(inId) + ' ×' + ((Number(rInputs[inId]) || 0) * itemBatches);
      });
      item.inputsText = parts.join(' + ');
      var subParts = Object.keys(itemSubCrafts).map(function(cid) {
        var subR = recipes[cid] || {};
        var subOut = Math.max(1, Number(subR.output_count || subR.count) || 1);
        return cultivationItemName(cid) + ' ×' + (itemSubCrafts[cid] * subOut);
      });
      item.subCraftsText = subParts.join('、');
    }
    if (item.shortfall === 0) {
      item.status = 'craftable';
    } else {
      item.status = 'insufficient';
      var itemMissMap = {};
      cultivationExpandMissingSubs(item.id, item.shortfall, pool, recipes, itemMissMap, globalCraftBatches, 0);
      item.missingSubs = Object.keys(itemMissMap).map(function(mid) {
        globalMissingMap[mid] = (globalMissingMap[mid] || 0) + itemMissMap[mid];
        return {id: mid, name: cultivationItemName(mid), count: itemMissMap[mid]};
      });
    }
  });

  // Sort directList: insufficient (red) FIRST, craftable (blue) SECOND, ready (green) THIRD
  var statusOrder = {insufficient: 0, craftable: 1, ready: 2};
  directList.sort(function(a, b) {
    var oa = statusOrder[a.status] !== undefined ? statusOrder[a.status] : 9;
    var ob = statusOrder[b.status] !== undefined ? statusOrder[b.status] : 9;
    if (oa !== ob) return oa - ob;
    return b.need - a.need;
  });

  var crafts = Object.keys(globalCraftBatches).map(function(id) {
    var batches = globalCraftBatches[id];
    var recipe = recipes[id] || {};
    var rInputs = cultivationWorkshopRecipeInputs(recipe) || {};
    var outCount = Math.max(1, Number(recipe.output_count || recipe.count) || 1);
    var parts = Object.keys(rInputs).map(function(inId) {
      return cultivationItemName(inId) + ' ×' + ((Number(rInputs[inId]) || 0) * batches);
    });
    return {
      id: id,
      name: cultivationItemName(id),
      count: batches * outCount,
      batches: batches,
      inputsText: parts.join(' + '),
    };
  });
  var missing = Object.keys(globalMissingMap).map(function(id) {
    return {id: id, name: cultivationItemName(id), count: globalMissingMap[id]};
  });

  var hasInsufficient = directList.some(function(d) { return d.status === 'insufficient'; });
  var hasCraftable = directList.some(function(d) { return d.status === 'craftable'; });
  var status = hasInsufficient ? 'insufficient' : (hasCraftable ? 'craftable' : 'ready');
  return {
    status: status,
    direct: directList,
    crafts: crafts,
    missing: missing,
    stepLabels: stepLabels,
    steps: steps,
  };
}

function renderCultivationMasteryPredictionBox(pred) {
  if (pred && !pred.status && pred.operator_id !== undefined) {
    pred = cultivationPredictMasteryMaterials(pred);
  }
  if (!pred || pred.status === 'no_data') {
    return '<div class="cult-mat-box">暂未收录该干员的详细专精材料表（仍可直接提交由 MaaCore 在游戏内自动识别并合成）。</div>';
  }
  if (pred.status === 'satisfied') {
    return '<div class="cult-mat-box is-ok"><b>✅ 所选技能已达到目标专精等级</b></div>';
  }
  var invScan = cultivationInventoryScan();
  var invNote = invScan.available
    ? '（基于 ' + esc(invScan.scanned_at || '最近') + ' 仓库库存预测）'
    : '（当前尚未识别仓库，按 0 库存预测；请先点击上方“库存识别”）';
  var okCount = 0;
  var craftCount = 0;
  var lackCount = 0;
  (pred.direct || []).forEach(function(d) {
    if (d.status === 'insufficient') lackCount += 1;
    else if (d.status === 'craftable') craftCount += 1;
    else okCount += 1;
  });

  var boxClass = pred.status === 'ready' ? 'cult-mat-box is-ok'
    : (pred.status === 'craftable' ? 'cult-mat-box is-craft' : 'cult-mat-box is-lack');
  var headline = pred.status === 'ready'
    ? '<span style="color:#2eb67d;font-size:13.5px"><b>✅ 库存材料充足（全部 ' + okCount + ' 项现成够用，无需合成，可直接进训练室专精）</b></span> ' + invNote
    : (pred.status === 'craftable'
      ? '<span style="color:#38bdf8;font-size:13.5px"><b>🔧 库存够合成 · 合成后全部充足！</b></span>' +
        '<span>（现成充足 <b>' + okCount + '</b> 项 + <b style="color:#38bdf8">合成后充足 ' + craftCount + ' 项</b>，无需刷图，可由 OperProgress 在加工站自动递归合成）</span> ' + invNote
      : '<span style="color:#ff6b6b;font-size:13.5px"><b>🚨 ⚠️ 材料不足提醒：算上加工站下位合成后，仍有 ' + lackCount + ' 项专精材料不足（已标红置顶）</b></span> ' + invNote);

  var missingBannerHtml = pred.missing.length
    ? '<div style="margin-top:8px;padding:7px 10px;border-radius:7px;background:rgba(229,72,77,.16);border:1px solid #e5484d">' +
        '<b style="color:#ff6b6b">需先刷取补充的基础/下位材料：</b>' +
        pred.missing.map(function(m) {
          return '<span class="cult-lack-pill">❌ 缺 ' + esc(m.name) + ' ×' + m.count + '</span>';
        }).join('') +
      '</div>'
    : '';

  var cardsHtml = '<div class="cult-mat-grid">' + (pred.direct || []).map(function(item) {
    if (item.status === 'insufficient') {
      var haveTotal = item.stockUsed + item.crafted;
      var missSubsLine = (item.missingSubs && item.missingSubs.length)
        ? '<div class="cult-mat-item-lack" style="margin-top:2px">⚠️ 缺下位原料：' + item.missingSubs.map(function(m) {
            return '<span class="cult-lack-pill">缺 ' + esc(m.name) + ' ×' + m.count + '</span>';
          }).join('') + '</div>'
        : '';
      return '<div class="cult-mat-item is-lack">' +
        '<div class="cult-mat-item-head">' +
          '<span style="color:#ff6b6b">❌ ' + esc(item.name) + ' ×' + item.need + '</span>' +
          '<span class="cult-mat-badge is-lack">合成后仍缺 ' + item.shortfall + '</span>' +
        '</div>' +
        '<div class="cult-mat-item-lack">仓库现成 ' + item.stockUsed + '（直缺 ' + item.rem + '）· 下位可合 +' + item.crafted +
          ' → <span style="text-decoration:underline">合计 ' + haveTotal + '/' + item.need + '（仍缺 ' + item.shortfall + '）</span></div>' +
        missSubsLine +
      '</div>';
    }
    if (item.status === 'craftable') {
      return '<div class="cult-mat-item is-craft">' +
        '<div class="cult-mat-item-head">' +
          '<span>🔧 ' + esc(item.name) + ' ×' + item.need + '</span>' +
          '<span class="cult-mat-badge is-craft">合成后充足</span>' +
        '</div>' +
        '<div class="cult-mat-item-sub">仓库现成 <b>' + item.stockUsed + '</b>（<span style="color:#ff8b8b;font-weight:600">直缺 ' + item.rem + '</span>）→ ' +
          '<b style="color:#2eb67d">可合成 +' + item.crafted + '（✅ 合成后充足 ' + item.need + '/' + item.need + '）</b></div>' +
        (item.inputsText ? '<div class="cult-mat-item-sub">合成消耗：' + esc(item.inputsText) + '</div>' : '') +
        (item.subCraftsText ? '<div class="cult-mat-item-sub" style="color:#38bdf8">↳ 含下位递归合成：' + esc(item.subCraftsText) + '（下位均充足）</div>' : '') +
      '</div>';
    }
    return '<div class="cult-mat-item is-ok">' +
      '<div class="cult-mat-item-head">' +
        '<span>✅ ' + esc(item.name) + ' ×' + item.need + '</span>' +
        '<span class="cult-mat-badge is-ok">现成充足</span>' +
      '</div>' +
      '<div class="cult-mat-item-sub">仓库现成 <b style="color:#2eb67d">' + item.stock + '</b> / 需 ' + item.need +
        '（无需合成，余 ' + Math.max(0, item.stock - item.need) + '）</div>' +
    '</div>';
  }).join('') + '</div>';

  var stepBarHtml = (pred.steps && pred.steps.length)
    ? '<div class="cult-step-bar"><span style="color:var(--text-muted);font-weight:600">分阶可用性预测：</span>' +
      pred.steps.map(function(st) {
        if (st.status === 'insufficient') {
          return '<span class="cult-lack-pill">❌ ' + esc(st.label) + '：缺 ' + esc(st.lackText) + '</span>';
        }
        if (st.status === 'craftable') {
          return '<span class="cult-craft-pill">🔧 ' + esc(st.label) + '：合成后充足</span>';
        }
        return '<span class="cult-ok-pill">✅ ' + esc(st.label) + '：现成充足</span>';
      }).join('') + '</div>'
    : '';

  return '<div class="' + boxClass + '">' +
    '<div>' + headline + '</div>' +
    missingBannerHtml +
    cardsHtml +
    stepBarHtml +
  '</div>';
}

async function saveCultivationMasterySettings(enabled) {
  var state = cultivationState();
  if (state.masterySaving) return false;
  state.masterySaving = true;
  state.masteryError = '';
  state.masteryNotice = '';
  state.fetchSeq += 1;
  cultivationRenderIfVisible();
  try {
    var response = await apiResponse('cultivation/mastery-settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        enabled: !!enabled,
        revision: (state.data || {}).revision,
      }),
    });
    if (!response.ok) {
      state.masteryError = cultivationErrorText(response.data) || '专精设置保存失败';
      return false;
    }
    var readData = await cultivationReadBack();
    if (!readData || !readData.mastery || !!readData.mastery.enabled !== !!enabled) {
      state.masteryError = '专精设置保存后读回不一致，请刷新重试';
      return false;
    }
    applyCultivationPayload(readData, {preserveDraft: true, keepMessages: true});
    state.masteryNotice = enabled
      ? '已开启随日常收尾自动推进技能专精'
      : '已关闭随日常收尾自动推进技能专精';
    return true;
  } finally {
    state.masterySaving = false;
    cultivationRenderIfVisible();
  }
}

async function saveCultivationMasteryPlan() {
  var state = cultivationState();
  if (state.masterySaving) return false;
  var draft = state.masteryDraft;
  if (!draft) return false;
  var targets = Array.isArray(draft.skill_mastery) ? draft.skill_mastery.map(function(v) {
    return Math.max(0, Math.min(3, parseInt(v, 10) || 0));
  }) : [0, 0, 0];
  if (!draft.operator_id) {
    state.masteryError = '请选择需要专精的干员';
    cultivationRenderIfVisible();
    return false;
  }
  if (targets.length !== 3 || targets.every(function(v) { return v === 0; })) {
    state.masteryError = '1~3 技能目标专精等级至少需有一项大于 0';
    cultivationRenderIfVisible();
    return false;
  }
  state.masterySaving = true;
  state.masteryError = '';
  state.masteryNotice = '';
  state.fetchSeq += 1;
  cultivationRenderIfVisible();
  try {
    var body = {
      operator_id: draft.operator_id,
      skill_mastery: targets,
      auto_skill_level_7: draft.auto_skill_level_7 !== false,
      revision: (state.data || {}).revision,
    };
    if (draft.id) body.id = draft.id;
    if (draft.completed) {
      body.completed = false;
      body.enabled = true;
    }
    var response = await apiResponse('cultivation/mastery-plan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      state.masteryError = cultivationErrorText(response.data) || '专精计划保存失败';
      return false;
    }
    var readData = await cultivationReadBack();
    if (!readData || !readData.mastery || !Array.isArray(readData.mastery.plans)) {
      state.masteryError = '专精计划读回失败，草稿已保留';
      return false;
    }
    var matched = readData.mastery.plans.some(function(p) {
      return p && p.operator_id === draft.operator_id &&
        Array.isArray(p.skill_mastery) &&
        p.skill_mastery[0] === targets[0] &&
        p.skill_mastery[1] === targets[1] &&
        p.skill_mastery[2] === targets[2];
    });
    if (!matched) {
      state.masteryError = '专精计划保存后读回与提交不一致，草稿已保留';
      return false;
    }
    applyCultivationPayload(readData, {preserveDraft: true, keepMessages: true});
    state.masteryDraft = null;
    state.masteryNotice = '专精计划已保存';
    return true;
  } finally {
    state.masterySaving = false;
    cultivationRenderIfVisible();
  }
}

async function setCultivationMasteryPlanState(planId, patch) {
  var state = cultivationState();
  if (state.masteryBusyId) return false;
  state.masteryBusyId = String(planId || '__auto__');
  state.masteryError = '';
  state.masteryNotice = '';
  state.fetchSeq += 1;
  cultivationRenderIfVisible();
  try {
    var base = {revision: (state.data || {}).revision};
    if (planId) base.id = planId;
    var body = Object.assign(base, patch || {});
    var response = await apiResponse('cultivation/mastery-plan-state', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      state.masteryError = cultivationErrorText(response.data) || '专精计划状态更新失败';
      return false;
    }
    var readData = await cultivationReadBack();
    if (!readData || !readData.mastery || !Array.isArray(readData.mastery.plans)) {
      state.masteryError = '专精计划状态读回失败';
      return false;
    }
    applyCultivationPayload(readData, {preserveDraft: true, keepMessages: true});
    state.masteryNotice = patch && patch.delete ? '专精计划已删除'
      : (patch && patch.move === 'auto' ? '已按专精状态自动排序（专精中置顶）'
      : (patch && patch.move ? '专精计划顺序已调整'
      : (patch && patch.completed ? '专精计划已标记完成'
      : (patch && patch.enabled === false ? '专精计划已暂停' : '专精计划已恢复'))));
    return true;
  } finally {
    state.masteryBusyId = '';
    cultivationRenderIfVisible();
  }
}

async function startCultivationMasteryRun() {
  var state = cultivationState();
  if (state.runPending) return false;
  state.runPending = true;
  state.masteryError = '';
  state.masteryNotice = '';
  state.fetchSeq += 1;
  cultivationRenderIfVisible();
  try {
    var response = await apiResponse('cultivation/mastery-run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}',
    });
    if (!response.ok || !response.data || response.data.ok === false) {
      state.masteryError = cultivationErrorText(response.data) || '技能专精未能启动';
      return false;
    }
    state.masteryNotice = '技能专精任务已启动（含训练室升级与加工站自动合成）';
    await loadCultivation({preserveDraft: true});
    return true;
  } finally {
    state.runPending = false;
    cultivationRenderIfVisible();
  }
}

function cultivationMasterySkillLabel(level) {
  var n = Number(level) || 0;
  if (n === 1) return '专精一';
  if (n === 2) return '专精二';
  if (n === 3) return '专精三';
  return '不专精';
}

function renderCultivationMasteryAvatarGrid(candidates) {
  var state = cultivationState();
  var list = Array.isArray(candidates) ? candidates : cultivationMasteryOperatorCandidates();
  if (!list.length) {
    return '<div class="cult-empty" style="margin-top:10px">没有符合筛选条件的可精二干员（可尝试取消“只看已拥有”或清空搜索词）。</div>';
  }
  var selectedId = state.masteryDraft ? String(state.masteryDraft.operator_id || '') : '';
  return '<div class="cult-picker" id="cultMasteryAvatarGrid" style="max-height:270px;margin-top:10px">' +
    list.map(function(entry) {
      var isSelected = selectedId === entry.id;
      var cls = 'cult-mastery-card' + (isSelected ? ' is-selected' : '');
      var levelText = entry.owned
        ? ('精英' + entry.elite + ' ' + entry.level + '级')
        : '图鉴未拥有';
      var badge = entry.elite === 2
        ? '<span class="cult-tag is-ready">精二</span>'
        : (entry.owned ? '<span class="cult-tag">已拥有</span>' : '<span class="cult-tag">图鉴</span>');
      var planBadge = '';
      if (entry.masteryPlan && !entry.masteryPlan.completed) {
        if (entry.masteryPlan.isTraining) {
          planBadge = '<span class="cult-tag is-craft">🔥 专精中 #' + (entry.masteryPlan.queueIndex + 1) + '</span>';
        } else if (!entry.masteryPlan.enabled) {
          planBadge = '<span class="cult-tag">⏸️ 队列 #' + (entry.masteryPlan.queueIndex + 1) + '</span>';
        } else {
          planBadge = '<span class="cult-tag is-craft">📋 队列 #' + (entry.masteryPlan.queueIndex + 1) + '</span>';
        }
      } else if (entry.masteryPlan && entry.masteryPlan.completed) {
        planBadge = '<span class="cult-tag is-ready">✅ 已专精</span>';
      }
      return '<button type="button" class="' + cls + '" onclick="cultivationMasteryPickOperator(\'' + esc(entry.id) + '\')">' +
        '<div class="cult-pick-head">' +
          cultivationAvatarHtml(entry.id, entry.name) +
          '<div><span class="cult-pick-name">' + esc(entry.name) + '</span>' + badge + planBadge +
          '<div class="cult-pick-meta">' + esc(entry.rarity + '★ · ' + cultivationProfessionLabel(entry.profession)) + '</div>' +
          '<div class="cult-pick-meta">' + esc(levelText) + '</div></div>' +
        '</div>' +
      '</button>';
    }).join('') +
  '</div>';
}

function renderCultivationMasteryFilterBar() {
  var state = cultivationState();
  var picker = cultivationMasteryPicker();
  var scan = cultivationOperatorScan();
  var profOpts = ['<option value=""' + (picker.profession ? '' : ' selected') + '>全部职业</option>'];
  CULTIVATION_PROFESSIONS.forEach(function(pair) {
    profOpts.push('<option value="' + esc(pair[0]) + '"' +
      (picker.profession === pair[0] ? ' selected' : '') + '>' + esc(pair[1]) + '</option>');
  });
  var rarityOpts = ['<option value=""' + (picker.rarity ? '' : ' selected') + '>全部星级</option>'];
  [6, 5, 4].forEach(function(r) {
    rarityOpts.push('<option value="' + r + '"' +
      (String(picker.rarity) === String(r) ? ' selected' : '') + '>' + r + '★</option>');
  });
  return '<div class="cult-toolbar" style="margin-top:10px">' +
    '<div class="cult-field"><label>筛选干员名称 / 职业</label>' +
      '<input id="cultMasterySearch" type="search" value="' + esc(state.masteryQuery || '') + '" ' +
      'placeholder="输入干员名称实时筛选头像（不跳页）" oninput="cultivationSetMasterySearch(this.value)"></div>' +
    '<div class="cult-field"><label>职业</label>' +
      '<select id="cultMasteryProfession" onchange="cultivationSetMasteryFilters({profession:this.value})">' +
      profOpts.join('') + '</select></div>' +
    '<div class="cult-field"><label>星级（可精二）</label>' +
      '<select id="cultMasteryRarity" onchange="cultivationSetMasteryFilters({rarity:this.value})">' +
      rarityOpts.join('') + '</select></div>' +
    (scan.available ? (
      '<div class="cult-switch"><input type="checkbox" id="cultMasteryOwnedOnly" ' +
      (picker.ownedOnly ? 'checked' : '') +
      ' onchange="cultivationSetMasteryFilters({ownedOnly:this.checked})"><span>只看已拥有干员（' +
      (scan.owned ? scan.owned.length : 0) + ' 名）</span></div>'
    ) : '') +
  '</div>';
}

function renderCultivationMasteryDraftForm(draft, precomputedCandidates) {
  var state = cultivationState();
  var candidates = Array.isArray(precomputedCandidates) ? precomputedCandidates : cultivationMasteryOperatorCandidates();
  var opOptions = candidates.map(function(entry) {
    var prefix = '';
    if (entry.masteryPlan && !entry.masteryPlan.completed) {
      prefix = entry.masteryPlan.isTraining
        ? ('[🔥 专精中 #' + (entry.masteryPlan.queueIndex + 1) + '] ')
        : ('[📋 队列 #' + (entry.masteryPlan.queueIndex + 1) + '] ');
    }
    var suffix = entry.owned
      ? '（已拥有 · 精英' + entry.elite + ' ' + entry.level + '级）'
      : '（图鉴 · ' + entry.rarity + '★）';
    return '<option value="' + esc(entry.id) + '"' +
      (entry.id === draft.operator_id ? ' selected' : '') + '>' +
      esc(prefix + entry.name + ' · ' + cultivationProfessionLabel(entry.profession) + suffix) + '</option>';
  }).join('');
  if (!opOptions) {
    opOptions = '<option value="">没有符合筛选条件的可精二干员</option>';
  }
  var opMeta = cultivationOperatorEntry(draft.operator_id) || {};
  var skillsMeta = (opMeta.mastery && Array.isArray(opMeta.mastery.skills)) ? opMeta.mastery.skills : [];
  var skillSelects = [0, 1, 2].map(function(idx) {
    var currentVal = Number((draft.skill_mastery || [])[idx]) || 0;
    var skEntry = skillsMeta[idx] || null;
    var skTitle = (idx + 1) + ' 技能目标' + (skEntry && skEntry.name ? (' · ' + skEntry.name) : '');
    var opts = [0, 1, 2, 3].map(function(val) {
      return '<option value="' + val + '"' + (currentVal === val ? ' selected' : '') + '>' +
        cultivationMasterySkillLabel(val) + '</option>';
    }).join('');
    return '<div class="cult-field"><label>' + esc(skTitle) + '</label>' +
      '<select id="cultMasteryS' + (idx + 1) + '" onchange="cultivationMasteryDraftField(\'skill_' + idx + '\', this.value)">' +
      opts + '</select></div>';
  }).join('');
  var opName = opMeta.name || cultivationOperatorName(draft.operator_id, null);
  var pred = cultivationPredictMasteryMaterials(
    draft.operator_id,
    draft.skill_mastery || [0, 0, 3],
    draft.current_mastery || [0, 0, 0],
    null
  );

  return '<div class="cult-card" style="margin-top:14px">' +
    '<div class="cult-pick-head" style="margin-bottom:8px">' +
      (draft.operator_id ? cultivationAvatarHtml(draft.operator_id, opName) : '') +
      '<div class="cult-section-title" style="margin-bottom:0">' +
        (draft.id ? '编辑技能专精计划' : '配置技能专精目标') +
        (draft.operator_id ? (' · ' + esc(opName)) : '') +
      '</div>' +
    '</div>' +
    '<div class="cult-toolbar" style="margin-top:10px">' +
      '<div class="cult-field"><label>当前选中干员（可点击上方头像切换）</label>' +
        '<select id="cultMasteryOperator" onchange="cultivationMasteryDraftField(\'operator_id\', this.value)">' +
        opOptions + '</select></div>' +
      skillSelects +
      '<div class="cult-field"><label>前置普通技能</label>' +
        '<div class="cult-switch"><input type="checkbox" id="cultMasteryAutoLv7" ' +
        (draft.auto_skill_level_7 !== false ? 'checked' : '') +
        ' onchange="cultivationMasteryDraftField(\'auto_skill_level_7\', this.checked)">' +
        '<span>未满 7 级时先自动升至普通技能 7 级</span></div></div>' +
    '</div>' +
    '<div id="cultMasteryPredictionMount">' + renderCultivationMasteryPredictionBox(pred) + '</div>' +
    '<div class="cult-actions" style="margin-top:12px">' +
      '<button class="btn btn-p" id="cultMasterySaveBtn" onclick="saveCultivationMasteryPlan()" ' +
      (state.masterySaving ? 'disabled' : '') + '>保存专精计划</button>' +
      '<button class="btn" onclick="cultivationCloseMasteryDraft()">收起配置表单</button>' +
    '</div>' +
  '</div>';
}

function renderCultivationMasteryPlanRow(plan, index, sharedPool, totalCount) {
  var state = cultivationState();
  var total = typeof totalCount === 'number' ? totalCount : ((cultivationMastery().plans || []).length || 1);
  var name = plan.operator_name || cultivationOperatorName(plan.operator_id, null);
  var targets = Array.isArray(plan.skill_mastery) ? plan.skill_mastery : [0, 0, 0];
  var current = Array.isArray(plan.current_mastery) ? plan.current_mastery : [0, 0, 0];
  var targetParts = [];
  var currentParts = [];
  for (var i = 0; i < 3; i += 1) {
    var tgt = Number(targets[i]) || 0;
    var cur = Number(current[i]) || 0;
    if (tgt > 0) {
      targetParts.push((i + 1) + '技能→' + cultivationMasterySkillLabel(tgt));
      currentParts.push((i + 1) + '技能: 专' + cur + '/' + tgt);
    }
  }
  var isTraining = !plan.completed && plan.enabled !== false && (
    plan.status === 'training' || Number(plan.training_skill) > 0
  );
  var statusLabel = plan.completed ? '已完成'
    : (plan.enabled === false ? '已暂停' : (plan.status_label || '待执行'));
  var tagClass = 'cult-tag' + (plan.completed ? ' is-ready' : (isTraining ? ' is-craft' : ''));
  var trainingLine = (!plan.completed && Number(plan.training_skill) > 0)
    ? '<div class="cult-goal-meta">训练室进度：正在训练 ' + Number(plan.training_skill) +
      ' 技能 → 专精 ' + Number(plan.training_level || 1) + '（训练倒计时结束后再次执行即可确认达标）</div>'
    : '';
  var lv7Note = plan.skill_level_7_verified
    ? '普通技能 7 级已确认（训练室繁忙时支持 0 秒快速跳过）'
    : (plan.auto_skill_level_7 !== false ? '未满 7 级时自动补升普通技能 7 级' : '仅执行专精');
  var predHtml = '';
  if (!plan.completed && plan.enabled !== false) {
    var pred = cultivationPredictMasteryMaterials(plan.operator_id, targets, current, sharedPool);
    predHtml = renderCultivationMasteryPredictionBox(pred);
  }
  var busyAttr = state.masteryBusyId ? ' disabled' : '';
  var topDisabled = (busyAttr || index === 0) ? ' disabled' : '';
  var upDisabled = (busyAttr || index === 0) ? ' disabled' : '';
  var downDisabled = (busyAttr || index >= total - 1) ? ' disabled' : '';
  var actions = '<button class="btn" title="置顶最高优先级" onclick="setCultivationMasteryPlanState(\'' + esc(plan.id) + '\', {move:\'top\'})"' + topDisabled + '>⏫ 置顶</button>' +
    '<button class="btn" title="上移一位" onclick="setCultivationMasteryPlanState(\'' + esc(plan.id) + '\', {move:\'up\'})"' + upDisabled + '>⬆️ 上移</button>' +
    '<button class="btn" title="下移一位" onclick="setCultivationMasteryPlanState(\'' + esc(plan.id) + '\', {move:\'down\'})"' + downDisabled + '>⬇️ 下移</button>' +
    '<button class="btn" onclick="cultivationEditMasteryPlan(\'' + esc(plan.id) + '\')"' + busyAttr + '>编辑</button>';
  if (!plan.completed) {
    actions += plan.enabled === false
      ? '<button class="btn" onclick="setCultivationMasteryPlanState(\'' + esc(plan.id) + '\', {enabled:true})"' + busyAttr + '>恢复</button>'
      : '<button class="btn" onclick="setCultivationMasteryPlanState(\'' + esc(plan.id) + '\', {enabled:false})"' + busyAttr + '>暂停</button>';
    actions += '<button class="btn" onclick="setCultivationMasteryPlanState(\'' + esc(plan.id) + '\', {completed:true})"' + busyAttr + '>完成</button>';
  }
  actions += '<button class="btn" onclick="setCultivationMasteryPlanState(\'' + esc(plan.id) + '\', {delete:true})"' + busyAttr + '>删除</button>';

  return '<div class="cult-goal' + (plan.completed ? ' is-completed' : '') + '">' +
    '<div class="cult-goal-head"><div style="width:100%">' +
      '<div class="cult-pick-head">' +
        cultivationAvatarHtml(plan.operator_id, name) +
        '<div><div class="cult-goal-name">' + (index + 1) + '. ' + esc(name) +
          '<span class="' + tagClass + '">' + (isTraining ? '🔥 ' : '') + esc(statusLabel) + '</span></div>' +
        '<div class="cult-goal-meta">目标专精：' + esc(targetParts.join(' · ') || '无') +
          ' · 已确认进度：' + esc(currentParts.join(' · ') || '待首次执行') + '</div>' +
        '<div class="cult-goal-meta">策略：' + esc(lv7Note) +
          (plan.last_run_at ? ' · 最近执行：' + esc(plan.last_run_at) : '') + '</div></div>' +
      '</div>' +
      trainingLine +
      predHtml +
    '</div></div>' +
    '<div class="cult-goal-actions">' + actions + '</div>' +
  '</div>';
}

function renderCultivationMasteryWindow() {
  var state = cultivationState();
  var mastery = cultivationMastery();
  var mRuntime = mastery.runtime || {};
  var mStatusLabel = CULTIVATION_MASTERY_RUNTIME_LABELS[mRuntime.status] || (mRuntime.status || '空闲');
  var mRuntimeLine = '专精运行状态：' + mStatusLabel +
    (mRuntime.message ? ' · ' + mRuntime.message : '') +
    (mRuntime.updated_at ? ' · 更新于 ' + mRuntime.updated_at : '');
  var candidates = cultivationMasteryOperatorCandidates();
  var sharedPool = cultivationFreshInventoryPool();
  var planRows = mastery.plans.map(function(plan, index) {
    return renderCultivationMasteryPlanRow(plan, index, sharedPool, mastery.plans.length);
  }).join('');
  var autoSortBtn = mastery.plans.length > 1
    ? '<button class="btn" style="padding:3px 10px;font-size:12px" onclick="setCultivationMasteryPlanState(\'\', {move:\'auto\'})"' +
      (state.masteryBusyId ? ' disabled' : '') + '>⚡ 按专精状态排序（专精中置顶）</button>'
    : '';

  return '<section class="card" id="cultMasteryWindow" style="margin-top:16px">' +
    '<div class="section-title">技能专精升级（独立小窗口 · 与干员培养异步）</div>' +
    '<div class="cult-toolbar">' +
      '<div class="cult-field"><label>自动推进开关</label>' +
        '<div class="cult-switch"><input type="checkbox" id="cultMasteryEnabled" ' +
        (mastery.enabled ? 'checked' : '') +
        ' onchange="saveCultivationMasterySettings(this.checked)">' +
        '<span>随日常任务收尾自动推进技能专精（与上方干员精英/等级培养完全异步，不接管日常理智刷图）</span></div></div>' +
      '<div class="cult-field"><label>专精操作</label><div class="cult-actions">' +
        '<button class="btn" id="cultMasteryAddBtn" onclick="cultivationOpenMasteryDraft(\'\')">+ 添加专精计划</button>' +
        '<button class="btn btn-p" id="cultMasteryRunBtn" onclick="startCultivationMasteryRun()" ' +
        (state.runPending ? 'disabled' : '') + '>立即执行专精</button>' +
      '</div>' +
      '<div class="cult-note">支持训练室 1~3 技能专精一至三，缺高阶材料时自动赴加工站递归合成下位材料。</div></div>' +
    '</div>' +
    '<div class="cult-note" style="margin-top:10px">' + esc(mRuntimeLine) + '</div>' +
    (state.masteryError ? '<div class="cult-error">' + esc(state.masteryError) + '</div>' : '') +
    (state.masteryNotice ? '<div class="cult-result">' + esc(state.masteryNotice) + '</div>' : '') +
    '<div class="cult-card" style="margin-top:14px">' +
      '<div class="cult-section-title">干员头像快速选择（专精中与队列干员自动置顶 · 点击头像配置专精并预测库存）</div>' +
      renderCultivationMasteryFilterBar() +
      '<div id="cultMasteryPickerMount">' + renderCultivationMasteryAvatarGrid(candidates) + '</div>' +
    '</div>' +
    '<div id="cultMasteryDraftMount">' +
      (state.masteryDraft ? renderCultivationMasteryDraftForm(state.masteryDraft, candidates) : '') +
    '</div>' +
    '<div class="cult-card" style="margin-top:14px"><div class="cult-section-title" style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap">' +
      '<span>专精计划队列（专精中默认优先 · 支持自由置顶/上下排序 · 自动结合仓库库存预测缺口）</span>' +
      autoSortBtn +
    '</div>' +
      (planRows || '<div class="cult-empty">还没有技能专精计划，点击上方任意干员头像或“+ 添加专精计划”即可配置。</div>') +
    '</div>' +
  '</section>';
}
