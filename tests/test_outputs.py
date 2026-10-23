"""Acceptance tests for calibrated time-of-use settlement reconstruction."""

import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


ENTRY = os.environ.get("AIR_SETTLEMENT_ENTRY", "/app/air_settlement.py")
SOURCE_ROOT = Path(os.environ.get("AIR_SETTLEMENT_DATA", "/data"))
SETPRIV = Path("/usr/bin/setpriv")


@pytest.fixture
def root(tmp_path):
    destination = tmp_path / "data"
    shutil.copytree(SOURCE_ROOT, destination, symlinks=True)
    prepare_candidate_root(tmp_path, destination)
    return destination


def prepare_candidate_root(parent, root):
    for directory in (parent.parent.parent, parent.parent, parent):
        directory.chmod(0o755)
    root.chmod(0o777)
    (root / ".settlement.lock").unlink(missing_ok=True)
    for name in ("output", "reports"):
        (root / name).chmod(0o777)


def candidate_command(root, entry=ENTRY):
    command = [sys.executable, entry, "--root", str(root)]
    if SETPRIV.is_file():
        return [
            str(SETPRIV),
            "--reuid=65534",
            "--regid=65534",
            "--clear-groups",
            "--",
            *command,
        ]
    return command


def run_task(root):
    process = subprocess.Popen(
        candidate_command(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def run_two_concurrently(root):
    first = subprocess.Popen(
        candidate_command(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    second = subprocess.Popen(
        candidate_command(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return first.wait(timeout=60), second.wait(timeout=60)
    finally:
        for process in (first, second):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


def report(root):
    return json.loads((root / "reports/air-settlement-report.json").read_text(encoding="utf-8"))


def database(root):
    return sqlite3.connect(root / "compressor_log.sqlite3")


def result_database(root):
    return sqlite3.connect(root / "output/air-settlement.sqlite3")


def test_air_tool_runs_from_clean_outputs(root):
    """题面：「成功产物：root/output/air-settlement.sqlite3 和 root/reports/air-settlement-report.json」；「成功时不向标准输出或标准错误写入内容。」"""
    (root / "output/air-settlement.sqlite3").unlink(missing_ok=True)
    (root / "reports/air-settlement-report.json").unlink(missing_ok=True)
    result = run_task(root)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert (root / "output/air-settlement.sqlite3").is_file()
    assert (root / "reports/air-settlement-report.json").is_file()


def test_baseline_air_totals(root):
    """题面：「汇总报告使用键排序的紧凑 JSON，并以一个 LF 结束。」"""
    result = run_task(root)
    assert result.returncode == 0
    assert report(root) == {
        "department_count": 6,
        "conflicting_pulse_readings": 0,
        "invalid_intervals": 0,
        "meter_count": 12,
        "total_charge_microcredits": 594135745,
        "total_air_nm3": 431514,
        "offline_air_nm3": 0,
    }


def test_air_output_tables_are_exact(root):
    """题面：「结果 SQLite 只允许以下三张表：」"""
    result = run_task(root)
    with result_database(root) as db:
        names = db.execute("select name from sqlite_master where type='table' order by name").fetchall()
    assert result.returncode == 0
    assert names == [("air_meter_quality",), ("department_charges",), ("meta",)]


def test_integer_config_rejects_boolean(root):
    """题面：「三个配置值都是 JSON 整数。」；「`settlement_end_ms` 严格大于开始时刻。」"""
    path = root / "config.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["max_offline_ms"] = True
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report
    equal_window = json.loads(path.read_text(encoding="utf-8"))
    equal_window["max_offline_ms"] = 60000
    equal_window["settlement_end_ms"] = equal_window["settlement_start_ms"]
    path.write_text(json.dumps(equal_window) + "\n", encoding="utf-8")
    equal_result = run_task(root)
    assert equal_result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_missing_config_member_is_refused(root):
    """题面：「`root/config.json` 是 UTF-8 JSON 对象，同一层不得出现重复键，成员只能是：」"""
    path = root / "config.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop("max_offline_ms")
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_extra_config_member_is_refused(root):
    """题面：「`root/config.json` 是 UTF-8 JSON 对象，同一层不得出现重复键，成员只能是：」"""
    path = root / "config.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["unexpected"] = 1
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_config_duplicate_member_is_refused(root):
    """题面：「`root/config.json` 是 UTF-8 JSON 对象，同一层不得出现重复键，成员只能是：」"""
    (root / "config.json").write_text(
        '{"settlement_start_ms":1,"settlement_end_ms":2,"max_offline_ms":60000,"max_offline_ms":70000}\n',
        encoding="utf-8",
    )
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_extra_compressor_table_is_refused(root):
    """题面：「除 SQLite 内部对象外，数据库中只能存在下列六张表：」"""
    with database(root) as db:
        db.execute("create table extra(x integer)")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_anchor_order_is_enforced(root):
    """题面：「按 `raw_ms` 排序后，原始时间与校正时间都必须严格递增，所有读数的原始时间必须位于首尾锚点闭区间。」"""
    with database(root) as db:
        meter = db.execute("select meter_id from air_meters limit 1").fetchone()[0]
        db.execute(
            "update clock_anchors set correct_ms=0 where meter_id=? and raw_ms=(select max(raw_ms) from clock_anchors where meter_id=?)",
            (meter, meter),
        )
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_duplicate_pulse_conflict_is_counted(root):
    """题面：「被丢弃行只要 `meter_id`、`raw_ms` 或 `total_nm3` 有任意一项不同，就令胜出表的 `conflicting_pulse_readings` 加一；仅接收时间或 `rowid` 不同不算冲突。」"""
    with database(root) as db:
        meter, reading_id, raw, received, total = db.execute(
            "select meter_id,reading_id,raw_ms,received_ms,total_nm3 from pulse_readings limit 1"
        ).fetchone()
        db.execute("insert into pulse_readings values(?,?,?,?,?)", (reading_id, meter, raw, received + 1, total + 7))
        db.commit()
    result = run_task(root)
    assert result.returncode == 0 and report(root)["conflicting_pulse_readings"] >= 1


def test_late_copy_is_not_a_conflict(root):
    """题面：「仅接收时间或 `rowid` 不同不算冲突。」"""
    with database(root) as db:
        row = db.execute(
            "select reading_id,meter_id,raw_ms,received_ms,total_nm3 from pulse_readings order by rowid limit 1"
        ).fetchone()
        db.execute("insert into pulse_readings values(?,?,?,?,?)", (row[0], row[1], row[2], row[3] + 999, row[4]))
        db.commit()
    result = run_task(root)
    assert result.returncode == 0 and report(root)["conflicting_pulse_readings"] == 0


def test_sensor_swap_allows_counter_drop(root):
    """题面：「累计值下降的区间内应恰有一条已归属换表工单。」；「换表前后值应能解释这次下降。」"""
    result = run_task(root)
    assert result.returncode == 0
    assert report(root)["invalid_intervals"] == 0
    assert report(root)["total_air_nm3"] == 431514


def test_band_window_continuity_is_enforced(root):
    """题面：「`pressure_bands` 按 `start_ms` 排列后首尾相接且互不重叠。」"""
    with database(root) as db:
        db.execute("update pressure_bands set end_ms=end_ms-1 where band_id='PEAK'")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_service_class_domain_is_enforced(root):
    """题面：「`service_class` 的取值见下方允许集合。」"""
    with database(root) as db:
        db.execute("update air_meters set service_class='industrial' where meter_id=(select meter_id from air_meters limit 1)")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_department_air_targets_must_partition_all_air_meters(root):
    """题面：「目标总量不闭合、无解或出现两套以上合法归属时，整次运行拒收。」"""
    with database(root) as db:
        db.execute("update departments set target_air_nm3=target_air_nm3+1 where rowid=1")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_pressure_band_overlap_is_refused(root):
    """题面：「`pressure_bands` 按 `start_ms` 排列后首尾相接且互不重叠。」"""
    with database(root) as db:
        db.execute("update pressure_bands set end_ms=end_ms+1 where band_id='OFFPEAK'")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_invalid_meter_identifier_is_refused(root):
    """题面：「各编号完整匹配 `[A-Z][A-Z0-9_-]{0,39}`。」"""
    with database(root) as db:
        meter = db.execute("select meter_id from air_meters limit 1").fetchone()[0]
        db.execute("update air_meters set meter_id=lower(meter_id) where meter_id=?", (meter,))
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_air_meter_primary_key_is_enforced(root):
    """题面：「`meter_id` 在 `air_meters` 内唯一。」"""
    with database(root) as db:
        db.execute("alter table air_meters rename to air_meters_old")
        db.execute("create table air_meters(meter_id TEXT, service_class TEXT NOT NULL)")
        db.execute("insert into air_meters select * from air_meters_old")
        db.execute("drop table air_meters_old")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_negative_pulse_total_is_refused(root):
    """题面：「时间、累计气量和金额列使用非负 SQLite 整数。」"""
    with database(root) as db:
        db.execute("update pulse_readings set total_nm3=-1 where rowid=1")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_pressure_band_rate_must_be_positive(root):
    """题面：「费率为正数。」"""
    with database(root) as db:
        db.execute("update pressure_bands set rate_microcredits_per_nm3=0 where rowid=1")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_compressor_database_symlink_is_refused(root):
    """题面：「`root/compressor_log.sqlite3` 是路径解析不跟随软链接的普通 SQLite 文件。」"""
    database_path = root / "compressor_log.sqlite3"
    real_path = root / "compressor-log-real.sqlite3"
    database_path.rename(real_path)
    database_path.symlink_to(real_path.name)
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_compressor_database_view_is_refused(root):
    """题面：「该库不含视图或触发器。」"""
    with database(root) as db:
        db.execute("create view extra_view as select meter_id from air_meters")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_compressor_database_trigger_is_refused(root):
    """题面：「该库不含视图或触发器。」"""
    with database(root) as db:
        db.execute(
            "create trigger extra_trigger after insert on departments begin select 1; end"
        )
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_meter_schema_does_not_expose_department_id(root):
    """题面：「`air_meters` 不携带部门字段。」"""
    with database(root) as db:
        db.execute("alter table air_meters add column department_id TEXT")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_department_meter_count_is_part_of_partition(root):
    """题面：「每个部门的表数等于 `meter_count`，名下分表累计气量之和等于 `target_air_nm3`。」"""
    with database(root) as db:
        db.execute("update departments set meter_count=meter_count+1 where rowid=1")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_department_assignment_must_be_unique(tmp_path):
    """题面：「完整分配唯一。」"""
    root = tmp_path / "ambiguous-site"
    (root / "output").mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "config.json").write_text(
        '{"settlement_start_ms":0,"settlement_end_ms":20,"max_offline_ms":60000}\n',
        encoding="utf-8",
    )
    with sqlite3.connect(root / "compressor_log.sqlite3") as db:
        db.executescript(
            """
            create table air_meters(meter_id TEXT PRIMARY KEY, service_class TEXT NOT NULL);
            create table departments(department_id TEXT PRIMARY KEY,
                target_air_nm3 INTEGER NOT NULL, meter_count INTEGER NOT NULL);
            create table pulse_readings(reading_id TEXT, meter_id TEXT,
                raw_ms INTEGER, received_ms INTEGER, total_nm3 INTEGER);
            create table clock_anchors(meter_id TEXT, raw_ms INTEGER,
                correct_ms INTEGER, PRIMARY KEY(meter_id, raw_ms));
            create table sensor_swaps(swap_id TEXT PRIMARY KEY, field_tag TEXT NOT NULL,
                raw_ms INTEGER NOT NULL, before_nm3 INTEGER NOT NULL,
                after_nm3 INTEGER NOT NULL);
            create table pressure_bands(band_id TEXT PRIMARY KEY, start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL, rate_microcredits_per_nm3 INTEGER NOT NULL);
            insert into air_meters values('M1','hospital');
            insert into air_meters values('M2','food');
            insert into departments values('A',10,1);
            insert into departments values('B',10,1);
            insert into clock_anchors values('M1',0,0);
            insert into clock_anchors values('M1',10,10);
            insert into clock_anchors values('M2',0,0);
            insert into clock_anchors values('M2',10,10);
            insert into pulse_readings values('R1A','M1',0,1,0);
            insert into pulse_readings values('R1B','M1',10,2,10);
            insert into pulse_readings values('R2A','M2',0,1,0);
            insert into pulse_readings values('R2B','M2',10,2,10);
            insert into pressure_bands values('T1',0,20,1000);
            """
        )
        db.commit()
    prepare_candidate_root(tmp_path, root)
    result = run_task(root)
    assert result.returncode != 0
    assert not (root / "output/air-settlement.sqlite3").exists()
    assert not (root / "reports/air-settlement-report.json").exists()


def test_calibration_half_millisecond_rounds_up(tmp_path):
    """题面：「结果以有理数计算，毫秒四舍五入，正半分向正方向取整。」"""
    root = tmp_path / "half-millisecond-site"
    (root / "output").mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "config.json").write_text(
        '{"settlement_start_ms":0,"settlement_end_ms":10,"max_offline_ms":60000}\n',
        encoding="utf-8",
    )
    with sqlite3.connect(root / "compressor_log.sqlite3") as db:
        db.executescript(
            """
            create table air_meters(meter_id TEXT PRIMARY KEY, service_class TEXT NOT NULL);
            create table departments(department_id TEXT PRIMARY KEY,
                target_air_nm3 INTEGER NOT NULL, meter_count INTEGER NOT NULL);
            create table pulse_readings(reading_id TEXT, meter_id TEXT,
                raw_ms INTEGER, received_ms INTEGER, total_nm3 INTEGER);
            create table clock_anchors(meter_id TEXT, raw_ms INTEGER,
                correct_ms INTEGER, PRIMARY KEY(meter_id, raw_ms));
            create table sensor_swaps(swap_id TEXT PRIMARY KEY, field_tag TEXT NOT NULL,
                raw_ms INTEGER NOT NULL, before_nm3 INTEGER NOT NULL,
                after_nm3 INTEGER NOT NULL);
            create table pressure_bands(band_id TEXT PRIMARY KEY, start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL, rate_microcredits_per_nm3 INTEGER NOT NULL);
            insert into air_meters values('M1','hospital');
            insert into departments values('A',10,1);
            insert into clock_anchors values('M1',0,0);
            insert into clock_anchors values('M1',10,1);
            insert into pulse_readings values('R0','M1',0,1,0);
            insert into pulse_readings values('R5','M1',5,2,5);
            insert into pulse_readings values('R10','M1',10,3,10);
            insert into pressure_bands values('B0',0,1,1000);
            insert into pressure_bands values('B1',1,10,2000);
            """
        )
        db.commit()
    prepare_candidate_root(tmp_path, root)
    result = run_task(root)
    assert result.returncode == 0
    summary = report(root)
    assert summary["total_air_nm3"] == 10
    assert summary["total_charge_microcredits"] == 10


def test_residual_unit_uses_band_identifier_tie_break(tmp_path):
    """题面：「剩余整数单位按小数余数从大到小分配，余数相同按 `band_id` 升序。」"""
    root = tmp_path / "residual-tie-site"
    (root / "output").mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "config.json").write_text(
        '{"settlement_start_ms":0,"settlement_end_ms":2,"max_offline_ms":60000}\n',
        encoding="utf-8",
    )
    with sqlite3.connect(root / "compressor_log.sqlite3") as db:
        db.executescript(
            """
            create table air_meters(meter_id TEXT PRIMARY KEY, service_class TEXT NOT NULL);
            create table departments(department_id TEXT PRIMARY KEY,
                target_air_nm3 INTEGER NOT NULL, meter_count INTEGER NOT NULL);
            create table pulse_readings(reading_id TEXT, meter_id TEXT,
                raw_ms INTEGER, received_ms INTEGER, total_nm3 INTEGER);
            create table clock_anchors(meter_id TEXT, raw_ms INTEGER,
                correct_ms INTEGER, PRIMARY KEY(meter_id, raw_ms));
            create table sensor_swaps(swap_id TEXT PRIMARY KEY, field_tag TEXT NOT NULL,
                raw_ms INTEGER NOT NULL, before_nm3 INTEGER NOT NULL,
                after_nm3 INTEGER NOT NULL);
            create table pressure_bands(band_id TEXT PRIMARY KEY, start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL, rate_microcredits_per_nm3 INTEGER NOT NULL);
            insert into air_meters values('M1','hospital');
            insert into departments values('D1',1,1);
            insert into clock_anchors values('M1',0,0);
            insert into clock_anchors values('M1',2,2);
            insert into pulse_readings values('R0','M1',0,1,0);
            insert into pulse_readings values('R2','M1',2,2,1);
            insert into pressure_bands values('A',0,1,1000);
            insert into pressure_bands values('B',1,2,1000);
            """
        )
        db.commit()
    prepare_candidate_root(tmp_path, root)
    result = run_task(root)
    assert result.returncode == 0
    with result_database(root) as db:
        rows = db.execute(
            "select department_id,band_id,air_nm3,charge_microcredits from department_charges"
        ).fetchall()
    assert rows == [("D1", "A", 1, 1)]


def test_swap_ticket_hides_meter_identity(root):
    """题面：「`sensor_swaps` 工单只保留现场工单号 `field_tag`，不能携带分表编号。」"""
    with database(root) as db:
        db.execute("alter table sensor_swaps rename column field_tag to meter_id")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_swap_ticket_has_unique_meter(root):
    """题面：「`before_nm3` 不低于下降前的累计值。」"""
    with database(root) as db:
        db.execute("update sensor_swaps set before_nm3=0 where rowid=1")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_swap_ticket_interval_must_cover_ticket_time(root):
    """题面：「下降区间覆盖工单时刻。」"""
    with database(root) as db:
        db.execute("update sensor_swaps set raw_ms=0 where rowid=1")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_swap_ticket_after_value_must_match_counter(root):
    """题面：「下降后的累计值不低于 `after_nm3`。」"""
    with database(root) as db:
        db.execute("update sensor_swaps set after_nm3=after_nm3+1000000000 where rowid=1")
        db.commit()
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_offline_span_moves_to_air_credit(root):
    """题面：「区间时长超过 `max_offline_ms` 时，该区间气量不收费，全部计入 `offline_air_nm3`。」"""
    path = root / "config.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["max_offline_ms"] = 60000
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    result = run_task(root)
    assert result.returncode == 0
    assert report(root)["total_air_nm3"] == 0 and report(root)["offline_air_nm3"] > 0


def test_offline_limit_below_lower_endpoint_is_rejected(root):
    """题面：「`max_offline_ms` 的取值范围是 60000 到 86400000。」"""
    path = root / "config.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["max_offline_ms"] = 59999
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_offline_limit_lower_endpoint_is_accepted(root):
    """题面：「`max_offline_ms` 的取值范围是 60000 到 86400000。」"""
    path = root / "config.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["max_offline_ms"] = 60000
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    result = run_task(root)
    assert result.returncode == 0


def test_offline_limit_upper_endpoint_is_accepted(root):
    """题面：「`max_offline_ms` 的取值范围是 60000 到 86400000。」"""
    path = root / "config.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["max_offline_ms"] = 86400000
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    result = run_task(root)
    assert result.returncode == 0


def test_offline_limit_above_upper_endpoint_is_rejected(root):
    """题面：「`max_offline_ms` 的取值范围是 60000 到 86400000。」"""
    path = root / "config.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["max_offline_ms"] = 86400001
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    before_db = (root / "output/air-settlement.sqlite3").read_bytes()
    before_report = (root / "reports/air-settlement-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == before_db
    assert (root / "reports/air-settlement-report.json").read_bytes() == before_report


def test_air_report_schema_is_exact(root):
    """题面：「`meta` 必须恰好写入以下四项：」"""
    result = run_task(root)
    with result_database(root) as db:
        keys = set(dict(db.execute("select key,value from meta")))
    assert result.returncode == 0
    assert keys == {"settlement_start_ms", "settlement_end_ms", "total_air_nm3", "total_charge_microcredits"}


def test_air_report_matches_ledger(root):
    """题面：「报告里的部门数、分表数、总气量、总金额、冲突读数、无效区间和断线气量分别与结果库的对应列闭合。」"""
    result = run_task(root)
    values = report(root)
    with result_database(root) as db:
        air, cost = db.execute(
            "select coalesce(sum(air_nm3),0),coalesce(sum(charge_microcredits),0) from department_charges"
        ).fetchone()
        conflicts, invalid, gaps = db.execute(
            "select sum(conflicting_pulse_readings),sum(invalid_intervals),sum(offline_air_nm3) from air_meter_quality"
        ).fetchone()
    assert result.returncode == 0
    assert (values["total_air_nm3"], values["total_charge_microcredits"]) == (air, cost)
    assert (values["conflicting_pulse_readings"], values["invalid_intervals"], values["offline_air_nm3"]) == (
        conflicts,
        invalid,
        gaps,
    )


def test_air_report_has_canonical_bytes(root):
    """题面：「汇总报告使用键排序的紧凑 JSON，并以一个 LF 结束。」"""
    result = run_task(root)
    raw = (root / "reports/air-settlement-report.json").read_bytes()
    assert result.returncode == 0
    assert raw == (json.dumps(report(root), sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_repeat_run_is_byte_stable(root):
    """题面：「同一条现场记录重跑或并发重跑时，两个成品逐字节稳定，输入配置和压缩机运行库保持原字节。」"""
    assert run_task(root).returncode == 0
    first_database = (root / "output/air-settlement.sqlite3").read_bytes()
    first_report = (root / "reports/air-settlement-report.json").read_bytes()
    assert run_task(root).returncode == 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == first_database
    assert (root / "reports/air-settlement-report.json").read_bytes() == first_report


def test_compressor_log_is_unchanged(root):
    """题面：「输入配置和压缩机运行库保持原字节。」"""
    before = (root / "compressor_log.sqlite3").read_bytes()
    assert run_task(root).returncode == 0 and (root / "compressor_log.sqlite3").read_bytes() == before


def test_report_directory_failure_preserves_old_output(root):
    """题面：「任一失败发生时，旧成品的字节、权限和目录项类型保持原样。」；「失败后不留下临时文件。」"""
    assert run_task(root).returncode == 0
    old_output = (root / "output/air-settlement.sqlite3").read_bytes()
    old_report = (root / "reports/air-settlement-report.json").read_bytes()
    reports = root / "reports"
    reports.chmod(0o555)
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/air-settlement.sqlite3").read_bytes() == old_output
    assert (root / "reports/air-settlement-report.json").read_bytes() == old_report
    assert not any(path.name.startswith(".settlement-backup-") for path in root.rglob("*"))
    assert not any(path.name.startswith(".settlement-stage-") for path in root.rglob("*"))


def test_concurrent_runs_converge(root):
    """题面：「同一条现场记录重跑或并发重跑时，两个成品逐字节稳定，输入配置和压缩机运行库保持原字节。」"""
    first_code, second_code = run_two_concurrently(root)
    assert first_code == 0 and second_code == 0


def test_large_compressor_floor_finishes_in_time(tmp_path):
    """题面：「现场结算须在 20 秒内完成」"""
    root = tmp_path / "scale-site"
    shutil.copytree(SOURCE_ROOT, root, symlinks=True)
    database_path = root / "compressor_log.sqlite3"
    database_path.unlink()
    (root / "config.json").write_text(
        json.dumps(
            {
                "settlement_start_ms": 0,
                "settlement_end_ms": 86_400_000,
                "max_offline_ms": 86_400_000,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    with sqlite3.connect(database_path) as db:
        db.executescript(
            """
            create table air_meters(meter_id TEXT, service_class TEXT NOT NULL,
                primary key(meter_id));
            create table departments(department_id TEXT,
                target_air_nm3 INTEGER NOT NULL, meter_count INTEGER NOT NULL,
                primary key(department_id));
            create table pulse_readings(reading_id TEXT, meter_id TEXT,
                raw_ms INTEGER, received_ms INTEGER, total_nm3 INTEGER);
            create table clock_anchors(meter_id TEXT, raw_ms INTEGER,
                correct_ms INTEGER, primary key(meter_id, raw_ms));
            create table sensor_swaps(swap_id TEXT, field_tag TEXT NOT NULL,
                raw_ms INTEGER NOT NULL, before_nm3 INTEGER NOT NULL,
                after_nm3 INTEGER NOT NULL, primary key(swap_id));
            create table pressure_bands(band_id TEXT, start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL, rate_microcredits_per_nm3 INTEGER NOT NULL,
                primary key(band_id));
            """
        )
        db.executemany(
            "insert into air_meters values(?,?)",
            (
                (f"M{index:05d}", "hospital")
                for index in range(5_000)
            ),
        )
        db.executemany(
            "insert into departments values(?,?,?)",
            (
                (f"A{index:05d}", index + 1, 1)
                for index in range(5_000)
            ),
        )
        db.executemany(
            "insert into clock_anchors values(?,?,?)",
            (
                (f"M{index:05d}", raw, raw)
                for index in range(5_000)
                for raw in (0, 60_000)
            ),
        )
        db.executemany(
            "insert into pulse_readings values(?,?,?,?,?)",
            (
                (f"R{index:05d}{suffix}", f"M{index:05d}", raw, 1, total)
                for index in range(5_000)
                for suffix, raw, total in (
                    ("A", 0, 0),
                    ("B", 60_000, index + 1),
                )
            ),
        )
        db.executemany(
            "insert into pressure_bands values(?,?,?,?)",
            (
                (
                    f"T{index:04d}",
                    index * 60_000,
                    (index + 1) * 60_000,
                    1,
                )
                for index in range(1_440)
            ),
        )
        db.commit()
    prepare_candidate_root(tmp_path, root)
    started = time.monotonic()
    result = subprocess.run(
        candidate_command(root),
        capture_output=True,
        text=True,
        timeout=25,
    )
    elapsed = time.monotonic() - started
    summary = report(root)
    assert result.returncode == 0
    assert elapsed < 20.0
    assert summary["department_count"] == 5_000
    assert summary["meter_count"] == 5_000
    assert summary["total_air_nm3"] == 12_502_500
"""Acceptance suite for compressed-air department settlement recovery."""

