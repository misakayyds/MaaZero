"""Atomic cultivation goals, verified inventory, and completion notification state."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid

_THREAD_LOCK = threading.RLock()
MAX_GOALS = 20
DEPOT_GOLD_ITEM_ID = '4001'
PROFESSION_TO_OPER_ROLE = {
    'PIONEER': 'Pioneer',
    'WARRIOR': 'Warrior',
    'TANK': 'Tank',
    'SNIPER': 'Sniper',
    'CASTER': 'Caster',
    'MEDIC': 'Medic',
    'SUPPORT': 'Support',
    'SPECIAL': 'Special',
}
OPER_PROGRESS_DETAIL_ENUM = {
    0: 'Completed',
    1: 'AlreadySatisfied',
    2: 'ResourceInsufficient',
    3: 'OperatorNotFound',
    4: 'PrerequisiteNotMet',
    5: 'ChipNotCraftable',
    6: 'FormulaLocked',
    7: 'Unsupported',
    8: 'RecognitionFailed',
    9: 'TrainingRoomBusy',
    10: 'PrerequisiteTraining',
    11: 'Interrupt',
}
OPER_PROGRESS_RESULT_ENUM = {
    0: 'Success',
    1: 'Failed',
    2: 'Skipped',
}
OPER_PROGRESS_DETAIL_LABELS = {
    'Completed': '目标专精训练已启动',
    'AlreadySatisfied': '已达到目标专精等级',
    'ResourceInsufficient': '材料不足（加工站自动合成后仍缺底层材料）',
    'OperatorNotFound': '未在干员列表中找到该干员',
    'PrerequisiteNotMet': '前置条件未满足（需精英阶段2且通用技能7级）',
    'ChipNotCraftable': '芯片无法自动合成',
    'FormulaLocked': '加工站合成配方未解锁',
    'Unsupported': '不支持的培养参数',
    'RecognitionFailed': '界面识别未完成',
    'TrainingRoomBusy': '训练室正忙（已有干员正在训练）',
    'PrerequisiteTraining': '前置专精训练已启动',
    'Interrupt': '任务中断',
}


def load_catalog(path):
    from maa_cultivation import load_catalog as loader
    return loader(path)


def validate_goal(catalog, goal):
    from maa_cultivation import validate_goal as validator
    return validator(catalog, goal)


def calculate_plan(catalog, goals, inventory):
    from maa_cultivation import calculate_plan as calculate
    return calculate(catalog, goals, inventory)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _default_state():
    return {'schema_version': 1, 'revision': 0,
            'settings': {'enabled': False, 'fallback_stage': '1-7'},
            'goals': [], 'inventory': {'available': False, 'items': {}, 'scanned_at': ''},
            'currency': {'available': False, 'balance': 0, 'source': 'unknown',
                         'confirmed_at': '', 'updated_at': '', 'events': []},
            'runtime': {'status': 'idle', 'message': '', 'updated_at': ''},
            'mastery': {'enabled': False, 'plans': [],
                        'runtime': {'status': 'idle', 'message': '', 'updated_at': ''}},
            'notifications': {}}


def load_state(config_dir):
    path = Path(config_dir) / 'cultivation/state.json'
    if not path.exists():
        return _default_state()
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise ValueError('培养状态无法读取，请检查状态文件；未覆盖现有数据') from exc
    if (not isinstance(state, dict) or state.get('schema_version') != 1
            or type(state.get('revision')) is not int
            or not isinstance(state.get('goals'), list)
            or any(not isinstance(state.get(key), dict)
                   for key in ('settings', 'inventory', 'runtime', 'notifications'))):
        raise ValueError('培养状态格式无效；未覆盖现有数据')
    state.setdefault('currency', _default_state()['currency'])
    currency = state['currency']
    if not isinstance(currency, dict):
        raise ValueError('龙门币状态格式无效；未覆盖现有数据')
    currency.setdefault('source', 'manual' if currency.get('available') else 'unknown')
    currency.setdefault('confirmed_at', '')
    currency.setdefault('updated_at', '')
    currency.setdefault('events', [])
    if (not isinstance(currency, dict) or type(currency.get('available')) is not bool
            or type(currency.get('balance')) is not int or currency['balance'] < 0
            or currency.get('source') not in ('depot', 'manual', 'unknown')
            or not isinstance(currency.get('events', []), list)):
        raise ValueError('龙门币状态格式无效；未覆盖现有数据')
    state.setdefault('mastery', _default_state()['mastery'])
    mastery = state['mastery']
    if not isinstance(mastery, dict):
        raise ValueError('专精状态格式无效；未覆盖现有数据')
    mastery.setdefault('enabled', False)
    mastery.setdefault('plans', [])
    mastery.setdefault('runtime', {'status': 'idle', 'message': '', 'updated_at': ''})
    if (type(mastery.get('enabled')) is not bool
            or not isinstance(mastery.get('plans'), list)
            or not isinstance(mastery.get('runtime'), dict)):
        raise ValueError('专精状态格式无效；未覆盖现有数据')
    return state


@contextmanager
def _state_lock(config_dir):
    """WebUI threads and runner processes share an OS lock (no stale PID locks)."""
    directory = Path(config_dir) / 'cultivation'
    directory.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK:
        with (directory / 'state.lock').open('a+b') as handle:
            if os.name == 'nt':
                import msvcrt
                if handle.tell() == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == 'nt':
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def _write(config_dir, state):
    directory = Path(config_dir) / 'cultivation'
    fd, name = tempfile.mkstemp(prefix='.state-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, directory / 'state.json')
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _check_revision(state, body):
    if 'revision' in body and (type(body['revision']) is not int or body['revision'] != state['revision']):
        raise ValueError('培养目标已被其他操作修改，请刷新后重试')


def _object(body):
    if not isinstance(body, dict):
        raise ValueError('请求必须是对象')


def _inventory_snapshot_status(inventory):
    scanned_at = inventory.get('scanned_at', '')
    try:
        scanned = datetime.fromisoformat(str(scanned_at).replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - scanned).total_seconds()
    except (ValueError, TypeError):
        age = None
    last_error = str(inventory.get('last_error', '') or '')
    stale = (not inventory.get('available') or age is None or age < 0
             or age > 86400 or bool(last_error))
    return {
        'available': bool(inventory.get('available')),
        'scanned_at': scanned_at,
        'stale': stale,
        'last_error': last_error,
        'age_seconds': age,
    }


def _bool(body, key):
    if key in body and type(body[key]) is not bool:
        raise ValueError(f'{key} 必须为布尔值')


def _payload(config_dir, catalog, state):
    items = dict(state['inventory']['items'])
    gold = (catalog.get('currency') or {}).get('gold_item_id', DEPOT_GOLD_ITEM_ID)
    items.pop(gold, None)
    if state['currency']['available']:
        items[gold] = state['currency']['balance']
    plan = calculate_plan(catalog, state['goals'], items)
    if not state['currency']['available']:
        for row in [plan, *plan.get('goals', [])]:
            row['farm'] = [entry for entry in row.get('farm', []) if entry.get('item_id') != gold]
            row.get('missing', {}).pop(gold, None)
            if row.get('lmd', {}).get('total', 0) or row.get('requirements', {}).get(gold, 0):
                row['ready'] = False
                if row.get('status') in ('ready', 'missing'):
                    row['status'] = 'blocked'
                row.setdefault('blocked', []).append({'item_id': gold, 'count': 0,
                    'reason': 'currency_unknown', 'detail': '请先识别仓库确认龙门币余额'})
        if any(row.get('reason') == 'currency_unknown' for g in plan.get('goals', []) for row in g.get('blocked', [])):
            plan['ready'] = False
            if not any(x.get('reason') == 'currency_unknown' for x in plan.get('blocked', [])):
                plan.setdefault('blocked', []).append({'item_id': gold, 'count': 0,
                    'reason': 'currency_unknown', 'detail': '请先识别仓库确认龙门币余额'})
    from maa_operator_box import load_scan_status
    operator_status = load_scan_status(Path(config_dir) / 'operator-box.json',
                                      Path(config_dir) / 'operator-box-state.json')
    snapshot = operator_status.get('snapshot') or {}
    attempt = operator_status.get('last_attempt') or {}
    inventory = state['inventory']
    inventory_status = _inventory_snapshot_status(inventory)
    inventory_verified = inventory_status['available'] and not inventory_status['stale']
    plan['inventory_verified'] = inventory_verified
    plan['ready'] = bool(inventory_verified and plan.get('ready'))
    for row in plan.get('goals', []):
        if not isinstance(row, dict):
            continue
        row['inventory_verified'] = inventory_verified
        row['ready'] = bool(
            row.get('status') == 'completed'
            or (inventory_verified and row.get('status') == 'ready')
        )
    snapshot_status = {
        'inventory': inventory_status,
        'operators': {'available': operator_status['available'],
                      'scanned_at': snapshot.get('scanned_at', ''),
                      'stale': operator_status['stale'],
                      'last_error': attempt.get('message', '') if attempt.get('status') == 'failed' else ''},
    }
    return {**state, 'plan': plan, 'catalog': catalog, 'operator_snapshot': snapshot,
            'snapshot_status': snapshot_status}


def get_payload(config_dir, catalog_path):
    return _payload(config_dir, load_catalog(catalog_path), load_state(config_dir))


def update_settings(config_dir, catalog_path, body):
    _object(body)
    _bool(body, 'enabled')
    catalog = load_catalog(catalog_path)
    if 'fallback_stage' in body:
        known = {stage['stage'] for stage in catalog.get('stages', [])}
        if not isinstance(body['fallback_stage'], str) or body['fallback_stage'] not in known:
            raise ValueError('备用关卡必须选择已支持的常驻关卡')
    with _state_lock(config_dir):
        state = load_state(config_dir)
        _check_revision(state, body)
        for key in ('enabled', 'fallback_stage'):
            if key in body:
                state['settings'][key] = body[key]
        state['revision'] += 1
        _write(config_dir, state)
    return _payload(config_dir, catalog, state)


def save_goal(config_dir, catalog_path, body):
    _object(body)
    for key in ('enabled', 'completed'):
        _bool(body, key)
    catalog = load_catalog(catalog_path)
    with _state_lock(config_dir):
        state = load_state(config_dir)
        _check_revision(state, body)
        goal_id = body.get('id')
        existing = next((g for g in state['goals'] if g['id'] == goal_id), None)
        if goal_id and existing is None:
            raise ValueError('培养目标不存在')
        if existing is None and len(state['goals']) >= MAX_GOALS:
            raise ValueError('最多保存 20 个培养目标')
        fields = ('operator_id', 'current_elite', 'current_level', 'target_elite', 'target_level')
        candidate = dict(existing or {})
        candidate.update({k: body[k] for k in fields if k in body})
        candidate.update({'id': goal_id or uuid.uuid4().hex,
                          'enabled': body.get('enabled', candidate.get('enabled', True)),
                          'completed': body.get('completed', candidate.get('completed', False))})
        normalized = validate_goal(catalog, candidate)
        if 'valid' in normalized:
            if not normalized['valid']:
                errors = normalized.get('errors', [])
                raise ValueError('培养目标无效：' + '；'.join(str(e.get('message', e.get('code', '参数错误'))) for e in errors))
            candidate.update(normalized['goal'])
        else:
            candidate.update(normalized)
        candidate['updated_at'] = _now()
        candidate.pop('verified_at', None)
        candidate.pop('completion_source', None)
        if candidate.get('completed') and not (existing or {}).get('completed'):
            state['inventory']['available'] = False
            _mark_currency_unknown(state, '目标完成后需要重新识别仓库')
        if existing is None:
            state['goals'].append(candidate)
        else:
            state['goals'][state['goals'].index(existing)] = candidate
        state['revision'] += 1
        # Calculate before writing so invalid cross-goal plans never replace good state.
        calculate_plan(catalog, state['goals'], state['inventory']['items'])
        _write(config_dir, state)
    return _payload(config_dir, catalog, state)


def _move_list_item(items, target, move):
    if not isinstance(move, str) or move not in ('top', 'up', 'down', 'bottom'):
        raise ValueError('排序方向无效')
    idx = items.index(target)
    items.pop(idx)
    if move == 'top':
        new_idx = 0
    elif move == 'up':
        new_idx = max(0, idx - 1)
    elif move == 'down':
        new_idx = min(len(items), idx + 1)
    else:
        new_idx = len(items)
    items.insert(new_idx, target)


def _mastery_plan_auto_tier(plan):
    if plan.get('completed') or plan.get('status') == 'completed':
        return 4
    if not plan.get('enabled', True):
        return 3
    status = plan.get('status', '')
    if status == 'training' or int(plan.get('training_skill', 0) or 0) > 0:
        return 0
    cur = plan.get('current_mastery') or [0, 0, 0]
    if status == 'partial' or any(isinstance(v, int) and v > 0 for v in cur):
        return 1
    return 2


def set_goal_state(config_dir, catalog_path, body):
    _object(body)
    for key in ('enabled', 'completed'):
        _bool(body, key)
    catalog = load_catalog(catalog_path)
    with _state_lock(config_dir):
        state = load_state(config_dir)
        _check_revision(state, body)
        goal = next((g for g in state['goals'] if g['id'] == body.get('id')), None)
        if goal is None:
            raise ValueError('培养目标不存在')
        if (body.get('completed') is False
                and (goal['current_elite'], goal['current_level']) >= (goal['target_elite'], goal['target_level'])):
            raise ValueError('实际练度已达到目标，请先编辑一个更高的目标')
        if body.get('completed') is True and not goal.get('completed'):
            # The user may have consumed these resources outside MAA.
            state['inventory']['available'] = False
            _mark_currency_unknown(state, '目标完成后需要重新识别仓库')
        for key in ('enabled', 'completed'):
            if key in body:
                goal[key] = body[key]
        if 'move' in body and body['move'] is not None:
            _move_list_item(state['goals'], goal, body['move'])
        goal['updated_at'] = _now()
        state['revision'] += 1
        _write(config_dir, state)
    return _payload(config_dir, catalog, state)


def save_inventory(config_dir, items, *, scanned_at=None):
    if not isinstance(items, dict) or not items:
        raise ValueError('库存扫描为空；保留上次库存')
    if len(items) > 20000 or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9_]+', k)
                               or type(v) is not int or v < 0 or v > 10**12 for k, v in items.items()):
        raise ValueError('库存数量无效；保留上次库存')
    timestamp = scanned_at or _now()
    try:
        parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed > datetime.now(timezone.utc):
            raise ValueError('库存时间无效')
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('库存时间无效') from exc
    with _state_lock(config_dir):
        state = load_state(config_dir)
        state['inventory'] = {'available': True, 'items': dict(items), 'scanned_at': timestamp}
        if DEPOT_GOLD_ITEM_ID in items:
            confirmation = _now()
            state['currency'] = {
                'available': True,
                'balance': items[DEPOT_GOLD_ITEM_ID],
                'source': 'depot',
                'confirmed_at': confirmation,
                'updated_at': confirmation,
                'events': [],
            }
        else:
            _mark_currency_unknown(state, '本次仓库识别未返回龙门币，余额未知')
        _write(config_dir, state)
    return state['inventory']


def mark_inventory_failed(config_dir, message):
    """Retain the last snapshot for display without claiming this scan succeeded."""
    with _state_lock(config_dir):
        state = load_state(config_dir)
        state['inventory']['available'] = False
        state['inventory']['last_error'] = str(message)[:500]
        state['inventory']['attempted_at'] = _now()
        _mark_currency_unknown(state, '本次仓库识别失败，余额未知')
        _write(config_dir, state)


def _mark_currency_unknown(state, reason):
    currency = state['currency']
    currency['available'] = False
    currency['source'] = 'unknown'
    currency['reason'] = str(reason)[:500]
    currency['confirmed_at'] = ''
    currency['updated_at'] = _now()
    currency['events'] = []


def reconcile_operator_snapshot(config_dir, catalog_path):
    """Rebase active goals on a verified fresh scan before any target farming."""
    from maa_operator_box import load_scan_status
    catalog = load_catalog(catalog_path)
    scanned = load_scan_status(Path(config_dir) / 'operator-box.json',
                               Path(config_dir) / 'operator-box-state.json')
    if (not scanned['available'] or scanned['stale']
            or scanned.get('age_seconds') is None or scanned['age_seconds'] > 900):
        raise ValueError('干员复核未完成，本轮不按旧练度刷取')
    snapshot = scanned['snapshot']
    owners = {row['id']: row for row in snapshot['own_opers']}
    changes = []
    with _state_lock(config_dir):
        state = load_state(config_dir)
        for goal in state['goals']:
            if not goal.get('enabled') or goal.get('completed'):
                continue
            actual = owners.get(goal['operator_id'])
            if actual is None:
                raise ValueError('目标干员未在本次已拥有列表中确认，本轮暂停培养刷取')
            actual_level = (actual['elite'], actual['level'])
            target = (goal['target_elite'], goal['target_level'])
            caps = catalog['operators'].get(goal['operator_id'], {}).get('max_levels', [])
            if (actual['elite'] < 0 or actual['elite'] >= len(caps)
                    or not 1 <= actual['level'] <= caps[actual['elite']]):
                raise ValueError('干员复核练度超出有效范围，本轮暂停培养刷取')
            # A goal edited after this snapshot must be checked with a newer scan.
            if goal.get('verified_at') == snapshot['scanned_at']:
                continue
            if goal.get('updated_at') and datetime.fromisoformat(goal['updated_at'].replace('Z', '+00:00')) > datetime.fromisoformat(snapshot['scanned_at'].replace('Z', '+00:00')):
                raise ValueError('复核期间目标已修改，请重新开始复核')
            previous = (goal['current_elite'], goal['current_level'])
            if previous != actual_level or actual_level >= target:
                goal['current_elite'], goal['current_level'] = actual_level
                goal['completed'] = actual_level >= target
                if goal['completed']:
                    goal['completion_source'] = 'scan'
                goal['verified_at'] = snapshot['scanned_at']
                goal['updated_at'] = _now()
                changes.append({'id': goal['id'], 'before': list(previous),
                                'current': list(actual_level), 'completed': goal['completed']})
        if changes:
            # Observed cultivation can have spent money outside our drop ledger.
            _mark_currency_unknown(state, '复核发现练度变化，请重新识别仓库')
            state['revision'] += 1
            _write(config_dir, state)
    return {'changed': changes, 'scanned_at': snapshot['scanned_at']}


def confirm_balance(config_dir, catalog_path, body):
    _object(body)
    balance = body.get('balance')
    if type(balance) is not int or not 0 <= balance <= 10**12:
        raise ValueError('龙门币余额必须为非负整数')
    catalog = load_catalog(catalog_path)
    with _state_lock(config_dir):
        state = load_state(config_dir)
        _check_revision(state, body)
        timestamp = _now()
        state['currency'] = {'available': True, 'balance': balance, 'source': 'manual',
                             'confirmed_at': timestamp, 'updated_at': timestamp, 'events': []}
        state['revision'] += 1
        _write(config_dir, state)
    return _payload(config_dir, catalog, state)


def record_lmd_drops(config_dir, event_id, amount, *, confirmed_at=None):
    """Add only manual-balance drops; Depot snapshots are authoritative."""
    if (not isinstance(event_id, str) or not 1 <= len(event_id) <= 160
            or type(amount) is not int or not 0 <= amount <= 10**12):
        raise ValueError('龙门币掉落记录无效')
    with _state_lock(config_dir):
        state = load_state(config_dir)
        currency = state['currency']
        if (not currency['available'] or currency.get('source') != 'manual' or confirmed_at is None
                or confirmed_at != currency['confirmed_at'] or event_id in currency.get('events', [])):
            return False
        currency['balance'] += amount
        currency.setdefault('events', []).append(event_id)
        currency['updated_at'] = _now()
        _write(config_dir, state)
    return True


def invalidate_balance(config_dir, reason):
    with _state_lock(config_dir):
        state = load_state(config_dir)
        _mark_currency_unknown(state, reason)
        _write(config_dir, state)


def set_runtime(config_dir, status, message):
    if not isinstance(status, str) or not isinstance(message, str):
        raise ValueError('运行状态无效')
    with _state_lock(config_dir):
        state = load_state(config_dir)
        state['runtime'] = {'status': status[:80], 'message': message[:1000], 'updated_at': _now()}
        _write(config_dir, state)


def _notification_key(goal):
    values = {k: goal[k] for k in ('id', 'operator_id', 'current_elite', 'current_level', 'target_elite', 'target_level')}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def pending_notifications(config_dir, catalog_path, *, include_goals=True, include_uncompleted_goals=True):
    payload = get_payload(config_dir, catalog_path)
    if not include_goals:
        return _pending_mastery_notifications(payload)
    inventory_status = payload['snapshot_status']['inventory']
    if not inventory_status.get('available') or inventory_status.get('stale'):
        return _pending_mastery_notifications(payload)
    age = inventory_status.get('age_seconds')
    if age is None or age < 0 or age > 900:
        return _pending_mastery_notifications(payload)
    scanned = datetime.fromisoformat(inventory_status['scanned_at'].replace('Z', '+00:00'))
    result = []
    plan_rows = {g['id']: g for g in payload['plan']['goals']}
    for goal in payload['goals']:
        if not goal['enabled']:
            continue
        if goal['completed']:
            if goal.get('completion_source') != 'scan':
                continue
            operator_status = payload['snapshot_status']['operators']
            if not operator_status['available'] or operator_status['stale']:
                continue
            key = hashlib.sha256(('achieved:' + _notification_key(goal)).encode()).hexdigest()
            if key not in payload['notifications']:
                name = payload['catalog']['operators'][goal['operator_id']]['name']
                fallback = payload['settings']['fallback_stage']
                text = (f'[MAA] {name} 培养目标已达成\n'
                        f'复核练度：精英 {goal["current_elite"]}，等级 {goal["current_level"]}\n'
                        f'不再为该目标刷取；其余目标处理完成后继续备用关卡 {fallback}（开放且可用时）。')
                result.append({'key': key, 'text': text})
            continue
        if not include_uncompleted_goals:
            continue
        if scanned < datetime.fromisoformat(goal['updated_at'].replace('Z', '+00:00')):
            continue
        row = plan_rows.get(goal['id'], {})
        manual_blocks = [
            item for item in row.get('blocked', [])
            if isinstance(item, dict) and item.get('reason') == 'manual_exchange'
        ]
        if row.get('status') == 'blocked' and manual_blocks:
            signature = {
                'goal': _notification_key(goal),
                'blocked': [
                    {key: item.get(key, '') for key in ('item_id', 'count', 'reason', 'detail')}
                    for item in manual_blocks
                ],
            }
            key = hashlib.sha256(
                ('manual_exchange:' + json.dumps(signature, sort_keys=True, ensure_ascii=False)).encode()
            ).hexdigest()
            if key not in payload['notifications']:
                name = payload['catalog']['operators'][goal['operator_id']]['name']
                lines = []
                for item in manual_blocks:
                    item_name = payload['catalog']['items'].get(
                        item.get('item_id'), {}
                    ).get('name', item.get('item_id', '未知材料'))
                    detail = item.get('detail', '')
                    lines.append(f'{item_name} ×{item.get("count", 0)}' + (f'（{detail}）' if detail else ''))
                text = (f'[MAA] {name} 培养目标存在人工兑换阻塞\n'
                        f'目标：精英 {goal["target_elite"]}，等级 {goal["target_level"]}\n'
                        '请手动兑换以下材料后重新库存识别：\n' + '\n'.join(lines))
                result.append({'key': key, 'text': text[:3900]})
            continue
        if row.get('status') != 'ready' or row.get('missing'):
            continue
        key = _notification_key(goal)
        if key in payload['notifications']:
            continue
        name = payload['catalog']['operators'][goal['operator_id']]['name']
        text = f'[MAA] {name} 培养资源已齐备\n目标：精英 {goal["target_elite"]}，等级 {goal["target_level"]}\n请手动合成并培养，完成后在培养目标页标记完成。'
        crafts = row.get('crafts', [])
        if crafts:
            lines = []
            for craft in crafts:
                item_id = craft.get('item_id', craft.get('item', ''))
                item_name = payload['catalog']['items'].get(item_id, {}).get('name', item_id)
                lines.append(f'{item_name} × {craft.get("quantity", craft.get("produced", craft.get("count", "")))}')
            text += '\n手动合成清单：\n' + '\n'.join(lines)
        result.append({'key': key, 'text': text[:3900]})
    result.extend(_pending_mastery_notifications(payload))
    return result


def _normalize_oper_progress_detail(value):
    if type(value) is int:
        return OPER_PROGRESS_DETAIL_ENUM.get(value, str(value))
    if isinstance(value, str) and value in OPER_PROGRESS_DETAIL_LABELS:
        return value
    return str(value or '')


def _normalize_oper_progress_result(value):
    if type(value) is int:
        return OPER_PROGRESS_RESULT_ENUM.get(value, str(value))
    if isinstance(value, str) and value:
        return value
    return ''


def update_mastery_settings(config_dir, catalog_path, body):
    _object(body)
    _bool(body, 'enabled')
    catalog = load_catalog(catalog_path)
    with _state_lock(config_dir):
        state = load_state(config_dir)
        _check_revision(state, body)
        if 'enabled' in body:
            state['mastery']['enabled'] = body['enabled']
        state['revision'] += 1
        _write(config_dir, state)
    return _payload(config_dir, catalog, state)


def save_mastery_plan(config_dir, catalog_path, body):
    _object(body)
    for key in ('enabled', 'completed', 'auto_skill_level_7'):
        _bool(body, key)
    catalog = load_catalog(catalog_path)
    operators = catalog.get('operators', {})
    with _state_lock(config_dir):
        state = load_state(config_dir)
        _check_revision(state, body)
        plan_id = body.get('id')
        plans = state['mastery']['plans']
        existing = next((p for p in plans if p['id'] == plan_id), None)
        if plan_id and existing is None:
            raise ValueError('专精计划不存在')
        if existing is None and len(plans) >= MAX_GOALS:
            raise ValueError('最多保存 20 个专精计划')
        operator_id = body.get('operator_id', (existing or {}).get('operator_id'))
        if not isinstance(operator_id, str) or operator_id not in operators:
            raise ValueError('专精干员不存在')
        op_meta = operators[operator_id]
        max_levels = op_meta.get('max_levels', [])
        if len(max_levels) < 3:
            raise ValueError('仅支持可晋升至精英 2 的干员设置技能专精')
        role = PROFESSION_TO_OPER_ROLE.get(op_meta.get('profession', ''), '')
        if not role:
            raise ValueError('干员职业暂不支持技能专精队列')
        raw_mastery = body.get('skill_mastery', (existing or {}).get('skill_mastery'))
        if (not isinstance(raw_mastery, list) or len(raw_mastery) != 3
                or any(type(v) is not int or not 0 <= v <= 3 for v in raw_mastery)
                or all(v == 0 for v in raw_mastery)):
            raise ValueError('专精目标需填写 1~3 技能的目标等级（0~3），且至少一项大于 0')
        same_operator_other = next(
            (p for p in plans if p['operator_id'] == operator_id and p['id'] != plan_id),
            None,
        )
        if same_operator_other is not None and existing is None:
            existing = same_operator_other
            plan_id = existing['id']
        prev_current = list((existing or {}).get('current_mastery', [0, 0, 0]))
        if (not isinstance(prev_current, list) or len(prev_current) != 3
                or any(type(v) is not int or not 0 <= v <= 3 for v in prev_current)):
            prev_current = [0, 0, 0]
        operator_changed = existing is not None and existing.get('operator_id') != operator_id
        if operator_changed:
            prev_current = [0, 0, 0]
        completed = body.get('completed', (existing or {}).get('completed', False))
        target_changed = existing is not None and (
            operator_changed or list(existing.get('skill_mastery', [])) != list(raw_mastery)
        )
        if target_changed and 'completed' not in body:
            completed = all(
                cur >= tgt for cur, tgt in zip(prev_current, raw_mastery) if tgt > 0
            ) and any(v > 0 for v in prev_current)
        prev_training_skill = 0 if operator_changed else int((existing or {}).get('training_skill', 0) or 0)
        prev_training_level = 0 if operator_changed else int((existing or {}).get('training_level', 0) or 0)
        still_training = (
            not completed
            and 1 <= prev_training_skill <= 3
            and prev_training_level > 0
            and raw_mastery[prev_training_skill - 1] >= prev_training_level
        )
        now = _now()
        candidate = {
            'id': plan_id or uuid.uuid4().hex,
            'operator_id': operator_id,
            'operator_name': op_meta.get('name', operator_id),
            'role': role,
            'skill_mastery': list(raw_mastery),
            'current_mastery': prev_current,
            'auto_skill_level_7': body.get('auto_skill_level_7', (existing or {}).get('auto_skill_level_7', True)),
            'skill_level_7_verified': bool(
                (existing or {}).get('skill_level_7_verified', False)
                if existing and not operator_changed else False
            ),
            'enabled': body.get('enabled', (existing or {}).get('enabled', True)),
            'completed': bool(completed),
            'status': 'completed' if completed else (
                'training' if still_training else (
                    'pending' if target_changed else (existing or {}).get('status', 'pending')
                )
            ),
            'status_detail': (existing or {}).get('status_detail', '') if still_training else (
                '' if target_changed else (existing or {}).get('status_detail', '')
            ),
            'status_label': '已完成' if completed else (
                (existing or {}).get('status_label', f'{prev_training_skill} 技能专精 {prev_training_level} 训练已启动')
                if still_training else (
                    '待执行' if target_changed else (existing or {}).get('status_label', '待执行')
                )
            ),
            'training_skill': prev_training_skill if still_training else 0,
            'training_level': prev_training_level if still_training else 0,
            'last_run_at': '' if operator_changed else (existing or {}).get('last_run_at', ''),
            'updated_at': now,
        }
        if existing is None:
            cand_tier = _mastery_plan_auto_tier(candidate)
            insert_idx = next(
                (i for i, p in enumerate(plans) if _mastery_plan_auto_tier(p) > cand_tier),
                len(plans),
            )
            plans.insert(insert_idx, candidate)
            if not state['mastery'].get('manual_order'):
                plans.sort(key=_mastery_plan_auto_tier)
        else:
            plans[plans.index(existing)] = candidate
        state['revision'] += 1
        _write(config_dir, state)
    return _payload(config_dir, catalog, state)


def set_mastery_plan_state(config_dir, catalog_path, body):
    _object(body)
    for key in ('enabled', 'completed', 'delete'):
        _bool(body, key)
    catalog = load_catalog(catalog_path)
    with _state_lock(config_dir):
        state = load_state(config_dir)
        _check_revision(state, body)
        plans = state['mastery']['plans']
        if body.get('move') == 'auto' and not body.get('id'):
            state['mastery']['manual_order'] = False
            plans.sort(key=_mastery_plan_auto_tier)
            state['revision'] += 1
            _write(config_dir, state)
            return _payload(config_dir, catalog, state)
        plan = next((p for p in plans if p['id'] == body.get('id')), None)
        if plan is None:
            raise ValueError('专精计划不存在')
        if body.get('delete') is True:
            plans.remove(plan)
        else:
            for key in ('enabled', 'completed'):
                if key in body:
                    plan[key] = body[key]
            if body.get('completed') is True:
                plan['status'] = 'completed'
                plan['status_label'] = '已完成'
                plan['training_skill'] = 0
                plan['training_level'] = 0
            elif body.get('completed') is False and plan.get('status') == 'completed':
                plan['status'] = 'pending'
                plan['status_label'] = '待执行'
            if 'move' in body and body['move'] is not None:
                if body['move'] == 'auto':
                    state['mastery']['manual_order'] = False
                    plans.sort(key=_mastery_plan_auto_tier)
                else:
                    _move_list_item(plans, plan, body['move'])
                    state['mastery']['manual_order'] = True
            elif body.get('completed') is True and not state['mastery'].get('manual_order'):
                plans.sort(key=_mastery_plan_auto_tier)
            plan['updated_at'] = _now()
        state['revision'] += 1
        _write(config_dir, state)
    return _payload(config_dir, catalog, state)


def set_mastery_runtime(config_dir, status, message):
    if not isinstance(status, str) or not isinstance(message, str):
        raise ValueError('专精运行状态无效')
    with _state_lock(config_dir):
        state = load_state(config_dir)
        state['mastery']['runtime'] = {'status': status[:80], 'message': message[:1000], 'updated_at': _now()}
        _write(config_dir, state)


def record_mastery_results(config_dir, catalog_path, details_list, *, summary=None):
    if not isinstance(details_list, list):
        raise ValueError('专精回调结果格式无效')
    catalog = load_catalog(catalog_path)
    now = _now()
    with _state_lock(config_dir):
        state = load_state(config_dir)
        plans = state['mastery']['plans']
        by_role_name = {
            (p.get('role', ''), p.get('operator_name', '')): p
            for p in plans if p.get('enabled', True)
        }
        by_name = {
            p.get('operator_name', ''): p
            for p in plans if p.get('enabled', True)
        }
        updated_plans = []
        advanced_in_run = set()
        for item in details_list:
            if not isinstance(item, dict):
                continue
            role = str(item.get('role', '') or '')
            name = str(item.get('name', '') or '')
            plan = by_role_name.get((role, name)) or by_name.get(name)
            if plan is None:
                continue
            detail_code = _normalize_oper_progress_detail(item.get('result_detail'))
            result_code = _normalize_oper_progress_result(item.get('result'))
            details_obj = item.get('details') if isinstance(item.get('details'), dict) else item
            is_skill_mastery_cb = 'skill_mastery' in details_obj or 'skill_mastery' in item
            is_skill_level_cb = ('skill_level' in details_obj or 'skill_level' in item) and not is_skill_mastery_cb
            if is_skill_level_cb:
                if detail_code in ('Completed', 'AlreadySatisfied'):
                    plan['skill_level_7_verified'] = True
                elif detail_code in ('ResourceInsufficient', 'PrerequisiteNotMet', 'RecognitionFailed', 'OperatorNotFound'):
                    plan['status'] = 'blocked'
                    plan['status_detail'] = detail_code
                    plan['status_label'] = '普通技能升 7 阻塞：' + OPER_PROGRESS_DETAIL_LABELS.get(detail_code, detail_code)
                    plan['last_run_at'] = now
                    plan['updated_at'] = now
                    updated_plans.append(plan['id'])
                continue
            # Anything reaching skill mastery or beyond PrerequisiteNotMet implies skill_level 7 is satisfied.
            if detail_code in ('Completed', 'AlreadySatisfied', 'PrerequisiteTraining', 'TrainingRoomBusy',
                               'ResourceInsufficient', 'FormulaLocked', 'ChipNotCraftable'):
                plan['skill_level_7_verified'] = True
            skill_vec = details_obj.get('skill_mastery')
            if not isinstance(skill_vec, list):
                skill_vec = item.get('skill_mastery')
            skill_idx = -1
            skill_val = 0
            if isinstance(skill_vec, list) and len(skill_vec) == 3:
                for idx, val in enumerate(skill_vec):
                    if type(val) is int and val > 0:
                        skill_idx = idx
                        skill_val = val
                        break
            cur = list(plan.get('current_mastery', [0, 0, 0]))
            if len(cur) != 3:
                cur = [0, 0, 0]
            if detail_code == 'AlreadySatisfied' and skill_idx >= 0:
                prev_val = cur[skill_idx]
                cur[skill_idx] = max(cur[skill_idx], min(3, skill_val))
                plan['current_mastery'] = cur
                if cur[skill_idx] > prev_val:
                    advanced_in_run.add(plan['id'])
                if plan.get('training_skill') == skill_idx + 1 and plan.get('training_level', 0) <= cur[skill_idx]:
                    plan['training_skill'] = 0
                    plan['training_level'] = 0
                    if plan.get('status') == 'training':
                        plan['status'] = 'pending'
            elif detail_code in ('Completed', 'PrerequisiteTraining') and skill_idx >= 0:
                cur[skill_idx] = max(cur[skill_idx], max(0, min(3, skill_val - 1)))
                plan['current_mastery'] = cur
                plan['training_skill'] = skill_idx + 1
                plan['training_level'] = min(3, skill_val)
                plan['status'] = 'training'
                plan['status_detail'] = detail_code
                plan['status_label'] = f'{skill_idx + 1} 技能专精 {skill_val} 训练已启动'
                plan['completed'] = False
                advanced_in_run.add(plan['id'])
            elif detail_code == 'TrainingRoomBusy':
                if plan.get('status') != 'training':
                    plan['status'] = 'busy'
                    plan['status_detail'] = detail_code
                    plan['status_label'] = OPER_PROGRESS_DETAIL_LABELS['TrainingRoomBusy']
            elif detail_code in ('ResourceInsufficient', 'FormulaLocked', 'PrerequisiteNotMet',
                                 'ChipNotCraftable', 'OperatorNotFound', 'RecognitionFailed',
                                 'Unsupported', 'Interrupt'):
                if detail_code == 'RecognitionFailed' and plan['id'] in advanced_in_run:
                    if plan.get('status') != 'training':
                        plan['status'] = 'partial'
                        plan['status_detail'] = 'AlreadySatisfied'
                        plan['status_label'] = '部分技能已达标，待继续推进'
                else:
                    plan['status'] = 'blocked'
                    plan['status_detail'] = detail_code
                    plan['status_label'] = OPER_PROGRESS_DETAIL_LABELS.get(detail_code, detail_code or result_code)
                    plan['training_skill'] = 0
                    plan['training_level'] = 0
            targets = plan.get('skill_mastery', [0, 0, 0])
            all_satisfied = (
                any(t > 0 for t in targets)
                and all(cur[i] >= targets[i] for i in range(3) if targets[i] > 0)
                and plan.get('status') != 'training'
            )
            if all_satisfied:
                plan['completed'] = True
                plan['status'] = 'completed'
                plan['status_detail'] = 'AlreadySatisfied'
                plan['status_label'] = '全部目标专精已达成'
                plan['training_skill'] = 0
                plan['training_level'] = 0
            elif detail_code == 'AlreadySatisfied' and plan.get('status') not in ('training', 'blocked', 'busy'):
                plan['status'] = 'partial'
                plan['status_detail'] = detail_code
                plan['status_label'] = '部分技能已达标，待继续推进'
            plan['last_run_at'] = now
            plan['updated_at'] = now
            updated_plans.append(plan['id'])
        if updated_plans and not state['mastery'].get('manual_order'):
            plans.sort(key=_mastery_plan_auto_tier)
        summary_msg = ''
        if isinstance(summary, dict):
            completed_cnt = summary.get('completed', summary.get('success', 0))
            skipped_cnt = summary.get('skipped', 0)
            failed_cnt = summary.get('failed', 0)
            summary_msg = f'本轮专精汇总：启动/完成 {completed_cnt} 项，跳过 {skipped_cnt} 项，受阻 {failed_cnt} 项。'
        elif updated_plans:
            summary_msg = f'已更新 {len(set(updated_plans))} 个干员专精状态。'
        if summary_msg:
            state['mastery']['runtime'] = {
                'status': 'succeeded',
                'message': summary_msg,
                'updated_at': now,
            }
        state['revision'] += 1
        _write(config_dir, state)
    return _payload(config_dir, catalog, state)


def _mastery_notification_key(plan, event_tag):
    values = {
        'id': plan.get('id', ''),
        'operator_id': plan.get('operator_id', ''),
        'skill_mastery': plan.get('skill_mastery', []),
        'current_mastery': plan.get('current_mastery', []),
        'training_skill': plan.get('training_skill', 0),
        'training_level': plan.get('training_level', 0),
        'status_detail': plan.get('status_detail', ''),
        'event': event_tag,
    }
    return hashlib.sha256(('mastery:' + json.dumps(values, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()


def _pending_mastery_notifications(payload):
    mastery = payload.get('mastery') or {}
    plans = mastery.get('plans') or []
    result = []
    now = datetime.now(timezone.utc)
    for plan in plans:
        if not plan.get('enabled', True):
            continue
        last_run_at = plan.get('last_run_at', '')
        if not last_run_at:
            continue
        try:
            ran = datetime.fromisoformat(last_run_at.replace('Z', '+00:00'))
            if (now - ran).total_seconds() > 900:
                continue
        except (ValueError, TypeError):
            continue
        name = plan.get('operator_name', plan.get('operator_id', '干员'))
        targets = plan.get('skill_mastery', [0, 0, 0])
        target_desc = ' / '.join(f'{i + 1}技能专{v}' for i, v in enumerate(targets) if v > 0)
        status = plan.get('status', '')
        if plan.get('completed') or status == 'completed':
            key = _mastery_notification_key(plan, 'completed')
            if key not in payload['notifications']:
                result.append({
                    'key': key,
                    'text': f'[MAA] {name} 技能专精目标已达成\n目标：{target_desc}',
                })
        elif status == 'training' and plan.get('training_skill', 0) > 0:
            key = _mastery_notification_key(plan, 'training')
            if key not in payload['notifications']:
                result.append({
                    'key': key,
                    'text': (f'[MAA] {name} 技能专精训练已启动\n'
                             f'当前训练：{plan["training_skill"]} 技能 → 专精 {plan["training_level"]}\n'
                             f'计划目标：{target_desc}'),
                })
        elif status == 'busy':
            key = _mastery_notification_key(plan, 'busy')
            if key not in payload['notifications']:
                label = plan.get('status_label') or OPER_PROGRESS_DETAIL_LABELS.get('TrainingRoomBusy', '训练室正忙')
                result.append({
                    'key': key,
                    'text': (f'[MAA] {name} 技能专精暂缓\n'
                             f'状态：{label}（将在后续轮次自动重试）\n'
                             f'计划目标：{target_desc}'),
                })
        elif status == 'blocked':
            key = _mastery_notification_key(plan, 'blocked')
            if key not in payload['notifications']:
                label = plan.get('status_label') or OPER_PROGRESS_DETAIL_LABELS.get(plan.get('status_detail', ''), '受阻')
                result.append({
                    'key': key,
                    'text': (f'[MAA] {name} 技能专精受阻\n'
                             f'原因：{label}\n'
                             f'计划目标：{target_desc}'),
                })
        elif status == 'partial':
            key = _mastery_notification_key(plan, 'partial')
            if key not in payload['notifications']:
                cur = plan.get('current_mastery', [0, 0, 0])
                cur_desc = ' / '.join(
                    f'{i + 1}技能专{cur[i] if i < len(cur) else 0}'
                    for i, v in enumerate(targets) if v > 0
                )
                result.append({
                    'key': key,
                    'text': (f'[MAA] {name} 技能专精进度已更新\n'
                             f'当前进度：{cur_desc}\n'
                             f'计划目标：{target_desc}'),
                })
    return result


def mark_notified(config_dir, key):
    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key):
        raise ValueError('通知标识无效')
    with _state_lock(config_dir):
        state = load_state(config_dir)
        state['notifications'][key] = _now()
        _write(config_dir, state)
