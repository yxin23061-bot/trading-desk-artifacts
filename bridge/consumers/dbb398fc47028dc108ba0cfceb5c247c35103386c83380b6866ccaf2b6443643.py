#!/usr/bin/env python3
"""Standalone HTTPS-only consumer. No production imports, credentials or files."""
import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

SCHEMA = 'trading-desk.bridge.v1'
TZ = timezone(timedelta(hours=8))
SLOTS = ('07:30','09:27','09:35','10:00','10:25','11:25','13:25','14:25','14:50','20:00')
BASE_URL = 'https://raw.githubusercontent.com/yxin23061-bot/trading-desk-artifacts/main/bridge/v1'
MAX_BYTES = 2_000_000

class InvalidArtifact(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def checksum(value):
    return hashlib.sha256(canonical({k:v for k,v in value.items() if k != 'checksum'})).hexdigest()


def stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(TZ)
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidArtifact('INVALID_TIMESTAMP') from exc


def identity(day, slot, mode='FORMAL'):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise InvalidArtifact('WRONG_DAY')
    datetime.strptime(day, '%Y-%m-%d')
    if mode == 'FORMAL' and slot not in SLOTS:
        raise InvalidArtifact('WRONG_SLOT')
    if mode == 'SMOKE' and not re.fullmatch(r'SMOKE_\d{6}', slot):
        raise InvalidArtifact('WRONG_SLOT')
    if mode not in ('FORMAL', 'SMOKE'):
        raise InvalidArtifact('WRONG_MODE')
    return 'CN_A_' + day.replace('-', '') + '_' + slot.replace(':', '')


def validate(value, day, slot, *, now=None, mode='FORMAL', historical=False):
    name = identity(day, slot, mode)
    required = {'schema_version','trading_day_id','trade_date','slot','mode','source_ts','generated_at',
                'job_status','data_status','a_chain','b_chain','dyn_signals','market_gate','mainline_top3',
                'model_only','ground_truth_provider','ledger_refs','failed_stage','recovery_status','checksum','ack_id'}
    if not isinstance(value, dict) or not required.issubset(value) or value['schema_version'] != SCHEMA:
        raise InvalidArtifact('SCHEMA_INVALID')
    if value['trade_date'] != day or value['trading_day_id'] != name[:13]:
        raise InvalidArtifact('WRONG_DAY')
    if value['slot'] != slot or value['mode'] != mode:
        raise InvalidArtifact('WRONG_SLOT')
    if value['ground_truth_provider'] != 'HiThink' or value['model_only'].get('real_trading_enabled') is not False:
        raise InvalidArtifact('PROVIDER_OR_MODEL_BOUNDARY')
    if value['checksum'] != checksum(value) or value['ack_id'] != 'ACK_' + name:
        raise InvalidArtifact('CHECKSUM_OR_ACK_INVALID')
    if 'presentation' in value:
        presentation = value['presentation']
        if (not isinstance(presentation, dict) or presentation.get('version') != 'trading-desk.card.v1'
            or not isinstance(presentation.get('lines'), list) or not presentation['lines']
            or not all(isinstance(line, str) for line in presentation['lines'])
            or not all(any(line.startswith(prefix) for line in presentation['lines']) for prefix in
                       ('市场：', '今日5只：', '盘中新机会：', '模型账户：', '系统健康：'))):
            raise InvalidArtifact('PRESENTATION_INVALID')
    for field in ('a_chain','b_chain','mainline_top3','model_only'):
        if not isinstance(value[field],dict): raise InvalidArtifact('SCHEMA_INVALID')
    if not isinstance(value['dyn_signals'],list) or not isinstance(value['ledger_refs'],list):
        raise InvalidArtifact('SCHEMA_INVALID')
    a, b = value['a_chain'], value['b_chain']
    if not isinstance(a.get('signals'),list) or not isinstance(a.get('status'),str):
        raise InvalidArtifact('A_CHAIN_INVALID')
    if a['status'] == 'OK' and (len(a['signals']) != 5 or len({x.get('signal_id') for x in a['signals']}) != 5 or any(not x.get('signal_id') for x in a['signals'])):
        raise InvalidArtifact('STABLE_FIVE_INVALID')
    for field in ('universe','scanner_hits','qualified_setups','new_signals','top_execution'):
        v = b.get(field)
        if field not in b or (v is not None and (type(v) is not int or v < 0)):
            raise InvalidArtifact('FUNNEL_INVALID')
    if any(not str(x.get('signal_id','')).startswith('DYN_') for x in value['dyn_signals']):
        raise InvalidArtifact('DYN_ID_INVALID')
    generated = stamp(value['generated_at'])
    if generated.date().isoformat() != day:
        raise InvalidArtifact('WRONG_DAY')
    current = (now or datetime.now(TZ)).astimezone(TZ)
    if not historical and (current.date().isoformat() != day or not timedelta(0) <= current-generated <= timedelta(minutes=10)):
        raise InvalidArtifact('STALE_ARTIFACT')
    if mode == 'FORMAL' and value['recovery_status'] != 'RECOVERED_STRUCTURE':
        scheduled = stamp(day+'T'+slot+':00+08:00')
        if not scheduled <= generated <= scheduled+timedelta(minutes=10):
            raise InvalidArtifact('OUTSIDE_SLOT_WINDOW')
        if not historical and not scheduled <= current <= scheduled+timedelta(minutes=10):
            raise InvalidArtifact('STALE_ARTIFACT')
    if value['source_ts']:
        source = stamp(value['source_ts'])
        if source > generated: raise InvalidArtifact('FUTURE_SOURCE')
        if mode == 'FORMAL' and slot not in ('07:30','20:00') and b.get('status') == 'OK':
            if source.date().isoformat() != day or generated-source > timedelta(minutes=10):
                raise InvalidArtifact('STALE_SOURCE')
    elif b.get('status') == 'OK':
        raise InvalidArtifact('SOURCE_MISSING')
    return value


def render(value):
    if 'presentation' in value:
        return list(value['presentation']['lines'])
    return render_legacy(value)


def render_legacy(value):
    a, b, m = value['a_chain'],value['b_chain'],value['model_only']
    lines = [f"【Trading Desk｜{value['slot']}｜{value['job_status']}】",f"交易日：{value['trade_date']}｜数据：{value['data_status']}",
             f"A链：{a['status']}｜B链：{b['status']}",
             '全A {universe} → 扫描命中 {scanner_hits} → 合格结构 {qualified_setups} → 新Signal {new_signals} → 执行 {top_execution}'.format(**b)]
    for row in a['signals']:
        lines.append(f"A {row.get('code','')}｜{row['signal_id']}｜{row.get('old_state','UNVERIFIED')} → {row.get('new_state','UNVERIFIED')}")
    if a['status'] != 'OK': lines.append('A链缺口保留，不补认历史Trigger。')
    for row in value['dyn_signals'][:5]:
        lines.append(f"DYN {row.get('code','')}｜{row['signal_id']}｜{row.get('new_state','UNVERIFIED')}")
    gate=value['market_gate'];gate=gate.get('state','DATA_GAP') if isinstance(gate,dict) else gate
    main=value['mainline_top3']
    lines += [f"Market Gate：{gate}｜主线Top3：{main.get('status','DATA_GAP')}",
              f"动态准入：{b.get('qualification_data_status','UNVERIFIED')}；扫描命中不等于Signal。",
              f"模型模拟：本槽成交 {m.get('slot_fill_count')}｜持仓 {m.get('position_count')}｜无实盘交易。",
              f"FAILED_STAGE={value['failed_stage'] or 'NONE'}｜Recovery={value['recovery_status']}",
              f"ACK_ID={value['ack_id']}｜CHECKSUM={value['checksum']}"]
    return lines


def read_remote(base_url, day, slot, *, now=None, mode='FORMAL', historical=False, timeout=15):
    if not base_url.startswith('https://'):
        raise InvalidArtifact('HTTPS_REQUIRED')
    name = identity(day, slot, mode)
    area='smoke' if mode=='SMOKE' else 'days'
    url=f"{base_url.rstrip('/')}/{area}/{day}/{name}.json"
    request=urllib.request.Request(url+'?read='+str(time.time_ns()),headers={'Cache-Control':'no-cache','User-Agent':'TradingDesk-ArtifactConsumer/1'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data=response.read(MAX_BYTES+1)
    if len(data)>MAX_BYTES:raise InvalidArtifact('ARTIFACT_TOO_LARGE')
    value=validate(json.loads(data),day,slot,now=now,mode=mode,historical=historical)
    return {'artifact':value,'url':url,'file_sha256':hashlib.sha256(data).hexdigest(),
            'render_lines':render(value),'consumer_status':'CONSUMER_VERIFIED',
            'receipt':{'ack_id':value['ack_id'],'checksum':value['checksum'],'stage':'CONSUMER_VERIFIED'},
            'user_visible_ack':False}


def main():
    p=argparse.ArgumentParser();p.add_argument('--date',required=True);p.add_argument('--slot',required=True)
    p.add_argument('--base-url',default=BASE_URL);p.add_argument('--smoke',action='store_true')
    args=p.parse_args()
    try:
        out=read_remote(args.base_url,args.date,args.slot,mode='SMOKE' if args.smoke else 'FORMAL')
        # This proves validation only. GPT_READ / USER_VISIBLE_ACK require actual platform evidence.
        print(json.dumps(out,ensure_ascii=False));return 0
    except Exception as exc:
        reason='ARTIFACT_MISSING' if isinstance(exc,urllib.error.HTTPError) and exc.code==404 else 'ARTIFACT_INVALID'
        print(json.dumps({'status':reason,'FAILED_STAGE':'PUBLISH_BRIDGE','detail':type(exc).__name__+': '+str(exc),
            'retry_next_slot':True,'tasks_must_remain_active':True,'user_visible_ack':False},ensure_ascii=False));return 2

if __name__=='__main__':sys.exit(main())
