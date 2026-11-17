# Battery Cell Cycle Capacity API

分批接收同一电芯的电流采样（UTC 秒 + 电流 A），识别完整充放电循环并按区间
前值积分计算充/放电容量（Ah），通过 FastAPI + SQLite 提供上传与查询接口。
另提供**对照组**接口：按同循环序号用对照电芯的放电容量筛出容量离群的待检
电芯，供寿命分析复核。

## 业务规则（容量报告）

1. 电流为 0 的采样是**段界**。
2. 只有连续序列 `零 → 正电流 → 零 → 负电流 → 零` 构成一个循环；末尾未闭合
   的序列不计入。
3. 相邻采样时间差 **> 60 秒**即断档：当前序列截断，且该长区间本身不参与积分
   （恰好 60 秒不断档）。
4. 容量按**区间前值积分**（左矩形法）：`Ah += |I_prev| * (t - t_prev) / 3600`，
   充电、放电分别累计。
5. 同电芯同秒同值重复上传**幂等**（不重复插入）；同秒异值则**整批拒收**
   （HTTP 409，库内数据不变）。
6. 迟到补点与历史采样合并后**全量重算**，循环可能被改写、编号重新排列；
   每次成功导入生成一个新的报告版本，历史版本保留。
7. 导入或计算失败时事务回滚，旧采样与旧报告保持不变，并在 `upload_log` 留痕。
8. 并发：写入用 `BEGIN IMMEDIATE` 串行化；查询在单事务快照内读取同一版本的
   版本号、循环明细与总数，保证三者属于同一次已提交状态（SQLite WAL）。

## 业务规则（对照组）

1. 建组时固定每颗对照电芯**当次最新的已提交报告版本**与**绝对容差**
   （Ah）；整组在同一事务里读到同一个已提交状态。
2. 判定按**循环序号**对齐，比较**放电容量**（`discharge_ah`）：
   - 每个序号至少需要 **3 份对照值**，否则该序号标 `indeterminate`（不可判）；
   - 待检电芯本身属于对照组时，**剔除自身**后再计数（剩 2 份即不可判）；
   - 偏离 = `|待检 - 对照中位数|`，阈值 = `max(3 × MAD, 绝对容差)`，
     **严格大于**阈值才标 `outlier`；
   - MAD 为未缩放的中位数绝对偏差（不乘 1.4826）。
3. 待检电芯在某序号没有循环时标 `missing`，不算离群、也不算不可判。
4. 任一对照电芯补点/重传产生**新的报告版本**后，旧组立即**过期**
   （`is_stale=true`，并列出 `stale_members` 的固定版本与最新版本）；
   此时判定接口返回 **HTTP 409**（响应体不含任何循环判定），须先重建。
   该规则按版本号保守判定：即使补点后循环明细恰好没变，也视为过期。
5. 重建在单事务内重新固定全部成员版本并递增 `revision`；失败（未知电芯、
   非法容差等）**整组回滚**，旧 revision 与其过期状态原样保留。
6. 判定响应列出每个序号实际参与判定的对照电芯及其**报告版本**，以及待检
   电芯的报告版本（默认取最新，也可用 `report_version` 显式指定）。
   整体结论 `verdict`：任一号序 `outlier` → `outlier`；否则若无任何可判
   序号或存在 `indeterminate` → `indeterminate`；其余 → `normal`。
   `judged_cycles` 列出真正参与比较的序号，`missing_cycles` 列出待检电芯
   缺测的序号。
7. 建组、重建、补传、判定交错进行时，判定在单个读事务内读取组快照、全部
   对照版本与待检报告，绝不混用不同已提交状态；原容量报告接口不受影响。
   并发时若读到过期快照按 409 处理，不会把旧快照当当前判定返回。

## 接口（启动后见 `/docs`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cells/{cell_id}/samples` | multipart 上传 CSV（字段名 `file`） |
| GET  | `/cells/{cell_id}/report` | 最新已提交报告：版本、总数、循环明细 |
| GET  | `/cells` | 全部电芯及最新版本概览 |
| POST | `/control-groups` | 建组：`cell_ids`、`abs_tolerance_ah`、可选 `name` |
| GET  | `/control-groups` | 全部对照组及过期状态 |
| GET  | `/control-groups/{group_id}` | 单个对照组、成员固定版本、过期成员 |
| PUT  | `/control-groups/{group_id}` | 重建：重新固定成员与容差，`revision+1` |
| GET  | `/control-groups/{group_id}/judge/{cell_id}` | 判定待检电芯，可选 `?report_version=` |
| GET  | `/health` | 存活探针 |

建组成功返回 201；未知电芯 404；非法定义（空成员、负容差等）422；
对照快照过期 409。

CSV 必须含表头，列名大小写/空格不敏感，时间列支持
`timestamp/ts/timestamp_utc/timestamp_seconds/utc_seconds/time_s`，
电流列支持 `current/current_a/i/amps` 等。示例见 `examples/`。

```bash
curl -X POST http://localhost:8000/cells/CELL-001/samples \
  -F "file=@examples/sample_one_cycle.csv"
curl http://localhost:8000/cells/CELL-001/report

curl -X POST http://localhost:8000/control-groups \
  -H 'Content-Type: application/json' \
  -d '{"cell_ids":["CTRL-01","CTRL-02","CTRL-03"],"abs_tolerance_ah":0.005}'
curl http://localhost:8000/control-groups/1/judge/CELL-001
curl -X PUT http://localhost:8000/control-groups/1 \
  -H 'Content-Type: application/json' \
  -d '{"cell_ids":["CTRL-01","CTRL-02","CTRL-03"],"abs_tolerance_ah":0.005}'
```

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# API:   http://localhost:8000
# Docs:  http://localhost:8000/docs
```

SQLite 数据保存在命名卷 `battery-data`（容器内 `/data/battery.db`）。

### 本地运行

```bash
pip install -r requirements.txt
BATTERY_DB_PATH=./data/battery.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 测试

```bash
pip install -r requirements-dev.txt   # 运行测试还需要 httpx
python -m unittest discover -s tests -v       # 算法单测 + HTTP 集成测试
python tests/live_smoke.py                     # 对已运行服务的端到端冒烟
```

覆盖场景：基本循环与前值积分、断档（61s 截断 / 60s 保留）、无零符号直翻、
未闭合不计、补点改写与重新编号、幂等重投、异值整批拒收、失败回滚、
8 写 8 读并发一致性、同秒异值并发竞争；对照组建组固定版本、中位数/MAD/
容差判定、待检电芯自比剔除、对照不足 3 份不可判、缺测序号 `missing`、
补点后过期 409、重建刷新与失败保留、判定与补传/重建交错的一致性。
实时冒烟额外覆盖真实 uvicorn 下的并发建组/补传/判定。

## 目录结构

```
app/
  algorithm.py    # 分词 + 状态机循环识别、Ah 积分
  control.py      # 对照组判定：中位数、MAD、阈值、逐序号结论
  csv_import.py   # CSV 表头解析与校验
  db.py           # SQLite 事务、幂等/冲突、版本化报告、对照组快照
  main.py         # FastAPI 路由
tests/            # 单元、集成、实时冒烟测试
examples/         # 示例 CSV
Dockerfile, docker-compose.yml
```

## 设计说明与限制

- 电流以**微安整数**存储（`current_ma` 列，单位 µA），使幂等比较为精确整数
  比较，避免浮点文本往返误差；低于 1 µA 的差异会被取整。
- 报告只增不删：每次导入（含纯幂等重投）都追加一个 `report_versions` 行，
  便于追溯；查询接口只返回最新版本。
- 对照组按 revision 追加保存成员固定版本，重建不覆盖历史 revision；
  `control_groups.revision` 指向当前生效的一版。
- 判定只在同一读事务内完成，读到的快照过期即整体拒绝（409），
  不会"部分对照用旧版本、部分用新版本"。
- 单文件 SQLite + WAL 适合本任务量级；写入在进程级连接上串行提交。
- 对照电芯之间按 `cycle_no` 对齐，不按时间戳对齐：若电芯循环编号错位
  （例如某电芯首循环不完整），需要人工核对；接口有意不做时间对齐推断。
