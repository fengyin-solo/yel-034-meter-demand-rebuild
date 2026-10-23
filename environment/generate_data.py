#!/usr/bin/env python3
import hashlib,json,random,sqlite3,sys
from pathlib import Path
ROOT=Path(sys.argv[1]) if len(sys.argv)>1 else Path('/data'); rng=random.Random(982451653); start=1787328000000; end=start+24*3600_000
ROOT.mkdir(parents=True,exist_ok=True);(ROOT/'output').mkdir(exist_ok=True);(ROOT/'reports').mkdir(exist_ok=True)
(ROOT/'config.json').write_text(json.dumps({'billing_start_ms':start,'billing_end_ms':end,'max_gap_ms':3_600_000},sort_keys=True,separators=(',',':'))+'\n')
(ROOT/'metering.sqlite3').unlink(missing_ok=True)
db=sqlite3.connect(ROOT/'metering.sqlite3')
db.executescript('''CREATE TABLE meters(meter_id TEXT PRIMARY KEY,kind TEXT NOT NULL);
CREATE TABLE accounts(account_id TEXT PRIMARY KEY,target_energy_wh INTEGER NOT NULL,meter_count INTEGER NOT NULL);
CREATE TABLE readings(reading_id TEXT,meter_id TEXT,raw_ms INTEGER,received_ms INTEGER,total_wh INTEGER);
CREATE TABLE calibrations(meter_id TEXT,raw_ms INTEGER,correct_ms INTEGER,PRIMARY KEY(meter_id,raw_ms));
CREATE TABLE resets(reset_id TEXT PRIMARY KEY,field_tag TEXT NOT NULL,raw_ms INTEGER NOT NULL,before_wh INTEGER NOT NULL,after_wh INTEGER NOT NULL);
CREATE TABLE tariffs(tariff_id TEXT PRIMARY KEY,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,rate_microcents_per_kwh INTEGER NOT NULL);''')
tariffs=[('OFFPEAK',start,start+7*3600_000,850000),('DAY',start+7*3600_000,start+18*3600_000,1325000),('PEAK',start+18*3600_000,end,2110000)];db.executemany('insert into tariffs values(?,?,?,?)',tariffs)
kinds=[('RES','residential',4),('SHOP','retail',2),('COLD','cold_storage',2),('CHG','charger',4)]; meter_count=0; reading_count=0; duplicate_count=0; reset_count=0; account_energy={}; account_meters={}
for prefix,kind,count in kinds:
  for n in range(count):
    token=hashlib.blake2s(f'{prefix}:{n}:{rng.getrandbits(64)}'.encode(),digest_size=5).hexdigest().upper(); meter=f'{prefix}_{token}'; account=f'ACC_{hashlib.sha1((prefix+str(n//2)).encode()).hexdigest()[:8].upper()}';db.execute('insert into meters values(?,?)',(meter,kind));meter_count+=1;account_meters[account]=account_meters.get(account,0)+1
    drift=rng.choice([-18000,-9000,6000,15000]); raw0=start-drift; raw1=end-drift+1200
    db.executemany('insert into calibrations values(?,?,?)',[(meter,raw0,start),(meter,raw1,end+1200)])
    total=rng.randint(10000,80000); interval=rng.choice([900000,1800000]); do_reset=(n==0 and prefix in {'SHOP','CHG'}); reset_at=start+12*3600_000; reset_done=False
    t=start
    while t<=end:
      raw=t-drift; digest=hashlib.md5(f'{meter}:{t}'.encode()).hexdigest()[:12].upper(); rid=f'RD_{digest[:4]}-{digest[4:8]}-{digest[8:12]}'; received=t+rng.randint(1000,180000)
      db.execute('insert into readings values(?,?,?,?,?)',(rid,meter,raw,received,total));reading_count+=1
      if rng.random()<0.018:
        db.execute('insert into readings values(?,?,?,?,?)',(rid,meter,raw,received+rng.randint(1,5000),total));duplicate_count+=1
      step={'residential':rng.randint(80,180),'retail':rng.randint(140,420),'cold_storage':rng.randint(300,620),'charger':rng.randint(0,1500)}[kind]
      if t+interval<=end:
        account_energy[account]=account_energy.get(account,0)+step
      if do_reset and not reset_done and t<reset_at<=t+interval:
        before=total+step//2; after=rng.randint(0,25); db.execute('insert into resets values(?,?,?,?,?)',(f'RST_{token}',f'FIELD_{hashlib.sha1(token.encode()).hexdigest()[:8].upper()}',reset_at-drift,before,after));reset_count+=1; total=after+(step-step//2);reset_done=True
      else: total+=step
      t+=interval
db.executemany('insert into accounts values(?,?,?)',[(account,account_energy[account],account_meters[account]) for account in sorted(account_energy)])
db.commit();db.close()
(ROOT/'output/bills.sqlite3').write_bytes(b'stale-bill\n');(ROOT/'reports/billing-report.json').write_text('{"stale":true}\n')
source_note=f'来源类型：人工构造的仿真生成现场数据。该生成脚本用固定随机种子 982451653 保持演练样本可复现；这批数据覆盖住宅、商铺、冷库和充电站四类负荷，共 {meter_count} 只表、{reading_count} 条读数，分布上约百分之二为同一读数标识的迟到补传，另有 {reset_count} 次计划内换表归零；采样间隔只有十五分钟和三十分钟两种，时间戳偏移、补传延迟、负荷规模与换表位置都按运营规则人工编排，不代表真实客户或设备。'
(ROOT/'DATA_SOURCE.txt').write_text(source_note+'\n')
