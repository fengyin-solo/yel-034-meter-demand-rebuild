#!/usr/bin/env python3
# Compressed-air department settlement recovery tool.
import argparse,bisect,fcntl,json,os,re,shutil,sqlite3,stat,sys,tempfile
from fractions import Fraction
from pathlib import Path

ID_RE=re.compile(r'[A-Z][A-Z0-9_-]{0,39}')
class AirSettlementError(Exception): pass
def nodup(pairs):
    out={}
    for k,v in pairs:
        if k in out: raise AirSettlementError(f'duplicate JSON key {k}')
        out[k]=v
    return out
def isint(v): return isinstance(v,int) and not isinstance(v,bool)
def regular(path):
    try:return stat.S_ISREG(path.lstat().st_mode)
    except OSError:return False
def config(root):
    p=root/'config.json'
    if not regular(p):raise AirSettlementError('config is not regular')
    try:o=json.loads(p.read_bytes().decode(),object_pairs_hook=nodup)
    except AirSettlementError:raise
    except Exception as exc:raise AirSettlementError(f'config: {exc}')from exc
    if not isinstance(o,dict) or set(o)!={'settlement_start_ms','settlement_end_ms','max_offline_ms'}:raise AirSettlementError('config fields')
    a,b,g=o['settlement_start_ms'],o['settlement_end_ms'],o['max_offline_ms']
    if not all(isint(x) for x in (a,b,g)) or a<0 or not (a<b) or not 60000<=g<=86400000:raise AirSettlementError('config values')
    return o
EXPECTED={
'air_meters':[('meter_id','TEXT',0,1),('service_class','TEXT',1,0)],
'pulse_readings':[('reading_id','TEXT',0,0),('meter_id','TEXT',0,0),('raw_ms','INTEGER',0,0),('received_ms','INTEGER',0,0),('total_nm3','INTEGER',0,0)],
'clock_anchors':[('meter_id','TEXT',0,1),('raw_ms','INTEGER',0,2),('correct_ms','INTEGER',0,0)],
'sensor_swaps':[('swap_id','TEXT',0,1),('field_tag','TEXT',1,0),('raw_ms','INTEGER',1,0),('before_nm3','INTEGER',1,0),('after_nm3','INTEGER',1,0)],
'departments':[('department_id','TEXT',0,1),('target_air_nm3','INTEGER',1,0),('meter_count','INTEGER',1,0)],
'pressure_bands':[('band_id','TEXT',0,1),('start_ms','INTEGER',1,0),('end_ms','INTEGER',1,0),('rate_microcredits_per_nm3','INTEGER',1,0)]}
def open_validate(root):
    path=root/'compressor_log.sqlite3'
    if not regular(path):raise AirSettlementError('database is not regular')
    try:db=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
    except sqlite3.Error as exc:raise AirSettlementError(f'database open: {exc}')from exc
    try:
        objects=db.execute("select name,type from sqlite_master where name not like 'sqlite_%' order by name").fetchall()
        if objects!=sorted((name,'table') for name in EXPECTED):raise AirSettlementError('database object set')
        for table,want in EXPECTED.items():
            got=[(r[1],r[2].upper(),r[3],r[5]) for r in db.execute(f'pragma table_info({table})')]
            if got!=want:raise AirSettlementError(f'{table} schema')
        if db.execute('pragma integrity_check').fetchone()!=('ok',):raise AirSettlementError('integrity')
    except Exception:db.close();raise
    return db
def valid_id(v):return isinstance(v,str) and ID_RE.fullmatch(v)
def stored_int(v):return type(v) is int and v>=0
def half_up(frac):
    if frac<0:raise AirSettlementError('negative rounding')
    q,r=divmod(frac.numerator,frac.denominator);return q+(2*r>=frac.denominator)
def calibrator(anchors):
    anchors=sorted(anchors)
    if len(anchors)<2 or any(a[0]>=b[0] or a[1]>=b[1] for a,b in zip(anchors,anchors[1:])):raise AirSettlementError('calibration order')
    def correct(raw):
        if raw<anchors[0][0] or raw>anchors[-1][0]:raise AirSettlementError('raw time outside anchors')
        if raw==anchors[-1][0]:return anchors[-1][1]
        for (r0,c0),(r1,c1) in zip(anchors,anchors[1:]):
            if r0<=raw<=r1:return half_up(Fraction(c0*(r1-r0)+(raw-r0)*(c1-c0),r1-r0))
        raise AirSettlementError('calibration lookup')
    return correct
def load(db,cfg):
    air_meters={}
    for meter,service_class in db.execute('select meter_id,service_class from air_meters'):
        if not valid_id(meter) or service_class not in {'hospital','food','textile','foundry'}:raise AirSettlementError('meter row')
        air_meters[meter]=service_class
    if not air_meters:raise AirSettlementError('no air_meters')
    departments={}
    for department,target,meter_count in db.execute('select department_id,target_air_nm3,meter_count from departments'):
        if not valid_id(department) or not stored_int(target) or not stored_int(meter_count) or department in departments:raise AirSettlementError('department row')
        if not 1<=meter_count<=3:raise AirSettlementError('department meter count')
        departments[department]=(target,meter_count)
    if not departments:raise AirSettlementError('no departments')
    anchors={m:[] for m in air_meters}
    for meter,raw,correct in db.execute('select meter_id,raw_ms,correct_ms from clock_anchors'):
        if meter not in air_meters or not stored_int(raw) or not stored_int(correct):raise AirSettlementError('calibration row')
        anchors[meter].append((raw,correct))
    correctors={m:calibrator(anchors[m]) for m in air_meters}
    pulse_readings=[]
    for row in db.execute('select rowid,reading_id,meter_id,raw_ms,received_ms,total_nm3 from pulse_readings'):
        rowid,rid,meter,raw,received,total=row
        if not valid_id(rid) or meter not in air_meters or not all(stored_int(v) for v in (raw,received,total)):raise AirSettlementError('reading row')
        correctors[meter](raw);pulse_readings.append(row)
    sensor_swaps=[];seen=set()
    for swap_id,field_tag,raw,before,after in db.execute('select swap_id,field_tag,raw_ms,before_nm3,after_nm3 from sensor_swaps'):
        if not valid_id(swap_id) or not valid_id(field_tag) or swap_id in seen or not all(stored_int(v) for v in (raw,before,after)):raise AirSettlementError('reset row')
        seen.add(swap_id);sensor_swaps.append((swap_id,field_tag,raw,before,after))
    pressure_bands=[]
    for tid,start,end,rate in db.execute('select band_id,start_ms,end_ms,rate_microcredits_per_nm3 from pressure_bands order by start_ms'):
        if not valid_id(tid) or not all(stored_int(v) for v in (start,end,rate)) or rate<=0 or end<=start:raise AirSettlementError('pressure_band row')
        pressure_bands.append((tid,start,end,rate))
    if not pressure_bands or pressure_bands[0][1]!=cfg['settlement_start_ms'] or pressure_bands[-1][2]!=cfg['settlement_end_ms'] or any(a[2]!=b[1] for a,b in zip(pressure_bands,pressure_bands[1:])):raise AirSettlementError('pressure_band coverage')
    return air_meters,departments,correctors,pulse_readings,sensor_swaps,pressure_bands
def allocate(air,start,end,pressure_bands,pressure_band_starts):
    overlaps=[];duration=end-start
    index=max(0,bisect.bisect_right(pressure_band_starts,start)-1)
    while index<len(pressure_bands) and pressure_bands[index][1]<end:
        tid,a,b,rate=pressure_bands[index]
        overlap=max(0,min(end,b)-max(start,a))
        if overlap:overlaps.append([tid,rate,Fraction(air*overlap,duration),0])
        index+=1
    for item in overlaps:item[3]=item[2].numerator//item[2].denominator
    remaining=air-sum(item[3] for item in overlaps)
    order=sorted(range(len(overlaps)),key=lambda i:(-(overlaps[i][2]-overlaps[i][3]),overlaps[i][0]))
    for i in order[:remaining]:overlaps[i][3]+=1
    return [(tid,wh,half_up(Fraction(wh*rate,1000))) for tid,rate,share,wh in overlaps if wh]


def assign_departments(meter_air, targets):
    from functools import lru_cache
    from itertools import combinations

    if sum(meter_air.values()) != sum(target for target, _ in targets.values()):
        raise AirSettlementError('department air target mismatch')
    if len(meter_air) == len(targets) and all(count == 1 for _, count in targets.values()):
        by_target = {}
        for department, (target, _) in targets.items():
            by_target.setdefault(target, []).append(department)
        if all(len(by_target.get(air, ())) == 1 for air in meter_air.values()):
            return {
                meter: by_target[air][0]
                for meter, air in meter_air.items()
            }
    air_meters = sorted(meter_air, key=lambda meter: (-meter_air[meter], meter))
    departments = sorted(targets.items(), key=lambda item: (item[1][1], -item[1][0], item[0]))
    energies = tuple(meter_air[meter] for meter in air_meters)

    @lru_cache(maxsize=None)
    def search(department_index, remaining_indices):
        if department_index == len(departments):
            return ({},) if not remaining_indices else ()
        department, (target, count) = departments[department_index]
        results = []
        for chosen in combinations(remaining_indices, count):
            if sum(energies[index] for index in chosen) != target:
                continue
            chosen_set = set(chosen)
            rest = tuple(index for index in remaining_indices if index not in chosen_set)
            for suffix in search(department_index + 1, rest):
                assignment = {air_meters[index]: department for index in chosen}
                assignment.update(suffix)
                results.append(assignment)
                if len(results) == 2:
                    return tuple(results)
        return tuple(results)

    solutions = search(0, tuple(range(len(air_meters))))
    if len(solutions) != 1:
        raise AirSettlementError('department meter association is not unique')
    return solutions[0]


def calculate(air_meters,departments,correctors,rows,sensor_swaps,pressure_bands,cfg):
    groups={m:[] for m in air_meters};by_id={};pressure_band_starts=[row[1] for row in pressure_bands]
    for row in rows:by_id.setdefault(row[1],[]).append(row)
    conflicts={m:0 for m in air_meters}
    for rid,items in by_id.items():
        items.sort(key=lambda r:(r[4],r[0]));winner=items[0];signature=(winner[2],winner[3],winner[5])
        for loser in items[1:]:
            if (loser[2],loser[3],loser[5])!=signature:conflicts[winner[2]]+=1
        groups[winner[2]].append((correctors[winner[2]](winner[3]),winner[1],winner[5]))
    for points in groups.values():
        points.sort(key=lambda x:(x[0],x[1]))
    matched={m:[] for m in air_meters}
    for swap_id,field_tag,raw,before,after in sensor_swaps:
        candidates=[]
        for meter,points in groups.items():
            try:reset_time=correctors[meter](raw)
            except AirSettlementError:continue
            times=[point[0] for point in points]
            index=bisect.bisect_left(times,reset_time)
            if index<len(times) and times[index]==reset_time:index-=1
            if index<0 or index+1>=len(points):continue
            a_time,a_id,a_total=points[index];b_time,b_id,b_total=points[index+1]
            if a_time<reset_time<=b_time and b_total<a_total and before>=a_total and b_total>=after:
                slack=(before-a_total)+(b_total-after)
                candidates.append((slack,meter,reset_time))
        if not candidates:raise AirSettlementError('reset meter association has no candidate')
        best=min(item[0] for item in candidates)
        winners=[item for item in candidates if item[0]==best]
        if len(winners)!=1:raise AirSettlementError('reset meter association is not unique')
        _,meter,reset_time=winners[0]
        matched[meter].append((reset_time,before,after,field_tag))
    department_charges={};quality={};segments={m:[] for m in air_meters};meter_air={m:0 for m in air_meters};start_bill=cfg['settlement_start_ms'];end_bill=cfg['settlement_end_ms']
    for meter,points in groups.items():
        invalid=0;gap_wh=0
        for (a_time,a_id,a_total),(b_time,b_id,b_total) in zip(points,points[1:]):
            if a_time<start_bill or b_time>end_bill or b_time<=a_time:continue
            inside=[r for r in matched[meter] if a_time<r[0]<=b_time]
            if b_total>=a_total:
                if inside:invalid+=1;continue
                air=b_total-a_total
            else:
                if len(inside)!=1 or inside[0][1]<a_total or b_total<inside[0][2]:invalid+=1;continue
                air=(inside[0][1]-a_total)+(b_total-inside[0][2])
            meter_air[meter]+=air
            if b_time-a_time>cfg['max_offline_ms']:gap_wh+=air;continue
            segments[meter].append((a_time,b_time,air))
        quality[meter]=(conflicts[meter],invalid,gap_wh)
    meter_departments=assign_departments(meter_air,departments)
    for meter,items in segments.items():
        department=meter_departments[meter]
        for a_time,b_time,air in items:
            for tid,wh,cost in allocate(air,a_time,b_time,pressure_bands,pressure_band_starts):
                old=department_charges.get((department,tid),(0,0));department_charges[(department,tid)]=(old[0]+wh,old[1]+cost)
    return department_charges,quality
def fsync_dir(p):
    fd=os.open(p,os.O_RDONLY|getattr(os,'O_DIRECTORY',0));os.fsync(fd);os.close(fd)
def remove(p):
    if p.is_symlink() or p.is_file():p.unlink()
    elif p.exists():shutil.rmtree(p)
def publish(root,staged):
    targets=[root/'output/air-settlement.sqlite3',root/'reports/air-settlement-report.json'];backups=[t.parent/f'.settlement-backup-{os.getpid()}-{i}' for i,t in enumerate(targets)];old=[os.path.lexists(t) for t in targets];done=[False,False]
    try:
        for i,t in enumerate(targets):
            if old[i]:os.replace(t,backups[i])
            os.replace(staged[i],t);done[i]=True
        fsync_dir(root/'output');fsync_dir(root/'reports')
    except BaseException:
        for i in (1,0):
            if done[i] and os.path.lexists(targets[i]):remove(targets[i])
            if old[i] and os.path.lexists(backups[i]):os.replace(backups[i],targets[i])
        raise
    else:
        for p in backups:
            if os.path.lexists(p):remove(p)
def build(path,department_charges,quality,cfg):
    db=sqlite3.connect(path);db.execute('pragma page_size=4096');db.execute('pragma journal_mode=off');db.execute('create table department_charges(department_id TEXT,band_id TEXT,air_nm3 INTEGER NOT NULL,charge_microcredits INTEGER NOT NULL,PRIMARY KEY(department_id,band_id)) without rowid');db.execute('create table air_meter_quality(meter_id TEXT PRIMARY KEY,conflicting_pulse_readings INTEGER NOT NULL,invalid_intervals INTEGER NOT NULL,offline_air_nm3 INTEGER NOT NULL) without rowid');db.execute('create table meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) without rowid')
    db.executemany('insert into department_charges values(?,?,?,?)',[(a,t,*department_charges[(a,t)]) for a,t in sorted(department_charges)]);db.executemany('insert into air_meter_quality values(?,?,?,?)',[(m,*quality[m]) for m in sorted(quality)])
    total_air=sum(x[0] for x in department_charges.values());total_cost=sum(x[1] for x in department_charges.values());meta={'settlement_start_ms':cfg['settlement_start_ms'],'settlement_end_ms':cfg['settlement_end_ms'],'total_air_nm3':total_air,'total_charge_microcredits':total_cost};db.executemany('insert into meta values(?,?)',[(k,str(meta[k])) for k in sorted(meta)]);db.commit();db.execute('vacuum');db.commit();db.close();return total_air,total_cost
def execute(root):
    for d in ['output','reports']:
        if not (root/d).is_dir() or (root/d).is_symlink():raise AirSettlementError(f'{d} directory')
    with open(root/'.settlement.lock','a+b')as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);cfg=config(root);db=open_validate(root)
        try:data=load(db,cfg)
        finally:db.close()
        department_charges,quality=calculate(*data,cfg);stage=Path(tempfile.mkdtemp(prefix='.settlement-stage-',dir=root))
        try:
            out=stage/'air-settlement.sqlite3';report=stage/'report.json';air,cost=build(out,department_charges,quality,cfg);summary={'department_count':len({a for a,t in department_charges}),'conflicting_pulse_readings':sum(q[0] for q in quality.values()),'invalid_intervals':sum(q[1] for q in quality.values()),'meter_count':len(quality),'total_charge_microcredits':cost,'total_air_nm3':air,'offline_air_nm3':sum(q[2] for q in quality.values())};report.write_text(json.dumps(summary,sort_keys=True,separators=(',',':'))+'\n')
            for p in [out,report]:
                with open(p,'rb')as h:os.fsync(h.fileno())
            publish(root,[out,report])
        finally:shutil.rmtree(stage,ignore_errors=True)
def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);a=p.parse_args()
    try:execute(Path(a.root));return 0
    except Exception as exc:print(f'settlement failed: {exc}',file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
