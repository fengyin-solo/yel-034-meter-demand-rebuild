# 压缩空气分站盲表归户与结算

夜班交接前，厂务能源组发现空压站控制器的升级把一批分表名称冲掉了。各用气部门保留当天的总气量目标，现场数据库保存着脉冲累计值、对时锚点和几次换表工单；每块分表属于哪个部门需要从这些约束恢复。

请修好 `/app/air_settlement.py`。程序从 `--root` 指定的现场目录读取配置与压缩机运行库，恢复分表归属，按压力档费率生成部门结算库和一份紧凑汇总。

```text
启动：python3 /app/air_settlement.py --root <现场目录>
运行依赖：只使用镜像自带依赖
成功产物：root/output/air-settlement.sqlite3 和 root/reports/air-settlement-report.json
现场结算须在 20 秒内完成
```

成功时不向标准输出或标准错误写入内容。

## 点位与底账

`root/config.json` 是 UTF-8 JSON 对象，同一层不得出现重复键，成员只能是：

```text
settlement_start_ms
settlement_end_ms
max_offline_ms
```

三个配置值都是 JSON 整数。`settlement_start_ms` 的取值不小于零。`settlement_end_ms` 严格大于开始时刻。`max_offline_ms` 的取值范围是 60000 到 86400000。

`root/compressor_log.sqlite3` 是路径解析不跟随软链接的普通 SQLite 文件。该库不含视图或触发器。除 SQLite 内部对象外，数据库中只能存在下列六张表：

```text
air_meters(meter_id, service_class)
departments(department_id, target_air_nm3, meter_count)
pulse_readings(reading_id, meter_id, raw_ms, received_ms, total_nm3)
clock_anchors(meter_id, raw_ms, correct_ms)
sensor_swaps(swap_id, field_tag, raw_ms, before_nm3, after_nm3)
pressure_bands(band_id, start_ms, end_ms, rate_microcredits_per_nm3)
```

各编号完整匹配 `[A-Z][A-Z0-9_-]{0,39}`。`meter_id` 在 `air_meters` 内唯一。`department_id` 在 `departments` 内唯一。`band_id` 在 `pressure_bands` 内唯一。`swap_id` 在 `sensor_swaps` 内唯一。时间、累计气量和金额列使用非负 SQLite 整数。费率为正数。

`service_class` 的取值见下方允许集合。

```text
hospital
food
textile
foundry
```

`air_meters` 不携带部门字段。部门归属由总气量和表数约束恢复。`departments.target_air_nm3` 是部门名下全部分表在结算窗口内的有效累计气量目标。

每个 `meter_id` 至少在 `clock_anchors` 中出现两次。按 `raw_ms` 排序后，原始时间与校正时间都必须严格递增，所有读数的原始时间必须位于首尾锚点闭区间。

`pressure_bands` 按 `start_ms` 排列后首尾相接且互不重叠。第一档从 `settlement_start_ms` 开始，最后一档在 `settlement_end_ms` 结束。

## 盲归属约束

每块分表归入一个部门。每个部门的表数等于 `meter_count`，名下分表累计气量之和等于 `target_air_nm3`。完整分配唯一。目标总量不闭合、无解或出现两套以上合法归属时，整次运行拒收。

`sensor_swaps` 工单只保留现场工单号 `field_tag`，不能携带分表编号。判断一条换表工单属于哪块表时，先把工单的 `raw_ms` 按候选表锚点换算为校正时间，再寻找跨过该时刻且累计值下降的相邻读数。

一个候选同时满足以下三项。下降区间覆盖工单时刻。`before_nm3` 不低于下降前的累计值。下降后的累计值不低于 `after_nm3`。候选代价为：

```text
(before_nm3 - 前值) + (后值 - after_nm3)
```

最小代价候选唯一。没有候选或并列时，程序拒收。

## 校正与流量拆分

相邻锚点为 `(r0,c0)` 与 `(r1,c1)` 时：

```text
correct_ms = c0 + (raw_ms-r0)*(c1-c0)/(r1-r0)
```

结果以有理数计算，毫秒四舍五入，正半分向正方向取整。锚点本身不做舍入。

同一 `reading_id` 出现多次时，先取 `received_ms` 最小者；接收时间也相同时取 SQLite `rowid` 最小者。被丢弃行只要 `meter_id`、`raw_ms` 或 `total_nm3` 有任意一项不同，就令胜出表的 `conflicting_pulse_readings` 加一；仅接收时间或 `rowid` 不同不算冲突。

相邻有效读数都落在结算窗口内，且后点校正时间严格更大时才形成区间。正常情况下，区间气量为后值减前值。累计值下降的区间内应恰有一条已归属换表工单。换表前后值应能解释这次下降。否则 `invalid_intervals` 加一且不结算。

区间时长超过 `max_offline_ms` 时，该区间气量不收费，全部计入 `offline_air_nm3`。未超时的区间气量按它与各压力档在毫秒上的重叠比例拆分，并以有理数向下取整。剩余整数单位按小数余数从大到小分配，余数相同按 `band_id` 升序。金额按：

```text
air_nm3 * rate_microcredits_per_nm3 / 1000
```

计算，并按正半分向正方向取整。拆分和金额都使用整数与有理数。

## 交付件

结果 SQLite 只允许以下三张表：

```text
department_charges(department_id, band_id, air_nm3, charge_microcredits)
air_meter_quality(meter_id, conflicting_pulse_readings, invalid_intervals, offline_air_nm3)
meta(key, value)
```

`meta` 必须恰好写入以下四项：

```text
settlement_start_ms
settlement_end_ms
total_air_nm3
total_charge_microcredits
```

汇总报告使用键排序的紧凑 JSON，并以一个 LF 结束。报告里的部门数、分表数、总气量、总金额、冲突读数、无效区间和断线气量分别与结果库的对应列闭合。

同一条现场记录重跑或并发重跑时，两个成品逐字节稳定，输入配置和压缩机运行库保持原字节。任一失败发生时，旧成品的字节、权限和目录项类型保持原样。失败后不留下临时文件。失败后也不留下备份文件或半成品。

## 值班背景

控制柜升级前，系统按控制器端口给分表命名；升级后端口表没有随数据库导出。能源组依据部门总气量和资产表中的表数恢复归属，不依据数组顺序或表号前缀推断。

本月有两台换表在中午完成。旧表拆下前的末值与新表的起始值记录在工单中，但工单没有写分表编号。中午前后又出现过短时断网，部分累计读数在恢复连接后集中上传，同一读数可能出现多条接收时间不同的记录。

空压机出口压力在早班、常白班和晚高峰使用不同的折算费率。脉冲累计值的横轴可能因控制器时钟漂移而偏移，程序按对时锚点把原始时间换回现场时间，再判断读数落在哪个压力档。

能源组会把结果库交给财务，把汇总交给设备主管。财务沿部门、压力档和金额核对，设备主管沿分表异常计数追查断网和换表。两边总数回到同一份运行库。失败重跑不覆盖旧结算。
