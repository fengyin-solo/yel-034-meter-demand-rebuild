"""Acceptance tests for calibrated time-of-use billing reconstruction."""

import json
import os
import signal
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest


ENTRY = os.environ.get("ENERGY_BILLING_ENTRY", "/app/rebuild_bills.py")
SOURCE_ROOT = Path(os.environ.get("ENERGY_BILLING_DATA", "/data"))


@pytest.fixture
def root(tmp_path):
    destination = tmp_path / "data"
    shutil.copytree(SOURCE_ROOT, destination, symlinks=True)
    return destination


def run_task(root):
    return subprocess.run(
        ["python3", ENTRY, "--root", str(root)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def run_two_concurrently(root):
    first = subprocess.Popen(
        ["python3", ENTRY, "--root", str(root)],
        start_new_session=True,
    )
    second = subprocess.Popen(
        ["python3", ENTRY, "--root", str(root)],
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
    return json.loads((root / "reports/billing-report.json").read_text(encoding="utf-8"))


def database(root):
    return sqlite3.connect(root / "metering.sqlite3")


def result_database(root):
    return sqlite3.connect(root / "output/bills.sqlite3")


def test_script_exists_and_runs_without_old_outputs(root):
    """题面：「成功产物：root/output/bills.sqlite3 和 root/reports/billing-report.json」。"""
    (root / "output/bills.sqlite3").unlink(missing_ok=True)
    (root / "reports/billing-report.json").unlink(missing_ok=True)
    result = run_task(root)
    assert result.returncode == 0
    assert (root / "output/bills.sqlite3").is_file()
    assert (root / "reports/billing-report.json").is_file()


def test_baseline_report_totals(root):
    """题面：「报告为键排序紧凑 JSON并换行结尾」。"""
    result = run_task(root)
    assert result.returncode == 0
    assert report(root) == {
        "account_count": 6,
        "conflicting_readings": 0,
        "invalid_intervals": 0,
        "meter_count": 12,
        "total_charge_microcents": 594135745,
        "total_energy_wh": 431514,
        "unbilled_gap_wh": 0,
    }


def test_output_database_has_exact_tables(root):
    """题面：「输出数据库只能有」。"""
    result = run_task(root)
    with result_database(root) as db:
        names = db.execute("select name from sqlite_master where type='table' order by name").fetchall()
    assert result.returncode == 0
    assert names == [("charges",), ("meta",), ("meter_quality",)]


def test_boolean_config_is_rejected(root):
    """题面：「布尔值不算整数」。"""
    path = root / "config.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["max_gap_ms"] = True
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    before_db = (root / "output/bills.sqlite3").read_bytes()
    before_report = (root / "reports/billing-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/bills.sqlite3").read_bytes() == before_db
    assert (root / "reports/billing-report.json").read_bytes() == before_report


def test_duplicate_config_key_is_rejected(root):
    """题面：「`root/config.json` 必须是无重复键的完整 UTF-8 JSON 对象。字段如下：」。"""
    (root / "config.json").write_text(
        '{"billing_start_ms":1,"billing_end_ms":2,"max_gap_ms":60000,"max_gap_ms":70000}\n',
        encoding="utf-8",
    )
    before_db = (root / "output/bills.sqlite3").read_bytes()
    before_report = (root / "reports/billing-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/bills.sqlite3").read_bytes() == before_db
    assert (root / "reports/billing-report.json").read_bytes() == before_report


def test_extra_database_table_is_rejected(root):
    """题面：「用户对象只能是以下六张表：」。"""
    with database(root) as db:
        db.execute("create table extra(x integer)")
        db.commit()
    before_db = (root / "output/bills.sqlite3").read_bytes()
    before_report = (root / "reports/billing-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/bills.sqlite3").read_bytes() == before_db
    assert (root / "reports/billing-report.json").read_bytes() == before_report


def test_calibration_order_is_enforced(root):
    """题面：「按 raw_ms 排序时 raw_ms 与 correct_ms 都严格递增」。"""
    with database(root) as db:
        meter = db.execute("select meter_id from meters limit 1").fetchone()[0]
        db.execute(
            "update calibrations set correct_ms=0 where meter_id=? and raw_ms=(select max(raw_ms) from calibrations where meter_id=?)",
            (meter, meter),
        )
        db.commit()
    before_db = (root / "output/bills.sqlite3").read_bytes()
    before_report = (root / "reports/billing-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/bills.sqlite3").read_bytes() == before_db
    assert (root / "reports/billing-report.json").read_bytes() == before_report


def test_conflicting_duplicate_reading_is_counted(root):
    """题面：「冲突行：meter_id、raw_ms、total_wh 任一不同计一个 conflicting_readings」。"""
    with database(root) as db:
        meter, reading_id, raw, received, total = db.execute(
            "select meter_id,reading_id,raw_ms,received_ms,total_wh from readings limit 1"
        ).fetchone()
        db.execute("insert into readings values(?,?,?,?,?)", (reading_id, meter, raw, received + 1, total + 7))
        db.commit()
    result = run_task(root)
    assert result.returncode == 0 and report(root)["conflicting_readings"] >= 1


def test_received_time_only_duplicate_is_not_conflict(root):
    """题面：「非冲突：只有 received_ms 或 rowid 不同不计冲突」。"""
    with database(root) as db:
        row = db.execute(
            "select reading_id,meter_id,raw_ms,received_ms,total_wh from readings order by rowid limit 1"
        ).fetchone()
        db.execute("insert into readings values(?,?,?,?,?)", (row[0], row[1], row[2], row[3] + 999, row[4]))
        db.commit()
    result = run_task(root)
    assert result.returncode == 0 and report(root)["conflicting_readings"] == 0


def test_reset_allows_counter_drop(root):
    """题面：「累计值回退时，必须恰好有一条换表记录的校正时间落在 (前点, 后点]」。"""
    result = run_task(root)
    assert result.returncode == 0
    assert report(root)["invalid_intervals"] == 0
    assert report(root)["total_energy_wh"] == 431514


def test_tariff_coverage_is_enforced(root):
    """题面：「`tariffs` 必须按 start_ms 首尾相接、无重叠地恰好覆盖整个账期。」"""
    with database(root) as db:
        db.execute("update tariffs set end_ms=end_ms-1 where tariff_id='PEAK'")
        db.commit()
    before_db = (root / "output/bills.sqlite3").read_bytes()
    before_report = (root / "reports/billing-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/bills.sqlite3").read_bytes() == before_db
    assert (root / "reports/billing-report.json").read_bytes() == before_report


def test_meter_kind_enum_is_enforced(root):
    """题面：「kind：residential、retail、cold_storage、charger」。"""
    with database(root) as db:
        db.execute("update meters set kind='industrial' where meter_id=(select meter_id from meters limit 1)")
        db.commit()
    before_db = (root / "output/bills.sqlite3").read_bytes()
    before_report = (root / "reports/billing-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/bills.sqlite3").read_bytes() == before_db
    assert (root / "reports/billing-report.json").read_bytes() == before_report


def test_account_energy_targets_must_partition_all_meters(root):
    """题面：「目标总电量不符、无解或存在多个合法分配时，整轮重建失败。」"""
    with database(root) as db:
        db.execute("update accounts set target_energy_wh=target_energy_wh+1 where rowid=1")
        db.commit()
    before_db = (root / "output/bills.sqlite3").read_bytes()
    before_report = (root / "reports/billing-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/bills.sqlite3").read_bytes() == before_db
    assert (root / "reports/billing-report.json").read_bytes() == before_report


def test_meter_schema_does_not_expose_account_id(root):
    """题面：「表计到账户的归属没有直接字段。」"""
    with database(root) as db:
        db.execute("alter table meters add column account_id TEXT")
        db.commit()
    before_db = (root / "output/bills.sqlite3").read_bytes()
    before_report = (root / "reports/billing-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/bills.sqlite3").read_bytes() == before_db
    assert (root / "reports/billing-report.json").read_bytes() == before_report


def test_account_meter_count_is_part_of_partition(root):
    """题面：「每个账户的表数要正好是 `meter_count`，合计电量要正好是 `target_energy_wh`。」"""
    with database(root) as db:
        db.execute("update accounts set meter_count=meter_count+1 where rowid=1")
        db.commit()
    before_db = (root / "output/bills.sqlite3").read_bytes()
    before_report = (root / "reports/billing-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/bills.sqlite3").read_bytes() == before_db
    assert (root / "reports/billing-report.json").read_bytes() == before_report


def test_reset_schema_requires_field_tag(root):
    """题面：「换表记录只带工单标签 `field_tag`，不得携带表计编号。」"""
    with database(root) as db:
        db.execute("alter table resets rename column field_tag to meter_id")
        db.commit()
    before_db = (root / "output/bills.sqlite3").read_bytes()
    before_report = (root / "reports/billing-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/bills.sqlite3").read_bytes() == before_db
    assert (root / "reports/billing-report.json").read_bytes() == before_report


def test_reset_requires_unique_meter_mapping(root):
    """题面：「某条工单没有候选，或最小代价并列时，整轮重建失败。」"""
    with database(root) as db:
        db.execute("update resets set before_wh=0 where rowid=1")
        db.commit()
    before_db = (root / "output/bills.sqlite3").read_bytes()
    before_report = (root / "reports/billing-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/bills.sqlite3").read_bytes() == before_db
    assert (root / "reports/billing-report.json").read_bytes() == before_report


def test_long_interval_becomes_unbilled_gap(root):
    """题面：「时长大于 max_gap_ms：不收费，电量计入 unbilled_gap_wh」。"""
    path = root / "config.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["max_gap_ms"] = 60000
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    result = run_task(root)
    assert result.returncode == 0
    assert report(root)["total_energy_wh"] == 0 and report(root)["unbilled_gap_wh"] > 0


def test_max_gap_below_lower_bound_is_rejected(root):
    """题面：「max_gap_ms: INTEGER from 60000 through 86400000」。"""
    path = root / "config.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["max_gap_ms"] = 59999
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    before_db = (root / "output/bills.sqlite3").read_bytes()
    before_report = (root / "reports/billing-report.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/bills.sqlite3").read_bytes() == before_db
    assert (root / "reports/billing-report.json").read_bytes() == before_report
    document["max_gap_ms"] = 60000
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    boundary = run_task(root)
    assert boundary.returncode == 0


def test_report_schema_and_meta_are_exact(root):
    """题面：「meta 恰好含 billing_start_ms、billing_end_ms、total_energy_wh、total_charge_microcents 四行」。"""
    result = run_task(root)
    with result_database(root) as db:
        keys = set(dict(db.execute("select key,value from meta")))
    assert result.returncode == 0
    assert keys == {"billing_start_ms", "billing_end_ms", "total_energy_wh", "total_charge_microcents"}


def test_report_totals_match_database(root):
    """题面：「报告中的账户数、表计数、总电量、总金额和三项质量计数与输出表对应列求和一致」。"""
    result = run_task(root)
    values = report(root)
    with result_database(root) as db:
        energy, cost = db.execute(
            "select coalesce(sum(energy_wh),0),coalesce(sum(charge_microcents),0) from charges"
        ).fetchone()
        conflicts, invalid, gaps = db.execute(
            "select sum(conflicting_readings),sum(invalid_intervals),sum(unbilled_gap_wh) from meter_quality"
        ).fetchone()
    assert result.returncode == 0
    assert (values["total_energy_wh"], values["total_charge_microcents"]) == (energy, cost)
    assert (values["conflicting_readings"], values["invalid_intervals"], values["unbilled_gap_wh"]) == (
        conflicts,
        invalid,
        gaps,
    )


def test_report_is_canonical_bytes(root):
    """题面：「报告为键排序紧凑 JSON并换行结尾」。"""
    result = run_task(root)
    raw = (root / "reports/billing-report.json").read_bytes()
    assert result.returncode == 0
    assert raw == (json.dumps(report(root), sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_repeat_run_is_byte_stable(root):
    """题面：「相同现场重复或并发运行均成功且输出字节一致。」"""
    assert run_task(root).returncode == 0
    first_database = (root / "output/bills.sqlite3").read_bytes()
    first_report = (root / "reports/billing-report.json").read_bytes()
    assert run_task(root).returncode == 0
    assert (root / "output/bills.sqlite3").read_bytes() == first_database
    assert (root / "reports/billing-report.json").read_bytes() == first_report


def test_input_database_is_unchanged(root):
    """题面：「输入数据库和配置不变」。"""
    before = (root / "metering.sqlite3").read_bytes()
    assert run_task(root).returncode == 0 and (root / "metering.sqlite3").read_bytes() == before


def test_report_directory_failure_preserves_old_output(root):
    """题面：「任一步骤失败，旧输出各自的字节、权限与目录项类型不变」。"""
    assert run_task(root).returncode == 0
    old = (root / "output/bills.sqlite3").read_bytes()
    reports = root / "reports"
    reports.rename(root / "reports-old")
    reports.symlink_to(root / "absent-reports")
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/bills.sqlite3").read_bytes() == old


def test_concurrent_runs_converge(root):
    """题面：「相同现场重复或并发运行均成功且输出字节一致。」"""
    first_code, second_code = run_two_concurrently(root)
    assert first_code == 0 and second_code == 0


def test_large_site_finishes_within_public_limit(tmp_path):
    """题面：「现场账单重建须在 20 秒内结束。」"""
    root = tmp_path / "scale-site"
    shutil.copytree(SOURCE_ROOT, root, symlinks=True)
    database_path = root / "metering.sqlite3"
    database_path.unlink()
    (root / "config.json").write_text(
        json.dumps(
            {
                "billing_start_ms": 0,
                "billing_end_ms": 86_400_000,
                "max_gap_ms": 86_400_000,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    with sqlite3.connect(database_path) as db:
        db.executescript(
            """
            create table meters(meter_id TEXT, kind TEXT NOT NULL,
                primary key(meter_id));
            create table accounts(account_id TEXT,
                target_energy_wh INTEGER NOT NULL, meter_count INTEGER NOT NULL,
                primary key(account_id));
            create table readings(reading_id TEXT, meter_id TEXT,
                raw_ms INTEGER, received_ms INTEGER, total_wh INTEGER);
            create table calibrations(meter_id TEXT, raw_ms INTEGER,
                correct_ms INTEGER, primary key(meter_id, raw_ms));
            create table resets(reset_id TEXT, field_tag TEXT NOT NULL,
                raw_ms INTEGER NOT NULL, before_wh INTEGER NOT NULL,
                after_wh INTEGER NOT NULL, primary key(reset_id));
            create table tariffs(tariff_id TEXT, start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL, rate_microcents_per_kwh INTEGER NOT NULL,
                primary key(tariff_id));
            """
        )
        db.executemany(
            "insert into meters values(?,?)",
            (
                (f"M{index:05d}", "residential")
                for index in range(5_000)
            ),
        )
        db.executemany(
            "insert into accounts values(?,?,?)",
            (
                (f"A{index:05d}", index + 1, 1)
                for index in range(5_000)
            ),
        )
        db.executemany(
            "insert into calibrations values(?,?,?)",
            (
                (f"M{index:05d}", raw, raw)
                for index in range(5_000)
                for raw in (0, 60_000)
            ),
        )
        db.executemany(
            "insert into readings values(?,?,?,?,?)",
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
            "insert into tariffs values(?,?,?,?)",
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
    started = time.monotonic()
    result = subprocess.run(
        ["python3", ENTRY, "--root", str(root)],
        capture_output=True,
        text=True,
        timeout=25,
    )
    elapsed = time.monotonic() - started
    summary = report(root)
    assert result.returncode == 0
    assert elapsed < 20.0
    assert summary["account_count"] == 5_000
    assert summary["meter_count"] == 5_000
    assert summary["total_energy_wh"] == 12_502_500
"""Batch-two acceptance suite for meter-demand reconstruction."""

