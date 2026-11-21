"""Atomic cultivation goals, verified inventory, and completion notification state."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import tomllib
import uuid

_THREAD_LOCK = threading.RLock()
MAX_GOALS = 20
PRIORITY_MODES = ('cultivation_first', 'activity_first')
MEDICINE_MODES = ('expire_only', 'cultivation_all', 'strict_none')
FALLBACK_MODES = ('weekly', 'stage')
CN_TZ = timezone(timedelta(hours=8))
WEEKDAY_KEYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
WEEKDAY_LABELS = {
    'mon': '周一',
    'tue': '周二',
    'wed': '周三',
    'thu': '周四',
    'fri': '周五',
    'sat': '周六',
    'sun': '周日',
}
def _resolve_cultivation_paths():
    cache_dir = Path(
        os.environ.get('MAA_CACHE_DIR')
        or (Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'maa')
    )
    data_dir = Path(
        os.environ.get('MAA_DATA_DIR')
        or (Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'maa')
    )
    act_file = Path(os.environ.get('MAA_ACTIVITY_FILE') or (cache_dir / 'StageActivityV2.json'))
    item_file = Path(os.environ.get('MAA_ITEM_FILE') or (data_dir / 'resource' / 'item_index.json'))
    return act_file, item_file


_DEFAULT_ACTIVITY_FILE, _DEFAULT_ITEM_FILE = _resolve_cultivation_paths()
ACTIVITY_FILE = _DEFAULT_ACTIVITY_FILE
ITEM_FILE = _DEFAULT_ITEM_FILE


def _resolve_client_type(config_dir):
    if config_dir is None:
        return 'Official'
    try:
        from maa_schedule_control import resolve_active_client
        resolved = resolve_active_client(config_dir)
        if isinstance(resolved, tuple) and resolved and isinstance(resolved[0], str):
            return resolved[0]
    except Exception:
        pass
    return 'Official'


class InvalidSettingsError(ValueError):
    status = 400
    code = 'invalid_settings'
DEPOT_GOLD_ITEM_ID = '4001'
DEPOT_RED_TICKET_ITEM_ID = '4006'
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
ELITE_PROGRESS_DETAIL_LABELS = {
    'Completed': '已自动晋升精英阶段',
    'AlreadySatisfied': '已达到目标精英阶段',
    'ResourceInsufficient': '晋升材料、红票或龙门币不足',
    'OperatorNotFound': '未在干员列表中找到该干员',
    'PrerequisiteNotMet': '晋升前置条件未满足',
    'ChipNotCraftable': '一阶芯片无法自动合成（需先刷取普通芯片）',
    'FormulaLocked': '加工站或制造站配方未解锁',
    'Unsupported': '不支持的晋升参数',
    'RecognitionFailed': '干员或晋升界面识别未完成',
    'Interrupt': '晋升任务中断',
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


def _weekly_now():
    return datetime.now(CN_TZ)


def _read_optional_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{target.name}-', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _default_medicine_mode(config_dir=None):
    if config_dir is not None:
        med_cfg = _read_optional_json(Path(config_dir) / 'medicine-plan.json')
        if isinstance(med_cfg, dict) and med_cfg.get('enabled') is False:
            return 'strict_none'
    return 'expire_only'


def _default_state(config_dir=None):
    return {'schema_version': 1, 'revision': 0,
            'settings': {'enabled': False, 'priority_mode': 'cultivation_first',
                         'medicine_mode': _default_medicine_mode(config_dir),
                         'fallback_mode': 'weekly', 'fallback_stage': '1-7',
                         'auto_elite': False},
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
        return _default_state(config_dir)
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
    settings = state['settings']
    settings.setdefault('auto_elite', False)
    settings.setdefault('priority_mode', 'cultivation_first')
    settings.setdefault('medicine_mode', _default_medicine_mode(config_dir))
    settings.setdefault('fallback_mode', 'weekly')
    settings.setdefault('fallback_stage', '1-7')
    if (type(settings.get('enabled')) is not bool
            or type(settings.get('auto_elite')) is not bool
            or settings.get('priority_mode') not in PRIORITY_MODES
            or settings.get('medicine_mode') not in MEDICINE_MODES
            or settings.get('fallback_mode') not in FALLBACK_MODES
            or not isinstance(settings.get('fallback_stage'), str)
            or not settings['fallback_stage'].strip()):
        raise ValueError('培养状态格式无效；未覆盖现有数据')
    if settings['priority_mode'] == 'activity_first' and settings['fallback_mode'] != 'weekly':
        settings['fallback_mode'] = 'weekly'
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
        raise InvalidSettingsError(f'{key} 必须为布尔值')


def _parse_side_story_stages(activity_data, item_map, client_type='Official'):
    if not isinstance(activity_data, dict):
        return {}
    region = client_type if client_type in activity_data and isinstance(activity_data.get(client_type), dict) else 'Official'
    official = activity_data.get(region) or {}
    side_story = official.get('sideStoryStage') or official.get('SideStoryStage') or {}
    if not isinstance(side_story, dict):
        return {}
    stages = {}
    for info in side_story.values():
        if not isinstance(info, dict):
            continue
        window = info.get('Activity') or info.get('activity') or {}
        start_raw = window.get('UtcStartTime') or window.get('utcStartTime')
        end_raw = window.get('UtcExpireTime') or window.get('utcExpireTime')
        if not start_raw or not end_raw:
            continue
        try:
            offset = int(window.get('TimeZone', window.get('timeZone', 8)))
        except (TypeError, ValueError):
            offset = 8
        tz = timezone(timedelta(hours=max(-12, min(14, offset))))
        try:
            start_dt = datetime.fromisoformat(str(start_raw).replace('/', '-').replace(' ', 'T'))
            end_dt = datetime.fromisoformat(str(end_raw).replace('/', '-').replace(' ', 'T'))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=tz)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=tz)
            start_cn = start_dt.astimezone(CN_TZ)
            end_cn = end_dt.astimezone(CN_TZ)
        except ValueError:
            continue
        for entry in info.get('Stages') or info.get('stages') or []:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get('Value') or entry.get('value') or '').strip()
            if not code:
                continue
            drop = str(entry.get('Drop') or entry.get('drop') or '').strip()
            item_id = ''
            item_name = ''
            if drop:
                if drop in item_map and isinstance(item_map[drop], dict):
                    item_id = drop
                    item_name = str(item_map[drop].get('name') or drop)
                else:
                    matches = [
                        str(k) for k, v in item_map.items()
                        if isinstance(v, dict) and v.get('name') == drop
                    ]
                    if len(matches) == 1:
                        item_id = matches[0]
                        item_name = str(item_map[item_id].get('name') or item_id)
            stages[code] = {
                'stage': code,
                'item_id': item_id,
                'item_name': item_name,
                'start': start_cn,
                'end': end_cn,
            }
    return stages


def _condition_matches(condition, now_cn, side_story_map, variant_stage=''):
    if not condition:
        return True
    if not isinstance(condition, dict):
        return False
    ctype = condition.get('type')
    if ctype == 'Always':
        return True
    if ctype == 'DateTime':
        try:
            offset = int(condition.get('timezone', 8))
        except (TypeError, ValueError):
            offset = 8
        tz = timezone(timedelta(hours=max(-12, min(14, offset))))
        cur = now_cn.astimezone(tz)
        for bound_key, is_end in (('start', False), ('end', True)):
            raw = condition.get(bound_key)
            if not raw:
                continue
            try:
                parsed = datetime.fromisoformat(str(raw).replace('/', '-').replace(' ', 'T').replace('Z', '+00:00'))
            except ValueError:
                return False
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            else:
                parsed = parsed.astimezone(tz)
            if not is_end and cur < parsed:
                return False
            if is_end and cur >= parsed:
                return False
        return True
    if ctype == 'Time':
        try:
            offset = int(condition.get('timezone', 8))
        except (TypeError, ValueError):
            offset = 8
        tz = timezone(timedelta(hours=max(-12, min(14, offset))))
        value = now_cn.astimezone(tz).strftime('%H:%M:%S')
        start = condition.get('start')
        end = condition.get('end')
        if start and end and str(start) > str(end):
            return value >= str(start) or value <= str(end)
        return (not start or value >= str(start)) and (not end or value <= str(end))
    if ctype in ('OnSideStory', 'SideStory'):
        if variant_stage:
            meta = side_story_map.get(variant_stage)
            return bool(meta and meta['start'] <= now_cn < meta['end'])
        return any(meta['start'] <= now_cn < meta['end'] for meta in side_story_map.values())
    if ctype == 'Weekday':
        try:
            offset = int(condition.get('timezone', 8))
        except (TypeError, ValueError):
            offset = 8
        tz = timezone(timedelta(hours=max(-12, min(14, offset))))
        local = now_cn.astimezone(tz)
        weekdays = condition.get('weekdays') or []
        names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        return names[local.weekday()] in weekdays
    if ctype == 'And':
        return all(_condition_matches(c, now_cn, side_story_map, variant_stage)
                   for c in (condition.get('conditions') or []))
    if ctype == 'Or':
        return any(_condition_matches(c, now_cn, side_story_map, variant_stage)
                   for c in (condition.get('conditions') or []))
    if ctype == 'Not':
        return not _condition_matches(condition.get('condition') or {}, now_cn, side_story_map, variant_stage)
    return False


def _is_annihilation_fight(task):
    if not isinstance(task, dict) or task.get('type') != 'Fight':
        return False
    stage = str((task.get('params') or {}).get('stage') or '')
    if stage == 'Annihilation' or stage.endswith('Annihilation'):
        return True
    for variant in task.get('variants') or []:
        if not isinstance(variant, dict):
            continue
        v_stage = str((variant.get('params') or {}).get('stage') or '')
        if v_stage == 'Annihilation' or v_stage.endswith('Annihilation'):
            return True
    return False


def _weekly_context(config_dir, catalog, inventory_items, inventory_verified):
    directory = Path(config_dir)
    now_cn = _weekly_now()
    if now_cn.tzinfo is None:
        now_cn = now_cn.replace(tzinfo=CN_TZ)
    else:
        now_cn = now_cn.astimezone(CN_TZ)
    game_dt = now_cn - timedelta(hours=4)
    weekday_key = WEEKDAY_KEYS[game_dt.weekday()]
    weekday_label = WEEKDAY_LABELS[weekday_key]

    med_cfg = _read_optional_json(directory / 'medicine-plan.json') or {}
    threshold = med_cfg.get('threshold', 80)
    if type(threshold) is not int or not 0 <= threshold <= 9999:
        threshold = 80
    med_enabled = med_cfg.get('enabled') is not False

    expiring_count = 0
    total_medicine_count = 0
    expiring_at = ''
    evidence = _read_optional_json(directory / 'medicine-evidence.json') or {}
    if evidence.get('accurate') is True and type(evidence.get('remaining_count')) is int and evidence['remaining_count'] > 0:
        horizon = now_cn + timedelta(hours=30)
        batches = evidence.get('batches')
        if isinstance(batches, list) and batches:
            min_unexpired = None
            for batch in batches:
                if not isinstance(batch, dict):
                    continue
                b_rem = batch.get('remaining', batch.get('count', 0))
                b_raw = str(batch.get('expires_at') or '')
                if type(b_rem) is not int or b_rem <= 0 or not b_raw:
                    continue
                try:
                    b_dt = datetime.fromisoformat(b_raw.replace('Z', '+00:00'))
                    if b_dt.tzinfo is None:
                        b_dt = b_dt.replace(tzinfo=CN_TZ)
                    if b_dt > now_cn:
                        total_medicine_count += b_rem
                        if b_dt < horizon:
                            expiring_count += b_rem
                        if min_unexpired is None or b_dt < min_unexpired:
                            min_unexpired = b_dt
                except ValueError:
                    continue
            if min_unexpired is not None:
                expiring_at = min_unexpired.astimezone(CN_TZ).isoformat()
        else:
            raw_exp = str(evidence.get('expires_at') or '')
            if raw_exp:
                try:
                    exp_dt = datetime.fromisoformat(raw_exp.replace('Z', '+00:00'))
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=CN_TZ)
                    if exp_dt > now_cn:
                        total_medicine_count = int(evidence['remaining_count'])
                        expiring_at = exp_dt.astimezone(CN_TZ).isoformat()
                        if exp_dt < horizon:
                            expiring_count = total_medicine_count
                except ValueError:
                    pass

    weekly_cfg = _read_optional_json(directory / 'weekly-plan.json') or {}
    weekly_enabled = weekly_cfg.get('enabled') is True
    source_name = f'daily-{weekday_key}.toml' if weekly_enabled else 'daily.toml'
    task_path = directory / 'tasks' / source_name
    if not task_path.is_file() and weekly_enabled:
        fallback_daily = directory / 'tasks' / 'daily.toml'
        if fallback_daily.is_file():
            source_name = 'daily.toml'
            task_path = fallback_daily

    ctx = {
        'weekly_enabled': weekly_enabled,
        'weekday_key': weekday_key,
        'weekday_label': weekday_label,
        'source_file': source_name,
        'available': False,
        'effective_stage': '',
        'base_stage': '',
        'is_activity_stage': False,
        'activity_item_id': '',
        'activity_item_name': '',
        'activity_inventory': None,
        'activity_threshold': threshold,
        'activity_end': '',
        'medicine_plan_enabled': med_enabled,
        'expiring_medicine_count': expiring_count,
        'total_medicine_count': total_medicine_count,
        'expiring_medicine_expires_at': expiring_at,
    }
    if not task_path.is_file():
        return ctx
    try:
        doc = tomllib.loads(task_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return ctx
    candidate_fights = [
        t for t in (doc.get('tasks') or [])
        if isinstance(t, dict)
        and t.get('type') == 'Fight'
        and t.get('_enabled') is not False
        and not _is_annihilation_fight(t)
    ]
    fight = next(
        (t for t in candidate_fights if t.get('name') == '理智作战'),
        candidate_fights[0] if candidate_fights else None,
    )
    if fight is None:
        return ctx

    item_map = dict((catalog or {}).get('items') or {})
    ext_items = _read_optional_json(ITEM_FILE)
    if isinstance(ext_items, dict):
        for k, v in ext_items.items():
            if k not in item_map and isinstance(v, dict):
                item_map[k] = v
    activity_data = _read_optional_json(ACTIVITY_FILE) or {}
    if not activity_data and Path(ACTIVITY_FILE) == _DEFAULT_ACTIVITY_FILE:
        try:
            from maa_activity_cache import get_activity
            try:
                activity_data = get_activity(ACTIVITY_FILE, config_dir=config_dir)[0] or {}
            except TypeError:
                activity_data = get_activity(ACTIVITY_FILE)[0] or {}
        except Exception:
            activity_data = {}
    side_story_map = _parse_side_story_stages(
        activity_data, item_map, client_type=_resolve_client_type(config_dir)
    )

    base_params = dict(fight.get('params') or {})
    base_stage = str(base_params.get('stage') or '').strip()
    effective_params = dict(base_params)
    variants = fight.get('variants') or []
    if isinstance(variants, list) and variants:
        matched = False
        strategy = str(fight.get('strategy') or 'first')
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            v_params = variant.get('params') if isinstance(variant.get('params'), dict) else {}
            v_stage = str(v_params.get('stage') or '').strip()
            if _condition_matches(variant.get('condition'), now_cn, side_story_map, v_stage):
                effective_params.update(v_params)
                matched = True
                if strategy != 'merge':
                    break
        if not matched and not base_stage:
            effective_params = {}
    eff_stage = str(effective_params.get('stage') or '').strip()
    meta = side_story_map.get(eff_stage)
    is_open_act = bool(meta and meta['start'] <= now_cn < meta['end'])
    if meta and not is_open_act:
        if base_stage and base_stage != eff_stage:
            eff_stage = base_stage
            meta = side_story_map.get(eff_stage)
            is_open_act = bool(meta and meta['start'] <= now_cn < meta['end'])
            if meta and not is_open_act:
                eff_stage = ''
                base_stage = ''
        else:
            eff_stage = ''
            if base_stage in side_story_map:
                base_stage = ''

    ctx['available'] = bool(eff_stage or base_stage)
    ctx['effective_stage'] = eff_stage or base_stage
    ctx['base_stage'] = base_stage or eff_stage
    if is_open_act and meta:
        ctx['is_activity_stage'] = True
        ctx['activity_item_id'] = meta['item_id']
        ctx['activity_item_name'] = meta['item_name']
        ctx['activity_end'] = meta['end'].isoformat()
        if meta['item_id'] and inventory_verified and isinstance(inventory_items, dict):
            ctx['activity_inventory'] = int(inventory_items.get(meta['item_id'], 0) or 0)
    return ctx


def _payload(config_dir, catalog, state):
    items = dict(state['inventory']['items'])
    currency_meta = catalog.get('currency') or {}
    gold = currency_meta.get('gold_item_id', DEPOT_GOLD_ITEM_ID)
    red_ticket_id = currency_meta.get('red_ticket_item_id', DEPOT_RED_TICKET_ITEM_ID)
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
            promo = row.get('promotion')
            if isinstance(promo, dict):
                cur_e = int(promo.get('current_elite', 0) or 0)
                clamped_e = cur_e
                chain_ok = True
                for ph in promo.get('phases', []):
                    if not isinstance(ph, dict):
                        continue
                    if ph.get('lmd_total', 0) > 0:
                        ph['ready'] = False
                        ph['status'] = 'blocked'
                        chain_ok = False
                    elif chain_ok and ph.get('ready'):
                        clamped_e = int(ph.get('elite', cur_e) or cur_e)
                    else:
                        chain_ok = False
                promo['ready_elite'] = clamped_e
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
    owned_ids = {
        str(op.get('id'))
        for op in (snapshot.get('own_opers') or [])
        if isinstance(op, dict) and op.get('id')
    }
    operator_verified = bool(operator_status['available'] and not operator_status['stale'])
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
        promo = row.get('promotion')
        if isinstance(promo, dict):
            cur_e = int(promo.get('current_elite', 0) or 0)
            raw_ready_e = int(promo.get('ready_elite', cur_e) or cur_e)
            can_promote = (
                inventory_verified
                and operator_verified
                and row.get('operator_id') in owned_ids
                and raw_ready_e > cur_e
            )
            promo['verified_ready_elite'] = raw_ready_e if can_promote else cur_e
    red_ticket = {
        'item_id': red_ticket_id,
        'available': bool(inventory_verified),
        'balance': int(inventory.get('items', {}).get(red_ticket_id, 0) or 0) if inventory.get('available') else 0,
        'source': 'depot' if inventory_verified else 'unknown',
        'scanned_at': inventory.get('scanned_at', '') if inventory.get('available') else '',
    }
    snapshot_status = {
        'inventory': inventory_status,
        'operators': {'available': operator_status['available'],
                      'scanned_at': snapshot.get('scanned_at', ''),
                      'stale': operator_status['stale'],
                      'last_error': attempt.get('message', '') if attempt.get('status') == 'failed' else ''},
    }
    weekly_context = _weekly_context(
        config_dir,
        catalog,
        inventory.get('items') if inventory.get('available') else {},
        inventory_verified,
    )
    return {**state, 'red_ticket': red_ticket, 'plan': plan, 'catalog': catalog,
            'operator_snapshot': snapshot, 'snapshot_status': snapshot_status,
            'weekly_context': weekly_context}


def get_payload(config_dir, catalog_path):
    return _payload(config_dir, load_catalog(catalog_path), load_state(config_dir))


def update_settings(config_dir, catalog_path, body):
    _object(body)
    _bool(body, 'enabled')
    _bool(body, 'auto_elite')
    if 'priority_mode' in body and body['priority_mode'] not in PRIORITY_MODES:
        raise InvalidSettingsError('主线优先级模式无效')
    if 'medicine_mode' in body and body['medicine_mode'] not in MEDICINE_MODES:
        raise InvalidSettingsError('理智药使用策略无效')
    if 'fallback_mode' in body and body['fallback_mode'] not in FALLBACK_MODES:
        raise InvalidSettingsError('兜底去向模式无效')
    if 'activity_threshold' in body:
        thr = body['activity_threshold']
        if type(thr) is not int or not 0 <= thr <= 9999:
            raise InvalidSettingsError('限时活动保底上限必须为 0 至 9999 的整数')
    catalog = load_catalog(catalog_path)
    if 'fallback_stage' in body:
        known = {stage['stage'] for stage in catalog.get('stages', [])}
        if not isinstance(body['fallback_stage'], str) or body['fallback_stage'] not in known:
            raise InvalidSettingsError('备用关卡必须选择已支持的常驻关卡')
    with _state_lock(config_dir):
        state = load_state(config_dir)
        _check_revision(state, body)
        cur = state['settings']
        next_priority = body.get('priority_mode', cur.get('priority_mode', 'cultivation_first'))
        next_medicine = body.get('medicine_mode', cur.get('medicine_mode', 'expire_only'))
        if 'fallback_mode' in body:
            next_fallback = body['fallback_mode']
        elif 'fallback_stage' in body:
            if next_priority == 'activity_first':
                raise InvalidSettingsError('限时活动优先模式下兜底去向已锁定为跟随周计划，禁止指定固定常驻关卡')
            next_fallback = 'stage'
        elif next_priority == 'activity_first':
            next_fallback = 'weekly'
        else:
            next_fallback = cur.get('fallback_mode', 'weekly')

        if next_priority == 'activity_first' and next_fallback != 'weekly':
            raise InvalidSettingsError('限时活动优先模式下兜底去向必须跟随周计划，禁止与固定常驻关卡同时启用')

        for key in ('enabled', 'fallback_stage', 'auto_elite'):
            if key in body:
                cur[key] = body[key]
        cur['priority_mode'] = next_priority
        cur['medicine_mode'] = next_medicine
        cur['fallback_mode'] = next_fallback
        state['revision'] += 1
        _write(config_dir, state)

        med_path = Path(config_dir) / 'medicine-plan.json'
        if 'medicine_mode' in body or 'activity_threshold' in body or med_path.exists():
            existing_med = _read_optional_json(med_path) or {}
            prev_thr = existing_med.get('threshold', 80)
            if type(prev_thr) is not int or not 0 <= prev_thr <= 9999:
                prev_thr = 80
            next_thr = body['activity_threshold'] if 'activity_threshold' in body else prev_thr
            _atomic_write_json(med_path, {
                'enabled': next_medicine != 'strict_none',
                'threshold': next_thr,
            })
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
        for stale_key in ('elite_status', 'elite_status_label', 'last_elite_result',
                          'last_elite_detail', 'last_elite_target', 'last_elite_at'):
            candidate.pop(stale_key, None)
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
    elite_notes = _pending_elite_notifications(payload)
    if not include_goals:
        return elite_notes + _pending_mastery_notifications(payload)
    inventory_status = payload['snapshot_status']['inventory']
    if not inventory_status.get('available') or inventory_status.get('stale'):
        return elite_notes + _pending_mastery_notifications(payload)
    age = inventory_status.get('age_seconds')
    if age is None or age < 0 or age > 900:
        return elite_notes + _pending_mastery_notifications(payload)
    scanned = datetime.fromisoformat(inventory_status['scanned_at'].replace('Z', '+00:00'))
    result = list(elite_notes)
    plan_rows = {g['id']: g for g in payload['plan']['goals']}
    auto_elite = bool((payload.get('settings') or {}).get('auto_elite'))
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
                settings_obj = payload.get('settings') or {}
                if settings_obj.get('fallback_mode') == 'weekly':
                    wctx = payload.get('weekly_context') or {}
                    wstage = wctx.get('effective_stage') or ''
                    fallback_desc = (
                        f'周计划备用关卡 {wstage}（自动跟随周计划）'
                        if wstage
                        else '周计划备用关卡（自动跟随周计划）'
                    )
                else:
                    fallback = settings_obj.get('fallback_stage', '1-7')
                    fallback_desc = f'备用关卡 {fallback}（开放且可用时）'
                text = (f'[MAA] {name} 培养目标已达成\n'
                        f'复核练度：精英 {goal["current_elite"]}，等级 {goal["current_level"]}\n'
                        f'不再为该目标刷取；其余目标处理完成后继续{fallback_desc}。')
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
        if goal['current_elite'] >= goal['target_elite']:
            action_hint = (
                f'当前已处于精英 {goal["current_elite"]}（{goal["current_level"]} 级），'
                '作战记录与龙门币已齐备，请在游戏内升至目标等级（下次扫描将自动确认完成）。'
            )
        elif auto_elite:
            action_hint = (
                '已开启自动精英化，可由 OperProgress 自动完成红票买助剂、制造站双芯片、加工站合成并晋升；'
                '晋升后如需继续升至目标等级，请在游戏内升级。'
            )
        else:
            action_hint = '请手动合成并培养，完成后在培养目标页标记完成。'
        text = (f'[MAA] {name} 培养资源已齐备\n'
                f'目标：精英 {goal["target_elite"]}，等级 {goal["target_level"]}\n'
                f'{action_hint}')
        crafts = row.get('crafts', [])
        if crafts:
            lines = []
            for craft in crafts:
                item_id = craft.get('item_id', craft.get('item', ''))
                item_name = payload['catalog']['items'].get(item_id, {}).get('name', item_id)
                lines.append(f'{item_name} × {craft.get("quantity", craft.get("produced", craft.get("count", "")))}')
            text += '\n合成清单：\n' + '\n'.join(lines) if auto_elite else '\n手动合成清单：\n' + '\n'.join(lines)
        exchanges = row.get('exchanges', [])
        if exchanges:
            ex_lines = []
            for ex in exchanges:
                item_name = ex.get('name') or payload['catalog']['items'].get(ex.get('item_id', ''), {}).get('name', ex.get('item_id', ''))
                curr_name = payload['catalog']['items'].get(ex.get('currency_id', ''), {}).get('name', ex.get('currency_id', '采购凭证'))
                ex_lines.append(f'{item_name} × {ex.get("quantity", 0)}（耗 {ex.get("cost", 0)} {curr_name}）')
            text += '\n红票兑换清单：\n' + '\n'.join(ex_lines)
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


def record_mastery_results(config_dir, catalog_path, details_list, *, summary=None, submitted_plans=None):
    if not isinstance(details_list, list):
        raise ValueError('专精回调结果格式无效')
    catalog = load_catalog(catalog_path)
    operators_meta = catalog.get('operators') or {}
    now = _now()
    pending_elite = []
    if isinstance(submitted_plans, list):
        for p in submitted_plans:
            if (isinstance(p, dict) and type(p.get('elite')) is int and p['elite'] > 0
                    and 'skill_mastery' not in p and 'skill_level' not in p):
                pending_elite.append((str(p.get('role', '') or ''), str(p.get('name', '') or ''), p['elite']))
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
        goal_by_role_name = {}
        goal_by_name = {}
        for g in state['goals']:
            if not g.get('enabled', True) or g.get('completed', False):
                continue
            meta = operators_meta.get(g.get('operator_id', '')) or {}
            op_name = meta.get('name') or g.get('operator_name', '')
            op_role = PROFESSION_TO_OPER_ROLE.get(meta.get('profession', ''), '')
            if op_name:
                goal_by_name.setdefault(op_name, g)
                if op_role:
                    goal_by_role_name.setdefault((op_role, op_name), g)
        updated_plans = []
        updated_elite_goals = []
        advanced_in_run = set()
        for item in details_list:
            if not isinstance(item, dict):
                continue
            role = str(item.get('role', '') or '')
            name = str(item.get('name', '') or '')
            detail_code = _normalize_oper_progress_detail(item.get('result_detail'))
            result_code = _normalize_oper_progress_result(item.get('result'))
            details_obj = item.get('details') if isinstance(item.get('details'), dict) else item
            is_skill_mastery_cb = 'skill_mastery' in details_obj or 'skill_mastery' in item
            is_skill_level_cb = ('skill_level' in details_obj or 'skill_level' in item) and not is_skill_mastery_cb
            has_elite_key = 'elite' in details_obj or 'elite' in item
            pending_idx = next(
                (idx for idx, (pr, pn, _pe) in enumerate(pending_elite)
                 if pn == name and (not pr or not role or pr == role)),
                None,
            )
            matched_goal = goal_by_role_name.get((role, name)) or goal_by_name.get(name)
            plan = by_role_name.get((role, name)) or by_name.get(name)
            is_elite_cb = has_elite_key or (
                not is_skill_mastery_cb and not is_skill_level_cb and (
                    pending_idx is not None or (matched_goal is not None and plan is None)
                )
            )
            if is_elite_cb:
                sub_elite = 0
                if pending_idx is not None:
                    _, _, sub_elite = pending_elite.pop(pending_idx)
                raw_e = details_obj.get('elite', item.get('elite'))
                target_e = raw_e if type(raw_e) is int and raw_e > 0 else (
                    sub_elite or (matched_goal.get('target_elite', 0) if matched_goal else 0)
                )
                if matched_goal is not None:
                    matched_goal['last_elite_at'] = now
                    matched_goal['last_elite_detail'] = detail_code
                    matched_goal['last_elite_result'] = result_code
                    matched_goal['last_elite_target'] = int(target_e or 0)
                    if detail_code == 'Completed':
                        matched_goal['elite_status'] = 'promoted'
                        matched_goal['elite_status_label'] = f'已自动晋升至精英 {target_e}'
                    elif detail_code == 'AlreadySatisfied':
                        matched_goal['elite_status'] = 'satisfied'
                        matched_goal['elite_status_label'] = f'已处于精英 {target_e}'
                    else:
                        matched_goal['elite_status'] = 'blocked'
                        matched_goal['elite_status_label'] = ELITE_PROGRESS_DETAIL_LABELS.get(
                            detail_code, detail_code or result_code
                        )
                    updated_elite_goals.append(matched_goal['id'])
                continue
            if plan is None:
                continue
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
        if isinstance(summary, dict) and updated_plans:
            completed_cnt = summary.get('completed', summary.get('success', 0))
            skipped_cnt = summary.get('skipped', 0)
            failed_cnt = summary.get('failed', 0)
            summary_msg = f'本轮专精汇总：启动/完成 {completed_cnt} 项，跳过 {skipped_cnt} 项，受阻 {failed_cnt} 项。'
        elif updated_plans:
            summary_msg = f'已更新 {len(set(updated_plans))} 个干员专精状态。'
        elif isinstance(summary, dict) and not updated_elite_goals:
            completed_cnt = summary.get('completed', summary.get('success', 0))
            skipped_cnt = summary.get('skipped', 0)
            failed_cnt = summary.get('failed', 0)
            summary_msg = f'本轮专精汇总：启动/完成 {completed_cnt} 项，跳过 {skipped_cnt} 项，受阻 {failed_cnt} 项。'
        if summary_msg:
            state['mastery']['runtime'] = {
                'status': 'succeeded',
                'message': summary_msg,
                'updated_at': now,
            }
        state['revision'] += 1
        _write(config_dir, state)
    return _payload(config_dir, catalog, state)


def _elite_notification_key(goal, event_tag):
    values = {
        'id': goal.get('id', ''),
        'operator_id': goal.get('operator_id', ''),
        'target_elite': goal.get('target_elite', 0),
        'last_elite_target': goal.get('last_elite_target', 0),
        'last_elite_detail': goal.get('last_elite_detail', ''),
        'event': event_tag,
    }
    return hashlib.sha256(('elite:' + json.dumps(values, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()


def _pending_elite_notifications(payload):
    result = []
    now = datetime.now(timezone.utc)
    operators = (payload.get('catalog') or {}).get('operators') or {}
    for goal in payload.get('goals') or []:
        if not goal.get('enabled', True):
            continue
        last_elite_at = goal.get('last_elite_at', '')
        if not last_elite_at:
            continue
        try:
            ran = datetime.fromisoformat(str(last_elite_at).replace('Z', '+00:00'))
            if (now - ran).total_seconds() > 900:
                continue
        except (ValueError, TypeError):
            continue
        name = operators.get(goal.get('operator_id', ''), {}).get('name', goal.get('operator_id', '干员'))
        elite_status = goal.get('elite_status', '')
        elite_target = goal.get('last_elite_target') or goal.get('current_elite', 0)
        if elite_status == 'promoted' and not goal.get('completed'):
            key = _elite_notification_key(goal, 'promoted')
            if key not in payload['notifications']:
                result.append({
                    'key': key,
                    'text': (f'[MAA] {name} 已自动晋升至精英 {elite_target}\n'
                             f'计划目标：精英 {goal["target_elite"]}，等级 {goal["target_level"]}'),
                })
        elif elite_status == 'blocked':
            key = _elite_notification_key(goal, 'blocked')
            if key not in payload['notifications']:
                label = goal.get('elite_status_label') or ELITE_PROGRESS_DETAIL_LABELS.get(
                    goal.get('last_elite_detail', ''), '自动精英化受阻'
                )
                result.append({
                    'key': key,
                    'text': (f'[MAA] {name} 自动精英化受阻\n'
                             f'原因：{label}\n'
                             f'计划目标：精英 {goal["target_elite"]}，等级 {goal["target_level"]}'),
                })
    return result


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
