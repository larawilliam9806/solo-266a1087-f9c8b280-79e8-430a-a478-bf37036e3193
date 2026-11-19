# 铁路票务日结发布台（Flask + SQLite + 本地文件）

每天上传一份**完整**铁路票务流水 CSV（≤ 1 MB），为两个下游系统分别按契约
（必需列的顺序 + integer/text 类型）预览转换结果，确认后把**原件 + 结算 CSV +
客流 CSV** 以同一日修订号一次发布。三个下载始终来自同一个不可变修订。

## 快速开始

### 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data PORT=8000 python3 run.py
# 打开 http://127.0.0.1:8000
```

### Docker Compose

```bash
docker compose up --build
# 打开 http://127.0.0.1:8000 ，数据保存在 rail-data 卷
```

## 功能与规则

| 需求 | 实现 |
| --- | --- |
| 两个下游各配列序与类型 | `/contracts` 页面或 `PUT /api/contracts/<name>`，按行序即下游列序，类型只允许 `integer`/`text` |
| 上传某日完整 CSV，≤ 1 MB | `POST /api/previews`，超过 1 MB 返回 413，空文件整批拒绝 |
| 预览各自转换结果和不兼容行列 | 返回两个下游的转换预览（前 50 行）、缺列、逐行逐列的整数错误 |
| 缺列/类型错误/重复表头整批拒绝 | 缺列与类型错误使该批 `convertible=false`，确认时 422；重复表头、字段数不一致、非 UTF-8、空行直接 422 整批拒绝 |
| 原版继续可取 | 拒绝只发生在预览阶段，不写发布记录；旧修订文件不变 |
| 确认后发布原件及两份下游文件 | `POST /api/previews/<id>/confirm`，同日修订号 +1 |
| 三者按同一日修订号下载 | `/download/<day>/<rev>/{original,settlement,ridership}`，同一 `rev` 的三个 blob 由一条发布记录一起提交 |
| 预览后契约或该日版本变化拒绝旧确认 | 确认时在写锁内校验契约版本+指纹与该日 `current_rev`，任一变化返回 409 |
| 修改契约不改已发布文件 | 发布行冻结列契约与内容寻址文件；编辑契约只影响后续预览 |
| 同日并发确认只成功一份 | `BEGIN IMMEDIATE` 串行化确认；先到者发布 r(N+1)，后到者因基线版本不符得 409 |
| 写盘/登记失败/重启不混用新旧版 | blob 先落盘并 fsync，再单事务插入发布行+更新日指针；下载只按发布行解析，损坏/缺失则 503，绝不回退 |
| 已开始的下载必须完整结束 | 下载先解析出版本并打开不可变 blob 再开始流式响应，`Content-Length` 与 `ETag` 固定为该 blob 哈希 |

### 整批拒绝与不可发布

* **整批拒绝（HTTP 422 `batch_rejected`）**：空文件、超限、非 UTF-8、空行、
  重复表头、空列名、字段数与表头不一致、CSV 语法错误。不会产生预览。
* **不兼容（预览 201，`convertible=false`；确认 422 `incompatible`）**：
  源 CSV 缺少某下游必需列，或整数列出现非整数。预览中会具体列出缺失列、
  出问题的数据行/文件行号/列名/原值；整批不可发布，修正后重新上传。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/contracts` | 两个下游的契约、版本、指纹 |
| PUT | `/api/contracts/<settlement\|ridership>` | body `{"columns":[{"name":"x","type":"integer"}]}` |
| POST | `/api/previews` | multipart：`day=YYYY-MM-DD`、`file=@x.csv` |
| GET | `/api/previews/<id>` | 重新获取预览快照 |
| DELETE | `/api/previews/<id>` | 放弃预览 |
| POST | `/api/previews/<id>/confirm` | 确认发布；201 返回发布版本 |
| GET | `/api/days`、`/api/days/<day>` | 发布记录（含每修订的列契约与哈希） |
| GET | `/download/<day>/<rev\|latest>/<kind>` | `kind` 为 `original`/`settlement`/`ridership` |
| GET | `/api/health` | 健康检查 |

下载响应头：`ETag` 与 `X-Content-SHA256` 为该文件 SHA-256，`X-Revision`
形如 `2026-11-01-r3`。

## 一致性设计

```
data/
  app.db              SQLite（WAL，synchronous=FULL）
  blobs/objects/<aa>/<sha256>   只写一次的不可变文件
  blobs/tmp/                    写入后 fsync 再原子 rename
```

1. **预览**：解析与转换全部在内存；原件与两份输出按 SHA-256 落到 blob 存储；
   预览行保存契约版本/指纹、基线修订号与结果哈希，2 小时过期。
2. **确认**（单个 `BEGIN IMMEDIATE` 事务）：
   * 重新校验未过期、未使用、基线 `current_rev` 仍相同、两个契约版本与指纹未变；
   * 用已存储的原件按**冻结的契约快照**重新转换，SHA-256 必须与预览一致；
   * 需要时补写 blob（写盘失败 → 503，事务回滚，旧修订不变）；
   * 一条 `publications` 行同时写入三个 blob 哈希 + 冻结契约，
     `days.current_rev` 同事务前移，`COMMIT` 后新修订才对下载可见。
3. **下载**：只按 `(day, rev)` 解析发布行，先确认三个 blob 都在（否则 503，
   不静默回退到其它修订），再打开目标 blob 并以固定 `Content-Length` 流式返回。
   已经在传输的下载持有已解析的不可变文件描述符，新发布不会影响其完整性。
4. **重启**：启动时清理 `tmp/`、自检所有发布记录的 blob 是否存在并在日志和首页
   报告，不删除、不改写任何已发布修订。

## 测试

```bash
pip install -r requirements.txt pytest
PYTHONPATH=. python3 -m pytest tests -q
```

测试覆盖：列增删/调序、缺列、类型错误、重复表头、字段数不一致、编码错误、
1 MB 上限、三文件同修订下载、契约变更使旧预览失效且不改历史文件、同日并发确认
只成功一份（线程竞争）、预览重复确认、写盘失败/事务中断后旧修订仍完整、
缺失 blob 时 503 不回退、下载字节与 `Content-Length`/`ETag` 一致。

## 说明与边界

* 单进程 gunicorn（1 worker、8 线程）+ SQLite 是默认部署；如需多副本，
  SQLite 数据库与 blob 目录必须位于同一共享卷，且只能有一个写者。
* 内容寻址会在不同修订间复用相同哈希的 blob（例如未变化的原件），这是有意
  设计：文件不可变，复用不会导致混用。
* 目前不提供已发布版本的删除或改签；如需清理历史 blob，应离线按
  `publications` 表中的哈希做引用计数。
* 表头按 UTF-8（容忍 BOM）解析，列名区分大小写、保留内部空格但拒绝首尾空白。
