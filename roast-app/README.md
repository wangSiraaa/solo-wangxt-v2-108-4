# 烘焙批次曲线对比系统

面向烘焙负责人的**过程记录**工具：并排查看豆温、环境温度与操作事件（回温点、一爆、风门变化、出锅），
而不是用成品评分替代过程。系统**不连接真实烘焙机**，数据来自带噪声、不均采样与探针失联的合成生成器。

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Svelte 4 + Vite + ECharts 5 |
| API | FastAPI（Pydantic 校验） |
| 计算 | NumPy：温升率、插值、阶段指标，全部为纯函数 |
| 存储 | PostgreSQL（原始采样、下豆点、人工标记），SQLAlchemy ORM |
| 测试 | pytest，同一套用例在 SQLite 与 PostgreSQL 上运行 |

## 数据与口径（重要）

### 温升率 RoR —— 窗口必须说明
采样间隔不均（1–5 s 抖动），因此不用相邻点差分。在每个**实测**时刻 t，取居中时间窗
`[t−W/2, t+W/2]`（默认 W=30 s）内的实测豆温点做普通最小二乘直线拟合，取斜率换算 °C/min。
- 至少 4 个实测点、时间跨度 ≥10 s 才给出 RoR，否则为 null（不编造）；
- **插值点不参与拟合**；探针失联的宽缺口处 RoR 直接断档；
- 序列边缘窗口被截断，返回值带 `ror_edge=true` 标记；
- 前端另有一个“显示平滑”参数（居中均值），只作用于展示曲线，窗口本身随接口参数和图表标题一起返回。

### 缺测与插值 —— 插值段不冒充实测
- `samples` 表**只存实测**：探针失联时豆温为 NULL，绝不回写；
- 查询时对 ≤`max_gap_fill_s`（默认 45 s）的内缺口做**相邻实测点线性插值**，
  逐点带 `is_interpolated=true`，图上为**虚线+空心菱形**，图例单列“插值段（非实测）”；
- 超过桥接上限的缺口与端点缺测**不填充**，曲线断档；缺测段在“缺测与插值审计”表逐条列出（通道、时长、处理方式）。

### 事件 —— 人工修正并保留来源
- 事件为只追加（append-only）。人工提交同类型事件时，旧行置 `superseded=true` 并记录
  `superseded_by_id`，不删除；自动建议记 `source=auto`，人工记 `source=manual`+`created_by`；
- 风门变化允许多条并存（离散操作点），金色虚线标出。

### 中断区间账本 —— 墙钟时间 vs 活动烘焙时间
现场会因供电中断、安全检查等真正停止加热；墙上时钟继续走，加热没有。两者必须分清：
- **只追加、带版本的账本**（`interrupt_records` 表）。操作员可记录 `start`（中断开始）、
  `resume`（恢复加热）、`terminate`（确认终止）；每条记录都有 `source`、`reason`、`created_by`、
  `version`、`superseded` 状态。后补修正**不改旧行**：以同一 `interval_id` 写入 version+1，
  旧版本完整保留（界面可显示旧区间与当时指标），当前指标按新版本重算。
- **批次状态机**（服务端强制，非法动作返回 409/422 而不是猜测）：
  `进行中 in_progress → 已中断 interrupted → 已恢复 resumed →（可再次中断）→ 已结束 ended`。
  无未关闭区间时的 `resume` 被拒绝；未关闭就再次 `start` 被拒绝；重复 `resume` 不累计第二次。
- **探针失联 ≠ 停机**：`samples` 里的 NULL 缺测永不自动开/关中账——掉探针不说明炉子停了，
  中断只能由操作员明确记录。
- **两种时长口径同时给出**（所有阶段指标与页面时钟）：

  | 口径 | 定义 | 字段后缀 |
  |---|---|---|
  | 墙钟时长 | `t_end − t_start`，时钟从不暂停 | `_s` |
  | 活动烘焙时长 | 墙钟减去区间内当前版本、已关闭、互不重叠中断区间的**并集** | `_active_s` |

  区间按半开 `[开始, 恢复)` 处理：锚点恰在恢复瞬间算活动，恰在开始瞬间（及区间内部）算中断。
  开放区间（未恢复）只封顶到**最后一个实测点**，绝不向未来外推。
- **不猜测的情况一律返回 `null` + 原因，不输出伪造 DTR**：
  区间相互重叠、重复开始/恢复、无开始的恢复、结束不晚于开始、已终止却有未关闭区间等
  结构冲突 → `computable=false`，全部活动指标为 null（墙钟指标照常）；
  阶段锚点（回温点/一爆/出锅）落在中断期内 → 仅该阶段活动值为 null，
  `active_metrics_status=partial` 并在 `active_blockers` 给出中文原因。
- 曲线上中断区间渲染为**琥珀色横带**（含区间号/版本/原因），冲突区间为**红色虚线带**。

### 发展时间比 —— 明确区间（双口径）
| 指标 | 区间 |
|---|---|
| 脱水期 drying | 下豆 charge → 回温点 turning_point |
| 梅纳期 maillard | 回温点 → 一爆开始 first_crack_start |
| 发展期 development | 一爆开始 → 出锅 drop |
| 一爆持续 | 一爆开始 → 一爆结束 |
| 总时长 total | 下豆 → 出锅 |
| **发展时间比 DTR（墙钟）** | development / total，字段 `development_ratio` |
| **发展时间比 DTR（活动）** | development_active / total_active，字段 `development_ratio_active`，冲突或锚点落区间内时为 `null` |

边界事件缺失时指标为 `null`（不猜测），并返回每个锚点的来源以便审计。
`duration_table` 把每个阶段的墙钟、活动、中断扣除三列并列；页面顶部同时给出整批的
「墙上时钟经过」与「真正持续加热」。

### 双批次对比 —— 不宣称因果
两批次按开火/下豆时刻对齐叠加；风门变化前后的形态变化仅供观察，接口和界面都附带声明：
无对照、无重复、无统计检验，**不构成因果结论**。

## 快速开始

### 方式一：本地

    # 终端 1 —— API（需要先有 PostgreSQL，或用 SQLite 做本地演示）
    cd backend
    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    # 默认连接 postgresql+psycopg2://roast:roast@localhost:5432/roast
    # 仅本地无 PG 时：export DATABASE_URL="sqlite:///./dev.db"
    uvicorn app.main:app --reload --port 8000

    # 终端 2 —— 前端
    cd frontend
    npm install
    npm run dev        # http://localhost:5173 （/api 已代理到 8000）

打开页面后点 **① 生成两个合成批次**：A 批在 300 s 有关一次风门（70%→40%），B 批无风门变化，
两批均含测量噪声、不均采样、一次短失联（5 s，插值桥接）和一次长失联（56 s，断档不桥接）。

### 方式二：docker compose

    docker compose up --build
    # web: http://localhost:5173  api: http://localhost:8000/docs

## 验证（对应需求中的验收项）

    pytest                       # SQLite
    DATABASE_URL=postgresql+psycopg2://roast:roast@localhost:5432/roast pytest

界面“缺测与插值审计 · 导出可复现”面板一键完成：
1. 导出 JSON（原始采样 + 全量事件含已取代行 + **全量中断账本含历史版本** + 参数 + 双口径阶段指标）；
2. 调 `/api/recompute` 从原始数据独立重算，逐指标比对（脱水/梅纳/发展/一爆/总时长/DTR，
   含 `_active_s` 活动口径），并比对中断边界/版本/开放状态与墙钟/活动总时长；
3. 再用翻倍窗口、不同平滑重取曲线，逐点比对原始豆温/环温**完全不变**。

`/api/recompute` 支持 `ledger_records_policy`：默认 `current`（非取代行＝当前指标）、
`all`（含历史行原始校验）、或记录 id 列表（**显式重放某个历史版本**，复现当时的指标）。

## 中断账本验收对照
1. 一次合法中断后恢复：页面同时给出墙钟与活动时长，中断色带落在曲线上，原始曲线逐点不变、可查；
2. 重复恢复不会把活动时长累计两次（首条恢复即关账，第二条 409 拒绝）；无未关闭中断的恢复 409；
3. 后补修正写入新版本，旧区间与旧行保留（`superseded_by_id` 指向新版本），当前指标按新版本重算，
   导出/重算可用历史版本 id 复算旧指标；
4. 两个重叠区间、或覆盖关键事件锚点的区间被明确标为冲突/不可计算：结构冲突不写入或令
   `computable=false`，锚点覆盖令相应活动值与活动 DTR 为 `null`，**绝不输出伪造 DTR**；
5. 刷新（同一状态机重放）、导出、独立重算三者对中断边界、所用口径（`_s` / `_active_s`）和结果一致。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/batches` | 批次列表 |
| POST | `/api/seed` | 生成两个合成批次 |
| GET | `/api/batches/{id}/series?window_s&display_smooth_s&max_gap_fill_s` | 曲线+RoR+指标 |
| GET/POST | `/api/batches/{id}/events[?include_history=true]` | 事件列表/人工修正（只追加） |
| GET/POST | `/api/batches/{id}/interruptions` | 中断账本列表/记录 start·resume·terminate（状态机强制） |
| POST | `/api/batches/{id}/interruptions/correct?interval_id=` | 后补修正：以新版本取代整个区间，旧版保留 |
| GET | `/api/compare?a=&b=` | 双批次叠加（含非因果声明） |
| GET | `/api/batches/{id}/export` | 自包含导出 |
| POST | `/api/recompute` | 从导出载荷独立重算全部派生指标 |

## 目录

    backend/app/  config.py models.py analysis.py synth.py schemas.py main.py
    frontend/src/ App.svelte lib/RoastChart.svelte lib/api.js
    tests/        test_analysis.py test_api.py（双后端同一套用例）
