# KiraAI Token 用量统计（Token Stats）

为 KiraAI 提供完整的 **Token 用量统计看板**：逐轮记录输入/输出/缓存 tokens、按价格规则实时估算费用（峰谷价、双币种）、API 余额监测（探测 + 估算 + 对表校准）、出错统计、**预警提醒**（余额/当日 tokens/当日费用达阈值自动提醒，机械直发或发给 LLM 感知）——WebUI 侧边栏仪表盘 + 悬浮挂件 + bot 工具（概览/聚合/明细）+ 可选自定义命令，四大入口全覆盖。

> 移植自 [Alife 的 TokenStats](https://github.com/1chuxin/1chuxin-Alife.TokenStats)（初心出品，参考其设计），并整合了 [KiraAI-plugin-api-balance](https://github.com/ChuXia2004/KiraAI-plugin-api-balance) 的查询模式与命令设计。
> **模型无关**：任何 Provider 只要在 LLMResponse 里上报 tokens 就能统计。

---

## ✨ 功能一览

| 功能 | 说明 |
|------|------|
| 📊 **逐轮采集** | 每轮 LLM 调用（含工具中间步）的 输入/输出/缓存 tokens，JSONL 落盘，重启不丢；**热读缓存**（mtime+length 判失效），大日志下查询/轮询不重复全量读盘 |
| 💰 **费用估算** | 价格规则按 `URL > 模型 > 渠道名` 加权匹配（4/2/1 分），峰谷价（工作日 9-12 点、14-18 点为峰）；展示时实时计算，**改价后全历史即时重定价**；**双币种**（CNY 元 / 积分）分桶累计永不混算 |
| 💳 **余额监测** | 六类监测源：`auto`（按 URL 自动分流官方端点/One-API 中转站）、`custom`（自定义接口多端点尝试 + json_path）、`newapi`（New-API 站点：New-Api-User 头 + quota 换算）、`preset`（预设扣减钱包型）、`daily`（每日重置积分）、`rolling`（每日累计滚存积分）；估算型支持**「当前余额(对表)」**：填上游后台真实余额即校准，此后按价格规则自动扣减 |
| 🔍 **来源归类** | 自定义关键词规则优先 → 群聊/私聊自动判定 → 工具续轮继承上一轮，可自定义标签名 |
| 🖥️ **WebUI 仪表盘** | 侧边栏「Token 用量」：概览卡片（KPI + 迷你走势线 + 双币种费用）、**时间趋势**（按天柱状 + Top8 模型费用分色堆叠 + 缓存命中率虚线，点柱下钻单天按小时 → 点小时看记录）、按天历史、今日按小时、维度分析、**会话统计**（细到每个会话，私聊显示「昵称(QQ号)」、群聊显示「群名(群号)」，按私聊/群聊归类汇总，点击看该会话逐轮明细）、最近逐轮记录、价格规则、余额监测、**预警提醒**（投递会话管理 + 规则可视化编辑） |
| 🤖 **Bot 工具** | 三个工具：`query_token_stats`（概览）、`query_token_usage`（维度聚合：channel/model/source/day + 过滤 + top）、`query_token_records`（逐轮明细 + minInput 查大上下文），输出带 4000 字符硬上限防回注爆上下文 |
| ⌨️ **自定义命令** | 可选 `/用量` 等命令词直接查询（默认关闭，防打扰），支持用户白名单 |
| 🪟 **悬浮挂件** | 默认开启。侧边栏「Token 挂件」页：迷你卡片实时显示会话/今日 tokens、费用、余额，可拖动（位置记忆）、**可折叠成球**（56px 圆球实时显示总量，球体单击即展开）、紧凑模式；**⧉ 按钮弹出真·独立小窗**（三级渐进增强：同源真实 URL 命名弹窗单例、位置记忆、可 window 级拖动/缩放折叠球）；**📌 一键置顶**（支持 documentPictureInPicture 的浏览器：侧边栏直接点 📌 优先直开 OS 级浮窗，被浏览器策略拦截时自动回退为弹窗 + 小窗内点 📌），随机背景默认开（👕 可关；🖼 自定义背景：单击选 1-10 张本地图、压缩存 localStorage、60s 随机轮换，双击清除恢复线上随机图），适合浏览器小窗钉角落或 OBS 采集 |
| 🔔 **预警提醒** | 监测**余额**（低于阈值，滞回复位——余额回升超过阈值才重置，充值后可再提醒）/ **当日 tokens** / **当日费用**（多币种可选，留空=任一币种达阈值即触发），达阈值自动提醒；同一站点可设**多段阈值**（如余额 10 元/5 元/1 元、tokens 1M/10M、费用 5 元/10 元）各自独立触发互不影响；token/cost 规则可限定**来源标签**（gm/dm/自定义关键词标签）与**会话 sid**（同时填取交集）；**两种投递**：`direct`=机械直发（自定义话语，不经过 LLM 零消耗，消息先发、概况图异步补发可开关）、`llm`=发给 LLM（bot 感知后主动告知，独立异步补发图开关）；**连续提醒控制**：最大连续提醒次数（默认 1=只提醒 1 次）+ 连续提醒间隔（默认 300 秒=5 分钟），只约束同一条规则自身；**生效时段**（HH:mm + 窗口分钟，如 9:00+60min=9:00-10:00）；WebUI「Token 用量 → 预警提醒」tab 可视化配置（增删改/启停/测试投递/复位/提醒效果实时预览） |
| ⚠️ **出错统计** | 扫描 AI 输出中的「出错：」标记（errScanPos 游标，工具循环不重复计数），按范围聚合展示 |

---

## 📦 安装

### 方法一：插件市场（若已上架）

1. 打开 KiraAI WebUI → **插件管理**
2. 搜索 `KiraAI_token_stats_plugin` → 安装
3. 在插件列表里启用

### 方法二：GitHub 安装

1. 打开 KiraAI WebUI → **插件管理** → **安装插件**
2. 选择 **从 GitHub 安装**，填入：
   ```
   https://github.com/znq19/KiraAI_token_stats_plugin
   ```
3. 安装完成后在列表里启用
4. 若提示缺少 `aiohttp`（余额监测需要），在插件管理里点 **安装依赖**（自动读取仓库内 `requirements.txt`），或手动执行：
   ```bash
   pip install aiohttp
   ```

### 方法三：手动安装

1. 下载 ZIP：`https://github.com/znq19/KiraAI_token_stats_plugin/archive/refs/heads/main.zip`
2. 解压后把整个目录 `KiraAI_token_stats_plugin` 复制到 KiraAI 的插件目录：
   ```
   data/plugins/KiraAI_token_stats_plugin/
   ```
3. 重启 KiraAI（或热重载插件）

目录内需包含：`main.py`、`manifest.json`、`schema.json`、`requirements.txt`、`icon.svg`、`icon-dark.svg`。

---

## 🚀 快速上手

### 1. 启用统计（默认已启用）

安装后插件即开始记录。发几条消息后打开 **侧边栏 → Token 用量** 即可看到数据。

### 2. 配置价格规则（可选，否则费用显示「—」）

打开 **侧边栏 → Token 用量 → 价格规则** 页，点「＋ 添加规则」即可可视化编辑（名称/URL/模型/渠道匹配、币种、峰谷开关、六个价格字段），支持编辑/删除，保存后全历史费用即时重算。无需手写 JSON。

预置了 DeepSeek V4-Flash / V4-Pro 官方峰谷价。自定义规则示例：

```json
[
  {
    "name": "我的中转站",
    "url_match": "myproxy.example.com",
    "currency": "CNY",
    "peak_enabled": true,
    "hit_peak": 0.1,
    "hit_off": 0.05,
    "miss_peak": 3.0,
    "miss_off": 1.5,
    "out_peak": 9.0,
    "out_off": 4.5
  },
  {
    "name": "京东积分渠道",
    "url_match": "jd-llm.example.com",
    "currency": "积分",
    "peak_enabled": false,
    "hit_peak": 0.5,
    "hit_off": 0.5,
    "miss_peak": 10.0,
    "miss_off": 10.0,
    "out_peak": 30.0,
    "out_off": 30.0
  }
]
```

字段说明（价格单位：**元 或 积分 / 百万 tokens**）：

| 字段 | 说明 |
|------|------|
| `name` | 规则名称（展示用） |
| `url_match` | 匹配 endpoint 域名（**推荐**，比渠道名稳定，双向包含匹配） |
| `model_match` | 匹配模型名（如 `flash`、`pro`） |
| `channel_match` | 匹配渠道名/主机名 |
| `currency` | 计价币种：`CNY`=元/百万tokens（默认），`积分`=积分/百万tokens（京东等积分制渠道）。费用按币种分开累计，不与 ¥ 混算 |
| `peak_enabled` | 是否启用峰谷价（false = 恒按谷价） |
| `hit_peak` / `hit_off` | 缓存命中部分的单价（峰/谷） |
| `miss_peak` / `miss_off` | 未命中缓存部分的单价（峰/谷） |
| `out_peak` / `out_off` | 输出 tokens 单价（峰/谷） |

> 匹配规则取**加权分最高**的一条：URL 命中 +4 分、模型命中 +2 分、渠道名命中 +1 分（可叠加）。建议用 `url_match` 按域名配置——渠道重排/改名不受影响；同一域名下不同模型（如 flash/pro）用 `url_match + model_match` 组合精确区分。

### 3. 配置余额监测（默认已启用）

打开 **侧边栏 → Token 用量 → 余额监测** 页，点「＋ 添加监测源」按类型填表即可（支持编辑/删除/启用开关），无需手写 JSON。类型怎么选：

| 类型 | 什么时候用 | 要填什么 |
|------|-----------|---------|
| `auto` | 官方平台（DeepSeek/Kimi/硅基/智谱）或 One-API 系中转站，**最省事** | 站点地址 + API Key |
| `custom` | 有自定义余额接口 | 接口地址（可选 json_path 指定余额字段） |
| `newapi` | New-API 站点 | 站点地址 + 系统访问令牌 + 纯数字用户ID（+ 换算比例可选） |
| `preset` | 固定钱包型：额度用一点少一点、不会自动恢复 | 「当前余额(对表)」= 现在后台看到的真实余额，之后每次调用按价格规则自动扣减 |
| `daily` | 每日重置积分：每天固定发 N 积分，当天用完第二天重置 | 「每日额度」+「刷新时刻 HH:mm」 |
| `rolling` | 每日累计滚存积分：每天发 N 积分，用不完的累积到下一天 | 「每日额度」+「刷新时刻」+「当前余额(对表)」= 现在后台看到的真实余额作为基准 |

> 💡 拿不准就选 `auto` 填地址+Key；`preset/daily/rolling` 是给没有余额接口的渠道用的，靠价格规则推算，记得先配好价格规则。

**方式一：官方平台快捷分区**（DeepSeek / Kimi / 硅基流动 / 智谱）——填 API Key 即自动并入，无需写 JSON。

**方式二：New-API 中转站简易文本格式**（对齐 [api-balance 插件](https://github.com/ChuXia2004/KiraAI-plugin-api-balance)）——配置页 → **New API 站点（简易文本格式）**，每行一个，英文分号分隔：

```
我的站点1;https://api.example.com;sk-xxxxxxxx;123456;500000
```

字段依次为：`名称;base_url;系统访问令牌;纯数字用户ID;换算比例(可选，默认500000)`。自动并入余额监测（type=newapi），quota ÷ 换算比例 = 元。

**方式三：高级 JSON**（`余额监测源`，支持全部六类）：

```json
[
  {
    "name": "DeepSeek 官方",
    "type": "auto",
    "url": "https://api.deepseek.com",
    "api_key": "sk-xxxxx",
    "enabled": true
  },
  {
    "name": "我的中转站",
    "type": "custom",
    "url": "https://myproxy.example.com",
    "api_key": "sk-yyyy",
    "json_path": "",
    "enabled": true
  },
  {
    "name": "小鸡 NewAPI",
    "type": "newapi",
    "url": "https://newapi.example.com",
    "api_key": "sk-zzzz",
    "api_user": "123456",
    "quota_conversion": 500000,
    "enabled": true
  },
  {
    "name": "钱包备用金",
    "type": "preset",
    "url": "",
    "anchor_balance": 45.5,
    "anchor_at": "2026-08-31T10:00:00",
    "currency": "CNY",
    "enabled": true
  },
  {
    "name": "京东每日积分",
    "type": "daily",
    "url": "jd-llm.example.com",
    "daily_quota": 1000,
    "refresh_time": "00:00",
    "anchor_balance": 800,
    "anchor_at": "2026-08-31T09:00:00",
    "currency": "积分",
    "enabled": true
  },
  {
    "name": "积分滚存",
    "type": "rolling",
    "url": "rolling-llm.example.com",
    "daily_quota": 100,
    "refresh_time": "00:00",
    "anchor_balance": 350,
    "anchor_at": "2026-08-31T00:00:00",
    "currency": "积分",
    "enabled": false
  }
]
```

| 类型 | 行为 |
|------|------|
| `auto` | 按 URL 自动分流：DeepSeek → `/user/balance`；Moonshot/Kimi → `/v1/users/me/balance`；硅基流动 → `/v1/user/info`；智谱 → `/api/paas/v4/users/me/balance`；其他一律按 One-API/New-API 中转站探测（subscription − usage） |
| `custom` | 依次尝试常见余额接口（`/user/balance`、`/v1/users/me/balance`、`/v1/user/info`、`/api/paas/v4/users/me/balance`、One-API 组合）；接口特殊可填**完整余额接口 URL** + `json_path` 取数（如 `data.available_balance`） |
| `newapi` | **New-API 站点专属**：请求带 `New-Api-User` 头（站点后台纯数字用户ID），打 `/api/user/self`，自动从 quota/balance/remaining/points 等字段提取额度，按 `quota_conversion` 换算（默认 500000，即 quota ÷ 500000 = 额度单位） |
| `preset` | 预设扣减（钱包型）：填 `initial`（初始额度）→ 当前 = 初始额度 − 该渠道累计计费；或填 `anchor_balance`（当前余额对表）→ 当前 = 设定值 − 其后计费（改价即时重估） |
| `daily` | **每日重置积分**：填 `daily_quota`（每日额度）+ `refresh_time`（刷新时刻，默认00:00）→ 当前 = 每日额度 − 上次刷新以来计费；可填 `anchor_balance` 在本周期内校准（跨刷新自动回落每日额度模型） |
| `rolling` | **每日累计滚存积分**：填 `anchor_balance` 建立基准 + `daily_quota` → 当前 = 设定余额 − 累计计费 + 发放次数 × 每日额度（没用完的结转滚存） |

估算型（preset/daily/rolling）核心字段：

| 字段 | 说明 |
|------|------|
| `anchor_balance` | **当前余额(对表)**：在后台看到的真实余额。填写即设定基准（填写时刻 = 对表点），此后 = 设定值 − 其后该渠道计费（按价格规则估算，改价自洽）。上游对不上时改一次数字即完成校准，此前的一切估算误差（价格规则错、漏记、多客户端、上游手动消耗）被吸收 |
| `anchor_at` | 对表时刻（ISO 格式 `2026-08-31T10:00:00`）；填写 `anchor_balance` 时也自动取当前时间，一般无需手填 |
| `daily_quota` | 每日额度（daily/rolling 的每周期发放额度） |
| `refresh_time` | 每日刷新时刻（HH:mm，默认 00:00）。纯时间推导不落状态，客户端离线期间的发放不丢 |
| `currency` | 展示币种；daily/rolling 默认「积分」，其他默认 CNY |

> 💡 余额单位：插件配置页 → 余额监测 → `balance_unit`（默认"元"），可改为"额度"、"美元"、"美刀"、"点数"等任意单位；积分制源自动显示「积分」不受影响。

> ⚠️ **安全提示**：`api_key` 以**明文**存储在插件配置文件（`data/config/plugins/KiraAI_token_stats_plugin.json`）中，请确保服务器文件权限安全。轮询间隔默认 10 秒（最小 5 秒）。

### 4. 让 bot 回答用量

启用 **Bot 工具**（默认开启）后，用户直接问：

> “今天用了多少 token？”
> “花了多少钱？”
> “余额还剩多少？”
> “哪个模型用得多？某渠道花了多少？昨天按小时的用量？”

bot 会自动调用对应工具返回结果。三个工具：

| 工具 | 用途 |
|------|------|
| `query_token_stats` | 概览：本次/今天/7天/30天/累计 + 余额（`range=all` 或不传=全部概览，也可只要某一段）；`render=image` 时把完整概览首页渲染成图片直接发送到会话（查加发一体，用户想要"图片/卡片"时用），`render=auto` 或不传=跟随插件配置 |
| `query_token_usage` | 维度聚合：`dim=channel/model/source/day`，支持 `range/from/to` 时间区间、`model/channel/source` 关键字过滤、`top` 行数上限（默认8，最大20） |
| `query_token_records` | 最近 N 轮逐轮明细（倒序），支持过滤 + `minInput`（只看输入超过某 token 数的轮次，定位大上下文） |

> **渲染图模式**：`query_token_stats` 的 `render=image` 会渲染一张**横屏宽幅**概览图（1200px 居中 + 毛玻璃面板：快照栏 + 五范围卡一行横排 + 错误卡 + 按天历史/今日按小时并排 + 余额源）直接发送。背景优先用 WebUI 🖼 自定义背景图（自动同步服务端），无自定义时用线上随机图（浏览器加载，失败自动纯色降级）。渲染依赖 Playwright（系统 Chrome/Edge → 内置 Chromium 自动下载），失败自动降级纯文本。默认关（LLM 显式要求才发图），可在配置页 **Bot 工具 → 默认渲染图发送** 开启。渲染图存于插件数据目录 `output/`，自动只保留最近 20 张。

### 6. 配置预警提醒（可选，默认启用）

WebUI 侧边栏 → **Token 用量 → 预警提醒** tab：

1. **添加投递会话**：提醒发到哪里（`qq:gm:群号`=群聊、`qq:dm:QQ号`=私聊），可配多个
2. **添加规则**：选监测类型（余额/当日 tokens/当日费用）→ 填目标（余额源名或来源标签，留空=全部）→ 填阈值 → 选投递方式（机械直发/发给 LLM）→ 自定义话语（支持 `{name}` `{value}` `{unit}` `{threshold}` 占位符）→ 设连续提醒次数/间隔与生效时段 → 勾选投递会话
3. 保存即热重载；每规则可**测试投递**、**复位**已触发状态

> 多段阈值示例：余额 10 元/5 元/1 元三条规则，余额从 8.5 降到 4 再降到 0.5 会收到 3 条提醒（各触发一次，不叠加刷屏）。

### 5. 自定义命令（可选，默认关闭）

插件配置页 → **自定义命令**：

1. `enable_command` 打开
2. `command_words` 添命令词（默认 `/用量`、`/token`，支持前缀匹配：`/用量 今天`）
3. 可选填写 `allowed_users` 白名单（留空 = 所有人可用）
4. 命令参数：`本次 / 今天 / 7天 / 30天 / 累计 / 余额`（留空 = 全部概览）

---

## 🖥️ WebUI 仪表盘

侧边栏 → **Token 用量**：

- **概览**：快照栏（来源/会话轮数/进行时长/最近一轮）+ 五个范围卡片（本次/今天/近7天/近30天/累计：KPI 数字 + 迷你走势线、轮数、输入、输出、缓存、命中率、双币种费用、出错）+ 按天历史（今日高亮 + 条形分布）+ 今日按小时柱状图（点击小时 → 查看该小时记录）
- **时间趋势**：按天柱状图（柱高=总量），Top8 模型费用分色堆叠，未计价灰色兜底；紫色虚线标注缓存命中率；**点日柱 → 下钻该天按小时 → 点小时柱 → 查看该小时前后记录**，「← 返回按天」回退
- **维度分析**：按 来源 / 渠道 / 模型 / 会话 四个维度聚合（可切 今天/7天/30天/累计），费用双币种分列
- **最近记录**：最近 15 轮逐轮明细（时间/模型/来源/渠道/各 tokens/耗时/双币种费用）
- **价格规则**：当前规则展示（含币种列），可「＋ 添加规则」/编辑/删除，保存后全历史费用即时重算
- **余额监测**：各源余额/类型/更新时间/状态（估算型标注「估算」），可手动「立即探测」，可「＋ 添加监测源」/编辑/删除
- **Token 挂件**（默认开启）：迷你悬浮卡片——会话/今日 tokens、今日费用（双币种）、输入/输出、当前模型、前 3 个余额源；标题栏可拖动（位置 localStorage 记忆）、**折叠收成 56px 圆球**（实时显示总量）、紧凑模式，10 秒自动刷新；**⧉ 按钮弹出无边框独立悬浮窗**（blob URL 方案，浏览器允许弹窗后即成为真正可拖动的独立挂件窗口，自带球模式与随机背景 👕）；「完整看板」链接直达全量页。适合浏览器开小窗钉角落，或 OBS 采集当直播挂件

数据每 5 秒自动刷新。

---

## 🛠️ 数据与文件

| 路径 | 说明 |
|------|------|
| `data/plugin_data/KiraAI_token_stats_plugin/usage-log.jsonl` | 用量日志（每轮一行） |
| `data/plugin_data/KiraAI_token_stats_plugin/balance_state.json` | 余额探测状态缓存 |
| `data/config/plugins/KiraAI_token_stats_plugin.json` | 插件配置（含价格规则、余额源 api_key） |

日志行格式：

```json
{"t":"2026-08-30T23:45:01.123","v":1234,"i":1000,"o":234,"c":500,"m":"deepseek-v4-flash","s":"gm","ch":"api.deepseek.com","h":"api.deepseek.com","sid":"qq:gm:12345"}
```

| 字段 | 含义 |
|------|------|
| `t` | 时间戳 |
| `v` / `i` / `o` / `c` | 总量 / 输入 / 输出 / 缓存命中 tokens |
| `m` | 模型名 |
| `s` | 来源（gm/dm/system/自定义） |
| `ch` | 渠道（endpoint 域名或 provider） |
| `h` | endpoint 域名 |
| `sid` | 会话 ID |
| `e` | （可选）该轮「出错：」次数 |

---

## ⚙️ 配置项总览

| 区块 | 配置 | 默认 | 说明 |
|------|------|------|------|
| 基础设置 | `enabled` | `true` | 总开关 |
| 基础设置 | `debug_log` | `false` | 调试日志 |
| 基础设置 | `source_rules` | `{}` | 自定义来源关键词规则 `{"关键词":"来源名"}` |
| 来源归类 | `source_default` / `source_group` / `source_dm` | `system` / `gm` / `dm` | 兜底/群聊/私聊来源标签 |
| 自定义命令 | `enable_command` | `false` | 命令开关 |
| 自定义命令 | `command_words` | `["/用量","/token"]` | 命令词列表 |
| 自定义命令 | `allowed_users` | `[]` | 白名单（空=全部） |
| 自定义命令 | `exact_match` | `false` | 价格规则 URL/模型名/渠道名全字匹配 |
| 自定义命令 | `denied_message` | 权限不足… | 无权限提示 |
| 自定义命令 | `command_success_template` | `📊 {provider}：{result}` | 单结果模板 |
| Bot 工具 | `enable_tool` | `true` | 工具开关（含三个工具） |
| Bot 工具 | `tool_include_balance` | `true` | 工具结果附带余额 |
| 价格规则 | `rules` | DeepSeek 官方价 | 价格规则数组（含 `currency` 币种字段） |
| 余额监测 | `enable_balance` | `true` | 余额监测开关（默认启用） |
| 余额监测 | `balance_interval` | `10` | 轮询间隔秒（最小 5） |
| 余额监测 | `balance_sources` | `[]` | 监测源数组（auto/custom/newapi/preset/daily/rolling） |
| 余额监测 | `balance_unit` | `元` | 余额显示单位（可自定义：额度/美元/美刀等） |
| 高级设置 | `max_log_size` | `100000` | 日志保留条数（0=不裁剪） |
| 高级设置 | `session_idle_minutes` | `30` | 「本次会话」滚动窗口分钟数 |
| 高级设置 | `session_expire_minutes` | `30` | 会话临时状态（来源继承/错误游标）无活动清理分钟数 |
| 预警提醒 | `enable_alert` | `true` | 预警提醒总开关 |
| 预警提醒 | `alert_deliveries` | `[]` | 投递会话数组 `{id,name,sid,enabled}` |
| 预警提醒 | `alert_rules` | `[]` | 规则数组（type/target/session/op/threshold/currency/mode/message/send_image/llm_send_image/max_alerts/alert_interval/time_start/time_end/time_window/deliveries） |

---

## ❓ FAQ

**Q：费用显示「—」？**
A：没有匹配到价格规则。查看 价格规则 页确认规则是否覆盖你的模型/渠道，按域名（`url_match`）配置最稳。积分制渠道记得在规则里把 `currency` 设为 `积分`。

**Q：为什么日志里没有我的模型名？**
A：模型名取自默认 LLM 客户端的 `model_id`/`model` 字段；如果你的 Provider 没暴露这些字段会显示「未知」。渠道识别同理，取 `base_url` 的域名，取不到显示「默认渠道」。

**Q：余额一直「尚未探测」？**
A：检查三点：① 余额监测开关已开启；② 源已 `enabled`；③ 轮询间隔到了（或用页面「立即探测」）。custom 源报错信息会给出具体原因（HTTP 状态/响应摘要）。preset/daily/rolling 估算型不需要接口，直接配置对表/额度字段即可。

**Q：「当前余额(对表)」有什么用？**
A：估算型余额源（preset/daily/rolling）不需要接口，成本全靠价格规则推算，误差会累积。直接在后台看一眼真实余额填到 `anchor_balance`，之后自动按「设定值 − 其后计费」推算，误差被吸收；上游对不上时改一次数字即重新校准。

**Q：改价格后历史费用会变吗？**
A：会。费用**只在展示时计算**（日志只存 tokens/模型/渠道/时间戳），改价即时全历史重定价，双币种同步。

**Q：能统计多开/多 bot 实例吗？**
A：当前为单实例统计（汇总 + 按会话 sid 维度分析）。多实例并发写日志有文件锁保护，不会写坏。

**Q：aiohttp 没装会怎样？**
A：统计/工具/页面全部正常，仅余额监测不可用（加载时日志有提示，插件管理里点「安装依赖」自动装）。

---

## 📄 许可证

[AGPL-3.0](LICENSE) — 修改后再分发需开源。

## 🙏 致谢

- [1chuxin/1chuxin-Alife.TokenStats](https://github.com/1chuxin/1chuxin-Alife.TokenStats) — 原版 Alife 插件（功能设计参考）
- [ChuXia2004/KiraAI-plugin-api-balance](https://github.com/ChuXia2004/KiraAI-plugin-api-balance) — 余额查询模式参考

---

## 📝 更新日志

<details>
<summary>点击展开</summary>

### v1.4.7（2026-09-05）

- **修复 Gemini 系列模型 400 报错（工具 enum 空值）**：`query_token_stats` 的 `range` / `render` 两个参数把「留空」写成了 enum 里的空字符串 `""`。Gemini 的 function calling 规范不允许 enum 含空值，注册工具时直接被拒：

  ```
  tools[0].function_declarations[N].properties[range].enum[0]: cannot be empty
  tools[0].function_declarations[N].properties[render].enum[0]: cannot be empty
  ```

  一个参数不合法就整份工具表被拒，表现为该渠道**所有**模型调用失败（`All models in the group failed to respond`）——本插件启用后，同渠道其他插件的工具也一起挂
- **空串改为显式哨兵**：`range` 空串 → `all`（全部概览），`render` 空串 → `auto`（跟随插件配置）；两个参数的 `default` 也改成对应哨兵（原来的 `default: ""` 本身就不在 enum 里，不合法）
- **行为完全不变**：`all` / `auto` / 不传 / 旧的空串**四种形式等价**，仍是「全部概览」与「跟随配置」；`session/today/d7/d30/total`、`image/text` 原样保留，并额外容错大小写与首尾空格。DeepSeek 等原本正常的渠道不受影响
- 顺带把参数描述里误导模型的「留空」措辞改掉——强制要求从 enum 取值的模型不会再瞎猜（此前有猜成 `image` 而意外触发渲染发图的风险）

### v1.4.6（2026-09-03）

- **余额轮询默认间隔 10 秒 → 600 秒（10 分钟）**：10 秒轮询对第三方 new-api 站属于无谓持续负载（余额是慢变量，10s 查一次无信息增量；多个 bot 指向同一站时聚合请求不可忽视）。600 秒检测几乎零感知损失
- **旧版默认 10 秒自动迁移**：配置里还是旧默认 10 的自动迁到 600；用户主动设置的其他值保留
- **立即检测不受影响**：bot 主动查询 / 用户手动「立即探测」仍即时请求，不走轮询
- **最小间隔 5 秒 → 60 秒**：防止误设超高频轮询

### v1.4.5（2026-09-03）

- **修复渲染图多会话并发**：两个会话同时补发概况图时毫秒级时间戳可能相同 → 文件名冲突 → 只有一个会话收到图。文件名加随机后缀
- **LLM 模式独立提醒素材**：新增 `llm_message` 字段（llm 模式专用，默认带「请告知用户情况」让 bot 主动告知用户），与机械直发 `message` 分开设定；WebUI 投递方式切换时显示对应输入框
- **旧版默认话语自动迁移**：v1.4.2 前默认模板（"⚠️ xxx预警：xxx 已达 xxx"）被手动保存过的自动清空，让新版默认生效（step 带「每新增 N 提醒一次」、llm 带「请告知用户情况」）；自定义话语保留

### v1.4.4（2026-09-03）

- **修复删除规则/投递会话无效**：KiraAI WebUI iframe 里原生 `confirm()` 被拦截返回 false → 删除直接 return。改用余额监测同款 `confirmBtn`（点两下确认）
- **修复测试按钮没用**：之前只弹 toast 提示"已投递到"，没真正发消息。现在调后端 `/alert-test` API 真实投递（direct 直发 / llm 发提醒事件）
- **规则卡片加启停开关**：与余额监测一致的 `.sw` 开关，切换即保存（POST /alert-config）

### v1.4.3（2026-09-03）

- **修复预警 tab 加载失败（500）**：v1.4.2 把 `_alert_fired` 从 float 时间戳改为 dict `{count,at,day}`，但 `api_alert` 仍用 `datetime.fromtimestamp(v)` 期望 float → TypeError → 500 → 前端 JSON.parse 报错、规则编辑不了。修复：兼容 dict/float 两种格式，异常值兜底空串
- **step 模式默认话语**：体现「每新增 N 提醒一次」（如"📈 tokens预警：全部 每新增 10000000 提醒一次，当前已达 12,000,000 tokens"）；新增 `{step}` 占位符
- **投递会话停用**：规则编辑弹窗里该会话勾选框置灰不可勾选（标注"已停用"），规则卡片投递到显示删除线；后端 `_alert_delivery_sids` 本就跳过停用会话

### v1.4.2（2026-09-03）

- **token/cost 新增「每新增 ≥」触发模式**（trigger=step）：每跨过一个阈值整数倍提醒一次（如每 10M tokens 提醒），受生效时段限制，跨天重置；与「当天累计达阈值提醒一次」（trigger=once，默认）并存
- step 模式天然每档一次，跳过 max_alerts 限制（仍受 alert_interval 防刷屏）；WebUI 规则编辑加「触发模式」下拉

### v1.4.1（2026-09-03）

- **前端填写优化**：类型切换时目标 hint 跟随（余额源名/来源标签说明联动）；token/cost 禁用「低于等于」只允许「达到」；阈值禁负数（0 已涵盖预扣费为负场景）；token 模式阈值支持 K/M/亿/B 后缀解析 + 按钮预填 + 实时换算显示（<0.1K 不显示）
- **修复**：添加投递会话/规则后切走再切回丢失——操作即自动保存（POST /alert-config），面板加「保存配置」按钮兜底，复位调 /alert-reset 同步后端；后端清洗保留规则 name 字段

### v1.4.0（2026-09-03）

- **新增「预警提醒」功能板块**：监测余额 / 当日 tokens / 当日费用，达到阈值自动提醒
  - **三类规则**：balance（余额低于阈值，滞回复位——余额回升超过阈值才重置，充值后可再提醒）；token（当日 tokens 达阈值，按自然日重置）；cost（当日费用达阈值，多币种可选，留空=任一币种达阈值即触发）
  - **多段可设**：同一站点/来源可设多个不同阈值（如余额 10 元/5 元/1 元、tokens 1M/10M、费用 5 元/10 元），各自独立触发互不影响
  - **来源/会话过滤**：token/cost 规则可限定来源标签（关键词规则/群/私聊）与具体会话 sid（如 qq:gm:123456）
  - **两种投递方式**：direct=机械直发（自定义话语，不经过 LLM 零消耗，消息先发、概况图异步补发可开关）；llm=发给 LLM（publish_notice 让 bot 感知后主动告知，独立异步补发图开关）
  - **生效时段**：time_start/time_end（HH:mm）+ time_window（分钟，如 9:00+60min=9:00-10:00），全留空=全天
  - **连续提醒控制**：最大连续提醒次数（默认 1=只提醒 1 次，未回升/未超过阈值前最多连续提醒 N 次）+ 连续提醒间隔（默认 300 秒=5 分钟），只约束同一条规则自身，多规则同时触发互不影响；多段阈值（10 元/5 元/1 元）各自独立触发一次不叠加
  - **WebUI**：预警提醒并入「Token 用量」页 tab（与余额监测并列），投递会话管理 + 规则可视化编辑（增删改/启停/测试投递/复位已触发状态/提醒效果实时预览）
  - 配置段 `section_alert`（enable_alert / alert_deliveries / alert_rules），WebUI 保存即热重载
- **性能与并发审计**：token/cost 统计单遍扫描日志 + 指纹缓存（记录数/最新时间戳/规则 hash/过滤条件未变直接复用，避免每轮 LLM 后全量重扫）；概况图构建丢线程执行不阻塞事件循环；渲染并发信号量限制同时最多 1 个浏览器实例（多规则同时补发图排队，防内存/CPU 吃满）
- 验证：30+ 组测试全过（时段/窗口、余额滞回、连续提醒次数/间隔、token/cost 来源+会话过滤、多币种、多规则并发、占位符、direct/llm 投递、缓存失效重算）

### v1.3.13（2026-09-02）

- **工具结果失败判定改为框架结构信号**（开发者建议）：框架 `core/agent/func_tool_manager.py` 执行工具失败时（超时/异常/未实现）**必包成 `{"error": ...}` JSON** 再转 ToolResult——主判定改为匹配该 JSON 结构（100% 可靠，非字符串猜测），不再匹配裸「超时/失败」或「动词+失败词」（bot 正常回答里"如果响应超时/查询失败会怎样"等说明文字不再误判）；保留权限类（权限不足/无权限/Forbidden/403/not allowed）与 `Error:` 前缀
- 验证：19 组测试——框架超时/异常/未实现、JSON error、权限类全部判定；记忆存储分析等正常回答、error=0 成功约定、200 字符后失败词全部不误判

### v1.3.12（2026-09-02）

- **工具结果失败误报修复（再收紧）**：bot 正常回答长文本（如分析"记忆存储卡住原因"时提到"如果响应超时可能失败"）被 `TOOL_ERR_RE` 的裸「超时」模式全文搜索命中 → 误判工具失败。修复：①正则去掉裸「超时」，只匹配「请求超时/连接超时/调用超时」等明确失败模式；②只检查文本开头 200 字符（真实工具失败几乎总是以错误信息开头，长文本中间提到失败词是正常说明）

### v1.3.11（2026-09-02）

- **背景显示更多**：去掉 `.wrap` 整块毛玻璃遮罩，改为只给内容元素（标题条/快照栏/卡片/表格/余额行/footer）加毛玻璃——卡片之间、标题与卡片之间的空隙背景图完整清晰可见
- **费用兜底**：渲染图费用计算补上 WebUI 同源的记录遍历兜底（内存 aggs 未匹配规则时直接遍历日志现算），历史费用不再显示「—」
- **隐私**：渲染图顶部去掉日志路径（只显示模型 + 渠道）
- **余额开关**：新增 `tool_render_balance` 配置（默认开），关闭后渲染图不显示 API 账户余额

### v1.3.10（2026-09-02）

- **渲染图截断修复**：内容超高时（如错误卡多、历史数据多）页面被截断——flex/min-height 布局下 Playwright `full_page` 的 scrollHeight 计算不可靠；改为先按内容实际高度动态设置视口再截图（内容超高自动撑高视口，完整不截断）
- **工具结果失败误报修复**：渲染图返回文本以「已发送渲染概览图到会话。」开头，`_SELF_TOOL_RE` 未覆盖 → 摘要里的「工具结果失败：N 次」被 `on.tool_result` 误判自增；前缀排除规则补上渲染图返回
- **文字溢出修复**：错误日志长路径（如 `C:\Users\...\SKILL.md not found`）溢出卡片，`.card .d` 加 `word-break:break-all`

### v1.3.9（2026-09-02）

- **内容面板居中**：body 改 flex 布局（`align-items:center;justify-content:center`），面板水平+垂直双居中；body 100% 宽 + 背景图 `position:absolute` 全屏铺满（`object-fit:cover` 缩放居中），任何宽高下背景完整无空隙
- **毛玻璃透明效果**：`.wrap` 加 `background:rgba(15,23,42,.72)` + `backdrop-filter:blur(14px)`（对齐 WebUI 看板 `body.bg-on #app` 风格），背景图透过面板柔和模糊，文字清晰可读
- **渲染图自动清理**：`output/` 目录只保留最近 20 张渲染图（按文件名时间戳排序，最旧先删），不再无限累积
- **README 精简**：顶部版本简介块删除，只保留标题 + 介绍 + 移植说明；更新日志保留在尾部折叠区

### v1.3.8（2026-09-02）

- **横屏宽幅**：渲染视口 800px → 1200px，五范围卡一行横排（grid 5 列），按天历史与今日按小时两栏并排，整体横向宽幅布局
- **随机背景修复**：之前用 Python `urllib.request` 同步下载随机图，在部分环境失败 → 纯色背景；改为 Playwright 浏览器加载随机图 URL（与 WebUI 看板同源，能显示），`<img>` 加载完成才截图，失败自动纯色降级；自定义背景图仍 data URI 内联
- **footer 修复**：`_build_summary_html` 是同步方法却调用 async `_resolve_sid_name`（未 await）→ footer 显示 `<coroutine object ...>`；会话昵称解析移到 async 调用方 `_build_summary_image` await 后传入；footer 按反馈改为「Token 用量统计 · provide by @znq19 · 生成于 YYYY-MM-DD HH:MM:SS」（保留标题与生成时间）

### v1.3.7（2026-09-02）

- **bot 渲染概览图发送（查加发一体）**：`query_token_stats` 工具新增 `render` 参数（`image`/`text`/留空跟随配置）——`image` 时插件自己查数据（与 WebUI 同源的内存聚合）→ 构建完整概览首页 HTML（快照栏 + 五范围卡 + 错误卡 + 按天历史 + 今日按小时 + 余额源）→ Playwright 渲染截图 → 直发图片到会话 + 返回文本摘要给 LLM 转述
- **背景**：优先用 WebUI 自定义背景图（前端保存/清除时自动同步到服务端 `bg_custom.jpg`），无自定义时用线上随机图（data URI 内联，渲染不依赖外网）；渲染失败自动降级纯文本，不吞消息
- **配置**：新增 `tool_render_image` 开关（默认关）——关=LLM 显式要求才发图，开=一查就发图；渲染依赖 Playwright（系统 Chrome/Edge → 内置 Chromium 自动下载三级降级），requirements 新增 `playwright`
- **WebUI**：🖼 自定义背景保存/清除时同步服务端（新增 `/bg-sync` API），渲染图与看板背景一致

### v1.3.6（2026-09-02）

- **最近记录列错位修复**：表头补上「耗时」列（时间/模型/来源/渠道/输入/输出/缓存/耗时/总量/费用），与数据行 10 个字段完全对齐，费用列不再右移漂移
- **时间趋势橙色耗时线修复**：SVG 线条颜色从 `stroke="var(--warn)"` 改为 `style="stroke:var(--warn)"`（SVG presentation attribute 不支持 CSS 变量，导致线条完全不渲染）；单数据点场景补圆点标记（近 7 天只有一天有耗时数据时，单点 path 不渲染，现在显示橙色圆点）；紫色缓存命中率虚线同步修复（颜色 + 单点圆点）

### v1.3.5（2026-09-02）

- **耗时统计（新维度）**：JSONL 记录新增 `d` 字段（LLMResponse.time_consumed，老记录兼容显示「—」）；聚合维护 avg/min/max
- **最近记录**：逐轮耗时列；**概览五卡**：「耗时 均X·快Y·慢Z」；**维度分析/会话统计**：均耗时/最快/最慢三列；**时间趋势**：平均耗时橙色实线（右轴，秒），三级下钻均有，无数据桶断线；**挂件**：轮均耗时行；**bot 工具**：概览/聚合/逐轮明细均带耗时（4000 字符上限守住）
- 统一 `fmtDur`：<10s→4.56s、<60s→23.4s、<60m→1m23s、≥1h→1h05m，前后端三处一致

### v1.3.4（2026-09-01）

- **时长格式**：概览快照栏「已进行」从「263分43秒」改为完整组合——秒→分秒→时分秒→天时分秒（如 56天7时34分3秒），不足一级自动省略；秒级 ticker 不变
- **数字单位**：K/M/亿/B 四级阶梯（千/百万/亿/十亿），沿用 995 平滑进位；Python `_fmt4` 与 dashboard/挂件 JS `fmt4` 三处实现统一，15 组边界值双端输出一致

### v1.3.3（2026-09-01）

- **背景回归修复**：上一轮重构后 `body::before` 伪元素 CSS 漏写，挂件侧边栏/弹窗/PiP 背景全部不渲染，已补齐（含 reduced-motion 兼容、🖼 按钮定位与 👕 同列并排）
- **主看板自定义背景**：Token 用量页新增 🖼 自定义背景（1-10 张、压缩降档、60s 轮换、双击清除），与挂件共享 localStorage 图池与开关；无自定义图时线上随机背景行为完整保留，看板背景叠 45% 暗化保证文字可读

### v1.3.2（2026-09-01）

- **余额源重复增殖修复**：简易文本格式与平台快捷配置派生的余额源加 `_origin` 标记，WebUI 保存时过滤不落盘、加载时按 name+url 去重，彻底根治「编辑/开关/删除后同源越变越多」；派生源在余额页标注「由配置项管理」并禁用开关（需到对应配置项修改）
- **概览秒级走时**：快照栏「已进行 xx 秒」改为前端 1s 本地计时（4s 轮询校准，切后台暂停、回来自动校准），不再几秒一跳
- **同类排查**：价格规则/峰谷方案/命令词等其余配置项均无此合并写回模式，确认无增殖风险

### v1.3.1（2026-09-01）

- **维度分析费用修复**：bySource/byChannel/byModel/bySid 每行下发 cost（此前前端恒为「—」）
- **时间趋势四项修复**：① 柱高按 x.v/maxV 比例（不再满高等高）；② 按天分支逐天下发模型费用（堆叠分色生效，不再灰色兜底）；③ frm→to 逐日补齐 0 值桶（漏天不错位）；④ 5 分钟桶补 h 字段（下钻看记录不再为空）。另：图例色与柱色统一映射；累计跨度大时按月聚合；缓存命中率紫色虚线用内联 SVG 实渲染；tooltip 右边缘自动 flip
- **挂件重写（三级渐进增强）**：⧉ 打开同源真实 URL 的命名弹窗（单例 + 位置记忆 + 被拦截页内 toast），?pop=1 双模式——标题栏拖拽 window.moveBy 移动真实窗口、折叠球 resizeTo(140,150)（弹窗外框最小尺寸钳制检测，失败 toast 退回卡片态）；📌 PiP 置顶（DOM+样式复制进 OS 级浮窗，pagehide 归还）；随机背景默认开；「打开看板」改同源新标签；数值变化 150ms 闪动、拖动 grabbing、失败 dot 变红；挂件停用提示文案修正
- **挂件打磨（PR #12 实机反馈）**：① 概览骨架卡首渲染即清理（此前 4 张 shimmer 空卡永久残留），真实卡片加 hover 高光扫过；② 折叠球球体单击展开（位移 <5px 判定为点击，与拖拽区分），弹窗球窗 140×150 居中；③ 📌 一步置顶——侧边栏 iframe 直接调 documentPictureInPicture（成功即浮窗归属主标签页、不留额外小窗），被 SecurityError/NotAllowedError 拦截自动回退弹窗流程；挂件 DOM 改经 #w 直接元素引用读写，拖拽 move/up 监听动态挂当前 ownerDocument，PiP 内刷新/拖动/折叠球均可用；④ 🖼 自定义背景——选 1-10 张图 canvas 压缩（1280/q0.8，超限降档 960/q0.7 再逐张减少）存 localStorage，加载随机选 + 每 60s 轮换（400ms 淡入），双击 🖼 清除恢复线上随机图，侧边栏/弹窗/PiP 共享同一池
- **性能优化**：估算余额/费用兜底计算按（记录数+最新时间戳+规则hash）缓存；日志行数内存计数、超阈值 20% 才裁剪一次（消除 O(N²) 重写）；看板轮询 1s→4s 且卡片按 data-k 签名增量更新；rolling 死代码与 _rec_cache_lock 死字段删除；_grant_count 除法直算
- **健壮性**：api_key 掩码回传（sk-****xxxx）+ 保存未改动保留原值 + 前端 password 输入；日志裁剪后估算余额附「可能偏高」警告；_parse_ts 防御 aware datetime；会话窗口按 sid 各有界 LRU（多群交替不再清零）；余额/错误统计落盘改原子写
- **界面美化**：骨架屏 shimmer、KPI 数字 rAF 300ms 插值滚动、卡片 hover 微浮起、tab 切换 fade-in、本次会话/今天主卡放大、空状态引导按钮、错误卡淡红底、alert/confirm 全换页内 toast/内联二次确认、--dim 对比度提升、编辑弹窗底色统一 --inset 变量、「上次更新 xx:xx:xx」轮询状态

### v1.3.0（2026-09-01）
  - **本次会话精确化**：会话窗口按 sid 切换即重置，不再混多个会话来源的总和；概览卡片标题显示当前会话昵称（如「周武(769690776)」）
  - **历史费用兜底**：d7/d30/累计费用在内存聚合未匹配规则时自动遍历记录现算，保证费用一定显示（不再只有本次会话有费用）
  - **最近记录来源细化**：来源列显示 `[私聊/群聊] 昵称(sid)`，与维度分析/会话统计对齐
  - **价格规则 hint 明确**：币种=计价口径（CNY/积分），显示单位=纯展示符号（¥/元/$），两者独立
  - **时间趋势修复**：下钻后切换范围按钮不再丢失 range 参数
  - **挂件弹窗修复**：「打开看板」被拦截时自动降级为链接点击打开；独立小窗 blob URL 方案保留

### v1.2.9（2026-08-31）

- **会话昵称/群名显示**：会话统计页不再只显示 `qq:dm:12345` 这种裸 ID——私聊自动解析为「周武(769690776)」、群聊解析为「群名(群号)」（OneBot `get_stranger_info`/`get_group_info`，1 小时缓存，解析失败自动降级为原 ID）
- **挂件独立小窗升级**：⧉ 弹出的独立窗口改为**无边框悬浮窗**（圆角 + 毛玻璃 + 无标题栏边框），可随意拖动；支持**球模式**（点折叠收成 56px 圆球，实时显示总量，再点展开）；小窗内自带**随机背景**（👕 开关，与内嵌挂件独立记忆）
- **内嵌挂件球模式**：侧边栏挂件同样支持收成圆球（显示总量），球模式下整球可拖动
- **余额检测 10 秒**：`balance_interval` 默认从 1 分钟改为 **10 秒**（最小 5 秒），单位从分钟改为秒；WebUI 余额页输入框同步

### v1.2.8（2026-08-31）

- **会话级统计**：新增「会话统计」页（WebUI 侧边栏 → Token 用量 → 会话统计），细到每个会话（如 `qq:dm:12345` / `qq:gm:12345`），展示轮数/输入/输出/缓存/总量/费用/最近活动；顶部按 **私聊 / 群聊 / 其他** 整体归类汇总卡片；点击某会话行直接跳转该会话的逐轮明细
- **查余额默认启用**：`enable_balance` 默认值改为 **开**（升级后余额监测自动生效，无需手动开启）；自定义命令（`/用量` 等）仍默认关闭
- **随机背景开关反馈**：点击右下角 👕 按钮切换时弹出「随机背景：开/关」提示，图标同步变化（👕 开 / 🚫 关），悬停 title 也显示当前状态
- **挂件弹窗修复**：挂件「独立小窗 ⧉」改用 blob URL 弹出真正可拖动的独立挂件窗口（绕过 `window.open('', '_blank')` 空窗被浏览器拦截的问题），「打开看板」按钮新标签页打开统计面板

### v1.2.7（2026-08-31）

- **价格规则 WebUI 可视化编辑器**：侧边栏「Token 用量 → 价格规则」页新增「＋ 添加规则」——按表单填写（规则名称/URL/模型/渠道匹配、币种、峰谷开关、缓存命中/未命中/输出 峰谷六价），支持编辑/删除，保存后自动热重载并即时重算全历史费用，无需再手写 JSON
- **工具结果失败统计排除自报**：本插件自身工具（query_token_stats/usage/records）的正常输出文本含「失败/出错」字样（如"工具结果失败：N 次"、"最近出错：…"、"后台日志错误：…（最近：Merge facts error）"）时不再被误计为一次工具失败——新增 `_SELF_TOOL_RE` 前缀排除，避免 bot 查一次统计就自增一次错误
- **余额类型配置说明优化**：schema 与 README 用大白话解释 preset/daily/rolling 三种估算型怎么填（preset=固定钱包用一点少一点；daily=每日重置；rolling=每日累计滚存），并给出「拿不准就选 auto」的引导
- **配置说明引导侧边栏**：插件配置页顶部说明改为推荐在 WebUI 侧边栏「Token 用量」页查看数据与配置（价格规则/余额监测均可页面内可视化编辑），不再引导手写 JSON

### v1.2.6（2026-08-31）

- **后台日志 ERROR 扫描**：新增对 KiraAI `data/log.log`（含轮转文件）的增量扫描（10s 间隔），捕捉控制台/日志里的真实错误——重点：**LLM 输出错误 XML 格式导致解析失败**（`Error parsing message: mismatched tag` 等）。按天分类聚合近 7 天：XML解析 / 模型调用 / 工具执行 / 网络超时 / 异常堆栈 / 其他；工具/命令摘要与 WebUI 仪表盘新增「后台日志 ERROR」卡片。与原有「出错：」响应内扫描并存
- **轮转文件按 inode 跟踪游标**：`log.log.1/2/…` 全部纳入扫描（历史 ERROR 也能统计到）；轮转改名后同一文件（同 st_ino）继续从原游标扫，不重复计数；文件截断/重建（新 ino）自动从头扫。修复此前只扫 log.log、轮转瞬间切文件导致游标重置重复计数的问题
- **扫描游标持久化**：`err_stats.json` 同时保存各文件 ino 游标，热重载后新实例从原游标续扫——历史 ERROR 不重复计数（修复热重载一次统计翻倍一次的问题）
- **错误统计持久化**：日志错误与工具失败统计每 30s 节流落盘 `err_stats.json`，热重载/重启自动恢复；`terminate` 时**强制落盘**（force 参数绕过节流），不再因距上次保存不足 30s 而丢最近统计
- **二进制模式扫描**：日志文件以 `rb` 读取，`tell()/seek()` 返回真实字节偏移——Python 3.10-3.12 文本模式 `tell()` 返回不透明 cookie，与 `st_size` 比较会误判截断导致重复计数，3.13 起才返回真实偏移；二进制模式全版本兼容
- **工具结果失败统计**：新增 `on.tool_result` 钩子，捕捉工具返回的失败结果——**error 返回 / 权限 denied（Permission denied、拒绝访问、403 Forbidden、HTTP 403）/ 超时 / 调用失败 / 未实现**等，这些都属于 LLM 白烧 token 的典型场景。按天聚合近 7 天，工具/命令摘要与 WebUI 仪表盘新增「工具结果失败」卡片
- **失败判定防误报**：`{"error": 0}`（很多 API 的成功约定）不再误判为失败，error 字段仅当值为非零数字（**含负数/小数**，如 `-1`、`1.5`、`0.5`）/非空字符串/true 时才算失败；裸 `403` 不再匹配（"第403条"会误报），只匹配 `403 Forbidden` / `HTTP 403` / `status 403`
- **余额可视化编辑器数据源修复**：`/balance` 接口返回完整配置字段（url/api_key/api_user/quota_conversion/daily_quota/anchor_balance/refresh_time 等）且**包含禁用源**——编辑已有源时输入框正确回填、保存不再清空配置；保存不再静默删除禁用源（禁用源在列表中灰显标注「(禁用)」，不探测只显示缓存状态）
- **日志扫描不占用日志文件**：Windows 下用 `CreateFileW + FILE_SHARE_DELETE` 共享删除模式只读打开，句柄毫秒级释放，不阻塞 KiraAI 的 RotatingFileHandler 轮转 rename；修复 `f.tell()` 在文件关闭后调用导致游标不前进、重复计数的 bug
- **来源标签支持多填**：`source_default` / `source_group` / `source_dm` 从 string 改为 list（兼容旧配置单个字符串），第一个为主标签（实际归类用），其余作为备选/别名
- **WebUI 余额监测可视化编辑器**：侧边栏「余额监测」页新增「＋ 添加监测源」——点开后按类型（auto/custom/newapi/preset/daily/rolling）动态显示对应字段表单（网址/API Key/用户ID/换算比例/每日额度/刷新时刻/对表余额等），支持编辑/删除/启用开关，保存后自动热重载，无需再手写 JSON
- **WebUI 分类名中文化**：后台日志 ERROR 卡片分类显示中文（XML解析/模型调用/工具执行/网络超时/异常堆栈/其他），与工具摘要一致
- **轮转文件显式匹配**：`log.log` + `log.log.[0-9]*` 显式 glob（RotatingFileHandler 轮转命名），防未来轮转策略变化误扫无关文件；`get_data_path()` 返回值用 `Path()` 包裹防 str；`st_ino` 为 0（FAT32 等文件系统）时退化为路径跟踪，避免共用游标 0 重复计数

### v1.2.5（2026-08-31）

- **修复查询崩溃**：`_fmt_num` 千分位格式符 `N0` 需 Python 3.10+，低版本直接 ValueError 导致工具查询全挂，改为 `f"{v:,}"` 全版本兼容
- **修复渠道/模型显示未知**：`_resolve_channel_model` 读错层级（KiraAI 的 LLMModelClient 结构是 `client.model = ModelInfo`），改为正确读取 `model.model_id` / `model.provider_name` / `provider_config.base_url`，自动显示真实模型名与渠道名
- **余额轮询默认 5 分钟**（最小 1 分钟）；工具/命令查询余额时先即时探测，保证返回最新值（与 api-balance 插件行为一致）
- **余额配置模板化**：新增 DeepSeek / Kimi / 硅基流动 / 智谱四个官方平台快捷分区，填 API Key 即自动并入余额监测（对齐 api-balance 插件风格）；自定义/中转站仍走「余额监测源」高级 JSON
- **挂件独立小窗**：⧉ 按钮 `window.open` 弹出真正可拖动的独立挂件窗口（浏览器允许弹窗后生效，卡片填满窗口、自带关闭按钮）；内嵌模式拖动位置 localStorage 记忆，刷新/重开不丢；底部新增「新标签」直开链接（弹窗被拦截时的兜底）
- **New-API 简易文本格式**：对齐 api-balance 插件，配置页新增「New API 站点（简易文本格式）」分区，每行 `名称;base_url;令牌;用户ID;换算比例(可选)` 英文分号分隔，自动并入余额监测（type=newapi）
- **余额探测并发与超时加固**（姐姐审计）：`_probe_all` 网络型源改为**并行探测**（单源 8s 超时、整体 15s 超时），不再串行 N×10s 拖住工具/命令查询；工具/命令/API 查询余额时若后台轮询正忙会**等待其完成**（最多 20s）再返回最新值，不再拿旧状态；模板源插入顺序修正（append 保持配置顺序，不再反转）；`create_task` 替代 `ensure_future`（3.13 兼容）、pending 取消后 `gather` 等待（防 Task was destroyed 警告）、`balance_sources` 复制 cfg list 再 append（防热重载重复追加）

### v1.2.4（2026-08-31）

- **群聊默认来源标签 qchat → gm**：`source_group` 默认值对齐 KiraAI 框架会话类型标准（`qq:gm:xxx`），避免来源标签与 sid 命名不一致；schema/README/工具描述同步更新。已配置过 source_group 的用户不受影响（仅默认值变更）

### v1.2.3（2026-08-31）

- **修复 /trend 5 分钟下钻短路**（三轮审查 #1，核心功能 bug）：hour 分支排在 day+hour 分支之前且无条件拦截，5 分钟桶永远不可达。已在 day 分支条件加 `not hour_s`，`_mins` 数据结构恢复可用
- **修复 session_expire_minutes 单位混用**（三轮审查 #2）：`max(60, ...)` 误把分钟当秒比，配置 ≤60 分钟时一律 60 分钟才清理；改为 `max(1, ...) * 60`，配置如实生效
- **余额行按币种显示**（三轮审查 #3）：工具/命令出口的余额行不再一律用全局 `balance_unit`，积分制源（daily/rolling）显示「积分」
- **balance_state.json 顶层类型校验**（三轮审查 #4）：文件被写坏成数组/字符串时回退空 dict，不再 AttributeError；`_build_summary_text` 余额段整体包 try 兜底
- 回归测试：api_trend 五种分支场景、expire 计算、挂件 compact 注入逻辑全部断言通过

### v1.2.2（2026-08-31）

- **挂件紧凑模式真正生效**（二轮审查 A）：后端「紧凑模式」配置注入前端默认值；localStorage 有记忆时以用户为准，配置与前端操作不打架
- **会话状态自动清理**（二轮审查 C）：新增 `session_expire_minutes`（默认 30，可配置）——每个会话的来源继承文本与错误游标超过该时长无活动即清理，防长期运行内存缓慢增长
- **exact_match 文档如实化**（二轮审查 B）：schema hint 明确作用范围仅为「价格规则」匹配（URL/模型名/渠道名全字相等）；来源关键词归类与渠道 URL 探测不受影响
- **max_log_size 矛盾消除**：schema `minimum` 1000 → 0，与「0=不裁剪」文档一致
- **删除死代码**：`_ssl_connector`（从未使用）

### v1.2.1（2026-08-31）

- **修复时间戳兼容**（姐姐审查 #1）：日志统一写 6 位微秒，解析走 `_parse_ts` 兼容 3/6 位——Python 3.10- 的 `fromisoformat` 不再炸，`_apply_session` 主采集路径不再可能崩
- **修复错误统计重复计数**（姐姐审查 #2）：移植 errScanPos 位置游标（`_err_scan`），工具循环续轮同一段「出错：」只计一次，新响应自动重置
- **exact_match 配置生效**（姐姐审查 #3）：原为死代码，现模块级开关全量接入价格规则匹配器（URL/模型名/渠道名全字相等）
- **移除死代码**：`cmd_all_template`（从未使用）从 schema/代码删除
- **HTTPS 证书校验开关**：新增 `balance_ssl_verify`（默认关，兼容自签证书中转站；官方端点可开）
- **日志 IO 线程锁**：追加/裁剪/热读缓存统一走 `_IO_LOCK`，消除并发竞争面
- **`_range_agg` 统一遍历写法**：不再依赖 dict 插入序，与 `_range_cost_ex` 一致
- **data_dir 兜底**：`get_plugin_data_dir()` 返回 None 时降级插件目录，不再裸崩
- **新增 WebUI 悬浮挂件**（默认关闭）：侧边栏「Token 挂件」页——迷你卡片实时显示会话/今日 tokens、费用、余额，可拖动、可折叠成小球、紧凑模式，适合浏览器小窗钉角落；配置页「挂件」区开启

### v1.2.0（2026-08-31）

- **AI 查询函数扩容**：新增 `query_token_usage`（维度聚合：channel/model/source/day + 时间区间 + 关键字过滤 + top 上限）与 `query_token_records`（逐轮明细 + minInput 定位大上下文），输出带 4000 字符 ClampAiOutput 硬上限防回注撑爆上下文
- **热读缓存**：按 mtime+length 判失效，多端点轮询/查询不再重复全量读盘，大日志下性能显著提升
- **余额对表校准**：估算型源（preset/daily/rolling）支持 `anchor_balance`+`anchor_at`——填上游真实余额即校准，此后按价格规则自动扣减，吸收一切估算误差
- **每日重置/每日累计余额源**：新增 `daily`（每日额度 − 本周期计费，跨刷新锚定自动回落）与 `rolling`（设定余额 − 计费 + 每日发放结转滚存），`refresh_time` 纯时间推导不落状态，离线期间发放不丢
- **双币种计费**：价格规则新增 `currency` 字段（CNY/积分），费用按币种分桶累计永不混算，工具/命令/WebUI/API 全链路支持
- **时间趋势下钻**：WebUI 新增「时间趋势」页——按天柱状（Top8 模型费用分色堆叠 + 缓存命中率虚线），点日柱下钻单天按小时，点小时柱直达该小时逐轮记录
- **KPI 迷你走势线**：概览卡片加近 14 天用量走势 sparkline；概览页快照区按小时下钻入口
- WebUI 余额页标注估算型源、价格规则页新增币种列、记录页双币种费用显示

### v1.1.0（2026-08-31）

- **新增 `newapi` 余额源类型**：New-API 站点专属探测（请求带 `New-Api-User` 头，打 `/api/user/self`，自动从 quota/balance/remaining/points 等字段提取额度，按 `quota_conversion` 换算，默认 500000）
- **余额显示单位可自定义**：新增 `balance_unit` 配置（默认"元"，可改为"额度"/"美元"/"美刀"/"点数"等），工具/命令/WebUI 三端全局生效
- **requirements.txt**：新增依赖清单（aiohttp），支持插件管理自动安装依赖
- 配置文档与 README 同步更新

### v1.0.0（2026-08-30）

- 初始发布：Token 用量统计看板（逐轮采集/费用估算/余额监测/错误统计/WebUI 仪表盘/bot 工具/自定义命令）

</details>
