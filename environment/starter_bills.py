#!/usr/bin/env python3
import argparse,json,sqlite3
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--root',required=True);root=Path(p.parse_args().root)
db=sqlite3.connect(root/'metering.sqlite3'); total=0
for meter,lo,hi in db.execute('select meter_id,min(total_wh),max(total_wh) from readings group by meter_id'): total+=hi-lo
(root/'reports/billing-report.json').write_text(json.dumps({'total_energy_wh':total})+'\n')
