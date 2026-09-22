<div align="center">
  <img src="assets/ai-checklist-hub-icon.png" width="112" alt="AI Checklist Hub 图标">

  # AI Checklist Hub

  **把邮件、自然语言和文件中的行动事项，整理成真正可执行的任务清单。**

  本地优先 · Gmail 只读同步 · AI 辅助拆解 · Windows 桌面应用
</div>

AI Checklist Hub 是一个面向 Windows 的本地任务管理应用。它既可以像普通待办工具一样手动管理计划、清单和任务，也能从文本、Markdown、JSON、Webhook 以及 Gmail 中识别行动事项，经过预览或规则判断后写入清单。

应用默认把数据保存在本机。Gmail 同步采用增量游标与邮件唯一 ID 账本：已扫描的邮件不会被重复下载、分类或创建任务，从而减少请求次数和重复消耗。

## 目录

- [主要功能](#主要功能)
- [工作方式](#工作方式)
- [快速开始](#快速开始)
- [Gmail 任务同步](#gmail-任务同步)
- [LLM Provider 配置](#llm-provider-配置)
- [导入与 Webhook](#导入与-webhook)
- [配置参考](#配置参考)
- [数据隐私与备份](#数据隐私与备份)
- [项目结构与技术栈](#项目结构与技术栈)
- [API 概览](#api-概览)
- [开发测试与构建](#开发测试与构建)
- [常见问题](#常见问题)

## 主要功能

### 任务与计划

- 使用 `Plan → Checklist → Item` 组织计划、清单和任务。
- 创建、编辑、删除、完成和恢复任务，并在清单间移动任务。
- 支持优先级、日期、备注、标签、一级子任务和全文搜索。
- 提供 Today、Upcoming、All、Inbox、历史和统计等视图。
- 支持 Light、Dark、System 三种主题，并适配“减少动态效果”系统设置。

### 智能导入

- 从粘贴文本、TXT、Markdown、JSON、HTTP API 或 Webhook 导入任务。
- 本地规则解析器在没有 API Key 时也可工作。
- 可选 OpenAI-compatible Provider，将长文本拆解为结构化任务。
- 提供 Compact、Balanced、Detailed 三档任务粒度。
- 导入前可预览、改名、增删、合并任务，并决定是否跳过重复项。
- 使用标准化任务指纹阻止重复导入。

### Gmail 自动任务同步

- 通过 OAuth 2.0 连接 Gmail，仅申请 `gmail.readonly` 权限。
- 自动排除常见广告、促销、验证码和低价值通知。
- 高置信度任务邮件可自动进入清单；不确定邮件进入候选区等待确认。
- 首次进行有限时间窗扫描，之后使用 Gmail `historyId` 只检查新增邮件。
- 以 Gmail 账户和邮件 ID 建立本地唯一账本，避免同一邮件被多次处理。
- 扫描周期按小时设置，支持 1～168 小时。

### 本地优先与安全

- 使用本地 SQLite 持久化任务数据。
- Gmail OAuth 凭据、令牌和应用内录入的 API Key 使用 Windows DPAPI 加密。
- Gmail 正文默认只在本机进行规则分类，不发送给 LLM Provider。
- 服务默认只监听 `127.0.0.1`，不会直接暴露到局域网。

## 工作方式

```text
手动输入 / 文件 / Webhook / Gmail
                │
                ▼
       本地过滤与内容标准化
                │
       ┌────────┴────────┐
       ▼                 ▼
   规则解析器        可选 LLM Agent
       └────────┬────────┘
                ▼
       Schema 校验与重复检测
                │
       ┌────────┴────────┐
       ▼                 ▼
   预览后确认       受信任自动入库
                │
                ▼
       Plan → Checklist → Item
```

所有导入来源最终都转换为同一套领域模型。购物、旅行、会议行动项、软件发布和学习计划不需要各自维护一套任务表。

## 快速开始

### 环境要求

- Windows 10 或 Windows 11
- Python 3.12 或更高版本
- Node.js 20 或更高版本
- Microsoft Edge WebView2 Runtime（Windows 10/11 通常已经安装）

### 1. 首次安装

在项目根目录双击或运行：

```bat
setup.bat
```

该脚本会自动：

1. 创建项目专用 Python 虚拟环境 `.venv`；
2. 安装后端与桌面端依赖；
3. 执行数据库迁移；
4. 安装前端依赖并生成 production build；
5. 构建 Windows 桌面应用。

### 2. 启动桌面版

```bat
start-app.bat
```

桌面版使用独立 WebView2 窗口，内部服务会选择随机可用的本机端口，并在窗口关闭后停止。

### 3. 使用备用 Web 模式

```bat
start.bat
```

随后访问：

- 应用：<http://127.0.0.1:8000>
- OpenAPI 文档：<http://127.0.0.1:8000/api/docs>
- 健康检查：<http://127.0.0.1:8000/api/health>

## Gmail 任务同步

### 扫描策略

Gmail 同步分为首次扫描和增量扫描：

| 阶段 | 扫描范围 | 防重复方式 |
| --- | --- | --- |
| 首次扫描 | Inbox 最近 1～90 天，最多 500 封；默认排除 Spam、Trash 和 Promotions | 每封邮件在下载正文前检查并登记 `(account_email, message_id)` |
| 后续扫描 | 使用 Gmail `historyId` 获取自上次成功同步以来新增到 Inbox 的邮件 | 唯一账本继续兜底，历史记录重叠也不会重复处理 |
| 游标失效恢复 | 回退到有限时间窗扫描 | 仍先检查唯一账本，已登记邮件不会重新下载或分类 |

同步过程带有进程级锁，手动同步和后台定时同步不会并发处理同一批邮件。已经登记的邮件在应用重启后仍保持已处理状态。

> 首次扫描会登记范围内的邮件。此后修改过滤规则或置信度，不会自动重新处理这些历史邮件。这是为了节省资源并保证幂等性。

### 邮件如何进入任务清单

- 广告、促销、验证码、订阅简报等明显非任务邮件会在本机直接忽略。
- 包含明确行动要求，例如“请提交”“请回复”“请确认”“请审核”“请支付”或截止日期的邮件，会被判断为任务候选。
- 当“高置信度任务自动入库”开启且置信度达到阈值时，任务直接写入清单。
- 语义不明确的邮件进入候选区，由用户选择“接受”或“忽略”。
- 创建的任务保留 Gmail message ID、thread ID 和原邮件链接，便于回溯。

### 配置 Google Cloud OAuth

1. 打开 [Google Cloud Console](https://console.cloud.google.com/) 并创建或选择项目。
2. 在 **API 和服务 → 库** 中启用 **Gmail API**。
3. 配置 **Google Auth Platform / OAuth consent screen**：
   - 个人 Gmail 账户通常选择 **External**；
   - 应用处于 Testing 状态时，把实际登录的 Gmail 地址加入 **Test users**；
   - 添加 Gmail 只读权限 `https://www.googleapis.com/auth/gmail.readonly`。
4. 在 **Clients / 凭据** 中创建 OAuth 客户端，应用类型必须选择 **Desktop app**。
5. 复制 Client ID 和 Client Secret。
6. 打开 AI Checklist Hub 的 **设置 → Gmail 任务同步**，保存 Client ID 和 Client Secret。
7. 点击 **连接 Gmail（只读）**，在浏览器中使用已加入测试用户的账号授权。
8. 返回应用，点击 **立即同步**。

桌面客户端使用本机动态回调端口，例如：

```text
http://127.0.0.1:<随机端口>/api/integrations/gmail/oauth/callback
```

使用 **Desktop app** 类型时，无需把每一个随机端口手动加入重定向 URI。不要创建 Web application 类型来代替桌面客户端。

### 推荐同步设置

| 设置 | 默认值 | 范围 | 建议 |
| --- | ---: | ---: | --- |
| 扫描间隔 | 1 小时 | 1～168 小时 | 普通个人邮箱可设为 1～3 小时 |
| 首次回看天数 | 14 天 | 1～90 天 | 邮件较多时保持 7～14 天 |
| 自动导入置信度 | 0.90 | 0.70～1.00 | 希望更谨慎时提高到 0.95 |
| 高置信度任务自动入库 | 开启 | 开 / 关 | 首次使用可关闭，先观察候选结果 |
| 首次扫描排除促销分类 | 开启 | 开 / 关 | 建议保持开启 |

### 解决 `错误 403: access_denied`

如果 Google 授权页显示 `access_denied`，依次检查：

1. OAuth Audience 是否为 External；
2. 应用处于 Testing 时，当前 Gmail 地址是否已经加入 Test users；
3. OAuth 客户端类型是否为 Desktop app；
4. Gmail API 是否已在当前 Cloud 项目中启用；
5. 应用中填写的 Client ID 和 Client Secret 是否来自同一个项目；
6. 保存设置后是否重新点击了“连接 Gmail”。

Google OAuth 应用处于 External + Testing 状态时，测试用户授权可能需要定期重新进行。若应用只供个人使用，可以继续使用测试模式；正式分发时应按 Google 的要求配置发布状态和验证。

### 断开 Gmail

在设置页点击断开连接会移除本机保存的 Gmail 令牌与连接状态。已经创建的任务和已登记邮件账本不会被删除，以免重新连接后重复创建任务。

## LLM Provider 配置

LLM 不是应用运行的必需条件。没有 API Key 时，任务 CRUD、搜索、统计、本地规则解析、Gmail 过滤、导入预览和重复检测仍然可用。

### 在应用中配置

1. 打开 **设置 → LLM Provider**。
2. 填写服务名称、Base URL、Model 和 API Key。
3. 勾选启用并保存。
4. 点击 **测试连接** 验证配置。

应用支持与 OpenAI Chat Completions 风格接口兼容的 Provider。API Key 使用 Windows DPAPI 加密保存，不写入 SQLite，设置接口也不会返回密钥原文。

### 通过 `.env` 配置

复制 `.env.example` 为 `.env`，按需填写：

```dotenv
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4.1-mini
WEBHOOK_TOKEN=
```

使用自定义 Provider 时，把 `OPENAI_BASE_URL` 和 `OPENAI_MODEL` 改为服务商提供的值。不要把包含密钥的 `.env` 提交到版本控制。

### 智能解析模式

| 模式 | 行为 |
| --- | --- |
| 强制 AI | 每次都调用当前启用的 Provider；配置或请求失败时明确报错 |
| 智能模式 | 规则结果清晰时直接使用；内容模糊或涉及角色判断时再调用 LLM |
| 仅本地规则 | 不需要外部模型，适合结构清晰的列表、Markdown 和简单行动项 |

Agent v2 会识别目标、执行阶段、责任主体、步骤、子任务、依赖和条件动作。模型结果必须通过 Pydantic Schema 校验，随后再由本地逻辑进行去重、粒度规范化和顶层任务数量控制。

## 导入与 Webhook

### 支持的来源

- 应用内粘贴文本
- `.txt`、`.md`、`.json` 文件
- REST API
- 通用 Webhook
- Gmail 同步

### 导入流程

1. 来源内容保存为 RawContent；
2. 规则解析器或可选 LLM 转换为标准任务 Schema；
3. 本地计算重复指纹；
4. 用户在预览页修改计划、清单和任务；
5. 确认后事务式写入数据库。

重复指纹综合标准化标题、清单、计划、来源和日期计算。预览中可以跳过重复项，也可以明确选择全部导入。

### Webhook 示例

```powershell
$body = @{
  source_name = "会议纪要"
  content = "发布准备：`n确认发布窗口`n通知相关团队`n检查回滚方案"
  use_llm = $false
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/webhooks/import `
  -ContentType application/json `
  -Body $body
```

如果 `.env` 中设置了 `WEBHOOK_TOKEN`，请求还必须包含：

```text
X-Webhook-Token: <你的令牌>
```

## 配置参考

| 配置 | 位置 | 用途 |
| --- | --- | --- |
| Gmail Client ID / Secret | 设置 → Gmail 任务同步 | 建立 OAuth 只读连接 |
| Gmail 扫描间隔 | 设置 → Gmail 任务同步 | 每 1～168 小时检查一次新邮件 |
| 首次回看天数 | 设置 → Gmail 任务同步 | 控制首次连接时扫描的历史范围 |
| 自动导入阈值 | 设置 → Gmail 任务同步 | 控制任务邮件自动入库的谨慎程度 |
| LLM Base URL / Model / Key | 设置 → LLM Provider 或 `.env` | 启用 AI 任务拆解 |
| `WEBHOOK_TOKEN` | `.env` | 保护 Webhook 导入入口 |
| UI 主题 | 设置 | Light / Dark / System |

## 数据隐私与备份

### 数据位置

桌面版数据：

```text
%LOCALAPPDATA%\AI Checklist Hub\checklist.db
%LOCALAPPDATA%\AI Checklist Hub\secrets.json
```

- `checklist.db`：计划、清单、任务、Gmail 处理账本及应用配置。
- `secrets.json`：使用当前 Windows 用户 DPAPI 加密的 API Key、OAuth 客户端信息和令牌。

备用 Web 模式默认数据库：

```text
backend\data\checklist.db
```

首次启动桌面版时，如果发现旧的 `backend\data\checklist.db`，应用会通过 SQLite Backup API 迁移一次。此后重新构建或移动程序不会覆盖 `%LOCALAPPDATA%` 中的数据。

### 隐私边界

- Gmail 权限为只读，应用不会发送、删除、归档或标记邮件。
- Gmail 正文默认只在本机分类，不会自动发送给已配置的 LLM。
- Provider Key 和 OAuth 令牌不会以明文写入数据库或通过设置 API 返回。
- 本地服务默认绑定 `127.0.0.1`。

如果需要把 Web 服务暴露到其他设备，请自行增加 TLS、身份认证和严格的 Webhook Token；项目默认配置不面向公网部署。

### 备份

先关闭应用，再运行：

```bat
backup.bat
```

备份文件会写入项目根目录的 `backups\`。脚本优先备份桌面版数据库，如果不存在则备份 Web 模式数据库。

> `secrets.json` 只能由创建它的 Windows 用户解密。移机时建议在新电脑重新填写 API Key 并重新授权 Gmail，而不是依赖复制密钥文件。

## 项目结构与技术栈

```text
AI-Checklist-Hub/
├─ assets/                 # 应用图标
├─ backend/
│  ├─ alembic/             # 数据库迁移
│  ├─ app/
│  │  ├─ api/              # REST API
│  │  ├─ core/             # 配置、数据库、密钥存储
│  │  ├─ models/           # SQLAlchemy 模型
│  │  ├─ schemas/          # Pydantic Schema
│  │  └─ services/         # Parser、Import、Gmail、Source Adapter
│  └─ tests/               # pytest 测试
├─ desktop/                # pywebview 桌面启动器
├─ frontend/               # React + TypeScript + Vite
├─ .env.example
├─ backup.bat
├─ build-desktop.bat
├─ setup.bat
├─ start-app.bat
└─ start.bat
```

| 层 | 技术 |
| --- | --- |
| Frontend | React、TypeScript、Vite、Lucide Icons |
| Backend | FastAPI、SQLAlchemy 2、Pydantic 2、HTTPX |
| Database | SQLite、Alembic |
| Desktop | pywebview、PyInstaller、WebView2 |
| Tests | pytest、FastAPI TestClient |

## API 概览

所有业务端点都以 `/api` 开头。

| 功能 | 端点 |
| --- | --- |
| 健康检查 | `GET /api/health` |
| 任务 | `GET/POST /api/items`、`PUT/DELETE /api/items/{id}`、`POST /api/items/{id}/complete` |
| 清单 | `GET/POST /api/lists`、`PUT/DELETE /api/lists/{id}` |
| 计划 | `GET/POST /api/plans`、`PUT/DELETE /api/plans/{id}` |
| 分类与统计 | `GET /api/categories`、`GET /api/stats` |
| 导入 | `POST /api/import/parse`、`/actors`、`/file`、`/confirm` |
| Webhook | `POST /api/webhooks/import` |
| Provider | `GET/POST /api/settings/providers`、`PUT/DELETE /api/settings/providers/{id}`、`POST /test` |
| Gmail 状态与配置 | `GET /api/integrations/gmail/status`、`POST /client`、`PUT /settings` |
| Gmail OAuth 与同步 | `POST /api/integrations/gmail/oauth/start`、`GET /oauth/callback`、`POST /sync` |
| Gmail 候选 | `GET /api/integrations/gmail/candidates`、`POST /{id}/accept`、`POST /{id}/ignore` |
| 断开 Gmail | `DELETE /api/integrations/gmail` |

精确请求结构、响应 Schema 和在线调试入口请查看 `/api/docs`。

## 开发测试与构建

### 启动后端开发服务器

```bat
.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --reload
```

### 启动前端开发服务器

```bat
cd frontend
npm run dev
```

### 后端测试

```bat
cd backend
..\.venv\Scripts\python.exe -m pytest -q
```

### 前端类型检查与构建

```bat
cd frontend
npm run lint
npm run build
```

### 构建 Windows 桌面应用

先确保前端已构建，再运行：

```bat
build-desktop.bat
```

输出位置：

```text
desktop_dist\AI Checklist Hub\AI Checklist Hub.exe
```

### 数据库迁移

在修改 SQLAlchemy 模型后：

```bat
cd backend
..\.venv\Scripts\alembic.exe revision --autogenerate -m "describe change"
..\.venv\Scripts\alembic.exe upgrade head
```

提交模型修改时应同时提交对应的 Alembic migration。

## 扩展开发

### 新增 Source Adapter

`backend/app/services/sources.py` 中的来源适配器只负责获取原始内容：

```python
class ContentSource(ABC):
    def fetch(self) -> list[SourceContent]: ...
```

新增来源时：

1. 继承 `ContentSource`；
2. 返回一个或多个 `SourceContent`；
3. 不在 Adapter 中直接写数据库或创建领域专用任务模型；
4. 将内容交给统一 Parser；
5. 通过预览确认或明确受信任的自动路径保存。

### 新增 LLM Provider

实现与 `OpenAICompatibleProvider.parse(content) -> ParsedPlan` 相同的契约。模型返回后必须执行 `ParsedPlan.model_validate()`；Provider 只负责文本到标准 Schema 的转换，不直接操作数据库。

### 新增 Parser

Parser 应保持领域无关，并返回 `ParsedPlan`、`ParsedList` 和 `ParsedItem`。新增策略时至少补充一个正常输入、一个模糊输入和一个非目标领域回归测试。

## 常见问题

<details>
<summary><strong>启动时报错：No module named 'html.parser'</strong></summary>

请重新运行 `setup.bat` 或至少重新执行 `build-desktop.bat`。当前版本已经移除桌面打包环境中对 `html.parser` 的运行时依赖；旧的 `desktop_dist` 仍可能包含过期构建。

</details>

<details>
<summary><strong>Google 授权时报 403 access_denied</strong></summary>

最常见原因是 OAuth 应用仍处于 Testing，但当前 Gmail 地址没有加入 Test users。也请确认客户端类型是 Desktop app、Gmail API 已启用，且 Client ID/Secret 来自同一 Cloud 项目。详见上方“解决 403”章节。

</details>

<details>
<summary><strong>没有 OpenAI API Key 能否使用？</strong></summary>

可以。手动任务管理、搜索、统计、本地规则解析、导入预览、Gmail 本地分类和重复检测均不依赖 LLM。

</details>

<details>
<summary><strong>为什么同一封邮件不会再次扫描？</strong></summary>

应用在下载正文前，会把 Gmail 账户和 message ID 写入具有唯一约束的本地账本。重复手动同步、定时同步、应用重启或 Gmail history 结果重叠，都无法为同一邮件创建第二条登记记录。

</details>

<details>
<summary><strong>修改 Gmail 规则后，怎样重新处理历史邮件？</strong></summary>

当前设计把“扫描过即不重复处理”作为资源保护边界，界面不提供批量重扫。需要验证新规则时，建议先使用候选审核或新收到的测试邮件。直接修改或删除 Gmail 账本属于维护操作，执行前必须备份数据库。

</details>

<details>
<summary><strong>端口 8000 被占用怎么办？</strong></summary>

桌面版使用随机端口，通常不受影响。Web 模式需要关闭占用 8000 端口的程序，或同时修改 `start.bat` 中的服务端口与浏览器地址。

</details>

<details>
<summary><strong>为什么页面刷新后数据仍然存在？</strong></summary>

确认保存的数据位于 SQLite 中，不依赖浏览器内存。FastAPI 还会把非 API 前端路由回退到 production `index.html`，因此直接刷新应用路由不会返回 404。

</details>

<details>
<summary><strong>是否可以直接连接 ChatGPT Scheduled Tasks？</strong></summary>

当前没有依赖未公开或不稳定的任务历史接口，也没有使用网页爬虫。可将定时任务输出粘贴到导入页，或让能够访问本机服务的自动化调用 `/api/webhooks/import`。

</details>

---

AI Checklist Hub 目前以本地个人使用为主要场景。准备将它暴露到公网或分发给其他用户前，请补充身份认证、发布流程、OAuth 验证和安全审计。
