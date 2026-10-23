#!/usr/bin/env python3
# Batch-two variant of the meter-demand reconstruction tool.
import argparse,bisect,fcntl,json,os,re,shutil,sqlite3,stat,sys,tempfile
from fractions import Fraction
from pathlib import Path

ID_RE=re.compile(r'[A-Z][A-Z0-9_-]{0,39}')
class BillingError(Exception): pass
def nodup(pairs):
    out={}
    for k,v in pairs:
        if k in out: raise BillingError(f'duplicate JSON key {k}')
        out[k]=v
    return out
def isint(v): return isinstance(v,int) and not isinstance(v,bool)
def regular(path):
    try:return stat.S_ISREG(path.lstat().st_mode)
    except OSError:return False
def config(root):
    p=root/'config.json'
    if not regular(p):raise BillingError('config is not regular')
    try:o=json.loads(p.read_bytes().decode(),object_pairs_hook=nodup)
    except BillingError:raise
    except Exception as exc:raise BillingError(f'config: {exc}')from exc
    if not isinstance(o,dict) or set(o)!={'billing_start_ms','billing_end_ms','max_gap_ms'}:raise BillingError('config fields')
    a,b,g=o['billing_start_ms'],o['billing_end_ms'],o['max_gap_ms']
    if not all(isint(x) for x in (a,b,g)) or a<0 or not (a<b) or not 60000<=g<=86400000:raise BillingError('config values')
    return o
EXPECTED={
'meters':[('meter_id','TEXT',0,1),('kind','TEXT',1,0)],
'readings':[('reading_id','TEXT',0,0),('meter_id','TEXT',0,0),('raw_ms','INTEGER',0,0),('received_ms','INTEGER',0,0),('total_wh','INTEGER',0,0)],
'calibrations':[('meter_id','TEXT',0,1),('raw_ms','INTEGER',0,2),('correct_ms','INTEGER',0,0)],
'resets':[('reset_id','TEXT',0,1),('field_tag','TEXT',1,0),('raw_ms','INTEGER',1,0),('before_wh','INTEGER',1,0),('after_wh','INTEGER',1,0)],
'accounts':[('account_id','TEXT',0,1),('target_energy_wh','INTEGER',1,0),('meter_count','INTEGER',1,0)],
'tariffs':[('tariff_id','TEXT',0,1),('start_ms','INTEGER',1,0),('end_ms','INTEGER',1,0),('rate_microcents_per_kwh','INTEGER',1,0)]}
def open_validate(root):
    path=root/'metering.sqlite3'
    if not regular(path):raise BillingError('database is not regular')
    try:db=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
    except sqlite3.Error as exc:raise BillingError(f'database open: {exc}')from exc
    try:
        objects=db.execute("select name,type from sqlite_master where name not like 'sqlite_%' order by name").fetchall()
        if objects!=sorted((name,'table') for name in EXPECTED):raise BillingError('database object set')
        for table,want in EXPECTED.items():
            got=[(r[1],r[2].upper(),r[3],r[5]) for r in db.execute(f'pragma table_info({table})')]
            if got!=want:raise BillingError(f'{table} schema')
        if db.execute('pragma integrity_check').fetchone()!=('ok',):raise BillingError('integrity')
    except Exception:db.close();raise
    return db
def valid_id(v):return isinstance(v,str) and ID_RE.fullmatch(v)
def stored_int(v):return type(v) is int and v>=0
def half_up(frac):
    if frac<0:raise BillingError('negative rounding')
    q,r=divmod(frac.numerator,frac.denominator);return q+(2*r>=frac.denominator)
def calibrator(anchors):
    anchors=sorted(anchors)
    if len(anchors)<2 or any(a[0]>=b[0] or a[1]>=b[1] for a,b in zip(anchors,anchors[1:])):raise BillingError('calibration order')
    def correct(raw):
        if raw<anchors[0][0] or raw>anchors[-1][0]:raise BillingError('raw time outside anchors')
        if raw==anchors[-1][0]:return anchors[-1][1]
        for (r0,c0),(r1,c1) in zip(anchors,anchors[1:]):
            if r0<=raw<=r1:return half_up(Fraction(c0*(r1-r0)+(raw-r0)*(c1-c0),r1-r0))
        raise BillingError('calibration lookup')
    return correct
def load(db,cfg):
    meters={}
    for meter,kind in db.execute('select meter_id,kind from meters'):
        if not valid_id(meter) or kind not in {'residential','retail','cold_storage','charger'}:raise BillingError('meter row')
        meters[meter]=kind
    if not meters:raise BillingError('no meters')
    accounts={}
    for account,target,meter_count in db.execute('select account_id,target_energy_wh,meter_count from accounts'):
        if not valid_id(account) or not stored_int(target) or not stored_int(meter_count) or account in accounts:raise BillingError('account row')
        if not 1<=meter_count<=3:raise BillingError('account meter count')
        accounts[account]=(target,meter_count)
    if not accounts:raise BillingError('no accounts')
    anchors={m:[] for m in meters}
    for meter,raw,correct in db.execute('select meter_id,raw_ms,correct_ms from calibrations'):
        if meter not in meters or not stored_int(raw) or not stored_int(correct):raise BillingError('calibration row')
        anchors[meter].append((raw,correct))
    correctors={m:calibrator(anchors[m]) for m in meters}
    readings=[]
    for row in db.execute('select rowid,reading_id,meter_id,raw_ms,received_ms,total_wh from readings'):
        rowid,rid,meter,raw,received,total=row
        if not valid_id(rid) or meter not in meters or not all(stored_int(v) for v in (raw,received,total)):raise BillingError('reading row')
        correctors[meter](raw);readings.append(row)
    resets=[];seen=set()
    for reset_id,field_tag,raw,before,after in db.execute('select reset_id,field_tag,raw_ms,before_wh,after_wh from resets'):
        if not valid_id(reset_id) or not valid_id(field_tag) or reset_id in seen or not all(stored_int(v) for v in (raw,before,after)):raise BillingError('reset row')
        seen.add(reset_id);resets.append((reset_id,field_tag,raw,before,after))
    tariffs=[]
    for tid,start,end,rate in db.execute('select tariff_id,start_ms,end_ms,rate_microcents_per_kwh from tariffs order by start_ms'):
        if not valid_id(tid) or not all(stored_int(v) for v in (start,end,rate)) or rate<=0 or end<=start:raise BillingError('tariff row')
        tariffs.append((tid,start,end,rate))
    if not tariffs or tariffs[0][1]!=cfg['billing_start_ms'] or tariffs[-1][2]!=cfg['billing_end_ms'] or any(a[2]!=b[1] for a,b in zip(tariffs,tariffs[1:])):raise BillingError('tariff coverage')
    return meters,accounts,correctors,readings,resets,tariffs
def allocate(energy,start,end,tariffs,tariff_starts):
    overlaps=[];duration=end-start
    index=max(0,bisect.bisect_right(tariff_starts,start)-1)
    while index<len(tariffs) and tariffs[index][1]<end:
        tid,a,b,rate=tariffs[index]
        overlap=max(0,min(end,b)-max(start,a))
        if overlap:overlaps.append([tid,rate,Fraction(energy*overlap,duration),0])
        index+=1
    for item in overlaps:item[3]=item[2].numerator//item[2].denominator
    remaining=energy-sum(item[3] for item in overlaps)
    order=sorted(range(len(overlaps)),key=lambda i:(-(overlaps[i][2]-overlaps[i][3]),overlaps[i][0]))
    for i in order[:remaining]:overlaps[i][3]+=1
    return [(tid,wh,half_up(Fraction(wh*rate,1000))) for tid,rate,share,wh in overlaps if wh]


def assign_accounts(meter_energy, targets):
    from functools import lru_cache
    from itertools import combinations

    if sum(meter_energy.values()) != sum(target for target, _ in targets.values()):
        raise BillingError('account energy target mismatch')
    if len(meter_energy) == len(targets) and all(count == 1 for _, count in targets.values()):
        by_target = {}
        for account, (target, _) in targets.items():
            by_target.setdefault(target, []).append(account)
        if all(len(by_target.get(energy, ())) == 1 for energy in meter_energy.values()):
            return {
                meter: by_target[energy][0]
                for meter, energy in meter_energy.items()
            }
    meters = sorted(meter_energy, key=lambda meter: (-meter_energy[meter], meter))
    accounts = sorted(targets.items(), key=lambda item: (item[1][1], -item[1][0], item[0]))
    energies = tuple(meter_energy[meter] for meter in meters)

    @lru_cache(maxsize=None)
    def search(account_index, remaining_indices):
        if account_index == len(accounts):
            return ({},) if not remaining_indices else ()
        account, (target, count) = accounts[account_index]
        results = []
        for chosen in combinations(remaining_indices, count):
            if sum(energies[index] for index in chosen) != target:
                continue
            chosen_set = set(chosen)
            rest = tuple(index for index in remaining_indices if index not in chosen_set)
            for suffix in search(account_index + 1, rest):
                assignment = {meters[index]: account for index in chosen}
                assignment.update(suffix)
                results.append(assignment)
                if len(results) == 2:
                    return tuple(results)
        return tuple(results)

    solutions = search(0, tuple(range(len(meters))))
    if len(solutions) != 1:
        raise BillingError('account meter association is not unique')
    return solutions[0]


def calculate(meters,accounts,correctors,rows,resets,tariffs,cfg):
    groups={m:[] for m in meters};by_id={};tariff_starts=[row[1] for row in tariffs]
    for row in rows:by_id.setdefault(row[1],[]).append(row)
    conflicts={m:0 for m in meters}
    for rid,items in by_id.items():
        items.sort(key=lambda r:(r[4],r[0]));winner=items[0];signature=(winner[2],winner[3],winner[5])
        for loser in items[1:]:
            if (loser[2],loser[3],loser[5])!=signature:conflicts[winner[2]]+=1
        groups[winner[2]].append((correctors[winner[2]](winner[3]),winner[1],winner[5]))
    for points in groups.values():
        points.sort(key=lambda x:(x[0],x[1]))
    matched={m:[] for m in meters}
    for reset_id,field_tag,raw,before,after in resets:
        candidates=[]
        for meter,points in groups.items():
            try:reset_time=correctors[meter](raw)
            except BillingError:continue
            times=[point[0] for point in points]
            index=bisect.bisect_left(times,reset_time)
            if index<len(times) and times[index]==reset_time:index-=1
            if index<0 or index+1>=len(points):continue
            a_time,a_id,a_total=points[index];b_time,b_id,b_total=points[index+1]
            if a_time<reset_time<=b_time and b_total<a_total and before>=a_total and b_total>=after:
                slack=(before-a_total)+(b_total-after)
                candidates.append((slack,meter,reset_time))
        if not candidates:raise BillingError('reset meter association has no candidate')
        best=min(item[0] for item in candidates)
        winners=[item for item in candidates if item[0]==best]
        if len(winners)!=1:raise BillingError('reset meter association is not unique')
        _,meter,reset_time=winners[0]
        matched[meter].append((reset_time,before,after,field_tag))
    charges={};quality={};segments={m:[] for m in meters};meter_energy={m:0 for m in meters};start_bill=cfg['billing_start_ms'];end_bill=cfg['billing_end_ms']
    for meter,points in groups.items():
        invalid=0;gap_wh=0
        for (a_time,a_id,a_total),(b_time,b_id,b_total) in zip(points,points[1:]):
            if a_time<start_bill or b_time>end_bill or b_time<=a_time:continue
            inside=[r for r in matched[meter] if a_time<r[0]<=b_time]
            if b_total>=a_total:
                if inside:invalid+=1;continue
                energy=b_total-a_total
            else:
                if len(inside)!=1 or inside[0][1]<a_total or b_total<inside[0][2]:invalid+=1;continue
                energy=(inside[0][1]-a_total)+(b_total-inside[0][2])
            meter_energy[meter]+=energy
            if b_time-a_time>cfg['max_gap_ms']:gap_wh+=energy;continue
            segments[meter].append((a_time,b_time,energy))
        quality[meter]=(conflicts[meter],invalid,gap_wh)
    meter_accounts=assign_accounts(meter_energy,accounts)
    for meter,items in segments.items():
        account=meter_accounts[meter]
        for a_time,b_time,energy in items:
            for tid,wh,cost in allocate(energy,a_time,b_time,tariffs,tariff_starts):
                old=charges.get((account,tid),(0,0));charges[(account,tid)]=(old[0]+wh,old[1]+cost)
    return charges,quality
def fsync_dir(p):
    fd=os.open(p,os.O_RDONLY|getattr(os,'O_DIRECTORY',0));os.fsync(fd);os.close(fd)
def remove(p):
    if p.is_symlink() or p.is_file():p.unlink()
    elif p.exists():shutil.rmtree(p)
def publish(root,staged):
    targets=[root/'output/bills.sqlite3',root/'reports/billing-report.json'];backups=[t.parent/f'.billing-backup-{os.getpid()}-{i}' for i,t in enumerate(targets)];old=[os.path.lexists(t) for t in targets];done=[False,False]
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
def build(path,charges,quality,cfg):
    db=sqlite3.connect(path);db.execute('pragma page_size=4096');db.execute('pragma journal_mode=off');db.execute('create table charges(account_id TEXT,tariff_id TEXT,energy_wh INTEGER NOT NULL,charge_microcents INTEGER NOT NULL,PRIMARY KEY(account_id,tariff_id)) without rowid');db.execute('create table meter_quality(meter_id TEXT PRIMARY KEY,conflicting_readings INTEGER NOT NULL,invalid_intervals INTEGER NOT NULL,unbilled_gap_wh INTEGER NOT NULL) without rowid');db.execute('create table meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) without rowid')
    db.executemany('insert into charges values(?,?,?,?)',[(a,t,*charges[(a,t)]) for a,t in sorted(charges)]);db.executemany('insert into meter_quality values(?,?,?,?)',[(m,*quality[m]) for m in sorted(quality)])
    total_energy=sum(x[0] for x in charges.values());total_cost=sum(x[1] for x in charges.values());meta={'billing_start_ms':cfg['billing_start_ms'],'billing_end_ms':cfg['billing_end_ms'],'total_energy_wh':total_energy,'total_charge_microcents':total_cost};db.executemany('insert into meta values(?,?)',[(k,str(meta[k])) for k in sorted(meta)]);db.commit();db.execute('vacuum');db.commit();db.close();return total_energy,total_cost
def execute(root):
    for d in ['output','reports']:
        if not (root/d).is_dir() or (root/d).is_symlink():raise BillingError(f'{d} directory')
    with open(root/'.billing.lock','a+b')as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);cfg=config(root);db=open_validate(root)
        try:data=load(db,cfg)
        finally:db.close()
        charges,quality=calculate(*data,cfg);stage=Path(tempfile.mkdtemp(prefix='.billing-stage-',dir=root))
        try:
            out=stage/'bills.sqlite3';report=stage/'report.json';energy,cost=build(out,charges,quality,cfg);summary={'account_count':len({a for a,t in charges}),'conflicting_readings':sum(q[0] for q in quality.values()),'invalid_intervals':sum(q[1] for q in quality.values()),'meter_count':len(quality),'total_charge_microcents':cost,'total_energy_wh':energy,'unbilled_gap_wh':sum(q[2] for q in quality.values())};report.write_text(json.dumps(summary,sort_keys=True,separators=(',',':'))+'\n')
            for p in [out,report]:
                with open(p,'rb')as h:os.fsync(h.fileno())
            publish(root,[out,report])
        finally:shutil.rmtree(stage,ignore_errors=True)
def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);a=p.parse_args()
    try:execute(Path(a.root));return 0
    except Exception as exc:print(f'billing failed: {exc}',file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
