// Weekly editor: saved snapshots, editable draft, stage selection, and persistence.
function cloneWeekly(value) {
  return JSON.parse(JSON.stringify(value));
}

function medicineErrorMessage(payload) {
  const error = payload && payload.error;
  if (typeof error === 'string') return error;
  if (error && typeof error.message === 'string') return error.message;
  return '读取理智药计划失败';
}

function applyMedicinePayload(payload, options={}) {
  if (!S.medicine) S.medicine = {loaded:false, loading:false, data:null, error:'', draft:null, draftDirty:false, saving:false};
  const state = S.medicine;
  const previousDraft = state.draft && {
    enabled: state.draft.enabled === true,
    threshold: state.draft.threshold
  };
  const keepDraft = options.preserveDraft === undefined
    ? state.draftDirty === true : options.preserveDraft === true;
  state.loaded = true;
  state.loading = false;
  state.error = '';
  state.data = payload || {};
  if (keepDraft && previousDraft) {
    state.draft = previousDraft;
  } else {
    state.draft = {
      enabled: payload && payload.enabled === true,
      threshold: payload && Number.isInteger(payload.threshold) ? payload.threshold : 80
    };
    state.draftDirty = false;
  }
}

function setMedicineEnabled(enabled) {
  if (!S.medicine) return;
  if (!S.medicine.draft) S.medicine.draft = {enabled:false, threshold:80};
  S.medicine.draft.enabled = !!enabled;
  S.medicine.draftDirty = true;
  render();
}

function setMedicineThreshold(value) {
  if (!S.medicine) return;
  if (!S.medicine.draft) S.medicine.draft = {enabled:false, threshold:80};
  const threshold = Number.parseInt(value, 10);
  S.medicine.draft.threshold = Number.isInteger(threshold) ? threshold : 80;
  S.medicine.draftDirty = true;
  render();
}

async function reloadMedicinePlan() {
  const state = S.medicine || (S.medicine = {loaded:false, loading:false, data:null, error:'', draft:{enabled:false, threshold:80}, draftDirty:false, saving:false});
  state.loading = true;
  try {
    const payload = await api('medicine-plan');
    if (!payload || payload.ok === false) throw new Error(medicineErrorMessage(payload));
    applyMedicinePayload(payload);
    return true;
  } catch (error) {
    state.error = '读取理智药计划失败：' + (error.message || error);
    state.loaded = !!state.data;
    return false;
  } finally {
    state.loading = false;
    if (S.page === 'weekly' || S.page === 'home') render();
  }
}

function formatMedicineTimestamp(value) {
  if (!value) return '未知';
  return esc(String(value).replace('T', ' '));
}

function medicineEvidenceStatus(evidence) {
  if (evidence && evidence.expires_at && new Date(evidence.expires_at).getTime() <= Date.now()) return '已过期，待重新识别';
  return evidence && evidence.accurate && evidence.expires_at ? '可用于排期' : '期限不足';
}

function medicineAttemptStatusLabel(status) {
  return ({attempted:'已开始', finished:'已结束待复核', failed:'执行失败'})[status] || (status ? '已记录' : '无本批次记录');
}

function medicineDecisionLabel(action) {
  return ({drain:'到点集中用药', wait:'等待下一场', skip:'超过阈值跳过', blocked:'已阻止', completed:'本批次已处理', disabled:'未启用'})[action] || action || '未知';
}

function medicinePolicyText(payload) {
  const evidence = (payload && payload.evidence) || {};
  if ((payload && payload.needs_observation) || !evidence.accurate || !evidence.remaining_count) {
    return '证据未知时保留原有一天内到期药兜底，以取得后续识别证据；不会新增药量。';
  }
  if (((payload || {}).decision || {}).action === 'wait') {
    return '已知期限等待期间其他作战禁药；仅在最后匹配的 02:00 场次进入集中用药判断。';
  }
  return '已知期限按最后匹配的 02:00 场次判断，其他作战保持禁药。';
}

function renderMedicinePlan() {
  const state = S.medicine || {loaded:false, data:null, error:'', draft:{enabled:false, threshold:80}, saving:false};
  if (!state.loaded || !state.data) {
    return `<section class="card medicine-plan"><div class="section-title">理智药策略</div><div class="help" role="status">理智药 API 读取失败：${esc(state.error || '暂无数据')}；周计划仍可正常编辑和保存。</div></section>`;
  }
  const payload = state.data;
  const evidence = payload.evidence || {};
  const decision = payload.decision || {};
  const last = payload.last_attempt || {};
  const draft = state.draft || {enabled:payload.enabled === true, threshold:Number.isInteger(payload.threshold) ? payload.threshold : 80};
  const rules = Array.isArray(payload.rules) ? payload.rules : [];
  const slots = Array.isArray(payload.slots) ? payload.slots : [];
  const rows = rules.length ? rules.map(rule => {
    const slot = slots.find(item => item.stage === rule.stage);
    const next = slot
      ? `${formatMedicineTimestamp(slot.run_at)}（游戏日 ${esc(slot.game_date || '未知')}）`
      : '无可用 02:00 场次';
    return `<li><strong>${esc(rule.material_name || rule.item_id || '未知材料')}</strong> · ${esc(rule.stage || '未知关卡')}<span class="help">下一次 02:00 用药日：${next}</span></li>`;
  }).join('') : '<li>当前周计划没有匹配官方限时关卡材料。</li>';
  const confirmed = Number.isInteger(last.confirmed_used) ? last.confirmed_used : '未知';
  const attemptStatus = medicineAttemptStatusLabel(last.status);
  const loadNotice = state.error ? `<div class="help" role="status">理智药 API 读取失败：${esc(state.error)}；周计划仍可正常编辑和保存。</div>` : '';
  return `<section class="card medicine-plan">
    ${loadNotice}<div class="page-head"><div><div class="section-title">理智药策略</div><div class="help">每个匹配的限时关卡使用同一个全局材料阈值，默认 80。</div></div>
      <label class="switch-label">使用理智药 <input id="medicine-enabled" type="checkbox" ${draft.enabled?'checked':''} onchange="setMedicineEnabled(this.checked)"></label></div>
    <div class="info-grid">
      <div class="info-box"><span>药证据时刻 / 数量</span><strong>${formatMedicineTimestamp(evidence.observed_at)} / ${evidence.remaining_count ?? '未知'}</strong><div>来源：${esc(evidence.source || '未知')}；${medicineEvidenceStatus(evidence)}</div></div>
      <div class="info-box"><span>期限保守下界</span><strong>${formatMedicineTimestamp(evidence.expires_at)}</strong><div>${esc(evidence.reason || medicineEvidenceStatus(evidence))}</div></div>
      <div class="info-box"><span>阈值</span><input id="medicine-threshold" type="number" min="0" max="9999" step="1" value="${esc(String(draft.threshold))}" onchange="setMedicineThreshold(this.value)"></div>
      <div class="info-box"><span>当前决定</span><strong>${esc(medicineDecisionLabel(decision.action))}</strong><div>${esc(decision.reason || '暂无等待原因')}</div></div>
    </div>
    <div class="section-title">目标材料与下一次 02:00</div><ul class="medicine-rules">${rows}</ul>
    <div class="help">${esc(medicinePolicyText(payload))}</div>
    <div class="help">集中用药受 150 分钟运行时限约束；成功退出不代表药品已经全部使用。确认已用药：<strong>${esc(String(confirmed))}</strong>；本批次状态：<strong>${esc(String(attemptStatus))}</strong>。</div>
    <button class="btn btn-p" type="button" onclick="saveMedicineSettings()" ${state.saving?'disabled':''}>${state.saving?'保存中…':'保存理智药设置'}</button>
  </section>`;
}

async function saveMedicineSettings(enabled, threshold) {
  const state = S.medicine || (S.medicine = {loaded:false, loading:false, data:null, error:'', draft:{enabled:false, threshold:80}, saving:false});
  const draft = state.draft || {enabled:false, threshold:80};
  if (typeof enabled !== 'boolean') enabled = !!document.getElementById('medicine-enabled')?.checked;
  if (threshold === undefined) threshold = Number.parseInt(document.getElementById('medicine-threshold')?.value, 10);
  if (!Number.isInteger(threshold) || threshold < 0 || threshold > 9999) {
    showMsg('err', '阈值必须是 0 至 9999 的整数');
    return false;
  }
  draft.enabled = enabled;
  draft.threshold = threshold;
  state.draft = draft;
  state.saving = true;
  render();
  let result = false;
  let message = ['err', '保存未确认'];
  try {
    const posted = await api('medicine-plan/settings', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled, threshold})
    });
    if (!posted || posted.ok === false) throw new Error(medicineErrorMessage(posted));
    const verified = await api('medicine-plan');
    if (!verified || verified.ok === false) throw new Error(medicineErrorMessage(verified));
    if (verified.enabled !== enabled || verified.threshold !== threshold) {
      throw new Error('读取结果与本次设置不一致');
    }
    applyMedicinePayload(verified, {preserveDraft:false});
    result = true;
    message = ['ok', '理智药设置已保存并读取确认 ✓'];
  } catch (error) {
    state.error = '保存未确认：' + (error.message || error);
    message = ['err', state.error];
  } finally {
    state.saving = false;
    if (S.page === 'weekly') render();
  }
  showMsg(message[0], message[1]);
  return result;
}

function weeklyDraft(weekday) {
  return cloneWeekly(S.weeklyDays[weekday] || []);
}

function weeklyTaskFingerprint(tasks) {
  const ordered = value => {
    if (Array.isArray(value)) return value.map(ordered);
    if (value && typeof value === 'object') {
      return Object.fromEntries(Object.keys(value).sort().map(key => [key, ordered(value[key])]));
    }
    return value;
  };
  // The TOML codec omits empty params/variants and adds the default enabled flag.
  return JSON.stringify(ordered(tasks.map(task => ({
    ...task, _enabled:task._enabled !== false, params:task.params || {}, variants:task.variants || []
  }))));
}

async function resolveWeeklyDraft() {
  if (S.weeklySaving) return false;
  if (!S.weeklyDirty) return true;
  if (confirm('当前星期有未保存修改，是否先保存？')) return await saveWeeklyDay();
  if (!confirm('确定放弃当前星期的未保存修改？')) return false;
  S.tasks = weeklyDraft(S.weeklyDay);
  S.weeklyDirty = false;
  return true;
}

function applyWeeklyPayload(payload) {
  S.weeklyPlan = payload;
  S.weeklyDays = {};
  (payload.days || []).forEach(day => { S.weeklyDays[day.weekday] = cloneWeekly(day.tasks || []); });
  if (!S.weeklyDay || !PLAN_WEEKDAYS.some(day => day.v === S.weeklyDay)) {
    S.weeklyDay = payload.current_weekday || 'mon';
  }
  if (!S.weeklyPlan.initialized) S.weeklyDay = payload.current_weekday || 'mon';
  S.weeklyDirty = false;
}

function applyWeeklyOperationPayload(payload) {
  // A response may arrive after navigation. Never replace the daily editor's tasks.
  const keepDraft = S.page === 'weekly' && S.weeklyDirty;
  applyWeeklyPayload(payload);
  if (keepDraft) S.weeklyDirty = true;
  else if (S.page === 'weekly') S.tasks = weeklyDraft(S.weeklyDay);
}

async function toggleWeeklyPlan(enabled) {
  if (S.weeklyBusy || S.weeklySaving) return;
  if (!(await resolveWeeklyDraft())) { render(); return; }
  if (enabled && !S.weeklyPlan.initialized && !confirm('首次启用会把当前 daily.toml 复制到周一至周日，是否继续？')) {
    render();
    return;
  }
  S.weeklyBusy = true;
  render();
  let message;
  try {
    const result = await api('weekly-plan/settings', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled})
    });
    if (!result.ok) throw new Error(result.error || '服务拒绝了请求');
    const payload = await api('weekly-plan');
    applyWeeklyOperationPayload(payload);
    await reloadMedicinePlan();
    message = ['ok', enabled ? '周计划已启用 ✓' : '周计划已关闭，恢复 daily.toml ✓'];
  } catch (error) {
    message = ['err', '周计划设置未确认: ' + error.message];
  } finally {
    S.weeklyBusy = false;
    if (S.page === 'weekly') render();
  }
  showMsg(...message);
}

async function switchWeeklyDay(weekday) {
  if (S.weeklyBusy) return;
  if (!PLAN_WEEKDAYS.some(day => day.v === weekday) || weekday === S.weeklyDay) return;
  if (!(await resolveWeeklyDraft())) return;
  S.weeklyDay = weekday;
  S.tasks = weeklyDraft(weekday);
  S.sel = S.tasks.length ? 0 : -1;
  S.weeklyDirty = false;
  render();
}

async function saveWeeklyDay() {
  if (S.weeklySaving || S.weeklyBusy) return false;
  const weekday = S.weeklyDay;
  const submitted = cloneWeekly(S.tasks);
  const date = dateForPlanWeekday(S.weeklyDay);
  const fightIndex = getWeeklyFarmFightIndex(S.tasks, S.weeklyDay, date);
  if (fightIndex >= 0) {
    const stage = getFightStageForDate(S.tasks[fightIndex], S.weeklyDay, date).stage;
    const availability = getStageAvailability(stage, S.weeklyDay, date);
    if (isInvalidStageAvailability(availability)) {
      showMsg('err', `无法保存：${stage || '所选关卡'} ${availability.label}`);
      return false;
    }
  }
  S.weeklySaving = true;
  try {
    const result = await api('weekly-plan/day', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({weekday, tasks:submitted})
    });
    if (!result.ok) {
      showMsg('err', '保存失败: ' + (result.error || ''));
      return false;
    }
    const payload = await api('weekly-plan');
    const saved = (payload.days || []).find(day => day.weekday === weekday);
    if (!saved || !saved.valid || !Array.isArray(saved.tasks)) {
      throw new Error('已提交，但无法确认保存后的配置，请重试读取');
    }
    if (weeklyTaskFingerprint(saved.tasks) !== weeklyTaskFingerprint(submitted)) {
      throw new Error('读取结果与本次提交不一致，已保留编辑内容，请核对后重试');
    }
    S.weeklyDays[weekday] = cloneWeekly(saved.tasks);
    if (JSON.stringify(S.tasks) === JSON.stringify(submitted)) {
      S.tasks = weeklyDraft(weekday);
      S.weeklyDirty = false;
    }
    await reloadMedicinePlan();
    render();
    showMsg('ok', `${planWeekdayName(weekday)}配置已保存并读取确认 ✓${S.weeklyDirty?'；仍有新的未保存修改':''}`);
    return !S.weeklyDirty;
  } catch (error) {
    showMsg('err', '保存未确认: ' + error.message);
    return false;
  } finally {
    S.weeklySaving = false;
  }
}

async function copyWeeklyDay() {
  if (S.weeklySaving || S.weeklyBusy) return;
  const source = S.weeklyDay;
  const targets = PLAN_WEEKDAYS
    .map(day => day.v)
    .filter(day => day !== S.weeklyDay && document.getElementById('copy_' + day)?.checked);
  if (!targets.length) {
    showMsg('err', '请至少选择一个目标星期');
    return;
  }
  if (S.weeklyDirty && !(await saveWeeklyDay())) return;
  const names = targets.map(planWeekdayName).join('、');
  if (!confirm(`复制当前日到 ${names}？目标配置会被完整覆盖。`)) return;
  S.weeklyBusy = true;
  render();
  let message;
  try {
    const result = await api('weekly-plan/copy', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({source, targets})
    });
    if (!result.ok) throw new Error(result.error || '服务拒绝了请求');
    const payload = await api('weekly-plan');
    applyWeeklyOperationPayload(payload);
    await reloadMedicinePlan();
    message = ['ok', `已复制到 ${names} ✓`];
  } catch (error) {
    message = ['err', '复制未确认: ' + error.message];
  } finally {
    S.weeklyBusy = false;
    if (S.page === 'weekly') render();
  }
  showMsg(...message);
}

function renderWeeklyPlan() {
  const wp = S.weeklyPlan || {};
  const selectedDate = dateForPlanWeekday(S.weeklyDay);
  const fightIndex = getWeeklyFarmFightIndex(S.tasks, S.weeklyDay, selectedDate);
  const fightTask = fightIndex >= 0 ? S.tasks[fightIndex] : null;
  const checksAnnihilation = hasWeeklyAnnihilationFight(S.tasks, S.weeklyDay, selectedDate);
  const stage = fightTask ? getFightStageForDate(fightTask, S.weeklyDay, selectedDate).stage : '';
  const availability = getStageAvailability(stage, S.weeklyDay, selectedDate);
  const optionValues = [];
  const addOption = (value, label) => {
    if (!value || optionValues.some(item => item.value === value)) return;
    optionValues.push({value,label});
  };
  (S.stages || []).forEach(group => (group.stages || []).forEach(item => addOption(item.Value, `${item.Display || item.Value}${item.Drop?' · '+item.Drop:''}`)));
  commonStageEntries().forEach(item => addOption(item.value, `${stageDisplayLabel(item.value)}${stageDropLabel(item.value)?' · '+stageDropLabel(item.value):''}`));
  if (stage) addOption(stage, stageDisplayLabel(stage));
  const stageOptions = optionValues.map(item => {
    const itemAvailability = getStageAvailability(item.value, S.weeklyDay, selectedDate);
    const disabled = isInvalidStageAvailability(itemAvailability) ? ' disabled' : '';
    const suffix = itemAvailability.state === 'closed' ? '（当天未开放）' : itemAvailability.state === 'unverified' ? '（导航待确认）' : itemAvailability.state === 'unsupported' ? '（不可导航）' : itemAvailability.state === 'expired' ? '（已结束）' : '';
    return `<option value="${esc(item.value)}" ${item.value===stage?'selected':''}${disabled}>${esc(item.label)} ${esc(suffix)}</option>`;
  }).join('');
  const targets = PLAN_WEEKDAYS.filter(day => day.v !== S.weeklyDay).map(day =>
    `<label style="font-size:12px"><input id="copy_${day.v}" type="checkbox"> ${esc(day.n)}</label>`
  ).join(' ');
  const mode = wp.enabled
    ? `已启用，自动运行${planWeekdayName(wp.current_weekday)}配置`
    : '未启用，定时与默认手动运行仍使用 daily.toml';
  const validationClass = availability.state === 'open' ? 'open' : isInvalidStageAvailability(availability) ? 'invalid' : 'unknown';
  const infrastTask = S.tasks.find(task => task.type === 'Infrast' && task._enabled !== false);
  const infrastBlock = S.infrastSchedule && S.infrastSchedule.manifestCurrent
    ? `<div class="info-box"><span>基建所有权</span><strong>独立基建排班已接管</strong><div class="infrast-plan-meta">本页不重复显示基础 Infrast 编辑；原任务仍保留。</div></div>`
    : `<div class="info-box"><span>基础基建</span><strong>${infrastTask?'已启用':'未启用'}</strong><div class="infrast-plan-meta">基础基建任务配置${infrastTask?'继续随当日计划运行':'当前日没有启用的 Infrast 任务'}</div></div>`;
  return `<fieldset ${S.weeklyBusy?'disabled':''} style="border:0;margin:0;padding:0;min-width:0">
    ${S.weeklyBusy?'<div class="help" role="status">正在处理，请稍候…</div>':''}
    <div class="page-head"><div><h2>周计划</h2><p>${esc(mode)}。关闭不会删除七天配置。</p></div><label class="switch-label">周计划已${wp.enabled?'启用':'关闭'} <input type="checkbox" ${wp.enabled?'checked':''} onchange="toggleWeeklyPlan(this.checked)"></label></div>
    <div class="help">以下预览各星期下一次游戏日的配置；北京时间 04:00 换日，活动按对应日期判断。</div>
    ${renderWeekStrip({clickable:true,selected:S.weeklyDay,preferConfigured:true})}
    <section class="card weekly-editor"><section><div class="section-title">${esc(planWeekdayName(S.weeklyDay))}计划${S.weeklyDirty?' · 未保存':''}</div>
      <label class="eyebrow" for="weekly-stage-select">当日关卡</label>
      <div class="help">重新选关会替换该任务原有选关规则；活动关卡按自身时间窗生效，结束后回落兜底。其他任务和用药参数保留。</div>
      ${fightTask ? `<select id="weekly-stage-select" class="stage-select" onchange="setWeeklyPrimaryStage(this.value)">${stageOptions}</select>
      <div class="stage-validation ${validationClass}">关卡开放状态：${esc(availability.label)}</div>` : '<div class="help">当前日没有启用的作战任务，请在高级任务配置中添加。</div>'}
    </section><section><div class="section-title">执行摘要</div><div class="info-grid">
      <div class="info-box"><span>活动覆盖</span><strong>${fightTask && getFightStageForDate(fightTask,S.weeklyDay,selectedDate).source==='activity'?'已生效':'无'}</strong></div>
      ${fightTask && getFightModel(fightTask).activityEnabled ? `<div class="info-box"><span>活动结束后兜底</span><strong>${esc(getFightModel(fightTask).baseStage || '未设置')}</strong></div>` : ''}
      <div class="info-box"><span>任务</span><strong>${esc(fightTask ? (fightTask.name || '日常') : '未设置')}</strong></div>
      <div class="info-box"><span>剿灭</span><strong>${checksAnnihilation?'先执行，满额自动跳过':'未配置'}</strong></div>
      ${infrastBlock}<div class="info-box"><span>下次游戏日</span><strong>${esc(formatCalendarDate(selectedDate,true))}</strong></div>
    </div></section></section>
    ${renderMedicinePlan()}
    <div class="weekly-actions"><button class="btn btn-p" onclick="saveWeeklyDay()">保存${esc(planWeekdayName(S.weeklyDay))}计划</button><button class="btn btn-s" onclick="document.getElementById('weekly-copy-panel').open=true">复制到其他日期</button></div>
    <details class="card" id="weekly-copy-panel"><summary>复制当前日到其他日期</summary><div class="help">${targets}</div><button class="btn btn-s" style="margin-top:12px" onclick="copyWeeklyDay()">复制选中星期</button></details>
    <details class="advanced-panel"${S.weeklyAdvancedOpen?' open':''} ontoggle="setWeeklyAdvancedOpen(this.open)"><summary>高级任务配置</summary><div class="advanced-body">${renderTasks(true)}</div></details></fieldset>`;
}

function setWeeklyPrimaryStage(value) {
  if (S.weeklyBusy) return;
  const fightIndex = getWeeklyFarmFightIndex(S.tasks, S.weeklyDay, dateForPlanWeekday(S.weeklyDay));
  if (fightIndex < 0) return;
  const availability = getStageAvailability(value, S.weeklyDay);
  if (isInvalidStageAvailability(availability)) {
    render();
    showMsg('err', `无法选择：${value} ${availability.label}`);
    return;
  }
  try {
    S.tasks[fightIndex] = buildWeeklyStageTask(S.tasks[fightIndex], value);
  } catch (error) {
    render();
    showMsg('err', error.message);
    return;
  }
  markTaskDirty();
  render();
}

function setWeeklyAdvancedOpen(open) {
  S.weeklyAdvancedOpen = !!open;
}

function buildWeeklyStageTask(original, stage) {
  // Replace stage rules; never reuse the advanced editor's activity-field setter.
  if ((original.variants || []).length && original.strategy !== 'merge') {
    throw new Error('此任务使用 first 自定义条件，请在高级任务配置中修改选关规则');
  }
  const task = cloneWeekly(original);
  const model = getFightModel(original, original.params || {});
  const window = getFightActivityWindow(stage, model);
  let fallback = model.baseStage || '1-7';
  if (activityStageStatus(fallback)) fallback = '1-7';
  task.params = {...(task.params || {}), stage:window ? fallback : stage};
  task.variants = (task.variants || []).map(variant => {
    const next = cloneWeekly(variant);
    delete (next.params || {}).stage;
    return next;
  }).filter(variant => Object.keys(variant.params || {}).length > 0);
  if (window) task.variants.push({condition:{type:'DateTime', ...window}, params:{stage}});
  if (task.variants.length) {
    // Keep the task active even when no conditional parameter override matches.
    task.variants.unshift({params:{stage:task.params.stage}});
    task.strategy = 'merge';
  } else {
    delete task.strategy;
  }
  return task;
}
