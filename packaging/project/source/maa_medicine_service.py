"""Cached medicine evidence and last activity opportunity; never starts the game."""
import argparse
import json
import os
import sys
import tomllib
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from maa_medicine_plan import CN, DAYS, effective_params, farm_stage, parse_time, plan_opportunities, prepare_fights, select_drain
from maa_task_config import load_tasks_strict, render_tasks, _toml_val
from maa_weekly_plan import atomic_write_text, load_settings as weekly_settings
from maa_activity_cache import get_activity

def _resolve_medicine_paths():
    cache_dir = Path(os.environ.get("MAA_CACHE_DIR") or (Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "maa"))
    data_dir = Path(os.environ.get("MAA_DATA_DIR") or (Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "maa"))
    state_dir = Path(os.environ.get("MAA_STATE_DIR") or (Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "maa"))

    act_file = Path(os.environ.get("MAA_ACTIVITY_FILE") or (cache_dir / "StageActivityV2.json"))
    item_file = Path(os.environ.get("MAA_ITEM_FILE") or (data_dir / "resource" / "item_index.json"))
    core_log = Path(os.environ.get("MAA_ASST_LOG") or (state_dir / "debug" / "asst.log"))
    return act_file, item_file, core_log


ACTIVITY_FILE, ITEM_FILE, CORE_LOG = _resolve_medicine_paths()


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


def read_json(path, default=None):
    try:
        value=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return deepcopy(default) if default is not None else {}
    if not isinstance(value,dict):
        raise ValueError(f'{Path(path).name}: 状态必须是对象')
    return value


def write_json(path, value):
    atomic_write_text(Path(path),json.dumps(value,ensure_ascii=False,indent=2,default=lambda value:value.isoformat() if isinstance(value,datetime) else str(value))+'\n')


def settings(config_dir):
    value=read_json(Path(config_dir)/'medicine-plan.json',{'enabled':False,'threshold':80})
    enabled=value.get('enabled',False);threshold=value.get('threshold',80)
    if type(enabled) is not bool or type(threshold) is not int or not 0<=threshold<=9999:
        raise ValueError('理智药设置损坏，禁止自动用药')
    return {'enabled':enabled,'threshold':threshold}


def _sync_cultivation_medicine_mode(config_dir, enabled):
    cult_path=Path(config_dir)/'cultivation/state.json'
    if not cult_path.exists():
        return
    try:
        import maa_cultivation_service as cult_svc
        with cult_svc._state_lock(config_dir):
            try:
                state=cult_svc.load_state(config_dir)
                cur_mode=state['settings'].get('medicine_mode','expire_only')
                next_mode=('strict_none' if not enabled
                           else ('cultivation_all' if cur_mode=='cultivation_all' else 'expire_only'))
                if state['settings'].get('medicine_mode')!=next_mode:
                    state['settings']['medicine_mode']=next_mode
                    state['revision']+=1
                    cult_svc._write(config_dir,state)
                return
            except ValueError:
                pass
    except Exception:
        pass
    try:
        raw=read_json(cult_path)
        if isinstance(raw.get('settings'),dict):
            cur_mode=raw['settings'].get('medicine_mode','expire_only')
            raw['settings']['medicine_mode']=('strict_none' if not enabled
                else ('cultivation_all' if cur_mode=='cultivation_all' else 'expire_only'))
            write_json(cult_path,raw)
    except Exception:
        pass


def update_settings(config_dir, body):
    if not isinstance(body,dict) or set(body)-{'enabled','threshold'}:
        raise ValueError('仅支持启用与材料阈值')
    current=settings(config_dir)
    current.update(body)
    if type(current['enabled']) is not bool or type(current['threshold']) is not int or not 0<=current['threshold']<=9999:
        raise ValueError('启用必须为布尔值，阈值必须为0至9999的整数')
    write_json(Path(config_dir)/'medicine-plan.json',current)
    _sync_cultivation_medicine_mode(config_dir,current['enabled'])
    return get_payload(config_dir)


def load_week(config_dir):
    directory=Path(config_dir)
    enabled=weekly_settings(directory)['enabled']
    return {day:load_tasks_strict(directory/'tasks'/f'daily-{day}.toml' if enabled else directory/'tasks/daily.toml')
            for day in DAYS}


def activity_rules(week, threshold, activity_file, item_file, *, config_dir=None):
    if Path(activity_file)==ACTIVITY_FILE:
        try:
            activity_data=get_activity(activity_file, config_dir=config_dir)[0]
        except TypeError:
            activity_data=get_activity(activity_file)[0]
    else:
        activity_data=read_json(activity_file)
    client_type=_resolve_client_type(config_dir)
    region=(client_type if isinstance(activity_data,dict) and isinstance(activity_data.get(client_type),dict) else 'Official')
    catalog=activity_data.get(region,{}).get('sideStoryStage',{})
    items=read_json(item_file)
    selected=set()
    for tasks in week.values():
        for task in tasks:
            if task.get('type')!='Fight' or task.get('_enabled') is False:
                continue
            selected.add(str((task.get('params') or {}).get('stage','')))
            selected.update(str((v.get('params') or {}).get('stage','')) for v in task.get('variants',[]))
    rules=[]
    for activity in catalog.values():
        window=activity.get('Activity') or {}
        offset=window.get('TimeZone',8)
        if type(offset) is not int or not -12<=offset<=14:
            continue
        def local(value):
            value=str(value).replace('/','-').replace(' ','T')
            parsed=datetime.fromisoformat(value)
            return (parsed.replace(tzinfo=timezone(timedelta(hours=offset))) if parsed.tzinfo is None else parsed).astimezone(CN).isoformat()
        try:
            start,end=local(window['UtcStartTime']),local(window['UtcExpireTime'])
        except (ValueError,KeyError):
            continue
        for stage in activity.get('Stages',[]):
            code=str(stage.get('Value',''))
            if code not in selected:
                continue
            drop=str(stage.get('Drop',''))
            candidates=[str(key) for key,value in items.items() if isinstance(value,dict) and value.get('name')==drop]
            item_id=drop if drop in items else candidates[0] if len(candidates)==1 else ''
            if not item_id:
                continue
            rules.append({'stage':code,'item_id':item_id,'material_name':items[item_id].get('name',item_id),
                          'threshold':threshold,'start':start,'end':end})
    # Resolve legacy OnSideStory conditions against the exact selected activity.
    for tasks in week.values():
        for task in tasks:
            for variant in task.get('variants',[]):
                if (variant.get('condition') or {}).get('type')=='OnSideStory':
                    matching=next((rule for rule in rules if rule['stage']==(variant.get('params') or {}).get('stage')),None)
                    variant['condition']=({'type':'DateTime','start':matching['start'],'end':matching['end'],'timezone':8}
                                          if matching else {'type':'Unsupported'})
    return rules


def evidence_from_log(log_file=CORE_LOG, since=None):
    from maa_medicine_evidence import parse_medicine_evidence
    try:
        # Depot produces megabytes after Fight. A byte-tail can silently omit the
        # last pharmacy observation, so retain a bounded stream of relevant lines.
        lines=deque(maxlen=600)
        with Path(log_file).open('r',encoding='utf-8',errors='replace') as stream:
            for line in stream:
                if ('init_count medicine' in line or 'Using medicines init finished' in line or
                    ('Assistant::append_callback' in line and 'UseMedicine' in line)):
                    lines.append(line)
        text=''.join(lines)
    except FileNotFoundError:
        return None
    value=parse_medicine_evidence(text,since=since)
    if value:
        value=json.loads(json.dumps(value,default=lambda value:value.isoformat() if isinstance(value,datetime) else str(value)))
    return value


def get_payload(config_dir, now=None, *, activity_file=ACTIVITY_FILE, item_file=ITEM_FILE):
    now=parse_time(now or datetime.now(CN));directory=Path(config_dir)
    config=settings(directory)
    evidence=read_json(directory/'medicine-evidence.json')
    if not evidence:
        evidence=evidence_from_log() or {}
    attempt=read_json(directory/'medicine-attempt.json')
    state=read_json(directory/'cultivation/state.json')
    inventory=state.get('inventory') or {}
    slots=[];rules=[]
    decision={'action':'disabled','reason':'集中用药未启用'}
    try:
        week=load_week(directory)
        rules=activity_rules(week,config['threshold'],activity_file,item_file,config_dir=directory)
    except (OSError,ValueError) as error:
        decision={'action':'blocked','reason':f'周计划或活动材料映射不可用：{error}'}
        week={}
    if config['enabled'] and week:
        if (state.get('settings') or {}).get('enabled') is True:
            decision={'action':'blocked','reason':'培养任务已接管，禁止叠加集中用药'}
        elif evidence.get('attempt_started_at') and not attempt:
            decision={'action':'blocked','reason':'药证据已有用药绑定但执行账本缺失，禁止重放'}
        elif attempt.get('status')=='attempted':
            decision={'action':'blocked','reason':'上次集中用药没有可靠结束记录，禁止重放'}
        elif not evidence.get('accurate') or not evidence.get('expires_at') or not evidence.get('remaining_count',0):
            decision={'action':'blocked','reason':'缺少可用药期限证据，等待普通刷取日志；不新增扫描'}
        elif parse_time(evidence['observed_at'])>now:
            decision={'action':'blocked','reason':'药期限证据时间异常，禁止自动用药'}
        elif parse_time(evidence['expires_at'])<=now:
            decision={'action':'blocked','reason':'药期限证据已过期，等待普通刷取重新识别'}
        else:
            slots=plan_opportunities(week,rules,now=now,expires_at=parse_time(evidence['expires_at']))
            completed=bool(attempt and parse_time(evidence['observed_at'])<=parse_time(attempt.get('finished_at') or attempt['started_at']))
            try:
                age=(now-parse_time(inventory['scanned_at'])).total_seconds()
                fresh=inventory.get('available') is True and not inventory.get('last_error') and 0<=age<=86400
            except (KeyError,ValueError,TypeError):
                fresh=False
            decision=select_drain(slots,inventory.get('items') or {},fresh=fresh,now=now,completed=completed)
            if not slots and not completed:
                decision={'action':'blocked','reason':'药到期或活动关闭前，没有能留出3小时的匹配02:00场次'}
    for rule in rules:
        rule['inventory']=(inventory.get('items') or {}).get(rule['item_id'])
    cult_settings=state.get('settings') if isinstance(state.get('settings'),dict) else {}
    cult_active=cult_settings.get('enabled') is True
    cult_med_mode=cult_settings.get('medicine_mode','expire_only') if cult_active else 'inactive'
    coordination={
        'active':cult_active,
        'enabled':cult_active,
        'revision':state.get('revision') if isinstance(state.get('revision'),int) else None,
        'medicine_mode':cult_med_mode,
        'priority_mode':cult_settings.get('priority_mode','cultivation_first') if cult_active else 'inactive',
        'fallback_mode':cult_settings.get('fallback_mode','weekly') if cult_active else 'inactive',
    }
    return {**config,'evidence':evidence,'needs_observation':needs_observation(evidence,now),
            'rules':rules,'slots':slots,'decision':decision,'last_attempt':attempt,
            'cultivation_coordination':coordination}


def _expiring_batches_count(evidence, now, *, cutoff=None, min_expires_at=None):
    batches=evidence.get('batches')
    if isinstance(batches,list) and batches:
        total=0
        for batch in batches:
            if not isinstance(batch,dict):
                continue
            rem=batch.get('remaining',batch.get('count',0))
            if type(rem) is not int or rem<=0:
                continue
            raw_exp=batch.get('expires_at')
            if not raw_exp:
                continue
            try:
                exp_dt=parse_time(raw_exp)
            except ValueError:
                continue
            if exp_dt<=now:
                continue
            if min_expires_at is not None and exp_dt<min_expires_at:
                continue
            if cutoff is None or exp_dt<cutoff:
                total+=rem
        return total
    rem=evidence.get('remaining_count',0)
    if type(rem) is not int or rem<=0:
        return 0
    try:
        exp_dt=parse_time(evidence['expires_at'])
    except (KeyError,TypeError,ValueError):
        return 0
    if exp_dt<=now:
        return 0
    if min_expires_at is not None and exp_dt<min_expires_at:
        return 0
    if cutoff is not None and exp_dt>=cutoff:
        return 0
    return rem


def _earliest_unexpired_expiry(evidence, now):
    batches=evidence.get('batches')
    if isinstance(batches,list) and batches:
        candidates=[]
        for batch in batches:
            if not isinstance(batch,dict):
                continue
            rem=batch.get('remaining',batch.get('count',0))
            raw_exp=batch.get('expires_at')
            if type(rem) is not int or rem<=0 or not raw_exp:
                continue
            try:
                exp_dt=parse_time(raw_exp)
            except ValueError:
                continue
            if exp_dt>now:
                candidates.append(exp_dt)
        return min(candidates) if candidates else None
    raw_top=evidence.get('expires_at')
    if not raw_top:
        return None
    try:
        top_dt=parse_time(raw_top)
    except (TypeError,ValueError):
        return None
    return top_dt if top_dt>now else None


def claim_cultivation_drain(config_dir, now=None, *, started_at=None,
                            priority_mode='cultivation_first',
                            fresh_inventory=None, activity_file=ACTIVITY_FILE,
                            item_file=ITEM_FILE, log_file=CORE_LOG):
    current_now=parse_time(now or datetime.now(CN))
    window_ref=parse_time(started_at) if started_at else current_now
    directory=Path(config_dir)
    current=settings(directory)
    if not current['enabled']:
        return {'action':'disabled','expiring_count':0,'reason':'集中用药未启用'}
    observation=evidence_from_log(log_file)
    cached=read_json(directory/'medicine-evidence.json')
    attempt=read_json(directory/'medicine-attempt.json')
    orphaned_binding=bool(cached.get('attempt_started_at') and not attempt)
    if observation and not orphaned_binding and (not cached or parse_time(observation['observed_at'])>parse_time(cached['observed_at'])):
        write_json(directory/'medicine-evidence.json',observation)
        cached=observation
    evidence=cached
    if evidence.get('attempt_started_at') and not attempt:
        return {'action':'blocked','expiring_count':0,'reason':'药证据已有用药绑定但执行账本缺失，禁止重放'}
    if attempt.get('status')=='attempted':
        return {'action':'blocked','expiring_count':0,'reason':'上次集中用药没有可靠结束记录，禁止重放'}
    if not evidence.get('accurate') or not evidence.get('expires_at') or not evidence.get('remaining_count',0):
        return {'action':'blocked','expiring_count':0,'reason':'缺少可用药期限证据，平时仅用自然理智'}
    if parse_time(evidence['observed_at'])>current_now:
        return {'action':'blocked','expiring_count':0,'reason':'药期限证据时间异常，禁止自动用药'}
    expires_at=_earliest_unexpired_expiry(evidence,current_now)
    if expires_at is None:
        return {'action':'blocked','expiring_count':0,'reason':'药期限证据已过期，等待普通刷取重新识别'}

    slot_start=window_ref.replace(hour=2,minute=0,second=0,microsecond=0)
    in_0200_window=bool(slot_start<=window_ref<slot_start+timedelta(minutes=30))
    min_exp_bound=None
    stale_pre_attempt=False
    if attempt and attempt.get('started_at'):
        attempt_started=parse_time(attempt['started_at'])
        attempt_end=parse_time(attempt.get('finished_at') or attempt['started_at'])
        ev_observed=parse_time(evidence['observed_at'])
        attempt_slot_start=attempt_started.replace(hour=2,minute=0,second=0,microsecond=0)
        if attempt_slot_start<=window_ref<attempt_slot_start+timedelta(hours=3) and ev_observed<=attempt_end:
            return {'action':'done','expiring_count':0,'reason':'当前药批次已完成集中清药，禁止重复吃药'}
        deducted_raw=evidence.get('blind_deducted_since')
        deducted_for_attempt=False
        if deducted_raw:
            try:
                deducted_for_attempt=parse_time(deducted_raw)>=attempt_slot_start
            except ValueError:
                deducted_for_attempt=False
        stale_pre_attempt=bool(ev_observed<attempt_started and not deducted_for_attempt)
        if stale_pre_attempt:
            raw_cutoff=attempt.get('drain_cutoff')
            min_exp_bound=(
                parse_time(raw_cutoff)
                if raw_cutoff
                else attempt_slot_start+timedelta(hours=27)
            )

    if not in_0200_window:
        return {'action':'wait','expiring_count':0,'reason':'非02:00临期清药窗口，仅用自然理智'}

    next_0200_finish=(slot_start+timedelta(days=1))+timedelta(hours=3)
    due_before_tomorrow=_expiring_batches_count(
        evidence,current_now,cutoff=next_0200_finish,min_expires_at=min_exp_bound
    )
    if due_before_tomorrow<=0:
        if stale_pre_attempt:
            return {'action':'done','expiring_count':0,'reason':'当前药批次已完成集中清药，禁止重复吃药'}
        return {'action':'wait','expiring_count':0,'reason':'理智药尚未进入临期清药场次，本轮仅用自然理智'}

    slots=[]
    act_decision=None
    try:
        week=load_week(directory)
        rules=activity_rules(week,current['threshold'],activity_file,item_file,config_dir=directory)
        slots=plan_opportunities(week,rules,now=window_ref,expires_at=expires_at)
        inv_items=dict(fresh_inventory) if isinstance(fresh_inventory,dict) else {}
        act_decision=select_drain(slots,inv_items,fresh=isinstance(fresh_inventory,dict),now=window_ref,completed=False)
    except Exception:
        act_decision=None

    expiring_count=due_before_tomorrow
    if act_decision and act_decision.get('action')=='drain':
        decision={**act_decision,'expiring_count':expiring_count,
                  'reason':'培养接管临期清药场次（先按优先级分配自然理智与临期药）'}
    else:
        decision={'action':'drain','expiring_count':expiring_count,
                  'reason':'理智药到期前最后02:00清药场次（培养接管按优先级消耗临期药）'}

    attempt_ts=window_ref.isoformat()
    bound_evidence={**evidence,'attempt_started_at':attempt_ts}
    write_json(directory/'medicine-evidence.json',bound_evidence)
    write_json(directory/'medicine-attempt.json',{
        **decision,
        'started_at':attempt_ts,
        'drain_cutoff':next_0200_finish.isoformat(),
        'status':'attempted',
        'evidence_observed_at':evidence.get('observed_at'),
        'expires_at':expires_at.isoformat(),
        'confirmed_used':0,
    })
    write_json(directory/'medicine-decision.json',{**decision,'decided_at':current_now.isoformat(),'slots':slots})
    return decision


def needs_observation(evidence, now):
    if not evidence.get('accurate') or not evidence.get('remaining_count',0):
        return True
    try:
        return parse_time(evidence['expires_at'])<=now
    except (KeyError,TypeError,ValueError):
        return True


def prepare(config_dir, source, dest, *, now=None, activity_file=ACTIVITY_FILE, item_file=ITEM_FILE, log_file=CORE_LOG):
    now=parse_time(now or datetime.now(CN));directory=Path(config_dir)
    current=settings(directory)
    if not current['enabled']:
        return {'action':'disabled','reason':'集中用药未启用'}
    observation=evidence_from_log(log_file)
    cached=read_json(directory/'medicine-evidence.json')
    orphaned_binding=bool(cached.get('attempt_started_at') and not read_json(directory/'medicine-attempt.json'))
    if observation and not orphaned_binding and (not cached or parse_time(observation['observed_at'])>parse_time(cached['observed_at'])):
        write_json(directory/'medicine-evidence.json',observation)
    payload=get_payload(directory,now,activity_file=activity_file,item_file=item_file)
    decision=payload['decision']
    original=load_tasks_strict(Path(source))
    # The activity guard already resolved the file that actually runs. A fallback must never get drugs.
    if decision['action']=='drain' and farm_stage(original,now)!=decision['stage']:
        decision={'action':'blocked','reason':'实际任务已回落或关卡不匹配，禁止集中用药'}
    result=prepare_fights(original,decision,now)
    # Without usable evidence, retain ONLY the pre-existing <=1-day expiry observation path.
    # Core opens the medicine panel only when medicine or expire-days is nonzero.
    # This never increases an existing ordinary medicine allowance.
    if needs_observation(payload['evidence'],now):
        stage=farm_stage(original,now)
        for old,new in zip(original,result):
            params=effective_params(old,now) if old.get('type')=='Fight' else None
            if params is not None and params.get('stage')==stage and stage:
                expiry=params.get('medicine_expire_days',0)
                if type(expiry) is int and expiry>0:
                    new['params']['medicine_expire_days']=min(expiry,1)
                    decision={**decision,'reason':decision['reason']+'；保留原有1天内到期药兜底以取得后续识别'}
                break
    document=tomllib.loads(Path(source).read_text(encoding='utf-8'))
    header='\n'.join(f'{key} = {_toml_val(value)}' for key,value in document.items() if key not in ('tasks','disabled_tasks'))
    atomic_write_text(Path(dest),header+'\n'+render_tasks(result))
    load_tasks_strict(Path(dest))
    if decision['action']=='drain':
        bound_evidence={**payload['evidence'],'attempt_started_at':now.isoformat()}
        write_json(directory/'medicine-evidence.json',bound_evidence)
        write_json(directory/'medicine-attempt.json',{**decision,'started_at':now.isoformat(),'status':'attempted',
                   'evidence_observed_at':payload['evidence'].get('observed_at'),
                   'expires_at':payload['evidence'].get('expires_at'),'confirmed_used':0})
    write_json(directory/'medicine-decision.json',{**decision,'decided_at':now.isoformat(),'slots':payload['slots']})
    return decision


def _deduct_blind_use_from_cached_batches(cached, evidence, *, since_dt):
    if not isinstance(cached,dict):
        return None
    used=evidence.get('confirmed_used',0)
    if type(used) is not int or used<=0:
        return None
    since_iso=since_dt.isoformat()
    same_since=cached.get('blind_deducted_since')==since_iso
    if same_since:
        already_used=int(cached.get('blind_deducted_used',0) or 0)
        to_deduct=max(0,used-already_used)
        if to_deduct==0:
            idem=deepcopy(cached)
            idem['confirmed_used']=max(already_used,used)
            return idem
        if cached.get('accurate') is not True:
            return deepcopy(cached)
    else:
        if cached.get('accurate') is not True:
            return None
        cached_obs_raw=cached.get('observed_at')
        if not cached_obs_raw:
            return None
        try:
            if parse_time(cached_obs_raw)>=since_dt:
                return None
        except ValueError:
            return None
        to_deduct=used

    raw_batches=cached.get('batches')
    if not isinstance(raw_batches,list) or not raw_batches:
        return None
    parsed_batches=[]
    for item in raw_batches:
        if not isinstance(item,dict):
            return None
        rem=item.get('remaining',item.get('count',0))
        exp_raw=item.get('expires_at')
        if type(rem) is not int or rem<0 or not exp_raw:
            return None
        try:
            exp_dt=parse_time(exp_raw)
        except ValueError:
            return None
        parsed_batches.append((exp_dt,deepcopy(item),rem))
    parsed_batches.sort(key=lambda row:row[0])
    kept=[]
    for exp_dt,batch_copy,rem in parsed_batches:
        if rem<=0:
            continue
        take=min(rem,to_deduct)
        to_deduct-=take
        left=rem-take
        if left>0:
            batch_copy['remaining']=left
            kept.append((exp_dt,batch_copy))
    total_left=sum(b[1]['remaining'] for b in kept)
    preserved={
        'observed_at':evidence.get('observed_at') or cached.get('observed_at'),
        'expires_at':min(b[0] for b in kept).isoformat() if kept else None,
        'remaining_count':total_left,
        'source':cached.get('source') or evidence.get('source') or 'trc',
        'batches':[b[1] for b in kept],
        'confirmed_used':used,
        'accurate':bool(kept),
        'blind_deducted_since':since_iso,
        'blind_deducted_used':used,
    }
    if not kept:
        preserved['reason']='all known medicine batches consumed'
    return preserved


def finish(config_dir, started_at, exit_code, log_file=CORE_LOG):
    directory=Path(config_dir)
    started_dt=parse_time(started_at)
    attempt=read_json(directory/'medicine-attempt.json')
    cached=read_json(directory/'medicine-evidence.json')
    evidence=evidence_from_log(log_file,since=started_dt)
    matching_attempt=bool(
        attempt and parse_time(attempt['started_at'])>=started_dt
    )
    if evidence:
        if evidence.get('accurate') is False and evidence.get('confirmed_used',0)>0:
            preserved=_deduct_blind_use_from_cached_batches(
                cached,evidence,since_dt=started_dt
            )
            if preserved is not None:
                evidence=preserved
        if matching_attempt:
            evidence['attempt_started_at']=attempt['started_at']
        write_json(directory/'medicine-evidence.json',evidence)
    if matching_attempt and attempt.get('status') in ('attempted','finished','failed'):
        if attempt.get('status')=='attempted' or exit_code!=0:
            attempt.update(
                finished_at=datetime.now(CN).isoformat(),
                status='finished' if exit_code==0 else 'failed',
                exit_code=exit_code,
            )
        if evidence is not None:
            attempt['confirmed_used']=max(
                int(attempt.get('confirmed_used',0) or 0),
                int(evidence.get('confirmed_used',0) or 0),
            )
        write_json(directory/'medicine-attempt.json',attempt)


def summary(config_dir, started_at):
    directory=Path(config_dir)
    decision=read_json(directory/'medicine-decision.json')
    if not decision or parse_time(decision['decided_at'])<parse_time(started_at):return ''
    attempt=read_json(directory/'medicine-attempt.json')
    line='理智药：'+decision['reason']
    if decision.get('action')=='drain':
        if 'material_name' in decision and 'stage' in decision:
            line+=f"；{decision['stage']} / {decision['material_name']} 库存{decision.get('inventory')}≤{decision.get('threshold')}"
        elif decision.get('expiring_count'):
            line+=f"；临期药额度{decision['expiring_count']}个"
        line+=f"；确认用药{attempt.get('confirmed_used',0)}个；状态{attempt.get('status','attempted')}"
    elif decision.get('slots'):
        slot=decision['slots'][0]
        line+=f"；预计{slot['run_at']}，{slot['stage']} / {slot['material_name']}"
    return line


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=('prepare','finish','summary'))
    parser.add_argument('--config-dir',required=True)
    parser.add_argument('--source');parser.add_argument('--dest')
    parser.add_argument('--started-at');parser.add_argument('--exit-code',type=int,default=1)
    args=parser.parse_args()
    try:
        if args.mode=='prepare':
            decision=prepare(args.config_dir,args.source,args.dest)
            print(decision['action'])
        elif args.mode=='finish':finish(args.config_dir,args.started_at,args.exit_code)
        else:print(summary(args.config_dir,args.started_at))
    except Exception as error:
        print(f'MEDICINE_POLICY_ERROR: {error}',file=sys.stderr)
        return 93
    return 0

if __name__=='__main__':sys.exit(main())
