# KOL Outreach Agent — 阿里企业邮箱达人邀约工作台

> **电商达人主动开发智能体**：批量发送产品合作邀请邮件，自动识别达人回信意图，有合作意向则生成**工单**由人工跟进，明确拒绝则自动发送感谢邮件，不回复不处理。

---

## 核心流程

```
发送产品邀请邮件
       ↓
   达人回信？
  ┌────┴────┐
 是         否 → 忽略
  ↓
LLM 意图识别
  ├─ interested（确认合作）    → 生成工单，人工跟进
  ├─ need_followup（有意向·询问细节）→ 生成工单，人工跟进
  ├─ not_interested（明确拒绝） → 自动发送感谢+歉意邮件
  └─ manual_review（语义不明）  → 仅记录日志
```

**工单由人工对接**，系统不再做自动返款、自动追评等后续操作。

---

## 功能概览

| 功能 | 说明 |
|---|---|
| **达人库管理** | CSV 批量导入或手工录入，记录平台、标签、语言等画像信息 |
| **产品库管理** | 维护产品名称、ASIN、佣金比例、卖点等，支持自动推荐匹配 |
| **批量外呼** | 按批次为每位达人生成个性化邀请邮件草稿，支持逐条编辑后发送 |
| **回信意图识别** | 基于 LangGraph + LLM，自动分类 interested / need_followup / not_interested / manual_review |
| **工单管理** | 有意向的达人自动创建工单，支持待跟进→跟进中→已完成状态流转 |
| **感谢邮件自动发送** | 检测到明确拒绝时，自动生成并发送礼貌的感谢+歉意邮件 |
| **多轮对话记忆** | 按邮件线程（Thread）隔离存储完整收发历史 |
| **Web 仪表盘** | 全流程可视化：达人库、产品库、外呼批次、合作工单、回信识别、运行日志 |

---

## 快速启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入邮箱密码、LLM API Key 等

# 3. 启动服务（自动初始化数据库）
python -m app.main
# 或：
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 4. 打开仪表盘
# http://localhost:8000/dashboard
```

---

## 项目结构

```
.
├── app/
│   ├── agent.py              # 回信处理核心：收信→意图识别→生成工单/发感谢邮件
│   ├── config.py             # 环境变量统一读取
│   ├── database.py           # SQLite 持久化层
│   ├── llm_service.py        # LLM 调用：意图识别 + 邮件生成 + 产品推荐
│   ├── mail_service.py       # IMAP 收件 + SMTP 发件
│   ├── main.py               # FastAPI 入口 + 仪表盘路由
│   ├── graphs/
│   │   ├── inbound_graph.py  # LangGraph：回信意图分类 → 建议回复
│   │   └── outbound_graph.py # LangGraph：产品推荐 → 外呼邮件生成
│   ├── services/
│   │   ├── campaign_service.py  # 外呼批次逻辑
│   │   ├── creator_service.py   # 达人库管理
│   │   ├── lead_service.py      # 工单查询与状态更新
│   │   └── product_service.py   # 产品库管理
│   └── web/
│       └── dashboard.html    # 单页 Web 仪表盘
├── data/
│   └── products.json         # 本地产品库（初始种子数据）
├── .env                      # 实际配置（不提交 Git）
├── .env.example              # 配置模板
├── kol_agent.db              # SQLite 数据库（自动创建）
└── requirements.txt
```

---

## 数据库表结构

系统使用 SQLite，共 7 张表：

| 表名 | 职责 |
|---|---|
| `creators` | 达人主档：邮箱、平台、画像、合作状态 |
| `products` | 产品库：名称、ASIN、佣金比例、卖点关键词 |
| `campaigns` | 外呼批次：名称、绑定产品、创建时间 |
| `outreach_messages` | 外呼邮件草稿与发送记录 |
| `tickets` | 合作工单：有意向达人的跟进任务，含状态（new/in_progress/done） |
| `intent_results` | 每封回信的意图识别结果明细 |
| `kol_threads` | 邮件线程状态：阶段、意图标签、最后处理消息 |
| `thread_messages` | 多轮对话历史：每封来信和我方回复 |
| `processed_messages` | 已处理邮件去重记录 |

### `tickets` — 合作工单（核心）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增主键 |
| creator_id | INTEGER | 关联达人 |
| campaign_id | INTEGER | 关联外呼批次 |
| product_id | TEXT | 关联产品 |
| thread_id | TEXT | 邮件线程 Key |
| status | TEXT | `new`（待跟进）/ `in_progress`（跟进中）/ `done`（已完成） |
| intent | TEXT | `interested` 或 `need_followup` |
| intent_summary | TEXT | LLM 对达人回信的一句话摘要 |
| latest_message | TEXT | 达人最新回信节选（前 500 字符） |
| commission_rate | REAL | 对应产品佣金比例 |
| notes | TEXT | 备注 |
| created_at / updated_at | TEXT | 时间戳 |

---

## 工单状态流转

```
new（待跟进）
    ↓  点击「开始跟进」
in_progress（跟进中）
    ↓  点击「标记完成」
done（已完成）
    ↓  可点击「重置」回到 new
```

仪表盘顶部"待处理工单"卡片只统计 `status=new` 的数量，便于快速感知待办量。

---

## 仪表盘说明

访问 `http://localhost:8000/dashboard`

| 标签页 | 内容 |
|---|---|
| **达人库** | CSV 导入或手工录入达人，查看/编辑达人列表 |
| **产品库** | 维护产品信息，佣金比例等 |
| **外呼批次** | 创建批次草稿，逐条编辑邮件主题/正文，单条或整批发送 |
| **合作工单** | 查看有意向达人的工单，按状态筛选，一键推进状态 |
| **回信识别** | 最近 50 条意图识别结果 + 线程概览 |
| **运行日志** | 最近 80 条运行日志，每 6 秒自动刷新 |

---

## API 端点

| 端点 | 说明 |
|---|---|
| `GET /dashboard` | Web 仪表盘 |
| `POST /check` | 立即触发一轮回信检查 |
| `POST /start-auto` | 启动后台定时轮询 |
| `POST /stop-auto` | 停止后台轮询 |
| `GET /status` | 服务状态与汇总数据 |
| `GET /creators` | 达人列表 |
| `POST /creators` | 新增达人 |
| `PUT /creators/{id}` | 更新达人信息 |
| `POST /creators/import-csv` | 批量导入达人 CSV |
| `GET /products` | 产品列表 |
| `POST /products` | 新增/更新产品 |
| `DELETE /products/{id}` | 删除产品 |
| `GET /campaigns` | 批次列表 |
| `POST /campaigns/draft` | 生成外呼批次草稿 |
| `GET /campaigns/{id}/messages` | 批次下的外呼消息 |
| `PUT /outreach/{id}` | 编辑外呼草稿 |
| `POST /outreach/{id}/send` | 发送单条外呼邮件 |
| `POST /campaigns/{id}/send` | 整批发送 |
| `GET /leads` | 工单列表 |
| `PATCH /leads/{id}/status` | 更新工单状态（new/in_progress/done） |
| `GET /leads/export` | 导出工单 CSV |
| `GET /intents` | 意图识别结果列表 |
| `GET /kols` | 所有线程概览 |
| `GET /thread/{thread_id}` | 线程完整对话历史 |
| `DELETE /thread/{thread_id}` | 删除线程数据 |
| `GET /logs` | 最近运行日志 |

---

## 配置项速查

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EMAIL_ADDRESS` | — | 阿里企业邮箱地址 |
| `EMAIL_PASSWORD` | — | 邮箱密码 |
| `LLM_API_KEY` | — | LLM 服务 API Key |
| `LLM_BASE_URL` | 通义千问 | LLM 接入地址（OpenAI Chat Completions 格式） |
| `LLM_MODEL` | qwen-max | 模型名称（当前指向 qwen3-max-2026-01-23，258K 上下文） |
| `BRAND_NAME` | Our Brand | 品牌名（注入 LLM prompt） |
| `BRAND_SIGNATURE` | The Partnership Team | 邮件署名 |
| `DB_FILE` | kol_agent.db | SQLite 数据库文件路径 |
| `PRODUCTS_PATH` | data/products.json | 产品库 JSON 路径 |
| `MAX_THREAD_MESSAGES` | 10 | 每线程送 LLM 的最大历史条数 |
| `BODY_EXCERPT_LENGTH` | 600 | 邮件正文入库截断字符数 |
| `POLL_INTERVAL` | 120 | 轮询间隔（秒） |
| `MAX_EMAILS_PER_CYCLE` | 20 | 每轮最多处理邮件数 |

---

## 注意事项

- **不做自动返款/追评**：系统职责止于生成工单，后续对接完全由人工完成。
- **多轮历史与线程绑定**：即使同一达人发起多个独立线程，历史绝不跨线程混用。
- **我方回复可信来源**：仅在 SMTP 发送成功后才写入 `thread_messages(role=our)`。
- **产品注入可选**：产品库文件不存在时系统自动降级，不影响正常运行。
- **重复处理保护**：`processed_messages` 表保证同一封邮件不会被处理两次。
