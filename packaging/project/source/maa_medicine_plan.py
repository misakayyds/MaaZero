"""Pure last-opportunity planning and stage-scoped medicine parameters."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))
DAYS = ('mon','tue','wed','thu','fri','sat','sun')
RUN_BUDGET = timedelta(hours=3)


def parse_time(value):
    if str(value).isdigit():
        return datetime.fromtimestamp(int(value),CN)
    parsed = datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return (parsed.replace(tzinfo=CN) if parsed.tzinfo is None else parsed).astimezone(CN)


def condition_matches(condition, now):
    if not condition or condition.get('type')=='Always':
        return True
    kind=condition.get('type')
    offset=condition.get('timezone',8)
    if type(offset) is not int or not -12<=offset<=14:
        return False
    local=now.astimezone(timezone(timedelta(hours=offset)))
    if kind=='DateTime':
        def bound(value):
            parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
            return parsed.replace(tzinfo=local.tzinfo) if parsed.tzinfo is None else parsed
        return (not condition.get('start') or local>=bound(condition['start'])) and (
            not condition.get('end') or local<=bound(condition['end']))
    if kind=='Weekday':
        return local.strftime('%a') in condition.get('weekdays',[])
    if kind=='Time':
        value=local.strftime('%H:%M:%S')
        start,end=condition.get('start'),condition.get('end')
        if start and end and start>end:
            return value>=start or value<=end
        return (not start or value>=start) and (not end or value<=end)
    # Unknown custom conditions are never assumed active by the medicine planner.
    return False


def effective_params(task, now):
    if task.get('_enabled') is False:
        return None
    result=deepcopy(task.get('params') or {})
    variants=task.get('variants') or []
    if not variants:
        return result
    matched=False
    for variant in variants:
        if condition_matches(variant.get('condition'),now):
            result.update(deepcopy(variant.get('params') or {}))
            matched=True
            if task.get('strategy','first')!='merge':
                break
    return result if matched else None


def farm_stage(tasks, now):
    fights=[(task,effective_params(task,now)) for task in tasks if task.get('type')=='Fight']
    fights=[(task,params) for task,params in fights if params is not None]
    selected=next(((task,params) for task,params in fights if task.get('name')=='理智作战'),None)
    if selected is None:
        selected=next(((task,params) for task,params in fights
                       if not str(params.get('stage','')).endswith('Annihilation')),None)
    return str(selected[1].get('stage','')) if selected else ''


def plan_opportunities(week, rules, *, now, expires_at, calendar_mode='game_day'):
    """Last 02:00 slot per stage, with the entire three-hour budget before cutoff."""
    now=parse_time(now)
    expires_at=parse_time(expires_at)
    if calendar_mode not in ('game_day','natural_day'):
        raise ValueError('unknown calendar mode')
    if expires_at<=now or expires_at-now>timedelta(days=42):
        return []
    result=[]
    for rule in rules:
        if rule.get('enabled') is False:
            continue
        opening,closing=parse_time(rule['start']),parse_time(rule['end'])
        cutoff=min(expires_at,closing)
        latest=None
        cursor=now.replace(hour=2,minute=0,second=0,microsecond=0)
        for _ in range(44):
            logical=cursor-timedelta(hours=4) if calendar_mode=='game_day' else cursor
            weekday=DAYS[logical.weekday()]
            if cursor+timedelta(minutes=30)>now and cursor>=opening and max(cursor,now)+RUN_BUDGET<=cutoff and (
                farm_stage(week.get(weekday,[]),cursor)==rule['stage']):
                latest={**deepcopy(rule),'run_at':cursor.isoformat(),'weekday':weekday,
                        'cutoff':cutoff.isoformat(),'game_date':logical.date().isoformat()}
            cursor+=timedelta(days=1)
            if cursor>=cutoff:
                break
        if latest:
            result.append(latest)
    return sorted(result,key=lambda item:(item['run_at'],item['cutoff'],item['stage']))


def select_drain(slots, inventory, *, fresh, now, completed=False):
    if completed:
        return {'action':'completed','reason':'本批次已处理，新药不沿用旧期限'}
    now=parse_time(now)
    due=[slot for slot in slots if parse_time(slot['run_at'])<=now<parse_time(slot['run_at'])+timedelta(minutes=30)
         and now+RUN_BUDGET<=parse_time(slot['cutoff'])]
    if not due:
        return {'action':'wait','reason':'等待最后匹配关卡的02:00场次'}
    if not fresh:
        return {'action':'blocked','reason':'材料库存证据过旧或扫描失败'}
    for slot in due:
        count=inventory.get(slot['item_id'])
        if type(count) is not int or count<0:
            continue
        if count<=slot.get('threshold',80):
            return {**slot,'action':'drain','inventory':count,'reason':'过期前最后匹配场次且材料未超过阈值'}
    missing=any(type(inventory.get(slot['item_id'])) is not int or inventory.get(slot['item_id'],0)<0 for slot in due)
    return {'action':'blocked' if missing else 'skip','reason':'材料库存未知' if missing else '材料库存已超过阈值'}


def prepare_fights(tasks, decision, now):
    """Freeze only Fight params, keep originals intact, never leak medicines to fallback."""
    result=deepcopy(tasks)
    assigned=False
    for task in result:
        if task.get('type')!='Fight' or task.get('_enabled') is False:
            continue
        params=effective_params(task,now)
        if params is None:
            task['_enabled']=False
            continue
        params.pop('expiring_medicine',None)
        params.update(medicine=0,medicine_expire_days=0,stone=0,series=0)
        if not assigned and decision.get('action')=='drain' and params.get('stage')==decision.get('stage'):
            params['medicine']=9999
            # The inventory threshold only triggers the session, never stops it at 80.
            params.pop('drops',None)
            params.pop('times',None)
            assigned=True
        task['params']=params
        task.pop('variants',None)
        task.pop('strategy',None)
    return result
