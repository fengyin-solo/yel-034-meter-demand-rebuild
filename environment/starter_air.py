#!/usr/bin/env python3
import argparse,json,sqlite3
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--root',required=True);root=Path(p.parse_args().root)
db=sqlite3.connect(root/'compressor_log.sqlite3'); total=0
for meter,lo,hi in db.execute('select meter_id,min(total_nm3),max(total_nm3) from pulse_readings group by meter_id'): total+=hi-lo
(root/'reports/air-settlement-report.json').write_text(json.dumps({'total_air_nm3':total})+'\n')
