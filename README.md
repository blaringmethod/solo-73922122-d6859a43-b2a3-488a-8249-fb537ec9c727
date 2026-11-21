# 预测区间回测台

用 **FastAPI + SQLite + 原生前端** 实现的预测区间（prediction interval）回测工作台。
数据工程师上传预测 CSV 与实际值 CSV，指定 UTC 评估截止时间，系统选取截止前最新一版
预测，按实际 lead time（issue → target）分组统计覆盖率与平均区间宽度，并列出区间外样本。
整批校验通过后才写入数据库，可随时下载结果。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

打开 <http://localhost:8000>。数据库保存在命名卷 `backtest-data` 中。

端口与上传大小可覆盖：

```bash
BACKTEST_PORT=8080 BACKTEST_MAX_UPLOAD_BYTES=52428800 docker compose up --build
```

本机直接运行（不使用 Docker）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

跑测试：

```bash
pip install pytest httpx
python -m pytest -q
```

## 输入格式

预测 CSV（表头必须齐全，顺序不限）：

| 列 | 说明 |
| --- | --- |
| `series_id` | 序列标识 |
| `issue_time` | 预测发布时间，ISO-8601 |
| `target_time` | 被预测的目标时刻，ISO-8601 |
| `lower` / `upper` | 区间下界 / 上界 |

实际值 CSV：

| 列 | 说明 |
| --- | --- |
| `series_id` | 序列标识 |
| `target_time` | 目标时刻 |
| `actual` | 实际值 |

时间字段统一按 **UTC** 处理：

* 带 `Z`、`+08:00` 等偏移的值会被换算到 UTC；
* 不带时区的值**按 UTC 解释**（不会套用服务器本地时区）；
* 界面上 `datetime-local` 输入框填写的墙上时间同样视为 UTC。

`samples/` 下有一组可直接上传的示例（`predictions.csv`、`actuals.csv`，
以及用于验证拒绝路径的 `predictions-invalid.csv`），截止时间填 `2026-11-20 12:00`。
页面上的「填充示例数据」按钮也能一键载入。

## 计算规则

1. **选取预测**：对每个 `(series_id, target_time)`，在所有 `issue_time <= cutoff`
   的预测中取 `issue_time` 最新的一条。`issue_time == cutoff` 视为可用；晚于截止的预测
   被忽略，若某目标时刻只剩截止后的预测，则该批立即被拒。
2. **分组**：horizon 由 `target_time - issue_time` 的**实际时长**决定，而不是文件名或额外列。
   时长相同的样本归为同一组，标签如 `30m`、`1h`、`1h 30m`、`1d 2h`。
3. **统计**：每组输出样本数、覆盖数、覆盖率（`covered / samples`）、平均区间宽度
   （`upper - lower` 的均值）。覆盖判定为 `lower <= actual <= upper`。
4. **区间外样本**：单独列出，并给出越界幅度。
5. **整批校验**：任一行解析失败、重复预测（同 series/issue/target）、冲突重复
   （同键不同上下界）、上下界反转（`lower > upper`）、时间反转
   （`issue_time > target_time`）、重复实际值、或任一目标时刻缺少可用预测 / 实际值，
   都会 **拒绝整批**，不保存任何记录，并在页面列出全部错误。

## 输出下载

校验通过并保存后，页面与 API 提供：

* `GET /api/backtests/{id}/download/samples.csv` — 每个被评估样本一行
  （含 horizon、lead_seconds、区间、实际值、是否覆盖）
* `GET /api/backtests/{id}/download/summary.csv` — 各 horizon 的覆盖率与平均宽度
* `GET /api/backtests/{id}/download/input/prediction.csv` — 原始预测文件
* `GET /api/backtests/{id}/download/input/actual.csv` — 原始实际值文件

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 工作台页面 |
| `GET` | `/healthz` | 健康检查（容器 HEALTHCHECK 使用） |
| `POST` | `/api/backtests` | multipart：`prediction_csv`、`actual_csv`、`cutoff`（UTC）；成功 `201`，校验失败 `422` 且 `errors` 列出原因 |
| `GET` | `/api/backtests?limit=20` | 最近的回测（不含逐样本明细） |
| `GET` | `/api/backtests/{id}` | 单次回测的完整明细 |

请求示例：

```bash
curl -X POST http://localhost:8000/api/backtests \
  -F prediction_csv=@samples/predictions.csv \
  -F actual_csv=@samples/actuals.csv \
  -F cutoff=2026-11-20T12:00:00Z
```

## 结构

```
app/
  main.py       FastAPI 路由、上传、下载
  backtest.py   纯计算：解析、校验、选版、分组、统计
  storage.py    SQLite 持久化与 CSV 渲染
  static/       index.html / style.css / app.js（无框架）
tests/          20 个用例覆盖校验规则、分组统计、API 往返
```
