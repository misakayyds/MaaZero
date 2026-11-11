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


def update_settings(config_dir, body):
    if not isinstance(body,dict) or set(body)-{'enabled','threshold'}:
        raise ValueError('仅支持启用与材料阈值')
    current=settings(config_dir)
    current.update(body)
    if type(current['enabled']) is not bool or type(current['threshold']) is not int or not 0<=current['threshold']<=9999:
        raise ValueError('启用必须为布尔值，阈值必须为0至9999的整数')
    write_json(Path(config_dir)/'medicine-plan.json',current)
    return get_payload(config_dir)


def load_week(config_dir):
    directory=Path(config_dir)
    enabled=weekly_settings(directory)['enabled']
    return {day:load_tasks_strict(directory/'tasks'/f'daily-{day}.toml' if enabled else directory/'tasks/daily.toml')
            for day in DAYS}


def activity_rules(week, threshold, activity_file, item_file):
    activity_data=(get_activity(activity_file)[0] if Path(activity_file)==ACTIVITY_FILE else read_json(activity_file))
    catalog=activity_data.get('Official',{}).get('sideStoryStage',{})
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
        rules=activity_rules(week,config['threshold'],activity_file,item_file)
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
    return {**config,'evidence':evidence,'needs_observation':needs_observation(evidence,now),
            'rules':rules,'slots':slots,'decision':decision,'last_attempt':attempt}


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


def finish(config_dir, started_at, exit_code, log_file=CORE_LOG):
    directory=Path(config_dir)
    attempt=read_json(directory/'medicine-attempt.json')
    evidence=evidence_from_log(log_file,since=parse_time(started_at))
    if evidence:
        if attempt and attempt.get('status')=='attempted' and parse_time(attempt['started_at'])>=parse_time(started_at):
            evidence['attempt_started_at']=attempt['started_at']
        write_json(directory/'medicine-evidence.json',evidence)
    if (attempt and attempt.get('status')=='attempted' and
        parse_time(attempt['started_at'])>=parse_time(started_at)):
        attempt.update(finished_at=datetime.now(CN).isoformat(),status='finished' if exit_code==0 else 'failed',exit_code=exit_code)
        # Consumption counters are evidence only; a successful exit alone does not prove all drugs were used.
        attempt['confirmed_used']=(evidence or {}).get('confirmed_used',0)
        write_json(directory/'medicine-attempt.json',attempt)


def summary(config_dir, started_at):
    directory=Path(config_dir)
    decision=read_json(directory/'medicine-decision.json')
    if not decision or parse_time(decision['decided_at'])<parse_time(started_at):return ''
    attempt=read_json(directory/'medicine-attempt.json')
    line='理智药：'+decision['reason']
    if decision.get('action')=='drain':
        line+=f"；{decision['stage']} / {decision['material_name']} 库存{decision['inventory']}≤{decision['threshold']}；确认用药{attempt.get('confirmed_used',0)}个；状态{attempt.get('status','attempted')}"
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
