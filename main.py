# -*- coding: utf-8 -*-
"""KiraAI Token Stats — Token 用量统计看板插件

为 KiraAI 提供完整的 Token 用量统计能力（移植自 Alife 的 1chuxin.TokenStats 4.9.x 设计，
并整合 KiraAI-plugin-api-balance 的查询模式）：

- 逐轮采集：@on.llm_response 钩子记录每轮 LLM 调用的 输入/输出/缓存 tokens，
  包含工具中间步；日志 JSONL 持久化到插件数据目录，重启不丢
- 费用估算：价格规则按 URL > 模型 > 渠道名 加权匹配（4/2/1 分），
  峰谷价（工作日 9:00-12:00 / 14:00-18:00 为峰，其余谷）；
  费用一律在展示时计算，改价后全历史即时重定价；
  双币种（CNY 元 / 积分）分桶累计，永不混算
- 余额监测：auto（按 URL 自动分流官方端点 / One-API 中转站）、
  custom（自定义接口多端点尝试 + json_path 取数）、
  newapi（New-API 站点：New-Api-User 头 + /api/user/self + quota 换算）、
  preset（预设扣减钱包型）、daily（每日重置积分）、rolling（每日累计滚存积分）；
  估算型支持「当前余额(对表)」锚定：填上游实际余额即校准，此后按价格规则自动扣减
- 来源归类：自定义关键词规则优先 → 群聊/私聊自动判定 → 工具续轮继承上一轮
- 多入口查询：WebUI 侧边栏仪表盘（KPI 走势 + 时间趋势下钻）/ bot 工具
  （概览 / 维度聚合 / 逐轮明细）/ 可选自定义命令
- AI 查询函数：query_token_usage（维度聚合）与 query_token_records（逐轮明细），
  输出带 4000 字符硬上限防止回注结果撑爆上下文
- 热读缓存：按 mtime+length 判失效，大日志下轮询/查询不重复全量读盘
- 错误统计：LLM 响应内「出错：」正则扫描 + **后台日志（log.log）ERROR 行增量扫描**（分类聚合：XML解析/模型调用/工具执行/网络超时/异常堆栈）+ **工具结果失败钩子**（error/权限denied/超时/调用失败——LLM 白烧 token 的典型），按范围聚合

模型无关：统计基于 LLMResponse 的 input_tokens/output_tokens/cached_tokens 字段，
任何 Provider 只要上报 tokens 即可统计。
"""

import asyncio
import base64
import html
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

try:
    import msvcrt  # Windows 共享删除模式打开日志（仅 NT 使用）
except Exception:  # pragma: no cover
    msvcrt = None

from fastapi import Request

from core.plugin import BasePlugin, logger, on, Priority, register
from core.plugin.plugin_registry import PluginPage, PageMenu
from core.chat.message_utils import KiraMessageEvent, KiraMessageBatchEvent
from core.chat import MessageChain
from core.chat.message_elements import Text, Image
from core.provider import LLMResponse, ToolResult

from .html_render import BrowserManager, render_html

try:
    from core.utils.path_utils import get_data_path
    _HAS_DATA_PATH = True
except Exception:  # pragma: no cover
    get_data_path = None
    _HAS_DATA_PATH = False

try:
    import aiohttp
    from aiohttp.resolver import ThreadedResolver
    _HAS_AIOHTTP = True
except Exception:  # pragma: no cover
    aiohttp = None
    ThreadedResolver = None
    _HAS_AIOHTTP = False

# ────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────

RANGES = ("session", "today", "d7", "d30", "total")
RANGE_LABELS = {"session": "本次", "today": "今天", "d7": "近7天", "d30": "近30天", "total": "累计"}

ERROR_TAG_RE = re.compile(r"出错[：:]")

# ── 后台日志（log.log）ERROR 行扫描 ──
# 捕捉 KiraAI 控制台/日志文件里的真实错误，重点：LLM 输出错误 XML 格式导致解析失败
LOG_ERROR_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+ERROR\s+\[([^\]]+)\]\s*(.*)$")
# 分类：XML 解析失败（LLM 输出错误格式）→ 最关心的
LOG_ERR_XML_RE = re.compile(r"Error parsing message|Failed to fix xml|mismatched tag|unclosed token|no element found|unexpected end|junk after document")
# 分类：模型调用失败（Provider 层）
LOG_ERR_MODEL_RE = re.compile(r"Model .* failed|ProviderError|All models in the group failed")
# 分类：工具执行失败
LOG_ERR_TOOL_RE = re.compile(r"Tool '.*' timed out|Tool .* failed|tool execution failed|Permission denied|Access denied|权限不足|无权限|拒绝访问|Forbidden|not allowed")
# 分类：网络/超时
LOG_ERR_NET_RE = re.compile(r"超时|timed out|timeout|Connection error|Connection reset")
# 分类：Traceback（未分类异常堆栈）
LOG_ERR_TB_RE = re.compile(r"Traceback \(most recent call last\)")
LOG_ERR_CATS = ("xml", "model", "tool", "net", "traceback", "other")
LOG_ERR_LABELS = {"xml": "XML解析", "model": "模型调用", "tool": "工具执行", "net": "网络/超时", "traceback": "异常堆栈", "other": "其他"}

# ── 工具结果失败判定（on.tool_result 钩子）──
# 框架（core/agent/func_tool_manager.py）执行工具失败时必包成 {"error": ...} JSON：
#   超时 → {"error": "Tool 'x' timed out after Ns"}
#   异常 → {"error": "Failed to call tool 'x': ..."}
#   未实现 → {"error": "Tool x not implemented"}
# 因此主判定 = {"error": ...} JSON 结构（100% 可靠，非字符串猜测）。
# 补充：工具自身返回的权限类错误（正常回答开头不会出现）与 "Error:" 前缀。
# 注意：error 字段值须为非零数字（含负数/小数）/非空字符串/true 才算失败
# （{"error": 0} 是很多 API 的成功约定）；不匹配裸 403（"第403条"会误报）。
# 不再匹配裸"超时/失败"或"动词+失败词"——bot 正常回答里提到
# "如果响应超时/查询失败会怎样"等说明文字会误判；框架失败已由 JSON 覆盖。
TOOL_ERR_RE = re.compile(
    r"\{['\"]?error['\"]?\s*:\s*(?:-?(?:[1-9]\d*(?:\.\d+)?|0\.\d*[1-9]\d*)|['\"](?![+-]?0+(?:\.0+)?['\"])[^\"\\]+['\"]|true|True)\s*[,}]|"
    r"Error\s*:|Permission denied|Access denied|权限不足|无权限|拒绝访问|"
    r"Forbidden|HTTP\s*403|status\s*[=:]\s*403|not allowed",
    re.IGNORECASE)

# 本插件自身工具的正常输出前缀：摘要/聚合/明细文本里含"失败/出错"字样
# （如"工具结果失败：N 次"、"最近出错：…"）但并非工具执行失败，排除自报避免统计污染
_SELF_TOOL_RE = re.compile(r"^(?:【Token 用量统计】|【用量·|【最近轮次】|已发送渲染概览图到会话)")

# LLM 响应内自报片段剥离：bot 转述本插件统计结果时带「出错：」字样
# （如"最近出错：Merge facts error"、"出错标记合计：3"、"工具结果失败：2 次"、
# "后台日志错误：…"），并非真实错误，扫描前剥离避免自增；
# 非贪婪+标点边界：只吃到句号/感叹号/问号/换行/下一个「出错」标记即停，
# 避免吞掉后面的真实错误；出错标记合计/工具结果失败是固定数字格式，精确匹配不吞内容
_SELF_REPORT_RE = re.compile(
    r"最近出错：[^\n]*?(?=[。！？\n]|出错[：:]|$)"
    r"|出错标记合计：\d+"
    r"|工具结果失败：\d+\s*次"
    r"|后台日志错误：[^\n]*?(?=[。！？\n]|出错[：:]|$)"
)

# AI 回注文本硬上限（正常结果 1-2K 字符，防御性兜底防 Poke 回注撑爆上下文）
AI_OUTPUT_LIMIT = 4000

# 估算型余额源类型 / 积分制类型
EST_TYPES = ("preset", "daily", "rolling")
POINT_TYPES = ("daily", "rolling")

# 内置默认价格规则（DeepSeek 官方价，2026-08 抓取；可在配置页修改）
DEFAULT_RULES = [
    {
        "name": "DeepSeek V4-Flash（官方价）",
        "model_match": "flash",
        "currency": "CNY",
        "peak_enabled": True,
        "hit_peak": 0.10, "hit_off": 0.05,
        "miss_peak": 3.0, "miss_off": 1.5,
        "out_peak": 9.0, "out_off": 4.5,
    },
    {
        "name": "DeepSeek V4-Pro（官方价）",
        "model_match": "pro",
        "currency": "CNY",
        "peak_enabled": True,
        "hit_peak": 0.30, "hit_off": 0.15,
        "miss_peak": 9.0, "miss_off": 4.5,
        "out_peak": 27.0, "out_off": 13.5,
    },
]

# 余额 auto 模式：官方端点分流域名关键字
_DS_HOSTS = ("deepseek",)
_MS_HOSTS = ("moonshot", "kimi")
_SF_HOSTS = ("siliconflow",)
_ZP_HOSTS = ("bigmodel", "zhipu")


# ────────────────────────────────────────────────────────────
# 工具函数
# ────────────────────────────────────────────────────────────

def _fmt_num(v):
    """千分位格式化（兼容 Python <3.10：N 格式符 3.10+ 才有）"""
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        return "0"
    return f"{v:,}"


def _fmt4(v):
    """超长自动缩写：K(千)/M(百万)/亿/B(十亿)，沿用 995 平滑进位风格。
    与 dashboard/widget 内嵌 JS fmt4 规则完全一致（取整用 int(x+0.5) 对齐 JS Math.round）"""
    try:
        v = max(0, int(v or 0))
    except (TypeError, ValueError):
        return "0"
    if v < 1000:
        return str(v)
    if v < 9950:
        return f"{v/1000:.1f}".replace(".0", "") + "K"
    if v < 995000:
        return str(int(v / 1000 + 0.5)) + "K"
    if v < 9950000:
        return f"{v/1000000:.1f}".replace(".0", "") + "M"
    if v < 99500000:
        return str(int(v / 1000000 + 0.5)) + "M"
    if v < 995000000:
        return f"{v/100000000:.1f}".replace(".0", "") + "亿"
    if v < 9950000000:
        return str(int(v / 100000000 + 0.5)) + "亿"
    if v < 99500000000:
        return f"{v/1000000000:.1f}".replace(".0", "") + "B"
    return str(int(v / 1000000000 + 0.5)) + "B"


def _fmt_dur(v):
    """单轮 LLM 耗时（秒）格式化：与 dashboard/widget 内嵌 JS fmtDur 规则完全一致。
    <10s → 4.56s；<60s → 23.4s；<60m → 1m23s；≥1h → 1h05m；无值/非法 → —"""
    if v is None:
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if v != v or v < 0:  # NaN / 负值防御
        return "—"
    if v < 10:
        return f"{v:.2f}s"
    if v < 60:
        return f"{v:.1f}s"
    if v < 3600:
        return f"{int(v // 60)}m{int(v % 60):02d}s"
    return f"{int(v // 3600)}h{int(v % 3600 // 60):02d}m"


def _esc(s) -> str:
    """HTML 转义（渲染图模板用）"""
    return html.escape(str(s or ""), quote=False)


def _rec_dur(rec: dict):
    """从记录取耗时 d（秒，float）；无字段/非法 → None（兼容老记录）"""
    d = rec.get("d")
    if d is None:
        return None
    try:
        d = float(d)
    except (TypeError, ValueError):
        return None
    return d if d == d and d >= 0 else None


def _dur_acc(b: dict, d):
    """向聚合桶累计单轮耗时：维护 ds(和)/dc(数)/dmin/dmax；d=None 跳过"""
    if d is None:
        return
    b["ds"] = b.get("ds", 0.0) + d
    b["dc"] = b.get("dc", 0) + 1
    if b.get("dmin") is None or d < b["dmin"]:
        b["dmin"] = d
    if b.get("dmax") is None or d > b["dmax"]:
        b["dmax"] = d


def _dur_stats(b: dict):
    """聚合桶 → (avg, min, max)；无耗时数据（dc=0）→ (None, None, None)"""
    cnt = (b or {}).get("dc", 0)
    if not cnt:
        return None, None, None
    return b.get("ds", 0.0) / cnt, b.get("dmin"), b.get("dmax")


def _dur_fields(b: dict) -> dict:
    """聚合桶 → 下发字段 {da(avg秒), dmin, dmax}，无数据时全 None"""
    a, mn, mx = _dur_stats(b)
    return {"da": a, "dmin": mn, "dmax": mx}


def _parse_hhmm(s, default_h=0, default_m=0):
    """解析 HH:mm → (时, 分)；失败返回默认"""
    try:
        h, m = str(s or "").strip().split(":")
        return int(h), int(m)
    except Exception:
        return default_h, default_m


# 峰谷方案库：每条计价规则通过 peak_profile 字段引用方案名（留空=默认方案）。
# 方案格式：{"name": "默认工作日", "windows": [["09:00","12:00"],["14:00","18:00"]]}
# 可在 WebUI「价格规则」页管理方案库；热重载后历史按各规则引用的方案重新分桶计价
_PEAK_PROFILES = [
    {"name": "默认工作日", "windows": [("09:00", "12:00"), ("14:00", "18:00")]},
]


def _is_peak_in(t: datetime, windows) -> bool:
    """按窗口列表判断是否峰时（分钟级精度）；周末恒谷"""
    if t.weekday() >= 5:
        return False
    tm = t.hour * 60 + t.minute
    for ws, we in windows or []:
        sh, sm = _parse_hhmm(ws)
        eh, em = _parse_hhmm(we)
        if sh * 60 + sm <= tm < eh * 60 + em:
            return True
    return False


def _windows_for_rule(r: dict):
    """规则引用的峰谷方案窗口；未指定/找不到 → 默认方案"""
    ref = (r or {}).get("peak_profile") or ""
    if ref:
        for p in _PEAK_PROFILES:
            if p.get("name") == ref:
                return p.get("windows") or []
    return _PEAK_PROFILES[0]["windows"] if _PEAK_PROFILES else []


def _is_peak(t: datetime) -> bool:
    """默认方案判断（兼容旧调用）"""
    return _is_peak_in(t, _PEAK_PROFILES[0]["windows"] if _PEAK_PROFILES else [])


def _url_loose_match(candidate: str, pattern: str) -> bool:
    if not candidate or not pattern:
        return False
    c, p = candidate.lower(), pattern.lower()
    return p in c or c in p


# exact_match 全字匹配开关（插件 __init__ 时注入，默认 False=包含匹配）
_EXACT_MATCH = False


def _match_rule(rules: list, channel: str, model: str, url: str):
    """URL > 模型 > 渠道名 加权匹配：4/2/1 分，取最高分。
    _EXACT_MATCH=True 时匹配需全字相等（区分大小写），否则包含匹配"""
    exact = _EXACT_MATCH
    best, best_score = None, 0
    for r in rules or []:
        if r.get("enabled") is False:
            continue
        score = 0
        if exact:
            if r.get("url_match") and str(url or "") == str(r.get("url_match") or ""):
                score += 4
            if r.get("model_match") and str(model or "") == str(r.get("model_match") or ""):
                score += 2
            if r.get("channel_match") and str(channel or "") == str(r.get("channel_match") or ""):
                score += 1
        else:
            if r.get("url_match") and _url_loose_match(url or "", r.get("url_match") or ""):
                score += 4
            if r.get("model_match") and model and r["model_match"].lower() in str(model).lower():
                score += 2
            if r.get("channel_match") and channel and r["channel_match"].lower() in str(channel).lower():
                score += 1
        if score > best_score:
            best_score, best = score, r
    return best


def _rule_currency(r: dict) -> str:
    """规则计价币种：'积分'=积分/百万tokens，其余一律 CNY"""
    cur = (r or {}).get("currency") or "CNY"
    return "积分" if str(cur).strip() == "积分" else "CNY"


def _rule_unit(r: dict) -> str:
    """规则显示单位：显式 unit 优先（含空串=无单位只显示数字）；
    默认 CNY→¥、积分→积分"""
    if r is None:
        return ""
    if "unit" in r:
        return str(r.get("unit") or "").strip()
    return "¥" if _rule_currency(r) != "积分" else "积分"


def _rule_cost_ex(r: dict, input_t: int, output_t: int, cached_t: int, t: datetime):
    """按规则算费用 → (金额, 币种)；无规则/未匹配返回 (None, 'CNY')"""
    if r is None:
        return None, "CNY"
    peak = bool(r.get("peak_enabled", True)) and _is_peak_in(t, _windows_for_rule(r))
    hit = r.get("hit_peak" if peak else "hit_off", 0) or 0
    miss = r.get("miss_peak" if peak else "miss_off", 0) or 0
    out = r.get("out_peak" if peak else "out_off", 0) or 0
    amt = (cached_t * hit + max(0, input_t - cached_t) * miss + output_t * out) / 1_000_000
    return amt, _rule_currency(r)


def _rule_cost(r: dict, input_t: int, output_t: int, cached_t: int, t: datetime) -> float:
    """单币种包装（仅 CNY 金额，用于旧接口兼容）"""
    amt, _ = _rule_cost_ex(r, input_t, output_t, cached_t, t)
    return amt


# 文件 IO 线程锁（日志追加/裁剪/热读缓存并发保护）
_IO_LOCK = threading.RLock()


def _mask_api_key(key: str) -> str:
    """API Key 掩码：sk-****xxxx（短 key 全掩码）"""
    if not key:
        return ""
    if len(key) <= 7:
        return "****"
    head = key[:3] if "-" not in key[:6] else key[:key.index("-") + 1]
    return f"{head}****{key[-4:]}"


def _read_jsonl(path: Path):
    recs = []
    if not path.exists():
        return recs
    try:
        with _IO_LOCK:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return recs


# JSONL 行数内存计数（启动时数一次，此后增量），避免每写一条全量读盘
_JSONL_LINES: dict = {}
# 发生过裁剪的日志文件集合（估算余额据此提示早期计费缺失）
_LOG_TRIMMED: set = set()


def _append_jsonl(path: Path, rec: dict, max_size: int = 0):
    try:
        with _IO_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            # 裁剪：超过 max_size 20% 才裁一次，只保留尾部（0 = 不裁剪）；
            # 行数走内存计数（首次读一次盘），避免每条都全量读盘 + O(N²) 重写
            if max_size and max_size > 0:
                key = str(path)
                n = _JSONL_LINES.get(key)
                if n is None:
                    try:
                        n = len(path.read_text(encoding="utf-8").splitlines())
                    except Exception:
                        n = 0
                n += 1
                if n > max_size * 1.2:
                    lines = path.read_text(encoding="utf-8").splitlines()
                    if len(lines) > max_size:
                        path.write_text("\n".join(lines[-max_size:]) + "\n", encoding="utf-8")
                        _LOG_TRIMMED.add(key)
                        n = max_size
                _JSONL_LINES[key] = n
    except Exception as e:
        logger.warning(f"[token_stats] 日志写入失败: {e}")


def _parse_ts(s: str):
    """日志时间戳解析：兼容 3/6 位微秒（Python 3.10- 的 fromisoformat 只认 6 位）；
    带时区的 aware 时间统一去 tzinfo（丢弃偏移），避免与本地 naive 时间比较炸 TypeError"""
    if not s:
        raise ValueError("empty timestamp")
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        pass
    if dt is None and "." in s:
        try:
            head, frac = s.split(".", 1)
            frac = (frac + "000000")[:6]
            dt = datetime.fromisoformat(f"{head}.{frac}")
        except ValueError:
            pass
    if dt is None:
        raise ValueError(f"bad timestamp: {s}")
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _fmt_ts(dt: datetime) -> str:
    """日志时间戳写入：统一 6 位微秒，避免低版本 Python 解析失败"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _err_scan(full_text: str, prev):
    """errScanPos：位置游标错误计数。
    传入 (prev_text, prev_end)；新文本若以旧文本为前缀（续写/追加），
    只统计新增段，工具循环同一段「出错：」不重复计数；
    全新响应（前缀不匹配）整段重数并重置游标。
    返回 (新增计数, 新游标状态)。"""
    if prev is None:
        n = len(ERROR_TAG_RE.findall(full_text))
        return n, (full_text, len(full_text))
    prev_text, prev_end = prev
    if len(full_text) >= prev_end and full_text[:prev_end] == prev_text[:prev_end]:
        return len(ERROR_TAG_RE.findall(full_text[prev_end:])), (full_text, len(full_text))
    n = len(ERROR_TAG_RE.findall(full_text))
    return n, (full_text, len(full_text))


def _clamp_ai_output(s: str) -> str:
    """AI 回注结果硬上限，防止 Poke 回注撑爆上下文"""
    if len(s) <= AI_OUTPUT_LIMIT:
        return s
    return s[:AI_OUTPUT_LIMIT - 60] + "\n…（结果过长已截断，请缩小范围或减少 top/n）"


def _last_refresh(now: datetime, refresh_time: str) -> datetime:
    """上次刷新时刻（纯时间推导，不落状态）：客户端离线期间发放不丢"""
    h, m = _parse_hhmm(refresh_time)
    today_refresh = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now >= today_refresh:
        return today_refresh
    return today_refresh - timedelta(days=1)


def _grant_count(since: datetime, now: datetime, refresh_time: str) -> int:
    """since（严格大于）之后到 now 的刷新发放次数（除法直算，不逐日循环）"""
    h, m = _parse_hhmm(refresh_time)
    first = since.replace(hour=h, minute=m, second=0, microsecond=0)
    if first <= since:
        first += timedelta(days=1)
    last = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if last > now:
        last -= timedelta(days=1)
    if first > last:
        return 0
    return (last.date() - first.date()).days + 1
# ────────────────────────────────────────────────────────────
# 插件主类
# ────────────────────────────────────────────────────────────

class TokenStatsPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        # ── 基础 ──
        basic = cfg.get("section_basic", {})
        self.enabled = basic.get("enabled", True)
        self.debug_log = bool(basic.get("debug_log", False))
        self.source_rules = basic.get("source_rules", {}) or {}
        if not isinstance(self.source_rules, dict):
            self.source_rules = {}

        # ── 来源归类 ──
        src = cfg.get("section_source", {})
        # 默认来源：支持 string（旧配置兼容）或 list（多标签，取第一个为主标签）
        self.source_default = self._src_label(src.get("source_default", "system"), "system")
        # 群聊默认标签对齐 KiraAI 会话类型（qq:gm:xxx），而非自定义的 qchat
        self.source_group = self._src_label(src.get("source_group", "gm"), "gm")
        self.source_dm = self._src_label(src.get("source_dm", "dm"), "dm")

        # ── 自定义命令 ──
        cmd = cfg.get("section_command", {})
        self.enable_command = bool(cmd.get("enable_command", False))
        self.command_words = cmd.get("command_words", ["/用量", "/token"]) or ["/用量"]
        self.allowed_users = [str(u).strip() for u in (cmd.get("allowed_users", []) or []) if str(u).strip()]
        self.exact_match = bool(cmd.get("exact_match", False))
        self.denied_message = cmd.get("denied_message", "权限不足：您没有查询用量统计的权限")
        self.cmd_success_template = cmd.get("command_success_template", "📊 {provider}：{result}")

        # ── Bot 工具 ──
        tool = cfg.get("section_tool", {})
        self.enable_tool = bool(tool.get("enable_tool", True))
        self.tool_include_balance = bool(tool.get("tool_include_balance", True))
        # 渲染图模式：默认关（LLM 显式要求才发图）；开启后 query_token_stats 默认发渲染图
        self.tool_render_image = bool(tool.get("tool_render_image", False))
        # 渲染图是否显示余额（默认开；涉及账户余额，用户可自行关闭更安全）
        self.tool_render_balance = bool(tool.get("tool_render_balance", True))
        self._browser = BrowserManager()

        # ── 价格规则 ──
        pr = cfg.get("section_pricing", {})
        rules = pr.get("rules", None)
        # 空数组也保留（用户删光规则 → 费用显示「—」），仅 None/非 list 回退默认
        self.rules = rules if isinstance(rules, list) else DEFAULT_RULES
        # 峰谷方案库：每条规则可选引用（peak_profile 字段）；兼容旧 peak_windows 全局配置
        global _PEAK_PROFILES
        profiles = pr.get("peak_profiles", None)
        if isinstance(profiles, list) and profiles:
            cleaned_p = []
            for p in profiles:
                if not isinstance(p, dict) or not str(p.get("name") or "").strip():
                    continue
                w = p.get("windows")
                if not isinstance(w, list) or not w:
                    continue
                cleaned_w = []
                for item in w:
                    if not (isinstance(item, (list, tuple)) and len(item) == 2):
                        continue
                    sh, sm = _parse_hhmm(item[0])
                    eh, em = _parse_hhmm(item[1])
                    if sh * 60 + sm < eh * 60 + em:
                        cleaned_w.append((f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"))
                if cleaned_w:
                    cleaned_p.append({"name": str(p["name"]).strip(), "windows": cleaned_w})
            if cleaned_p:
                _PEAK_PROFILES = cleaned_p
        else:
            # 兼容旧配置：全局 peak_windows → 默认方案
            pw = pr.get("peak_windows", None)
            if isinstance(pw, list) and pw:
                cleaned_w = []
                for item in pw:
                    if not (isinstance(item, (list, tuple)) and len(item) == 2):
                        continue
                    sh, sm = _parse_hhmm(item[0])
                    eh, em = _parse_hhmm(item[1])
                    if sh * 60 + sm < eh * 60 + em:
                        cleaned_w.append((f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"))
                if cleaned_w:
                    _PEAK_PROFILES = [{"name": "默认工作日", "windows": cleaned_w}]

        # ── 余额监测 ──
        bal = cfg.get("section_balance", {})
        self.enable_balance = bool(bal.get("enable_balance", True))
        interval = bal.get("balance_interval", 600)
        self.balance_interval = max(60, int(interval) if interval is not None else 600)
        # 旧版默认 10 秒轮询自动迁移：10 秒是对第三方站点的持续负载（多实例聚合不小），
        # 且余额是慢变量，600 秒检测零感知损失。未主动改过的（配置里还是旧默认 10）统一迁到 600；
        # 主动设置的其他值保留。bot 主动/用户立即检测仍即时探测，不受轮询间隔影响
        if interval == 10:
            self.balance_interval = 600
            logger.info("[token_stats] 余额轮询默认间隔已从 10s 自动迁移到 600s（良性轮询）")
        # 会话昵称解析（OneBot）缓存与适配器
        self._ada_obj = None
        self._name_cache = {}
        sources = bal.get("balance_sources", [])
        # 复制一份再 append，避免直接引用 cfg 原始 list（热重载时重复追加）
        self.balance_sources = list(sources) if isinstance(sources, list) else []
        # 派生源去重兜底：历史配置可能已被污染（派生源曾被整体写回 balance_sources），
        # append 派生源前按 (type,url,name) 查重，已存在则跳过
        def _bal_src_exists(name, url):
            for ex in self.balance_sources:
                if ex.get("name") == name and (not url or (ex.get("url") or "") == url):
                    return True
            return False
        # New-API 站点简易文本格式（对齐 api-balance 插件）：每行 名称;base_url;令牌;用户ID;换算比例(可选)
        simple_sec = cfg.get("section_balance_newapi_simple", {}) or {}
        simple_list = simple_sec.get("newapi_sites_simple", [])
        if isinstance(simple_list, list):
            for line in simple_list:
                if not line or not str(line).strip():
                    continue
                parts = [p.strip() for p in str(line).split(";")]
                if len(parts) < 4:
                    continue
                name = parts[0] or "未命名站点"
                base_url = parts[1].rstrip("/")
                api_key = parts[2]
                api_user = parts[3]
                conversion = parts[4] if len(parts) >= 5 and parts[4].strip() else "500000"
                try:
                    conversion = float(conversion)
                except (TypeError, ValueError):
                    conversion = 500000
                if not base_url or not api_key or not api_user:
                    continue
                if _bal_src_exists(name, base_url):
                    continue
                self.balance_sources.append({
                    "name": name,
                    "type": "newapi",
                    "url": base_url,
                    "api_key": api_key,
                    "api_user": api_user,
                    "quota_conversion": conversion,
                    "enabled": True,
                    "_origin": "simple",
                })
        # 固定平台快捷配置（对齐 api-balance 插件风格）：启用 + 填 key 即自动并入余额源
        for key, dname, durl in (
                ("deepseek", "DeepSeek", "https://api.deepseek.com"),
                ("moonshot", "月之暗面 Kimi", "https://api.moonshot.cn/v1"),
                ("siliconflow", "硅基流动", "https://api.siliconflow.cn"),
                ("zhipu", "智谱", "https://open.bigmodel.cn/api/paas/v4")):
            sec = cfg.get(f"section_balance_{key}", {}) or {}
            if sec.get("enabled") and sec.get("api_key"):
                qname = (sec.get("name") or dname).strip()
                qurl = (sec.get("base_url") or durl).strip()
                if _bal_src_exists(qname, qurl):
                    continue
                self.balance_sources.append({
                    "name": qname,
                    "type": "auto",
                    "url": qurl,
                    "api_key": sec.get("api_key"),
                    "enabled": True,
                    "_origin": f"quick:{key}",
                })
        self.balance_unit = (bal.get("balance_unit", "元") or "元").strip() or "元"

        # ── 高级 ──
        adv = cfg.get("section_advanced", {})
        max_log = adv.get("max_log_size", 100000)
        self.max_log_size = int(max_log) if max_log is not None else 100000
        idle = adv.get("session_idle_minutes", 30)
        self.session_idle_minutes = max(1, int(idle) if idle is not None else 30)
        expire = adv.get("session_expire_minutes", 30)
        # 会话内临时状态（来源继承/错误游标）无活动清理时间，秒；最小 1 分钟
        self.session_expire_seconds = max(1, int(expire) if expire is not None else 30) * 60

        # ── 挂件（WebUI 悬浮小卡片，默认关闭）──
        wid = cfg.get("section_widget", {})
        self.enable_widget = bool(wid.get("enable_widget", True))
        self.widget_compact = bool(wid.get("widget_compact", False))

        # ── 预警提醒 ──
        al = cfg.get("section_alert", {})
        self.enable_alert = bool(al.get("enable_alert", True))
        deliveries = al.get("alert_deliveries", [])
        self.alert_deliveries = list(deliveries) if isinstance(deliveries, list) else []
        rules = al.get("alert_rules", [])
        self.alert_rules = list(rules) if isinstance(rules, list) else []
        # 旧版默认话语自动迁移：v1.4.2 前默认模板（"⚠️ xxx预警：xxx 已达 xxx" / "📊 今日 tokens 已达 xxx"）
        # 被用户手动保存过的话，清空让新版默认生效（step 带「每新增 N 提醒一次」、llm 带「请告知用户情况」）；
        # 自定义话语（不含"预警："格式）保留
        for _r in self.alert_rules:
            if isinstance(_r, dict):
                _m = str(_r.get("message") or "").strip()
                if not _m or "每新增" in _m or "请告知用户情况" in _m:
                    continue
                if ("预警：" in _m and "已达" in _m) or "今日 tokens 已达" in _m:
                    _r["message"] = "" 
        # 已触发状态：{rule_id: {count, at, day}}（balance 滞回：余额回升超过阈值才重置；
        # token/cost 单调递增：达阈值触发一次，跨天自动重置）
        self._alert_fired = {}
        # token/cost 当天统计缓存：{(len,last_t,rules_hash,target,session): (v, units)}
        self._alert_usage_cache = {}
        self._alert_task: asyncio.Task = None
        self._alert_lock = asyncio.Lock()

        # ── 余额探测 ssl ──
        self.balance_ssl_verify = bool(bal.get("balance_ssl_verify", False))

        # ── 运行时状态 ──
        self._data_dir: Path = None  # initialize 时赋值
        self._log_path: Path = None
        self._lock = asyncio.Lock()

        # 按天聚合：{day: {r,v,i,o,c,e, aggs:{model\u001Fchannel\u001Fhost:[off,peak]}}}
        self._days = {}
        # 单天小时桶：{day: [None]*24 each {r,v,i,o,c,e, aggs}}（aggs 供小时级费用分色）
        self._hours = {}
        # 5 分钟桶：{day: [None]*24 each [None]*12 each {r,v,i,o,c,e, aggs}}（时间趋势最深层下钻）
        self._mins = {}
        # 会话窗口（滚动）：按 sid 各维护一个有界 LRU 窗口（多群交替互不清零）；
        # self._sess 始终指向最近活跃会话的窗口（/stats 展示用）
        self._sess_map = {}
        self._sess_map_max = 20
        self._sess = {
            "start": time.time(), "last": time.time(),
            "r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0,
            "aggs": {}, "sid": "", "ds": 0.0, "dc": 0, "dmin": None, "dmax": None,
        }
        self._sess_map[""] = self._sess
        self._last_err_text = ""
        self._last_err_at = None
        self._cur_source = self.source_default
        self._cur_channel = "默认渠道"
        self._cur_model = "未知"
        self._last_round = {"i": 0, "o": 0, "c": 0}

        # 来源继承：{sid: {"text": str, "source": str, "steps": int, "at": float}}
        self._pending = {}

        # 余额状态：{name: {balance,currency,at,ok,msg}}
        self._bal_states = {}
        self._bal_busy = False
        self._bal_task: asyncio.Task = None

        # 热读缓存（4.9.x）：按 mtime+length 判失效；多端点轮询共享
        self._rec_cache = {"path": None, "mtime": None, "len": -1, "list": None}

        # 费用/估算计算缓存：记录数+最大时间戳+规则 hash 未变则直接复用（H1/H3）
        self._rules_hash = ""
        self._since_cost_cache = {}   # (url,name,model_ref,since_iso) → (fingerprint, cny, pts, matched)
        self._range_scan_cache = {}   # (frm,to) → (fingerprint, result)

        # 出错统计游标（errScanPos）：{sid: (prev_text, prev_end)}，工具循环续轮不重复计数
        self._err_cursor = {}

        # 后台日志（log.log）ERROR 扫描状态（按文件身份 st_ino 跟踪游标，轮转改名不丢不重）
        self._log_err_inodes = {}          # {st_ino: pos} 文件身份 → 字节游标
        self._log_err_hist = {}            # {day: {cat: count}} 按天分类聚合
        self._log_err_last = {"at": "", "cat": "", "text": ""}  # 最近一条 ERROR
        self._log_err_task: asyncio.Task = None
        self._log_err_lock = asyncio.Lock()
        self._err_save_at = 0.0            # 错误统计持久化节流时间戳

        # 工具结果失败统计（on.tool_result 钩子）：{day: count}
        self._tool_err_hist = {}
        # 注：ToolResult 无工具名字段，只记时间+文本
        self._tool_err_last = {"at": "", "text": ""}

        # exact_match 透传：模块级 flag（单实例插件可接受），匹配器全插件生效
        global _EXACT_MATCH
        _EXACT_MATCH = self.exact_match

    # ── 生命周期 ──

    async def initialize(self):
        self._data_dir = self.ctx.get_plugin_data_dir()
        if self._data_dir is None:
            # data_dir 不可用时兜底到插件包目录（只读退化，避免整个插件挂掉）
            self._data_dir = Path(__file__).resolve().parent
            logger.warning("[token_stats] get_plugin_data_dir() 返回 None，降级使用插件目录")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._data_dir / "usage-log.jsonl"
        self._bal_state_path = self._data_dir / "balance_state.json"

        self._load_history()
        self._load_bal_states()
        self._load_err_stats()

        # 渲染图模式：后台检测浏览器（系统 Chrome/Edge → 内置 Chromium → 自动下载），不阻塞加载
        asyncio.ensure_future(self._browser.initialize())
        logger.info("[token_stats] 渲染图浏览器检测已在后台启动")

        # 后台日志 ERROR 扫描：扫描 data 目录下 log.log*（含轮转文件），按 ino 增量
        self._log_err_task = asyncio.create_task(self._log_err_loop())
        logger.info("[token_stats] 后台日志 ERROR 扫描已启动（log.log* 含轮转）")

        if self.debug_log:
            logger.info(f"[token_stats] init: rules={len(self.rules)} balance_sources={len(self.balance_sources)} "
                        f"command={'on' if self.enable_command else 'off'} tool={'on' if self.enable_tool else 'off'}")

        # 余额轮询后台任务
        if self.enable_balance and self.balance_sources and _HAS_AIOHTTP:
            self._bal_task = asyncio.create_task(self._balance_loop())
            logger.info(f"[token_stats] 余额轮询已启动（间隔 {self.balance_interval} 秒）")
        elif self.enable_balance and self.balance_sources and not _HAS_AIOHTTP:
            logger.warning("[token_stats] aiohttp 未安装，余额监测不可用（pip install aiohttp）")

        # 预警提醒后台任务（余额类规则随轮询检查；token/cost 类在 on_llm_response 实时检查）
        if self.enable_alert and self.alert_rules:
            self._alert_task = asyncio.create_task(self._alert_loop())
            logger.info(f"[token_stats] 预警提醒已启动（{len(self.alert_rules)} 条规则）")

        logger.info("[token_stats] Token 用量统计已就绪")

    async def terminate(self):
        if self._bal_task and not self._bal_task.done():
            self._bal_task.cancel()
            try:
                await self._bal_task
            except asyncio.CancelledError:
                pass
            self._bal_task = None
        if self._alert_task and not self._alert_task.done():
            self._alert_task.cancel()
            try:
                await self._alert_task
            except asyncio.CancelledError:
                pass
            self._alert_task = None
        if self._log_err_task and not self._log_err_task.done():
            self._log_err_task.cancel()
            try:
                await self._log_err_task
            except asyncio.CancelledError:
                pass
            self._log_err_task = None
        # 退出前强制落盘错误统计（绕过节流，热重载/重启不丢）
        self._maybe_save_err_stats(force=True)

    # ── 历史加载 ──

    def _load_history(self):
        self._days.clear()
        self._hours.clear()
        self._mins.clear()
        for rec in self._read_records():
            self._apply_rec(rec)
        logger.info(f"[token_stats] 已加载历史 {len(self._days)} 天 / {sum(d['v'] for d in self._days.values())} tokens")

    def _read_records(self):
        """带热读缓存的日志读取：mtime+length 判失效（主日志只追加，安全）"""
        if self._log_path is None:
            return []
        try:
            mtime = self._log_path.stat().st_mtime
            length = self._log_path.stat().st_size
        except Exception:
            mtime, length = None, -1
        c = self._rec_cache
        if c["path"] == str(self._log_path) and c["mtime"] == mtime and c["len"] == length and c["list"] is not None:
            return c["list"]
        recs = _read_jsonl(self._log_path)
        c["path"] = str(self._log_path)
        c["mtime"] = mtime
        c["len"] = length
        c["list"] = recs
        return recs

    def _invalidate_rec_cache(self):
        self._rec_cache["list"] = None

    def _calc_fingerprint(self, recs=None):
        """计算结果缓存指纹：记录数 + 最新时间戳 + 规则内容 hash，任一变化即失效重算"""
        if recs is None:
            recs = self._read_records()
        try:
            rh = hash(json.dumps(self.rules or [], sort_keys=True, ensure_ascii=False))
        except Exception:
            rh = id(self.rules)
        if rh != self._rules_hash:
            self._rules_hash = rh
        last_t = recs[-1].get("t", "") if recs else ""
        return (len(recs), last_t, rh)

    def _apply_rec(self, rec: dict):
        try:
            t = _parse_ts(rec.get("t", ""))
        except Exception:
            return
        day = t.strftime("%Y-%m-%d")
        v = int(rec.get("v", 0) or 0)
        i = int(rec.get("i", 0) or 0)
        o = int(rec.get("o", 0) or 0)
        c = int(rec.get("c", 0) or 0)
        e = int(rec.get("e", 0) or 0)
        d = _rec_dur(rec)
        # 分桶按该记录命中的规则所引用的峰谷方案（改方案后热重载 → 历史重分桶）
        _rule = _match_rule(self.rules, rec.get("ch", ""), rec.get("m", ""), rec.get("h", ""))
        peak = bool(_rule.get("peak_enabled", True)) and _is_peak_in(t, _windows_for_rule(_rule)) if _rule else _is_peak(t)
        key = f"{rec.get('m', '')}\u001F{rec.get('ch', '')}\u001F{rec.get('h', '')}"

        ds = self._days.setdefault(day, {"r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0, "aggs": {}})
        ds["r"] += 1; ds["v"] += v; ds["i"] += i; ds["o"] += o; ds["c"] += c; ds["e"] += e
        _dur_acc(ds, d)
        slots = ds["aggs"].setdefault(key, [None, None])
        agg = slots[1 if peak else 0]
        if agg is None:
            agg = slots[1 if peak else 0] = {"i": 0, "o": 0, "c": 0}
        agg["i"] += i; agg["o"] += o; agg["c"] += c

        hs = self._hours.setdefault(day, [None] * 24)
        hr = hs[t.hour]
        if hr is None:
            hr = hs[t.hour] = {"r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0, "aggs": {}}
        hr["r"] += 1; hr["v"] += v; hr["i"] += i; hr["o"] += o; hr["c"] += c; hr["e"] += e
        _dur_acc(hr, d)
        hslots = hr["aggs"].setdefault(key, [None, None])
        hagg = hslots[1 if peak else 0]
        if hagg is None:
            hagg = hslots[1 if peak else 0] = {"i": 0, "o": 0, "c": 0}
        hagg["i"] += i; hagg["o"] += o; hagg["c"] += c

        # 5 分钟桶（时间趋势最深层下钻）
        m5 = t.minute // 5
        ms = self._mins.setdefault(day, [None] * 24)
        mrow = ms[t.hour]
        if mrow is None:
            mrow = ms[t.hour] = [None] * 12
        mb = mrow[m5]
        if mb is None:
            mb = mrow[m5] = {"r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0, "aggs": {}}
        mb["r"] += 1; mb["v"] += v; mb["i"] += i; mb["o"] += o; mb["c"] += c; mb["e"] += e
        _dur_acc(mb, d)
        mslots = mb["aggs"].setdefault(key, [None, None])
        magg = mslots[1 if peak else 0]
        if magg is None:
            magg = mslots[1 if peak else 0] = {"i": 0, "o": 0, "c": 0}
        magg["i"] += i; magg["o"] += o; magg["c"] += c

    def _apply_session(self, rec: dict):
        now = time.time()
        sid = rec.get("sid", "") or ""
        # 按 sid 各维护会话窗口（LRU）：多群交替互不清零；同会话超 idle 才重置
        s = self._sess_map.get(sid)
        if s is None:
            s = {
                "start": now, "last": now,
                "r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0, "aggs": {}, "sid": sid,
                "ds": 0.0, "dc": 0, "dmin": None, "dmax": None,
            }
            self._sess_map[sid] = s
        else:
            # LRU：移到末尾（最近活跃）
            self._sess_map[sid] = self._sess_map.pop(sid)
            if now - s["last"] > self.session_idle_minutes * 60:
                s.update({"start": now, "r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0, "aggs": {},
                          "ds": 0.0, "dc": 0, "dmin": None, "dmax": None})
        while len(self._sess_map) > self._sess_map_max:
            self._sess_map.pop(next(iter(self._sess_map)))
        s["last"] = now
        s["sid"] = sid
        self._sess = s
        s["r"] += 1
        s["v"] += rec["v"]; s["i"] += rec["i"]; s["o"] += rec["o"]; s["c"] += rec["c"]
        s["e"] += rec.get("e", 0)
        _dur_acc(s, _rec_dur(rec))
        key = f"{rec.get('m', '')}\u001F{rec.get('ch', '')}\u001F{rec.get('h', '')}"
        slots = s["aggs"].setdefault(key, [None, None])
        try:
            rec_t = _parse_ts(rec["t"])
        except Exception:
            rec_t = datetime.now()
        _rule = _match_rule(self.rules, rec.get("ch", ""), rec.get("m", ""), rec.get("h", ""))
        peak = bool(_rule.get("peak_enabled", True)) and _is_peak_in(rec_t, _windows_for_rule(_rule)) if _rule else _is_peak(rec_t)
        agg = slots[1 if peak else 0]
        if agg is None:
            agg = slots[1 if peak else 0] = {"i": 0, "o": 0, "c": 0}
        agg["i"] += rec["i"]; agg["o"] += rec["o"]; agg["c"] += rec["c"]

    # ── 余额状态 ──

    def _load_bal_states(self):
        try:
            if self._bal_state_path.exists():
                data = json.loads(self._bal_state_path.read_text(encoding="utf-8"))
                # 顶层类型校验：文件被写坏成数组/字符串时回退空 dict，避免后续 .get 崩
                self._bal_states = data if isinstance(data, dict) else {}
        except Exception:
            self._bal_states = {}

    def _save_bal_states(self):
        try:
            tmp = self._bal_state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._bal_states, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._bal_state_path)  # 原子写：防中途断电写坏
        except Exception:
            pass

    # ── 来源 / 渠道识别 ──

    @staticmethod
    def _src_label(v, fallback: str) -> str:
        """来源标签归一化：string 直接用；list 取第一个非空项（多标签时主标签）；
        空/非法回退默认。"""
        if isinstance(v, list):
            for item in v:
                s = str(item or "").strip()
                if s:
                    return s
            return fallback
        s = str(v or "").strip()
        return s or fallback

    def _classify_source(self, sid: str, event) -> str:
        pending = self._pending.get(sid)
        text = (pending or {}).get("text", "") or ""
        # 自定义关键词规则优先
        if self.source_rules:
            lowered = text.lower()
            for kw, src in self.source_rules.items():
                if kw and kw.lower() in lowered:
                    return str(src)
        # 自动判定
        try:
            if event.is_group_message():
                return self.source_group
            return self.source_dm
        except Exception:
            pass
        return self.source_default

    def _resolve_channel_model(self):
        """从默认 LLM 客户端取 provider 名/模型名/host（KiraAI 结构：client.model = ModelInfo）
        防御式，失败回退默认值"""
        channel, model, host = "默认渠道", "未知", ""
        try:
            client = self.ctx.get_default_llm_client()
            mi = getattr(client, "model", None)
            if mi is not None:
                model = getattr(mi, "model_id", None) or "未知"
                pname = getattr(mi, "provider_name", None) or ""
                pcfg = getattr(mi, "provider_config", None) or {}
                if not isinstance(pcfg, dict):
                    pcfg = {}
                base_url = (pcfg.get("base_url") or pcfg.get("baseUrl")
                            or pcfg.get("url") or pcfg.get("endpoint") or "")
                if base_url:
                    try:
                        host = urlparse(base_url).hostname or ""
                    except Exception:
                        host = ""
                channel = pname or host or "默认渠道"
            else:
                # 兜底：直接属性
                model = (getattr(client, "model_id", None)
                         or getattr(client, "model_name", None) or "未知")
                mcfg = getattr(client, "model_config", None) or {}
                if not isinstance(mcfg, dict):
                    mcfg = {}
                base_url = (mcfg.get("base_url") or mcfg.get("baseUrl")
                            or mcfg.get("url") or mcfg.get("endpoint") or "")
                if base_url:
                    try:
                        host = urlparse(base_url).hostname or ""
                    except Exception:
                        host = ""
                channel = (getattr(client, "provider_id", None)
                           or getattr(client, "provider_name", None) or host or "默认渠道")
        except Exception:
            pass
        return channel, model, host

    def _sid(self, event) -> str:
        sid = getattr(event, "sid", None)
        if sid:
            return sid
        try:
            return event.session.sid
        except Exception:
            return "default"

    def _is_allowed(self, user_id: str) -> bool:
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

    def _sweep_stale_sessions(self):
        """清理长期无活动的会话临时状态（来源继承/错误游标），防内存缓慢增长"""
        try:
            cutoff = time.time() - self.session_expire_seconds
            stale = [sid for sid, p in self._pending.items() if p.get("at", 0) < cutoff]
            for sid in stale:
                self._pending.pop(sid, None)
                self._err_cursor.pop(sid, None)
        except Exception:
            pass

    # ── 事件钩子 ──

    @on.im_message(priority=Priority.HIGH)
    async def on_im_message(self, event: KiraMessageEvent, *_):
        """捕获用户文本（来源归类用）+ 自定义命令处理"""
        sid = self._sid(event)
        text = "".join(e.text for e in event.message.chain if isinstance(e, Text))
        if text:
            self._sweep_stale_sessions()
            self._pending[sid] = {"text": text, "source": None, "steps": 0, "at": time.time(), "new_msg": True}

        if not self.enable_command:
            return
        if not text:
            return
        text = text.strip()

        matched = False
        for cmd in self.command_words:
            if text == cmd or text.startswith(cmd + " "):
                matched = True
                break
        if not matched:
            return

        user_id = ""
        try:
            user_id = str(event.message.sender.user_id)
        except Exception:
            pass
        if not user_id:
            return
        if not self._is_allowed(user_id):
            await self.ctx.message_processor.send_message_chain(
                sid, MessageChain([Text(self.denied_message)]))
            event.discard(force=True)
            event.stop()
            return

        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        reply = await self._build_query_reply(arg)
        await self.ctx.message_processor.send_message_chain(sid, MessageChain([Text(reply)]))
        event.discard(force=True)
        event.stop()

    @on.llm_response(priority=Priority.LOW)
    async def on_llm_response(self, event, resp: LLMResponse, *_):
        if not self.enabled:
            return
        inp = resp.input_tokens
        out = resp.output_tokens
        if inp is None and out is None:
            return
        inp = int(inp or 0)
        out = int(out or 0)
        cached = int(resp.cached_tokens or 0)
        if inp == 0 and out == 0:
            return

        sid = self._sid(event)

        # 错误统计（errScanPos：位置游标，工具循环同一段「出错：」不重复计数）
        text = (resp.text_response or "") or ""
        pending = self._pending.get(sid)
        if pending is None:
            pending = self._pending[sid] = {"text": "", "source": None, "steps": 0, "at": time.time()}
        else:
            pending["at"] = time.time()  # 活动触碰：续轮不视为过期
        # 剥离本插件统计结果的自报片段（bot 转述"最近出错：…/出错标记合计：N"不算真实错误）
        text = _SELF_REPORT_RE.sub("", text)
        # 剥离用户消息原文（bot 引用/复述用户原话时不算错误）：
        # 仅当用户原话含「出错」且长度 >= 4 才剥离，避免误删 bot 自己的真实错误
        user_text = (pending.get("text") or "").strip()
        if user_text and len(user_text) >= 4 and "出错" in user_text:
            text = text.replace(user_text, "")
        errs, self._err_cursor[sid] = _err_scan(text, self._err_cursor.get(sid))
        if errs > 0:
            self._last_err_text = self._err_snippet(text)
            self._last_err_at = datetime.now()

        # 来源：第一轮（新用户消息）自动判定，工具续轮继承
        # 新用户消息到达（on_im_message 置位）：重置步数重新判定——
        # 关键词规则按「消息包含关键词」逐条生效，而非会话首条消息锁定
        if pending.get("new_msg"):
            pending["steps"] = 0
            pending["new_msg"] = False
        pending["steps"] += 1
        if pending["steps"] <= 1 or pending.get("source") is None:
            src = self._classify_source(sid, event)
        else:
            src = pending.get("source", self.source_default)
        pending["source"] = src
        self._cur_source = src

        channel, model, host = self._resolve_channel_model()
        self._cur_channel = channel
        self._cur_model = model

        now = datetime.now()
        rec = {
            "t": _fmt_ts(now),
            "v": inp + out,
            "i": inp, "o": out, "c": cached,
            "m": model, "s": src, "ch": channel, "h": host,
            "sid": sid,
        }
        if errs > 0:
            rec["e"] = errs
        # 单轮 LLM 耗时（秒，含工具中间步各算一条）；LLMResponse.time_consumed 可空，空则不写
        dur = _rec_dur({"d": getattr(resp, "time_consumed", None)})
        if dur is not None:
            rec["d"] = dur

        self._last_round = {"i": inp, "o": out, "c": cached}

        async with self._lock:
            self._apply_rec(rec)
            self._apply_session(rec)
            _append_jsonl(self._log_path, rec, self.max_log_size)
            self._invalidate_rec_cache()

        # 预警提醒：token/cost 类规则每轮实时检查（balance 类由后台循环检查）
        if self.enable_alert and self.alert_rules:
            try:
                for r in self.alert_rules:
                    if (r.get("type") or "balance").strip().lower() != "balance":
                        self._alert_check_rule(r)
            except Exception as e:
                logger.warning(f"[token_stats] 预警实时检查异常: {e}")

        if self.debug_log:
            logger.info(f"[token_stats] rec: +{inp}in/{out}out/{cached}cache "
                        f"src={src} ch={channel} model={model}")

    @on.tool_result(priority=Priority.LOW)
    async def on_tool_result(self, event, result: ToolResult, *_):
        """工具结果失败统计：框架失败（超时/异常/未实现，必包 {"error": ...} JSON）
        或工具返回的权限类错误——LLM 白烧 token 的典型"""
        if not self.enabled:
            return
        text = (getattr(result, "text", "") or "") or ""
        if not text:
            return
        # 排除本插件自身工具的正常输出：摘要/聚合/明细文本里含"失败/出错"字样
        # （如"工具结果失败：N 次"、"最近出错：…"）但并非工具执行失败，避免自报污染统计
        if _SELF_TOOL_RE.match(text):
            return
        # 只检查文本开头 200 字符：真实工具失败几乎总是以错误信息开头；
        # bot 正常回答长文本中间提到"超时/失败"（如"如果响应超时可能失败"）是说明文字，不误判
        if not TOOL_ERR_RE.search(text[:200]):
            return
        day = datetime.now().strftime("%Y-%m-%d")
        self._tool_err_hist[day] = self._tool_err_hist.get(day, 0) + 1
        self._tool_err_last = {
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "text": text.replace("\r", " ").replace("\n", " ").strip()[:160],
        }
        if self.debug_log:
            logger.info(f"[token_stats] 工具结果失败 +1: {self._tool_err_last['text'][:60]}")

    @staticmethod
    def _err_snippet(text: str) -> str:
        idx = text.find("出错")
        if idx < 0:
            return ""
        start = max(0, idx - 20)
        s = text[start:idx + 40].replace("\r", " ").replace("\n", " ").strip()
        return s[:60]
    # ── 聚合查询（双币种）──

    def _range_agg(self, from_date: str, to_date: str):
        """按天键区间聚合 {v,i,o,c,r,e} + 耗时（ds/dc/dmin/dmax）（全遍历 + 区间判断，不依赖插入序）"""
        v = i = o = c = r = e = 0
        dur = {"ds": 0.0, "dc": 0, "dmin": None, "dmax": None}
        for key, ds in self._days.items():
            if from_date <= key <= to_date:
                v += ds["v"]; i += ds["i"]; o += ds["o"]; c += ds["c"]
                r += ds["r"]; e += ds["e"]
                if ds.get("dc"):
                    dur["ds"] += ds.get("ds", 0.0); dur["dc"] += ds["dc"]
                    if ds.get("dmin") is not None and (dur["dmin"] is None or ds["dmin"] < dur["dmin"]):
                        dur["dmin"] = ds["dmin"]
                    if ds.get("dmax") is not None and (dur["dmax"] is None or ds["dmax"] > dur["dmax"]):
                        dur["dmax"] = ds["dmax"]
        return {"v": v, "i": i, "o": o, "c": c, "r": r, "e": e, **dur}

    def _aggs_cost_units(self, aggs: dict):
        """聚合桶 → ({f"{cur}|{unit}": amt}, matched)。按 (币种, 显示单位) 分桶，永不混算"""
        units = {}
        matched = False
        for mkey, slots in aggs.items():
            parts = mkey.split("\u001F")
            model = parts[0] if len(parts) > 0 else ""
            channel = parts[1] if len(parts) > 1 else ""
            host = parts[2] if len(parts) > 2 else ""
            rule = _match_rule(self.rules, channel, model, host)
            if rule is None:
                continue
            cur = _rule_currency(rule)
            unit = _rule_unit(rule)
            key = f"{cur}|{unit}"
            for is_peak, agg in ((False, slots[0]), (True, slots[1])):
                if agg is None:
                    continue
                pk = bool(rule.get("peak_enabled", True)) and is_peak
                hit = rule.get("hit_peak" if pk else "hit_off", 0) or 0
                miss = rule.get("miss_peak" if pk else "miss_off", 0) or 0
                out = rule.get("out_peak" if pk else "out_off", 0) or 0
                amt = (agg["c"] * hit + max(0, agg["i"] - agg["c"]) * miss + agg["o"] * out) / 1_000_000
                units[key] = units.get(key, 0.0) + amt
                matched = True
        return units, matched

    def _aggs_cost_ex(self, aggs: dict):
        """聚合桶 → (cny_total, pts_total, matched)。双币种分桶累计，永不混算（旧接口兼容）"""
        units, matched = self._aggs_cost_units(aggs)
        cny = sum(v for k, v in units.items() if k.startswith("CNY|"))
        pts = sum(v for k, v in units.items() if k.startswith("积分|"))
        return cny, pts, matched

    def _range_cost_ex(self, from_date: str, to_date: str):
        return self._aggs_cost_ex({k: v["aggs"] for k, v in self._days.items()
                                   if from_date <= k <= to_date})

    def _range_cost_units(self, from_date: str, to_date: str):
        return self._aggs_cost_units({k: v["aggs"] for k, v in self._days.items()
                                      if from_date <= k <= to_date})

    def _range_cost(self, from_date: str, to_date: str):
        """旧接口兼容：仅 CNY"""
        cny, _, matched = self._range_cost_ex(from_date, to_date)
        return cny if matched else None

    def _session_cost_ex(self):
        return self._aggs_cost_ex(self._sess["aggs"])

    def _session_cost_units(self):
        return self._aggs_cost_units(self._sess["aggs"])

    def _session_cost(self):
        cny, _, matched = self._session_cost_ex()
        return cny if matched else None

    def _channel_cost_ex(self, url: str, name: str, model_ref: str = ""):
        """某渠道（URL/渠道名包含匹配）在全部历史里的计费 → (cny, pts, matched)；
        model_ref 非空时只统计关联模型（provider:model）的用量。
        带缓存：记录数/最新时间戳/规则 hash 未变直接复用"""
        fp = self._calc_fingerprint()
        ckey = ("all", url, name, model_ref)
        hit = self._since_cost_cache.get(ckey)
        if hit and hit[0] == fp:
            return hit[1], hit[2], hit[3]
        merged = {}
        for ds in self._days.values():
            for mkey, slots in ds["aggs"].items():
                parts = mkey.split("\u001F")
                channel = parts[1] if len(parts) > 1 else ""
                host = parts[2] if len(parts) > 2 else ""
                if not (_url_loose_match(host, url) or _url_loose_match(channel, url)
                        or (name and name.lower() in channel.lower())):
                    continue
                if not self._model_ref_hit(parts[0] if len(parts) > 0 else "", channel, host, model_ref):
                    continue
                slots2 = merged.setdefault(mkey, [None, None])
                for i in (0, 1):
                    if slots[i] is None:
                        continue
                    if slots2[i] is None:
                        slots2[i] = {"i": 0, "o": 0, "c": 0}
                    slots2[i]["i"] += slots[i]["i"]
                    slots2[i]["o"] += slots[i]["o"]
                    slots2[i]["c"] += slots[i]["c"]
        if not merged:
            self._since_cost_cache[ckey] = (fp, 0.0, 0.0, False)
            return 0.0, 0.0, False
        cny, pts, matched = self._aggs_cost_ex(merged)
        self._since_cost_cache[ckey] = (fp, cny, pts, matched)
        return cny, pts, matched

    def _channel_cost(self, url: str, name: str) -> float:
        """旧接口兼容：仅 CNY（preset 旧模型扣减用）"""
        cny, _, matched = self._channel_cost_ex(url, name)
        return cny if matched else 0.0

    def _channel_cost_since_ex(self, url: str, name: str, since: datetime, model_ref: str = ""):
        """自 since 时刻以来（含）的渠道计费 → (cny, pts, matched)。
        逐条扫日志，按 t >= since 过滤；双币种分开累计。
        带缓存：记录数/最新时间戳/规则 hash 未变直接复用（余额估算高频调用）"""
        recs = self._read_records()
        fp = self._calc_fingerprint(recs)
        ckey = ("since", url, name, model_ref, since.isoformat())
        hit = self._since_cost_cache.get(ckey)
        if hit and hit[0] == fp:
            return hit[1], hit[2], hit[3]
        cny, pts, matched = 0.0, 0.0, False
        for r in recs:
            try:
                t = _parse_ts(r["t"])
            except Exception:
                continue
            if t < since:
                continue
            if not (_url_loose_match(r.get("h", ""), url) or _url_loose_match(r.get("ch", ""), url)
                    or (name and name.lower() in (r.get("ch", "") or "").lower())):
                continue
            if not self._model_ref_hit(r.get("m", ""), r.get("ch", ""), r.get("h", ""), model_ref):
                continue
            rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
            if rule is None:
                continue
            amt, cur = _rule_cost_ex(rule, r.get("i", 0), r.get("o", 0), r.get("c", 0), t)
            if cur == "积分":
                pts += amt
            else:
                cny += amt
            matched = True
        self._since_cost_cache[ckey] = (fp, cny, pts, matched)
        if len(self._since_cost_cache) > 64:
            self._since_cost_cache.clear()  # 有界：键含 since/url 组合，防止无限增长
        return cny, pts, matched

    # ── 余额探测 ──

    def _bal_state_of(self, name: str) -> dict:
        s = self._bal_states.get(name) or {"balance": 0, "currency": "CNY", "at": "", "ok": False, "msg": "尚未探测"}
        return s

    @staticmethod
    def _model_ref_hit(model: str, channel: str, host: str, model_ref: str) -> bool:
        """估算型余额源「关联模型」过滤：model_ref 格式 provider:model（或仅模型名）。
        模型名包含匹配；provider 段匹配渠道名或 host 包含。留空 = 不过滤。"""
        ref = (model_ref or "").strip()
        if not ref:
            return True
        ref_p, _, ref_m = ref.partition(":")
        if ref_m and ref_m.lower() not in (model or "").lower():
            return False
        if ref_p and not (ref_p.lower() in (channel or "").lower()
                          or ref_p.lower() in (host or "").lower()):
            return False
        return True

    def _is_est(self, src: dict) -> bool:
        return (src.get("type") or "auto").strip().lower() in EST_TYPES

    def _src_currency(self, src: dict, fallback: str = "CNY") -> str:
        """源展示币种：显式设置优先；daily/rolling 默认积分；其余默认 CNY"""
        cur = (src.get("currency") or "").strip()
        if cur:
            return cur
        if (src.get("type") or "").strip().lower() in POINT_TYPES:
            return "积分"
        return fallback

    def _src_unit(self, src: dict) -> str:
        """余额源显示单位：显式 unit 优先（含空串=无单位只显示数字）；
        默认积分→积分、其余→全局 balance_unit"""
        if "unit" in src:
            return str(src.get("unit") or "").strip()
        return "积分" if self._src_currency(src) == "积分" else self.balance_unit

    def _resolve_balance_state(self, src: dict) -> dict:
        """当前额度：估算型（preset/daily/rolling）按公式本地推算；
        其余取最近探测结果。统一公式：
          当前 = 设定的「当前余额(对表)」− 自设定时刻以来计费（按价格规则现算，改价即时重估）；
          preset 无锚定时回落 初始额度 − 全历史计费；
          daily 无锚定时 = 每日额度 − 上次刷新以来计费；
          rolling 必须先锚定（当前 = 设定余额 − 其后计费 + 发放次数 × 每日额度）"""
        s_type = (src.get("type") or "auto").strip().lower()
        name = src.get("name", "")
        if s_type not in EST_TYPES:
            return self._bal_state_of(name)

        currency = self._src_currency(src)
        model_ref = src.get("model_ref", "") or ""
        anchor = src.get("anchor_balance")
        try:
            anchor = float(anchor) if anchor not in (None, "") else None
        except (TypeError, ValueError):
            anchor = None
        anchor_at = src.get("anchor_at") or ""
        try:
            anchor_dt = _parse_ts(anchor_at) if anchor_at else None
        except Exception:
            anchor_dt = None

        initial = src.get("initial")
        try:
            initial = float(initial) if initial not in (None, "") else None
        except (TypeError, ValueError):
            initial = None
        daily = float(src.get("daily_quota") or 0) if src.get("daily_quota") not in (None, "") else 0.0
        refresh = (src.get("refresh_time") or "00:00").strip() or "00:00"

        now = datetime.now()

        def _fmt(v):
            return f"{v:0.6f}".rstrip("0").rstrip(".")

        # M3：日志发生过裁剪 → 早期计费记录缺失，估算可能偏高，msg 附警告
        tw = "（⚠ 日志曾裁剪，早期计费缺失，估算可能偏高）" if str(self._log_path) in _LOG_TRIMMED else ""

        try:
            if anchor is not None and anchor_dt is not None:
                # 锚定：设定值 − 其后计费（daily/rolling 另加此后发放；daily 跨刷新自动回落）
                cny, pts, matched = self._channel_cost_since_ex(src.get("url", ""), name, anchor_dt, model_ref)
                cost = pts if currency == "积分" else cny
                if s_type == "daily" and _last_refresh(now, refresh) > anchor_dt:
                    # 跨刷新：锚定失效，回落每日额度模型
                    cost2 = self._channel_cost_since_ex(src.get("url", ""), name, _last_refresh(now, refresh), model_ref)
                    c2 = cost2[1] if currency == "积分" else cost2[0]
                    cur = daily - c2
                    return {"balance": cur, "currency": currency,
                            "at": now.strftime("%Y-%m-%d %H:%M:%S"), "ok": True,
                            "msg": f"每日重置：每日额度 {_fmt(daily)} − 本周期计费 {_fmt(c2)} = 当前 {_fmt(cur)}（锚定已跨刷新失效，回落每日额度模型）{tw}"}
                if s_type == "rolling":
                    grants = _grant_count(anchor_dt, now, refresh)
                    cur = anchor + grants * daily - cost
                    return {"balance": cur, "currency": currency,
                            "at": now.strftime("%Y-%m-%d %H:%M:%S"), "ok": True,
                            "msg": f"设定余额 {_fmt(anchor)}（{anchor_dt.strftime('%m-%d %H:%M')} 对表）− 其后计费 {_fmt(cost)} + 已发放 {grants} 期 × {_fmt(daily)} = 当前 {_fmt(cur)}（按价格规则估算）{tw}"}
                cur = anchor - cost
                return {"balance": cur, "currency": currency,
                        "at": now.strftime("%Y-%m-%d %H:%M:%S"), "ok": True,
                        "msg": f"设定余额 {_fmt(anchor)}（{anchor_dt.strftime('%m-%d %H:%M')} 对表）− 其后计费 {_fmt(cost)} = 当前 {_fmt(cur)}（按价格规则估算）{tw}"}

            if s_type == "daily":
                if daily <= 0:
                    return {"balance": 0, "currency": currency, "at": "", "ok": False,
                            "msg": "daily 源需填「每日额度」（当前 = 每日额度 − 本周期计费）；可再填「当前余额(对表)」校准"}
                cny, pts, matched = self._channel_cost_since_ex(src.get("url", ""), name, _last_refresh(now, refresh), model_ref)
                cost = pts if currency == "积分" else cny
                cur = daily - cost
                return {"balance": cur, "currency": currency,
                        "at": now.strftime("%Y-%m-%d %H:%M:%S"), "ok": True,
                        "msg": f"每日重置：每日额度 {_fmt(daily)} − 上次刷新以来计费 {_fmt(cost)} = 当前 {_fmt(cur)}（刷新 {refresh}）{tw}"}

            if s_type == "rolling":
                return {"balance": 0, "currency": currency, "at": "", "ok": False,
                        "msg": "rolling 源需先填「当前余额(对表)」建立基准（当前 = 设定余额 − 计费 + 每日发放，没用完的结转滚存）"}

            # preset
            if initial is None:
                return {"balance": 0, "currency": currency, "at": "", "ok": False,
                        "msg": "preset 源需先填「初始额度」或「当前余额(对表)」"}
            cny, pts, matched = self._channel_cost_ex(src.get("url", ""), name, model_ref)
            cost = pts if currency == "积分" else cny
            cur = initial - cost
            return {"balance": cur, "currency": currency,
                    "at": now.strftime("%Y-%m-%d %H:%M:%S"), "ok": True,
                    "msg": f"预设扣减：初始额度 {_fmt(initial)} − 累计计费 {_fmt(cost)} = 当前 {_fmt(cur)}（按价格规则估算）{tw}"}
        except Exception as e:
            return {"balance": 0, "currency": currency, "at": "", "ok": False, "msg": f"估算失败: {e}"}

    @staticmethod
    def _read_num(json_data, path: str):
        """点路径取数（data.available_balance）；支持数组下标（balance_infos.0.total_balance 或 [0]）"""
        try:
            el = json_data
            if path:
                for raw_seg in path.split("."):
                    seg = raw_seg.strip()
                    if not seg:
                        continue
                    if len(seg) >= 3 and seg[0] == "[" and seg[-1] == "]":
                        seg = seg[1:-1].strip()
                    if seg.isdigit() and isinstance(el, list):
                        idx = int(seg)
                        if idx < 0 or idx >= len(el):
                            return None
                        el = el[idx]
                    elif isinstance(el, dict) and seg in el:
                        el = el[seg]
                    else:
                        return None
            if isinstance(el, (int, float)) and not isinstance(el, bool):
                return float(el)
            if isinstance(el, str):
                try:
                    return float(el)
                except ValueError:
                    return None
            return None
        except Exception:
            return None

    @staticmethod
    def _first_balance_info(data: dict):
        """DeepSeek/智谱风格：balance_infos[0].total_balance (+currency)"""
        currency = ""
        infos = data.get("balance_infos") or []
        if not infos:
            raise ValueError("返回缺少 balance_infos")
        first = infos[0]
        currency = str(first.get("currency", "") or "")
        v = TokenStatsPlugin._read_num(first, "total_balance")
        if v is None:
            raise ValueError("balance_infos[0] 缺少 total_balance")
        return v, currency

    @staticmethod
    def _newapi_extract(data: dict):
        """New-API /api/user/self 风格：多字段自动提取 quota，返回 (quota, 是否找到)"""
        candidates = [
            "quota", "balance", "remaining", "points",
            "totalBalance", "total_balance", "amount", "credit", "available",
        ]
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, dict):
                for key in candidates:
                    v = TokenStatsPlugin._read_num(inner, key)
                    if v is not None:
                        return v, True
            if "error" in data and isinstance(data.get("error"), dict):
                err = data["error"]
                if isinstance(err, dict) and err.get("message"):
                    raise ValueError(f"NewAPI 返回错误: {err.get('message')}")
            for key in candidates:
                v = TokenStatsPlugin._read_num(data, key)
                if v is not None:
                    return v, True
            # balance_infos 风格兜底
            infos = data.get("balance_infos") or (inner or {}).get("balance_infos") or []
            if infos:
                v = TokenStatsPlugin._read_num(infos[0], "total_balance")
                if v is not None:
                    return v, True
        return None, False

    async def _probe_one(self, src: dict) -> dict:
        """探测单个余额源（auto/custom/newapi），失败返回 Ok=false + 原因"""
        st = {"balance": 0, "currency": self._src_currency(src),
              "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ok": False, "msg": ""}
        try:
            s_type = (src.get("type") or "auto").strip().lower()
            api_key = src.get("api_key", "") or ""
            jpath = (src.get("json_path") or "").strip()

            if s_type == "newapi":
                # New-API 专属模式：New-Api-User 头 + /api/user/self + quota_conversion 换算
                cu = (src.get("url") or "").strip()
                if not cu:
                    st["msg"] = "newapi 源缺少站点地址"
                    return st
                if not cu.startswith("http"):
                    cu = "https://" + cu
                croot = cu[:-3].rstrip("/") if cu.endswith("/v1") else cu.rstrip("/")
                api_user = str(src.get("api_user", "") or "").strip()
                conversion = src.get("quota_conversion", 500000)
                try:
                    conversion = float(conversion) if conversion not in (None, "") else 500000
                except (TypeError, ValueError):
                    conversion = 500000
                if not api_user:
                    st["msg"] = "newapi 源缺少 api_user（站点后台用户ID）"
                    return st
                try:
                    body = await self._http_get(croot + "/api/user/self", api_key, extra_headers={"New-Api-User": api_user})
                    quota, found = self._newapi_extract(body)
                    if not found:
                        raise ValueError("返回数据中未找到 quota/balance 等字段")
                    st["balance"] = quota / conversion
                    st["ok"] = True
                    st["msg"] = f"New-API 站点：quota {quota:,.0f} ÷ {conversion:g} = {quota / conversion:.4f}"
                    return st
                except Exception as ex:
                    st["msg"] = f"New-API 接口失败: {str(ex)[:160]}"
                    return st

            if s_type == "custom":
                cu = (src.get("url") or "").strip()
                if not cu:
                    st["msg"] = "custom 源缺少接口地址"
                    return st
                if not cu.startswith("http"):
                    cu = "https://" + cu
                croot = cu[:-3].rstrip("/") if cu.endswith("/v1") else cu.rstrip("/")
                cands = [
                    (cu, jpath),
                    (croot + "/user/balance", "balance_infos.0.total_balance"),
                    (croot + "/v1/users/me/balance", "data.available_balance"),
                    (croot + "/v1/user/info", "data.totalBalance"),
                    (croot + "/api/paas/v4/users/me/balance", "balance_infos.0.total_balance"),
                ]
                tried = []
                for ep, path in cands:
                    try:
                        body = await self._http_get(ep, api_key)
                        v = self._read_num(body, path) if path else self._read_num(body, "")
                        if v is None and path == "data.totalBalance":
                            v = self._read_num(body, "data.balance")
                        if v is None:
                            tried.append(ep)
                            continue
                        st["balance"] = v
                        st["ok"] = True
                        st["msg"] = f"自定义接口（{ep}）" + ("，按「余额字段」取数" if jpath else "")
                        return st
                    except Exception as ex:
                        tried.append(f"{ep}（{str(ex)[:60]}）")
                # One-API 系组合兜底
                try:
                    sub = await self._http_get(croot + "/v1/dashboard/billing/subscription", api_key)
                    limit = self._read_num(sub, "hard_limit_usd")
                    if limit is not None:
                        end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                        usage_body = await self._http_get(
                            f"{croot}/v1/dashboard/billing/usage?start_date=2024-01-01&end_date={end}", api_key)
                        used = (self._read_num(usage_body, "total_usage") or 0) / 100
                        st["balance"] = limit - used
                        st["currency"] = "USD"
                        st["ok"] = True
                        st["msg"] = f"One-API 系中转站：额度 {limit:.2f} − 已用 {used:.2f}"
                        return st
                except Exception as ex:
                    tried.append(f"subscription（{str(ex)[:60]}）")
                st["msg"] = "常见余额接口均未取到数字（" + "；".join(tried[-3:])[:240] + "）"
                return st

            # auto：按 URL 分流
            url = (src.get("url") or "").strip()
            if not url:
                st["msg"] = "缺少 URL"
                return st
            if not url.startswith("http"):
                url = "https://" + url
            root = url[:-3].rstrip("/") if url.endswith("/v1") else url.rstrip("/")
            try:
                host = (urlparse(root).hostname or root).lower()
            except Exception:
                host = root.lower()

            if any(h in host for h in _DS_HOSTS):
                body = await self._http_get(root + "/user/balance", api_key)
                v, cur = self._first_balance_info(body)
                st["balance"] = v
                st["currency"] = cur or "CNY"
                st["ok"] = True
                st["msg"] = "DeepSeek 官方端点"
            elif any(h in host for h in _MS_HOSTS):
                body = await self._http_get(root + "/v1/users/me/balance", api_key)
                v = self._read_num(body, "data.available_balance")
                if v is None:
                    raise ValueError("返回缺少 data.available_balance")
                st["balance"] = v
                st["currency"] = "CNY"
                st["ok"] = True
                st["msg"] = "Moonshot 官方端点"
            elif any(h in host for h in _SF_HOSTS):
                body = await self._http_get(root + "/v1/user/info", api_key)
                v = self._read_num(body, "data.totalBalance") or self._read_num(body, "data.balance")
                if v is None:
                    raise ValueError("返回缺少 data.totalBalance/balance")
                st["balance"] = v
                st["currency"] = "CNY"
                st["ok"] = True
                st["msg"] = "硅基流动官方端点"
            elif any(h in host for h in _ZP_HOSTS):
                body = await self._http_get(root + "/api/paas/v4/users/me/balance", api_key)
                v, cur = self._first_balance_info(body)
                st["balance"] = v
                st["currency"] = cur or "CNY"
                st["ok"] = True
                st["msg"] = "智谱端点（官方未文档化，失败可改 custom/preset）"
            else:
                # One-API / New-API 中转站：subscription − usage
                sub = await self._http_get(root + "/v1/dashboard/billing/subscription", api_key)
                limit = self._read_num(sub, "hard_limit_usd")
                if limit is None:
                    raise ValueError("中转站未实现 billing/subscription（hard_limit_usd 缺失）")
                end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                usage_body = await self._http_get(
                    f"{root}/v1/dashboard/billing/usage?start_date=2024-01-01&end_date={end}", api_key)
                used = (self._read_num(usage_body, "total_usage") or 0) / 100
                st["balance"] = limit - used
                st["currency"] = "USD"
                st["ok"] = True
                st["msg"] = f"One-API 系中转站：额度 {limit:.2f} − 已用 {used:.2f}"
        except Exception as ex:
            st["ok"] = False
            st["msg"] = str(ex)[:160]
        return st

    async def _http_get(self, url: str, api_key: str, extra_headers: dict = None):
        """GET 请求，返回解析后的 JSON；非 2xx 抛异常。
        ssl：balance_ssl_verify=True 时校验证书（https 默认行为）；
        False（默认）时禁用校验——兼容自签证书中转站，注意中间人风险"""
        if aiohttp is None:
            raise RuntimeError("aiohttp 未安装")
        headers = {}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        if extra_headers:
            headers.update(extra_headers)
        timeout = aiohttp.ClientTimeout(total=10)
        use_ssl = bool(self.balance_ssl_verify)
        try:
            if use_ssl:
                connector = aiohttp.TCPConnector(resolver=ThreadedResolver())
            else:
                connector = aiohttp.TCPConnector(resolver=ThreadedResolver(), ssl=False)
        except Exception:
            connector = None
        try:
            if connector is not None:
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    async with session.get(url, headers=headers) as resp:
                        body = await resp.text()
                        if resp.status >= 400:
                            raise RuntimeError(f"HTTP {resp.status} {body[:120]}")
                        return json.loads(body)
            else:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers) as resp:
                        body = await resp.text()
                        if resp.status >= 400:
                            raise RuntimeError(f"HTTP {resp.status} {body[:120]}")
                        return json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError("返回非 JSON")
        except aiohttp.ClientError as e:
            raise RuntimeError(f"网络请求失败: {e}")

    async def _probe_all(self, wait: bool = False):
        """探测全部余额源。

        wait=True：若后台轮询正忙，等待其完成（最多 20s）再返回最新状态，
        避免工具/命令查询拿到旧值；等待超时则返回现有状态不阻塞。
        网络型源并行探测：单源 8s 超时、整体 15s 超时，防止串行 N×10s 拖住查询。
        """
        if self._bal_busy:
            if not wait:
                return
            for _ in range(40):
                if not self._bal_busy:
                    break
                await asyncio.sleep(0.5)
            else:
                logger.warning("[token_stats] 等待余额探测完成超时(20s)，返回现有状态")
            return
        self._bal_busy = True
        try:
            est_srcs = [s for s in self.balance_sources
                        if s.get("enabled", True) and s.get("name") and self._is_est(s)]
            net_srcs = [s for s in self.balance_sources
                        if s.get("enabled", True) and s.get("name") and not self._is_est(s)]
            # 估算型本地推算，不走网络
            for src in est_srcs:
                self._bal_states[src.get("name")] = self._resolve_balance_state(src)

            if net_srcs:
                async def _safe_probe(src):
                    try:
                        st = await asyncio.wait_for(self._probe_one(src), timeout=8)
                    except asyncio.TimeoutError:
                        st = {"balance": 0, "currency": self._src_currency(src),
                              "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                              "ok": False, "msg": "探测超时(8s)"}
                    except Exception as e:
                        st = {"balance": 0, "currency": self._src_currency(src),
                              "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                              "ok": False, "msg": str(e)[:160]}
                    return src.get("name"), st

                tasks = [asyncio.create_task(_safe_probe(s)) for s in net_srcs]
                done, pending = await asyncio.wait(tasks, timeout=15)
                for t in pending:
                    t.cancel()
                if pending:
                    # 等取消完成，避免事件循环关闭时 Task was destroyed 警告
                    await asyncio.gather(*pending, return_exceptions=True)
                for t in done:
                    try:
                        name, st = t.result()
                        if name:
                            self._bal_states[name] = st
                    except Exception:
                        pass
                if pending:
                    logger.warning(f"[token_stats] 余额并行探测整体超时(15s)，{len(pending)} 个源未完成")
            self._save_bal_states()
        except Exception as e:
            logger.warning(f"[token_stats] 余额探测异常: {e}")
        finally:
            self._bal_busy = False

    async def _balance_loop(self):
        try:
            while True:
                await self._probe_all()
                await asyncio.sleep(max(60, self.balance_interval))
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("[token_stats] 余额轮询循环退出")

    # ── 预警提醒引擎 ──

    def _alert_rule_enabled(self, r: dict) -> bool:
        return bool(r.get("enabled", True)) and bool(str(r.get("id") or "").strip())

    def _alert_in_window(self, r: dict) -> bool:
        """生效时段/窗口检查：time_start/time_end（HH:mm，留空=全天）+ time_window（分钟，留空=不限）。
        窗口语义：time_start 起 N 分钟内生效（如 9:00 + 60min = 9:00-10:00）；time_end 与窗口同时给时取交集。"""
        now = datetime.now()
        cur = now.hour * 60 + now.minute
        start = end = None
        try:
            if str(r.get("time_start") or "").strip():
                h, m = _parse_hhmm(str(r["time_start"]).strip())
                start = h * 60 + m
            if str(r.get("time_end") or "").strip():
                h, m = _parse_hhmm(str(r["time_end"]).strip())
                end = h * 60 + m
        except Exception:
            start = end = None
        win = r.get("time_window")
        try:
            win = int(win) if win not in (None, "") else None
        except (TypeError, ValueError):
            win = None
        if start is not None and win is not None:
            end = min(end, start + win) if end is not None else start + win
        if start is not None and cur < start:
            return False
        if end is not None and cur > end:
            return False
        return True

    def _alert_delivery_sids(self, r: dict) -> list:
        """规则投递会话 → sid 列表（llm 模式只取第一个）"""
        ids = r.get("deliveries") or []
        if not isinstance(ids, list):
            ids = [ids]
        sids = []
        for d in self.alert_deliveries:
            if d.get("id") in ids and d.get("enabled", True) and str(d.get("sid") or "").strip():
                sids.append(str(d["sid"]).strip())
        return sids

    def _alert_fmt_value(self, r: dict, value, unit: str = "", mode: str = "") -> str:
        """提醒话语占位符替换：{name}{value}{unit}{threshold}{type}{target}{step}
        mode=llm 时用 llm_message（默认带「请告知用户情况」），否则用 message"""
        try:
            v = f"{float(value):,.4f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            v = str(value)
        is_llm = (mode or r.get("mode") or "direct").strip().lower() == "llm"
        msg = str(r.get("llm_message") if is_llm else r.get("message") or "").strip()
        if not msg:
            tname = {"balance": "余额", "token": "tokens", "cost": "费用"}.get(r.get("type"), "指标")
            if (r.get("trigger") or "once").strip().lower() == "step":
                # step 模式默认话语：体现「每新增 N 提醒一次」
                msg = f"📈 {tname}预警：{r.get('target') or '全部'} 每新增 {r.get('threshold')} 提醒一次，当前已达 {v}{unit}"
            else:
                msg = f"⚠️ {tname}预警：{r.get('target') or '全部'} 已达 {v}{unit}"
            if is_llm:
                # llm 模式：bot 感知后主动告知用户
                msg += "。请告知用户情况"
        return (msg.replace("{name}", str(r.get("target") or ""))
                   .replace("{value}", v)
                   .replace("{unit}", unit)
                   .replace("{threshold}", str(r.get("threshold") or ""))
                   .replace("{type}", str(r.get("type") or ""))
                   .replace("{target}", str(r.get("target") or ""))
                   .replace("{step}", str(r.get("threshold") or "")))

    async def _alert_send(self, r: dict, sids: list, text: str, send_image: bool):
        """投递提醒：direct=机械直发（消息先发，图异步后发）；llm=publish_notice 让 bot 感知。
        多会话并行投递互不影响；单会话失败不影响其他会话。"""
        if not sids:
            return
        mode = (r.get("mode") or "direct").strip().lower()
        if mode == "llm":
            # 发给 LLM：publish_notice 构造 is_notice 事件走正常管线，bot 感知后主动回复
            try:
                await self.ctx.publish_notice(sids[0], MessageChain([Text(text)]), is_mentioned=True)
            except Exception as e:
                logger.warning(f"[token_stats] 预警 llm 投递失败: {e}")
            if send_image:
                asyncio.ensure_future(self._alert_send_image_async(sids[0]))
            return
        # direct：机械直发，不经过 LLM
        for sid in sids:
            try:
                await self.ctx.message_processor.send_message_chain(sid, MessageChain([Text(text)]))
            except Exception as e:
                logger.warning(f"[token_stats] 预警直发失败 {sid}: {e}")
        if send_image:
            for sid in sids:
                asyncio.ensure_future(self._alert_send_image_async(sid))

    async def _alert_send_image_async(self, sid: str):
        """异步补发概况图：消息已先发，图构建好后补发（不阻塞投递）。
        同步重活（_build_summary_html 全量聚合）丢线程执行，避免阻塞事件循环。"""
        try:
            sess_name = ""
            if self._sess.get("sid"):
                try:
                    sess_name = await self._resolve_sid_name(self._sess["sid"])
                except Exception:
                    sess_name = ""
            html = await asyncio.to_thread(self._build_summary_html, "", sess_name=sess_name)
            out_dir = self._data_dir / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            png = out_dir / f"summary_{int(time.time() * 1000)}_{os.urandom(3).hex()}.png"
            wait_js = "document.querySelectorAll('img.bg').length===0 || (document.querySelector('img.bg').complete && document.querySelector('img.bg').naturalWidth>0)"
            await render_html(html, str(png), self._browser, wait_js=wait_js)
            await self.ctx.message_processor.send_message_chain(sid, MessageChain([Image(image=str(png))]))
            try:
                old = sorted(out_dir.glob("summary_*.png"), key=lambda p: p.name)
                for p in old[:-20]:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[token_stats] 预警概况图补发失败: {e}")

    def _alert_rule_fired(self, r: dict) -> bool:
        st = self._alert_fired.get(str(r.get("id")))
        if st is None:
            return False
        if isinstance(st, dict):
            return st.get("count", 0) >= 1
        return True  # 旧格式：float 时间戳 = 已触发

    def _alert_mark_fired(self, r: dict):
        """记录触发：count+1（连续提醒次数）、at（最近触发时间）、day（自然日，token/cost 跨天重置用）、
        step（step 模式上次提醒到的档位 = floor(当前值/阈值)）"""
        rid = str(r.get("id"))
        st = self._alert_fired.get(rid)
        if isinstance(st, dict):
            st["count"] = st.get("count", 0) + 1
            st["at"] = time.time()
        else:
            self._alert_fired[rid] = {"count": 1, "at": time.time(),
                                      "day": datetime.now().strftime("%Y-%m-%d")}

    def _alert_step_ok(self, r: dict, value: float, threshold: float) -> bool:
        """step 模式（每新增 ≥N 提醒一次）：当前档位 = floor(value/threshold)，
        档位比上次记录的大才触发（每跨过一个 N 的整数倍提醒一次，如每 10M 提醒）。
        非 step 模式恒 True。"""
        if (r.get("trigger") or "once").strip().lower() != "step":
            return True
        if threshold <= 0:
            return False
        cur_step = int(value // threshold)
        st = self._alert_fired.get(str(r.get("id")))
        last_step = st.get("step", 0) if isinstance(st, dict) else 0
        return cur_step > last_step

    def _alert_mark_step(self, r: dict, value: float, threshold: float):
        """step 模式触发后记录当前档位"""
        if (r.get("trigger") or "once").strip().lower() != "step":
            return
        if threshold <= 0:
            return
        rid = str(r.get("id"))
        st = self._alert_fired.get(rid)
        if isinstance(st, dict):
            st["step"] = int(value // threshold)

    def _alert_clear_fired(self, r: dict):
        self._alert_fired.pop(str(r.get("id")), None)

    def _alert_max_ok(self, r: dict) -> bool:
        """最大连续提醒次数：默认 1（只提醒 1 次）。
        在未回升/未超过阈值前，最多连续提醒 N 次；达到上限后不再提醒（等复位/回升/跨天）。"""
        st = self._alert_fired.get(str(r.get("id")))
        if st is None:
            return True
        count = st.get("count", 1) if isinstance(st, dict) else 1
        try:
            mx = int(r.get("max_alerts") if r.get("max_alerts") not in (None, "") else 1)
        except (TypeError, ValueError):
            mx = 1
        return count < max(1, mx)

    def _alert_interval_ok(self, r: dict) -> bool:
        """连续提醒间隔：默认 300 秒（5 分钟）。两次连续提醒的最小间隔。
        只约束同一条规则自身，多规则同时触发互不影响。"""
        st = self._alert_fired.get(str(r.get("id")))
        if st is None:
            return True
        at = st.get("at", 0) if isinstance(st, dict) else st
        try:
            iv = int(r.get("alert_interval") if r.get("alert_interval") not in (None, "") else 300)
        except (TypeError, ValueError):
            iv = 300
        return time.time() - at >= max(0, iv)

    def _alert_day_reset(self, r: dict) -> bool:
        """token/cost 跨天重置：已触发状态带自然日，非今天则清除（当天重新计数）。"""
        rid = str(r.get("id"))
        st = self._alert_fired.get(rid)
        if st is None or not isinstance(st, dict):
            return False
        if st.get("day") != datetime.now().strftime("%Y-%m-%d"):
            self._alert_fired.pop(rid, None)
            return True
        return False

    def _alert_balance_value(self, r: dict):
        """余额规则当前值：target=余额源名（留空=任一源）；返回 (value, unit, name) 或 None"""
        target = str(r.get("target") or "").strip()
        for src in self.balance_sources:
            if not src.get("enabled", True):
                continue
            name = str(src.get("name") or "")
            if target and target not in name:
                continue
            st = self._resolve_balance_state(src)
            if not st.get("ok"):
                continue
            return st["balance"], self._src_unit(src), name
        return None

    def _alert_usage_value(self, r: dict):
        """token/cost 规则当前值：target=来源标签（留空=全部）；session=限定会话 sid（留空=全部）。
        token → 当天 tokens 数；cost → 当天费用（currency 指定币种，留空=任一币种达阈值即触发）。
        返回 (value, unit, label) 或 None。
        性能：单遍扫描日志同时算 token 与 cost，并按 (target, session) 指纹缓存当天结果
        （记录数/最新时间戳/规则 hash 未变直接复用，避免每轮 LLM 后全量重扫）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        target = str(r.get("target") or "").strip()
        session = str(r.get("session") or "").strip()
        rtype = (r.get("type") or "token").strip().lower()
        # 指纹缓存：记录数 + 最新时间戳 + 规则 hash + 过滤条件
        recs = self._read_records()
        try:
            rh = hash(json.dumps(self.rules or [], sort_keys=True, ensure_ascii=False))
        except Exception:
            rh = id(self.rules)
        last_t = recs[-1].get("t", "") if recs else ""
        fp = (len(recs), last_t, rh, target, session)
        hit = self._alert_usage_cache.get(fp)
        if hit is not None:
            v, units = hit
        else:
            v = 0
            units = {}
            for rec in recs:
                try:
                    t = _parse_ts(rec.get("t", ""))
                except Exception:
                    continue
                if t.strftime("%Y-%m-%d") != today:
                    continue
                if target and target not in str(rec.get("s", "") or ""):
                    continue
                if session and session != str(rec.get("sid", "") or ""):
                    continue
                v += int(rec.get("v", 0) or 0)
                rule = _match_rule(self.rules, rec.get("ch", ""), rec.get("m", ""), rec.get("h", ""))
                if rule is None:
                    continue
                cur = _rule_currency(rule)
                unit = _rule_unit(rule)
                key = f"{cur}|{unit}"
                pk = bool(rule.get("peak_enabled", True)) and _is_peak_in(t, _windows_for_rule(rule))
                hit_r = rule.get("hit_peak" if pk else "hit_off", 0) or 0
                miss = rule.get("miss_peak" if pk else "miss_off", 0) or 0
                out = rule.get("out_peak" if pk else "out_off", 0) or 0
                amt = (int(rec.get("c", 0) or 0) * hit_r
                       + max(0, int(rec.get("i", 0) or 0) - int(rec.get("c", 0) or 0)) * miss
                       + int(rec.get("o", 0) or 0) * out) / 1_000_000
                units[key] = units.get(key, 0.0) + amt
            # 缓存上限防膨胀：超过 64 条清空重建
            if len(self._alert_usage_cache) > 64:
                self._alert_usage_cache.clear()
            self._alert_usage_cache[fp] = (v, units)
        if rtype == "token":
            return v, "tokens", (target or "全部来源")
        currency = str(r.get("currency") or "").strip()
        if currency:
            for key, amt in units.items():
                if key.startswith(currency + "|"):
                    _, _, unit = key.partition("|")
                    return amt, unit, (target or "全部来源")
            return None
        # 任一币种达阈值即触发：取当前值最大的币种
        if not units:
            return None
        key, amt = max(units.items(), key=lambda kv: kv[1])
        _, _, unit = key.partition("|")
        return amt, unit, (target or "全部来源")

    def _alert_check_rule(self, r: dict):
        """单条规则检查：命中 → 投递。返回是否触发（供测试/日志）。"""
        if not self._alert_rule_enabled(r):
            return False
        if not self._alert_in_window(r):
            return False
        rtype = (r.get("type") or "balance").strip().lower()
        try:
            threshold = float(r.get("threshold") or 0)
        except (TypeError, ValueError):
            return False
        op = (r.get("op") or "le").strip().lower()
        if rtype == "balance":
            got = self._alert_balance_value(r)
            if got is None:
                return False
            value, unit, name = got
            hit = value <= threshold if op == "le" else value >= threshold
            if not hit:
                # 滞回：余额回升超过阈值才重置，再次跌破才重新提醒
                if self._alert_rule_fired(r):
                    self._alert_clear_fired(r)
                return False
        else:
            # token/cost：跨天自动重置（当天重新计数）
            self._alert_day_reset(r)
            got = self._alert_usage_value(r)
            if got is None:
                return False
            value, unit, label = got
            hit = value >= threshold if op == "ge" else value <= threshold
            if not hit:
                return False
            # step 模式（每新增 ≥N 提醒一次）：档位未前进则不触发
            if not self._alert_step_ok(r, value, threshold):
                return False
        if self._alert_rule_fired(r):
            # 已触发过：受「最大连续提醒次数」与「连续提醒间隔」约束
            # step 模式天然每档一次，跳过 max_alerts 限制（仍受 alert_interval 防刷屏）
            if (r.get("trigger") or "once").strip().lower() != "step":
                if not self._alert_max_ok(r):
                    return False
            if not self._alert_interval_ok(r):
                return False
        sids = self._alert_delivery_sids(r)
        if not sids:
            return False
        text = self._alert_fmt_value(r, value, unit, mode=(r.get('mode') or 'direct'))
        # 概况图补发开关：llm 模式用 llm_send_image，direct 用 send_image
        send_image = bool(r.get("llm_send_image", False)) if (r.get("mode") or "direct").strip().lower() == "llm" \
            else bool(r.get("send_image", False))
        self._alert_mark_fired(r)
        self._alert_mark_step(r, value, threshold)
        asyncio.ensure_future(self._alert_send(r, sids, text, send_image))
        logger.info(f"[token_stats] 预警触发: {r.get('id')} type={rtype} value={value}{unit} threshold={threshold} mode={r.get('mode')}")
        return True

    async def _alert_loop(self):
        """后台预警循环：balance 类规则随轮询检查（token/cost 类在 on_llm_response 实时检查）"""
        try:
            while True:
                try:
                    for r in self.alert_rules:
                        if (r.get("type") or "balance").strip().lower() == "balance":
                            self._alert_check_rule(r)
                except Exception as e:
                    logger.warning(f"[token_stats] 预警检查异常: {e}")
                await asyncio.sleep(max(60, self.balance_interval))
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("[token_stats] 预警循环退出")

    # ── 后台日志（log.log）ERROR 扫描 ──

    @staticmethod
    def _log_err_classify(text: str) -> str:
        """按内容分类 ERROR 行：xml/model/tool/net/traceback/other"""
        if LOG_ERR_XML_RE.search(text):
            return "xml"
        if LOG_ERR_MODEL_RE.search(text):
            return "model"
        if LOG_ERR_TOOL_RE.search(text):
            return "tool"
        if LOG_ERR_NET_RE.search(text):
            return "net"
        if LOG_ERR_TB_RE.search(text):
            return "traceback"
        return "other"

    def _log_err_scan_file(self, path: Path, pos: int):
        """增量扫描单个日志文件：返回 (新游标, 新增计数, 最新一条)

        二进制模式读取：tell()/seek() 返回真实字节偏移（Python 3.10-3.12 文本模式
        tell() 返回不透明 cookie，不能与 st_size 直接比较，会误判截断）。
        只读打开 + FILE_SHARE_DELETE 共享模式（Windows）：
        不独占文件，不阻塞 KiraAI 的 RotatingFileHandler 轮转 rename。
        句柄生命周期仅限本次扫描（毫秒级），读完立即关闭。
        """
        count = 0
        last = None
        new_pos = pos
        try:
            size = path.stat().st_size
            if size < pos:
                # 文件被轮转/截断：从头扫
                pos = 0
            f = self._open_log_shared(path)
            if f is None:
                return pos, count, last
            try:
                f.seek(pos)
                for raw in f:
                    line = raw.decode("utf-8", errors="replace")
                    m = LOG_ERROR_RE.match(line)
                    if m:
                        count += 1
                        cat = self._log_err_classify(m.group(2))
                        day = line[:10]
                        d = self._log_err_hist.setdefault(day, {})
                        d[cat] = d.get(cat, 0) + 1
                        last = {"at": line[:19], "cat": cat, "text": m.group(2).strip()[:160]}
                new_pos = f.tell()
            finally:
                f.close()
            return new_pos, count, last
        except Exception as e:
            logger.warning(f"[token_stats] 日志扫描失败 {path}: {e}")
            return pos, count, last

    @staticmethod
    def _open_log_shared(path: Path):
        """以共享删除模式打开日志文件（Windows 用 CreateFileW + FILE_SHARE_DELETE，
        避免阻塞 RotatingFileHandler 轮转；其他平台普通只读打开）。二进制模式。"""
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                GENERIC_READ = 0x80000000
                FILE_SHARE_READ = 0x00000001
                FILE_SHARE_WRITE = 0x00000002
                FILE_SHARE_DELETE = 0x00000004
                OPEN_EXISTING = 3
                FILE_ATTRIBUTE_NORMAL = 0x80
                CreateFileW = ctypes.windll.kernel32.CreateFileW
                CreateFileW.restype = wintypes.HANDLE
                CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
                handle = CreateFileW(str(path), GENERIC_READ,
                                     FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                     None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
                if not handle or handle == wintypes.HANDLE(-1).value:
                    return None
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
                if fd < 0:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    return None
                return os.fdopen(fd, "rb")
            return open(path, "rb")
        except Exception:
            return None

    async def _log_err_loop(self):
        """后台循环：每 10s 增量扫描 log.log 及轮转文件（log.log.1/2/…）。

        按文件身份（st_ino）跟踪游标：
        - 轮转改名后同一文件继续从原游标扫，不重复计数
        - 新出现的轮转文件（log.log.1 等）从头扫，历史 ERROR 也能统计到
        - 文件被截断/重建（新 ino）时从头扫
        """
        try:
            while True:
                try:
                    async with self._log_err_lock:
                        self._log_err_scan_all()
                        self._maybe_save_err_stats()
                except Exception as e:
                    logger.warning(f"[token_stats] 日志扫描循环异常: {e}")
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("[token_stats] 日志扫描循环退出")

    def _log_err_scan_all(self):
        """扫描 data 目录下全部 log.log* 文件（含轮转），按 ino 增量"""
        try:
            if not (_HAS_DATA_PATH and get_data_path is not None):
                return
            d = Path(get_data_path())
            if not d.is_dir():
                return
            # 显式匹配 log.log + log.log.[0-9]*（RotatingFileHandler 轮转命名），
            # 防未来轮转策略变化误扫无关文件
            files = sorted(
                [p for p in d.glob("log.log") if p.is_file()]
                + [p for p in d.glob("log.log.[0-9]*") if p.is_file()]
            )
            if not files:
                return
            seen = set()
            for path in files:
                try:
                    st = path.stat()
                except OSError:
                    continue
                ino = getattr(st, "st_ino", None)
                if ino is None or ino == 0:
                    # 平台无 ino（如某些网络盘）或 ino 为 0（FAT32 等文件系统）：
                    # 退化为路径跟踪，避免所有文件共用游标 0 导致重复计数
                    ino = str(path)
                seen.add(ino)
                pos = self._log_err_inodes.get(ino, 0)
                size = st.st_size
                if size < pos:
                    # 文件被截断/重建：从头扫
                    pos = 0
                new_pos, count, last = self._log_err_scan_file(path, pos)
                self._log_err_inodes[ino] = new_pos
                if last:
                    self._log_err_last = last
                if count and self.debug_log:
                    logger.info(f"[token_stats] 日志扫描 {path.name} +{count} 条 ERROR")
            # 清理已消失文件的游标（轮转后旧 ino 不再出现）
            for ino in list(self._log_err_inodes):
                if ino not in seen:
                    del self._log_err_inodes[ino]
        except Exception as e:
            logger.warning(f"[token_stats] 日志扫描失败: {e}")

    def _maybe_save_err_stats(self, force: bool = False):
        """错误统计持久化（节流 30s，force 时绕过节流）：热重载/重启不丢"""
        now = time.time()
        if not force and now - self._err_save_at < 30:
            return
        self._err_save_at = now
        try:
            payload = {
                "log_hist": self._log_err_hist,
                "log_last": self._log_err_last,
                "tool_hist": self._tool_err_hist,
                "tool_last": self._tool_err_last,
                # 游标一并持久化：热重载后新实例从原游标续扫，历史 ERROR 不重复计数
                "log_inodes": {str(k): v for k, v in self._log_err_inodes.items()},
            }
            p = self._data_dir / "err_stats.json"
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _load_err_stats(self):
        """加载持久化的错误统计与扫描游标（热重载/重启恢复）"""
        try:
            p = self._data_dir / "err_stats.json"
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if isinstance(data.get("log_hist"), dict):
                    self._log_err_hist = data["log_hist"]
                if isinstance(data.get("log_last"), dict):
                    self._log_err_last = data["log_last"]
                if isinstance(data.get("tool_hist"), dict):
                    self._tool_err_hist = data["tool_hist"]
                if isinstance(data.get("tool_last"), dict):
                    self._tool_err_last = data["tool_last"]
                if isinstance(data.get("log_inodes"), dict):
                    inodes = {}
                    for k, v in data["log_inodes"].items():
                        try:
                            kk = int(k)
                        except (TypeError, ValueError):
                            kk = k
                        inodes[kk] = v
                    self._log_err_inodes = inodes
        except Exception:
            pass

    def _log_err_summary(self, days: int = 7) -> str:
        """近 N 天分类聚合摘要（含最近一条）"""
        if not self._log_err_hist:
            return ""
        today = datetime.now().strftime("%Y-%m-%d")
        cutoff = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        agg = {}
        for day, cats in self._log_err_hist.items():
            if day < cutoff or day > today:
                continue
            for cat, n in cats.items():
                agg[cat] = agg.get(cat, 0) + n
        if not agg:
            return ""
        parts = []
        for cat in LOG_ERR_CATS:
            if agg.get(cat):
                parts.append(f"{LOG_ERR_LABELS[cat]} {agg[cat]}")
        s = "后台日志错误：" + " · ".join(parts)
        if self._log_err_last and self._log_err_last.get("text"):
            s += f"（最近：{self._log_err_last['text'][:60]}）"
        return s

    def _tool_err_summary(self, days: int = 7) -> str:
        """近 N 天工具结果失败摘要（error/权限denied/超时等）"""
        if not self._tool_err_hist:
            return ""
        today = datetime.now().strftime("%Y-%m-%d")
        cutoff = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        total = 0
        for day, n in self._tool_err_hist.items():
            if day < cutoff or day > today:
                continue
            total += n
        if not total:
            return ""
        s = f"工具结果失败：{total} 次"
        if self._tool_err_last and self._tool_err_last.get("text"):
            s += f"（最近：{self._tool_err_last['text'][:60]}）"
        return s

    # ── 查询回复构建（命令 / 工具共用）──

    def _fmt_cost_units(self, units: dict) -> str:
        """按 (币种, 显示单位) 分桶的费用文本：单位留空=只显示数字"""
        parts = []
        for key, amt in units.items():
            if not amt:
                continue
            _, _, unit = key.partition("|")
            parts.append(f"{unit} {amt:,.4f}" if unit else f"{amt:,.4f}")
        return " · ".join(parts)

    def _build_summary_text(self, range_key: str = "") -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        sb = []
        channel, model, _ = self._resolve_channel_model()
        sb.append(f"【Token 用量统计】渠道 {channel} · 模型 {model}")

        def want(k):
            return not range_key or range_key.strip().lower() == k

        def dur_suffix(b):
            """耗时后缀：均/快/慢（单轮 LLM 响应），无数据不追加"""
            a, mn, mx = _dur_stats(b)
            if a is None:
                return ""
            return f" · 耗时 均{_fmt_dur(a)}·快{_fmt_dur(mn)}·慢{_fmt_dur(mx)}"

        if want("session"):
            s = self._sess
            units, matched = self._session_cost_units()
            line = f"本次会话：{_fmt_num(s['v'])} tokens · 输入 {_fmt_num(s['i'])} · 输出 {_fmt_num(s['o'])} · 缓存 {_fmt_num(s['c'])} · {s['r']} 轮"
            cp = self._fmt_cost_units(units)
            if cp:
                line += " · " + cp
            line += dur_suffix(s)
            if s["e"] > 0:
                line += f" · 出错 {s['e']}"
            sb.append(line)

        d7 = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        d30 = (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")
        for key, label, frm, to in (
                ("today", "今天", today, today),
                ("d7", "近7天", d7, today),
                ("d30", "近30天", d30, today),
                ("total", "累计", "0000-01-01", "9999-12-31")):
            if not want(key):
                continue
            agg = self._range_agg(frm, to)
            units, matched = self._range_cost_units(frm, to)
            line = f"{label}：{_fmt_num(agg['v'])} tokens · 输入 {_fmt_num(agg['i'])} · 输出 {_fmt_num(agg['o'])} · 缓存 {_fmt_num(agg['c'])} · {agg['r']} 轮"
            cp = self._fmt_cost_units(units)
            if cp:
                line += " · " + cp
            line += dur_suffix(agg)
            if agg["e"] > 0:
                line += f" · 出错 {agg['e']}"
            sb.append(line)

        if self.tool_include_balance and self.enable_balance and self.balance_sources:
            try:
                sb.append("账户余额：")
                for src in self.balance_sources:
                    if not src.get("enabled", True):
                        continue
                    st = self._resolve_balance_state(src)
                    name = src.get("name", "")
                    if st["ok"]:
                        unit = self._src_unit(src)
                        sb.append(f"- {name}：{st['balance']:.4f}" + (f" {unit}" if unit else "") + f"（{st.get('msg', '')[:40]}）")
                    else:
                        sb.append(f"- {name}：探测失败（{st['msg']}）")
            except Exception:
                sb.append("- 余额读取失败")

        if self._last_err_text:
            sb.append(f"最近出错：{self._last_err_text}")
        log_err = self._log_err_summary(7)
        if log_err:
            sb.append(log_err)
        tool_err = self._tool_err_summary(7)
        if tool_err:
            sb.append(tool_err)
        return "\n".join(sb)

    # ── 渲染图模式：查加发一体（LLM 工具触发 → 查数据 → 渲染 HTML → 截图 → 直发图片+文本摘要）──

    def _render_bg_url(self) -> str:
        """渲染图背景 URL：优先用户自定义背景（前端同步到插件数据目录 bg_custom.jpg，
        转 data URI 内联，渲染不依赖外网）；否则返回线上随机图 URL（由 Playwright
        浏览器加载，与 WebUI 看板同源）。两种都没有 → 空串 = 纯色背景。"""
        try:
            p = self._data_dir / "bg_custom.jpg"
            if p.exists() and p.stat().st_size > 0:
                return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()
        except Exception:
            pass
        return "https://image.astrdark.cyou/random?type=img&dir=image&orientation=auto&t=" + str(int(time.time()))

    def _render_spark_svg(self, vols) -> str:
        """迷你走势线（与 WebUI sparkHtml 一致：44x26 折线+面积）"""
        arr = [v for v in (vols or []) if v is not None]
        if not arr:
            return ""
        mx = max(arr) or 1
        n = len(arr)
        pts = " ".join(f"{i / (n - 1 or 1) * 44:.1f},{26 - (v / mx * 24):.1f}" for i, v in enumerate(arr))
        return ('<svg viewBox="0 0 44 26" preserveAspectRatio="none">'
                f'<polyline points="{pts}" fill="none" stroke="rgba(56,189,248,.85)" stroke-width="1.4"/>'
                f'<polygon points="0,26 {pts} 44,26" fill="rgba(56,189,248,.13)"/></svg>')

    def _render_cost_bits(self, units: dict) -> str:
        """费用片段：按 (币种, 显示单位) 分桶 → HTML span"""
        bits = []
        for key, amt in units.items():
            if not amt:
                continue
            _, _, unit = key.partition("|")
            if unit:
                bits.append(f'<span class="pts">{amt:,.4f} {_esc(unit)}</span>')
            else:
                bits.append(f'<span class="pts">{amt:,.4f}</span>')
        return " + ".join(bits) if bits else '<span class="cost">—</span>'

    def _build_summary_html(self, range_key: str = "", sess_name: str = "") -> str:
        """构建概览首页完整 HTML（对齐 WebUI 概览页：快照栏 + 五范围卡 + 错误卡 + 按天历史 + 今日小时）。
        数据全部来自插件内存聚合（与 /stats、/history 同源），不依赖 WebUI 前端。
        sess_name：会话显示名（由调用方在 async 上下文 await _resolve_sid_name 后传入）。"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        d7 = (now - timedelta(days=6)).strftime("%Y-%m-%d")
        d30 = (now - timedelta(days=29)).strftime("%Y-%m-%d")
        channel, model, _ = self._resolve_channel_model()
        s = self._sess
        elapsed = max(0, int(time.time() - s["start"]))

        def _fmt_elapsed(sec):
            sec = max(0, int(sec))
            d, h, m, ss = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60, sec % 60
            return (f"{d}天" if d else "") + (f"{h}时" if d or h else "") + (f"{m}分" if d or h or m else "") + f"{ss}秒"

        def _dur_line(b):
            a, mn, mx = _dur_stats(b)
            if a is None:
                return ""
            return f'<div class="d">耗时 均{_fmt_dur(a)}·快{_fmt_dur(mn)}·慢{_fmt_dur(mx)}</div>'

        # 费用兜底：内存 aggs 未匹配到规则时，直接遍历记录现算（与 WebUI /stats 同源逻辑，
        # 保证历史费用一定显示；按 (frm,to,指纹) 缓存避免重复全扫）
        def _cost_units_any(frm, to, units, matched):
            if matched and units:
                return units, matched
            try:
                recs = self._read_records()
                fp = self._calc_fingerprint(recs)
                ck = (frm, to)
                hit = self._range_scan_cache.get(ck)
                if hit and hit[0] == fp:
                    return hit[1]
                units2, matched2 = {}, False
                for r in recs:
                    try:
                        t = _parse_ts(r["t"])
                    except Exception:
                        continue
                    d = t.strftime("%Y-%m-%d")
                    if d < frm or d > to:
                        continue
                    rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
                    if rule is None:
                        continue
                    amt, cur = _rule_cost_ex(rule, r.get("i", 0), r.get("o", 0), r.get("c", 0), t)
                    if amt is None:
                        continue
                    ukey = f"{cur}|{_rule_unit(rule)}"
                    units2[ukey] = units2.get(ukey, 0.0) + amt
                    matched2 = True
                result = (units2, matched2)
                self._range_scan_cache[ck] = (fp, result)
                if len(self._range_scan_cache) > 16:
                    self._range_scan_cache.clear()
                return result
            except Exception:
                return units, matched

        # 五范围卡
        cards = []
        for k in RANGES:
            if k == "session":
                b = s
                units, matched = self._session_cost_units()
            else:
                frm, to = {"today": (today, today), "d7": (d7, today),
                           "d30": (d30, today), "total": ("0000-01-01", "9999-12-31")}[k]
                b = self._range_agg(frm, to)
                units, matched = _cost_units_any(frm, to, *self._range_cost_units(frm, to))
            rate = f"{b['c'] / b['i'] * 100:.1f}%" if b["i"] > 0 else "—"
            cost = self._render_cost_bits(units) if matched else '<span class="cost">—</span>'
            errs = b.get("e", 0)
            sp = "" if k == "session" else self._render_spark_svg(
                [self._days.get(dk, {}).get("v", 0) for dk in sorted(self._days)[-14:]])
            label = RANGE_LABELS[k]
            cards.append(
                f'<div class="card" data-k="{k}"><div class="k">{_esc(label)}</div>'
                f'<div class="topline"><div class="v">{_fmt4(b["v"])}</div>{sp}</div>'
                f'<div class="d">{_fmt_num(b["r"])} 轮 · 输入 {_fmt_num(b["i"])} · 输出 {_fmt_num(b["o"])} · 缓存 {_fmt_num(b["c"])} · 命中率 <span class="rate">{rate}</span></div>'
                f'<div class="d">费用 {cost}' + (f' · 出错 <span class="bad">{errs}</span>' if errs else '') + '</div>'
                + _dur_line(b) + '</div>')

        # 错误卡
        if self._last_err_text:
            cards.append(f'<div class="card errbox"><div class="k">最近出错</div><div class="d">{_esc(self._last_err_text)}</div></div>')
        log_err = self._log_err_summary(7)
        if log_err:
            cards.append(f'<div class="card errbox"><div class="k">后台日志 ERROR</div><div class="d">{_esc(log_err)}</div></div>')
        tool_err = self._tool_err_summary(7)
        if tool_err:
            cards.append(f'<div class="card errbox"><div class="k">工具结果失败</div><div class="d">{_esc(tool_err)}</div></div>')

        # 按天历史（近14天）
        hist_rows = ""
        day_keys = sorted(self._days)[-14:]
        if day_keys:
            mx = max((self._days[dk]["v"] for dk in day_keys), default=1) or 1
            for dk in day_keys:
                x = self._days[dk]
                cur = ' class="cur"' if dk == today else ""
                star = " ★" if dk == today else ""
                hist_rows += (f'<tr{cur}><td>{dk}{star}</td><td>{_fmt4(x["v"])}</td><td>{x["r"]}</td>'
                              f'<td>{_fmt_num(x["i"])}</td><td>{_fmt_num(x["o"])}</td><td>{_fmt_num(x["c"])}</td>'
                              f'<td><div class="bar"><i style="width:{x["v"] / mx * 100:.1f}%"></i></div></td></tr>')
        hist_html = (f'<table><thead><tr><th>日期</th><th>总量</th><th>轮数</th><th>输入</th><th>输出</th><th>缓存</th>'
                     f'<th style="width:30%">分布</th></tr></thead><tbody>{hist_rows}</tbody></table>'
                     if hist_rows else '<div class="note">暂无历史数据</div>')

        # 今日按小时
        hour_cells = ""
        hs = self._hours.get(today, []) or []
        hmx = max((h["v"] for h in hs if h), default=1) or 1
        for h in range(24):
            x = hs[h] if h < len(hs) else None
            if x:
                hour_cells += (f'<div class="h"><span class="hl">{h}时</span><i><b style="height:{max(4, x["v"] / hmx * 100):.1f}%"></b></i>'
                               f'<span class="hv">{_fmt4(x["v"])}</span></div>')
            else:
                hour_cells += f'<div class="h"><span class="hl">{h}时</span><i></i><span class="hv">—</span></div>'
        hours_html = f'<div class="hours">{hour_cells}</div>'

        # 余额（可配置关闭：涉及账户余额，默认开，用户可自行关）
        bal_html = ""
        if self.tool_render_balance:
            bal_bits = []
            for src in self.balance_sources:
                if not src.get("enabled", True):
                    continue
                st = self._resolve_balance_state(src)
                name = src.get("name", "")
                if st["ok"]:
                    unit = self._src_unit(src)
                    bal_bits.append(f'<span class="bal-ok">● {_esc(name)} {st["balance"]:.4f}' + (f" {_esc(unit)}" if unit else "") + '</span>')
                else:
                    bal_bits.append(f'<span class="bal-bad">● {_esc(name)} 探测失败</span>')
            bal_html = '<div class="bal">' + "".join(bal_bits) + '</div>' if bal_bits else ""

        bg = self._render_bg_url()
        bg_layer = ""
        if bg:
            bg_layer = f'<img class="bg" src="{_esc(bg)}" alt="">'

        return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><style>
html{{height:100%;}}
h1{{font-size:20px;margin:0 0 4px;display:flex;align-items:center;gap:10px;}}
body{{margin:0;width:100%;min-height:100%;box-sizing:border-box;position:relative;background:#0f172a;color:#e2e8f0;
font-family:"Segoe UI",system-ui,"Microsoft YaHei",sans-serif;font-size:14px;
display:flex;align-items:center;justify-content:center;}}
.bg{{position:absolute;top:0;left:0;right:0;bottom:0;z-index:0;width:100%;height:100%;object-fit:cover;object-position:center;opacity:.92;}}
.wrap{{position:relative;z-index:1;width:1200px;padding:20px;}}
.head{{background:rgba(15,23,42,.72);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(51,65,85,.5);border-radius:12px;padding:14px 16px;margin-bottom:16px;}}
h1{{font-size:20px;margin:0 0 4px;display:flex;align-items:center;gap:10px;}}
h1 .dot{{width:9px;height:9px;border-radius:50%;background:#34d399;box-shadow:0 0 8px #34d399;}}
.sub{{color:#a3b2c7;font-size:12px;margin:0;word-break:break-all;}}
.snap{{display:flex;gap:16px;align-items:center;flex-wrap:wrap;background:rgba(15,23,42,.72);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(51,65,85,.5);border-radius:12px;padding:12px 16px;margin-bottom:16px;font-size:12.5px;}}
.snap .dot{{width:8px;height:8px;border-radius:50%;background:#34d399;display:inline-block;margin-right:6px;box-shadow:0 0 6px #34d399;}}
.snap .st{{color:#a3b2c7;}}
.snap b{{color:#e2e8f0;}}
.cards{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:16px;}}
.card{{background:rgba(15,23,42,.72);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(51,65,85,.5);border-radius:12px;padding:14px 16px;position:relative;overflow:hidden;}}
.card .k{{color:#a3b2c7;font-size:11px;letter-spacing:.5px;}}
.card .topline{{display:flex;justify-content:space-between;align-items:baseline;}}
.card .v{{font-size:22px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums;}}
.card .d{{color:#a3b2c7;font-size:11px;margin-top:3px;line-height:1.5;word-break:break-all;}}
.card .spark{{position:absolute;right:10px;bottom:8px;width:44%;height:26px;opacity:.85;}}
.card .spark svg{{width:100%;height:100%;display:block;}}
.card.errbox{{background:linear-gradient(rgba(248,113,113,.07),rgba(248,113,113,.07)),rgba(15,23,42,.72);border-left:3px solid #f87171;}}
.rate{{color:#a78bfa;}}.cost{{color:#a3b2c7;}}.pts{{color:#c084fc;}}.bad{{color:#f87171;}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;}}
.box{{background:rgba(15,23,42,.72);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(51,65,85,.5);border-radius:12px;padding:14px 16px;}}
.box h3{{font-size:14px;margin:0 0 10px;color:#e2e8f0;}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;}}
th{{color:#a3b2c7;text-align:left;font-weight:500;padding:6px 8px;border-bottom:1px solid #334155;font-size:11px;letter-spacing:.5px;}}
td{{padding:6px 8px;border-bottom:1px solid rgba(51,65,85,.4);font-variant-numeric:tabular-nums;}}
tr.cur td{{background:rgba(52,211,153,.07);}}
.bar{{height:8px;border-radius:4px;background:#0b1220;overflow:hidden;margin-top:4px;}}
.bar i{{display:block;height:100%;background:linear-gradient(90deg,#38bdf8,#a78bfa);border-radius:4px;}}
.hours{{display:grid;grid-template-columns:repeat(12,1fr);gap:6px;}}
.hours .h{{background:rgba(11,18,32,.6);border-radius:6px;padding:6px;text-align:center;font-size:10px;color:#a3b2c7;}}
.hours .h i{{display:block;height:46px;background:rgba(11,18,32,.6);border-radius:3px;margin:4px 0 2px;position:relative;overflow:hidden;}}
.hours .h i b{{position:absolute;bottom:0;left:0;right:0;background:linear-gradient(180deg,#38bdf8,#6366f1);border-radius:3px 3px 0 0;}}
.hours .h .hl{{display:block;}}
.hours .h .hv{{display:block;font-variant-numeric:tabular-nums;}}
.bal{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;font-size:12px;background:rgba(15,23,42,.72);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(51,65,85,.5);border-radius:12px;padding:10px 16px;}}
.bal-ok{{color:#34d399;}}.bal-bad{{color:#f87171;}}
.note{{color:#a3b2c7;font-size:11.5px;margin-top:8px;line-height:1.6;}}
.footer{{margin-top:14px;padding:10px 0 4px;text-align:center;font-size:12px;color:#8f97ab;line-height:1.7;background:rgba(15,23,42,.72);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(51,65,85,.5);border-radius:12px;padding:10px 16px;}}
</style></head><body>
{bg_layer}
<div class="wrap">
<div class="head">
<h1><span class="dot"></span>Token 用量统计</h1>
<div class="sub">模型 {_esc(model)} · 渠道 {_esc(channel)}</div>
</div>
<div class="snap"><span><span class="dot"></span>{_esc(self._cur_source)}</span>
<span class="st">会话 <b>{s["r"]}</b> 轮 · <b>{_fmt4(s["v"])}</b> Token · 已进行 <b>{_fmt_elapsed(elapsed)}</b></span>
<span class="st">最近一轮：输入 <b>{_fmt_num(self._last_round["i"])}</b> · 输出 <b>{_fmt_num(self._last_round["o"])}</b>{f' · 缓存 <b>{_fmt_num(self._last_round["c"])}</b>' if self._last_round["c"] else ''}</span></div>
<div class="cards">{''.join(cards)}</div>
{bal_html}
<div class="grid2">
<div class="box"><h3>按天历史</h3>{hist_html}</div>
<div class="box"><h3>今日按小时</h3>{hours_html}</div>
</div>
<div class="footer">Token 用量统计 · provide by @znq19 · 生成于 {now.strftime("%Y-%m-%d %H:%M:%S")}</div>
</div>
</body></html>"""

    async def _build_summary_image(self, event: KiraMessageBatchEvent, range_key: str = "") -> str:
        """查加发一体：查数据 → 渲染 HTML → Playwright 截图 → 直发图片 + 文本摘要。
        渲染失败自动降级纯文本（不吞消息）。"""
        sid = event.sid
        try:
            # async 上下文 await 会话昵称（_build_summary_html 是同步方法，不能内部 await）
            sess_name = ""
            if self._sess.get("sid"):
                try:
                    sess_name = await self._resolve_sid_name(self._sess["sid"])
                except Exception:
                    sess_name = ""
            html = self._build_summary_html(range_key, sess_name=sess_name)
            out_dir = self._data_dir / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            png = out_dir / f"summary_{int(time.time() * 1000)}_{os.urandom(3).hex()}.png"
            # 等待背景图（<img>）加载完成；无背景或加载失败直接继续
            wait_js = "document.querySelectorAll('img.bg').length===0 || (document.querySelector('img.bg').complete && document.querySelector('img.bg').naturalWidth>0)"
            await render_html(html, str(png), self._browser, wait_js=wait_js)
            # 直发图片
            await self.ctx.message_processor.send_message_chain(sid, MessageChain([Image(image=str(png))]))
            # 清理旧渲染图：只保留最近 20 张（按文件名时间戳排序，最旧先删）
            try:
                old = sorted(out_dir.glob("summary_*.png"), key=lambda p: p.name)
                for p in old[:-20]:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
            # 文本摘要（bot 自行组织语言转述）
            text = self._build_summary_text(range_key)
            return f"已发送渲染概览图到会话。数据摘要：\n{text}"
        except Exception as e:
            logger.warning(f"[token_stats] 渲染图失败，降级纯文本: {e}")
            return self._build_summary_text(range_key)

    async def _build_query_reply(self, arg: str) -> str:
        arg = arg.strip().lower()
        aliases = {"本次": "session", "今天": "today", "7天": "d7", "近7天": "d7",
                   "30天": "d30", "近30天": "d30", "累计": "total", "余额": "balance"}
        key = aliases.get(arg, arg)
        if key in RANGES:
            text = self._build_summary_text(key)
            return self.cmd_success_template.format(provider=RANGE_LABELS[key], result=text)
        if key == "balance":
            if not self.enable_balance or not self.balance_sources:
                return "未启用余额监测或未配置余额源（插件配置页 → 余额监测）"
            await self._probe_all(wait=True)
            lines = ["💳 账户余额："]
            for src in self.balance_sources:
                if not src.get("enabled", True):
                    continue
                st = self._resolve_balance_state(src)
                name = src.get("name", "")
                if st["ok"]:
                    unit = self._src_unit(src)
                    lines.append(f"- {name}：{st['balance']:.4f}" + (f" {unit}" if unit else "") + f"（{st.get('msg', '')[:40]}）")
                else:
                    lines.append(f"- {name}：探测失败（{st['msg']}）")
            return "\n".join(lines)
        # 默认：全部概览
        return self._build_summary_text("")

    # ── AI 查询函数（维度聚合 / 逐轮明细）──

    def _ai_filter_hit(self, value: str, filter_kw: str) -> bool:
        return not filter_kw or (value or "").lower().find(filter_kw.lower()) >= 0

    def _build_ai_usage(self, dim, range_key, from_date, to_date, model, channel, source, top):
        """维度聚合：dim=channel/model/source/day，支持时间区间与关键字过滤"""
        try:
            d = (dim or "").strip().lower()
            if d not in ("model", "source", "day"):
                d = "channel"
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")

            def is_iso(s):
                return bool(s and re.match(r"^\d{4}-\d{2}-\d{2}$", s.strip()))

            if is_iso(from_date) and is_iso(to_date):
                f, t = from_date.strip(), to_date.strip()
                if f > t:
                    f, t = t, f
                range_label = f + " ~ " + t
            else:
                rg = range_key if range_key in ("today", "d30", "total") else "d7"
                if rg == "today":
                    f, t, range_label = today, today, "今天"
                elif rg == "d30":
                    f, t, range_label = (now - timedelta(days=29)).strftime("%Y-%m-%d"), today, "近30天"
                elif rg == "total":
                    f, t, range_label = "0000-01-01", "9999-12-31", "累计"
                else:
                    f, t, range_label = (now - timedelta(days=6)).strftime("%Y-%m-%d"), today, "近7天"

            filters = []
            if model:
                filters.append("模型~" + model.strip())
            if channel:
                filters.append("渠道~" + channel.strip())
            if source:
                filters.append("来源~" + source.strip())

            dim_label = {"model": "模型", "source": "来源", "day": "日期"}.get(d, "渠道")
            map_agg = {}
            t_r = t_i = t_o = t_c = t_v = 0
            t_dur = {"ds": 0.0, "dc": 0, "dmin": None, "dmax": None}
            errs = 0
            cny_tot = pts_tot = 0.0
            any_matched = False

            for r in self._read_records():
                try:
                    t_dt = _parse_ts(r["t"])
                except Exception:
                    continue
                day = t_dt.strftime("%Y-%m-%d")
                if day < f or day > t:
                    continue
                if not (self._ai_filter_hit(r.get("m", ""), model)
                        and self._ai_filter_hit(r.get("ch", ""), channel)
                        and self._ai_filter_hit(r.get("s", ""), source)):
                    continue
                rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
                amt, cur = _rule_cost_ex(rule, r.get("i", 0), r.get("o", 0), r.get("c", 0), t_dt) if rule else (None, "CNY")
                i, o, c, v = r.get("i", 0), r.get("o", 0), r.get("c", 0), r.get("v", 0)
                key = r.get("m", "") if d == "model" else (r.get("s", "") or "未知") if d == "source" else day if d == "day" else (r.get("ch", "") or "未知")
                if not key:
                    key = "未知"
                a = map_agg.setdefault(key, {"r": 0, "i": 0, "o": 0, "c": 0, "v": 0, "units": {}, "matched": False,
                                             "ds": 0.0, "dc": 0, "dmin": None, "dmax": None})
                a["r"] += 1; a["i"] += i; a["o"] += o; a["c"] += c; a["v"] += v
                dval = _rec_dur(r)
                _dur_acc(a, dval)
                _dur_acc(t_dur, dval)
                if amt is not None:
                    ukey = f"{cur}|{_rule_unit(rule) if rule else ''}"
                    a["units"][ukey] = a["units"].get(ukey, 0.0) + amt
                    a["matched"] = True
                    if cur == "积分":
                        pts_tot += amt
                    else:
                        cny_tot += amt
                    any_matched = True
                t_r += 1; t_i += i; t_o += o; t_c += c; t_v += v
                errs += max(0, int(r.get("e", 0) or 0))

            sb = [f"【用量·按{dim_label}】{range_label}"]
            if filters:
                sb.append(" · 筛选: " + "、".join(filters))
            sb.append("")
            if not map_agg:
                return _clamp_ai_output("".join(sb) + "（该条件下暂无记录）")
            sb.append(f"{dim_label} | 轮次 | 输入 | 输出 | 缓存 | 合计 | 均耗时 | 费用")
            cap = max(1, min(top or 8, 20))
            for k, a in sorted(map_agg.items(), key=lambda x: x[1]["v"], reverse=True)[:cap]:
                cost_txt = "—"
                if a["matched"]:
                    bits = []
                    for ukey, uamt in a["units"].items():
                        _, _, uu = ukey.partition("|")
                        bits.append(f"{uu} {uamt:,.4f}" if uu else f"{uamt:,.4f}")
                    cost_txt = " + ".join(bits)
                sb.append(f"{k} | {a['r']} | {_fmt_num(a['i'])} | {_fmt_num(a['o'])} | {_fmt_num(a['c'])} | {_fmt_num(a['v'])} | {_fmt_dur(_dur_stats(a)[0])} | {cost_txt}")
            if len(map_agg) > cap:
                sb.append(f"（共 {len(map_agg)} 行，仅显示前 {cap} 行，可调大 top 或增加过滤条件）")
            tot_bits = []
            if any_matched:
                if cny_tot:
                    tot_bits.append(f"¥{cny_tot:.4f}")
                if pts_tot:
                    tot_bits.append(f"积分 {pts_tot:,.4f}")
            sb.append(f"合计 | {t_r} | {_fmt_num(t_i)} | {_fmt_num(t_o)} | {_fmt_num(t_c)} | {_fmt_num(t_v)} | {_fmt_dur(_dur_stats(t_dur)[0])} | {' + '.join(tot_bits) if tot_bits else '—'}")
            if errs > 0:
                sb.append(f"出错标记合计：{errs}")
            return _clamp_ai_output("\n".join(sb))
        except Exception as ex:
            return f"用量聚合查询失败：{ex}"

    def _build_ai_records(self, n, model, channel, source, min_input):
        """最近 N 轮逐轮明细，倒序，支持过滤"""
        try:
            want = []
            if model:
                want.append("模型~" + model.strip())
            if channel:
                want.append("渠道~" + channel.strip())
            if source:
                want.append("来源~" + source.strip())
            if min_input is not None:
                want.append(f"输入>{min_input}")
            sb = ["【最近轮次】"]
            if want:
                sb.append(" · 筛选: " + "、".join(want))
            sb.append("")
            sb.append("时间 | 模型 | 来源 | 渠道 | 输入 | 输出 | 缓存 | 合计 | 费用（尾部按需附 出错/耗时）")
            cap = max(1, min(n or 10, 30))
            written = 0
            recs = self._read_records()
            for r in reversed(recs):
                if written >= cap:
                    break
                if not (self._ai_filter_hit(r.get("m", ""), model)
                        and self._ai_filter_hit(r.get("ch", ""), channel)
                        and self._ai_filter_hit(r.get("s", ""), source)):
                    continue
                if min_input is not None and int(r.get("i", 0) or 0) < min_input:
                    continue
                try:
                    t_dt = _parse_ts(r["t"])
                except Exception:
                    t_dt = None
                rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
                amt, cur = _rule_cost_ex(rule, r.get("i", 0), r.get("o", 0), r.get("c", 0), t_dt) if t_dt and rule else (None, "CNY")
                cost_txt = "—"
                if amt is not None:
                    uu = _rule_unit(rule) if rule else ""
                    cost_txt = f"{uu} {amt:,.4f}" if uu else f"{amt:,.4f}"
                ts = t_dt.strftime("%m-%d %H:%M:%S") if t_dt else r.get("t", "")
                row = f"{ts} | {r.get('m', '') or '未知'} | {r.get('s', '') or '未知'} | {r.get('ch', '') or '未知'} | " \
                      f"{_fmt_num(r.get('i', 0))} | {_fmt_num(r.get('o', 0))} | {_fmt_num(r.get('c', 0))} | {_fmt_num(r.get('v', 0))} | {cost_txt}"
                e = int(r.get("e", 0) or 0)
                if e > 0:
                    row += f" | 出错{e}"
                dval = _rec_dur(r)
                if dval is not None:
                    row += f" | 耗时{_fmt_dur(dval)}"
                sb.append(row)
                written += 1
            if written == 0:
                sb.append("（该条件下暂无记录）")
            else:
                sb.append(f"（匹配 {written} 条）" if written < cap else f"（已达条数上限 {cap}，可调大 n 或增加过滤条件）")
            return _clamp_ai_output("\n".join(sb))
        except Exception as ex:
            return f"轮次明细查询失败：{ex}"
    # ── Bot 工具 ──

    @register.tool(
        name="query_token_stats",
        description="查询 Token 用量统计：本次会话/今天/近7天/近30天/累计的 tokens、轮数、估算费用、出错次数，以及已配置的 API 账户余额。用户问到\"用了多少 token / 花了多少钱 / 余额还剩多少 / 出错了吗\"等时调用。render=image 时会把统计结果渲染成一张好看的概览图片直接发送到会话（查加发一体），用户想要\"图片/卡片/截图/好看的形式\"时用 image。",
        params={
            "type": "object",
            "properties": {
                "range": {
                    "type": "string",
                    "enum": ["all", "session", "today", "d7", "d30", "total"],
                    "description": "统计范围：all=全部概览（默认，不传也是全部），session=本次会话，today=今天，d7=近7天，d30=近30天，total=累计",
                    "default": "all",
                },
                "render": {
                    "type": "string",
                    "enum": ["auto", "image", "text"],
                    "description": "输出形式：auto=跟随插件配置（默认，不传也是跟随配置），image=渲染成概览图片并直接发送到会话，text=纯文本。用户想要图片/卡片/好看的形式时传 image",
                    "default": "auto",
                },
            },
            "required": [],
        },
    )
    async def query_token_stats(self, event: KiraMessageBatchEvent, range: str = "all", render: str = "auto") -> str:
        if not self.enabled:
            return "Token 统计未启用（插件配置页 → 基础设置）"
        try:
            # 工具查询余额时先即时探测，保证拿到最新值（与 api-balance 插件行为一致）
            if self.tool_include_balance and self.enable_balance and self.balance_sources:
                await self._probe_all(wait=True)
            # all / auto 是「全部概览 / 跟随配置」的哨兵值：部分模型（Gemini 系列）的
            # function calling 规范不允许 enum 含空字符串，故默认值用显式哨兵而非空串。
            # 空串与未知值一律按默认处理，兼容旧调用与省略参数
            range_key = (range or "").strip().lower()
            if range_key in ("", "all"):
                range_key = ""
            render_key = (render or "").strip().lower()
            effective = render_key if render_key in ("image", "text") else ("image" if self.tool_render_image else "text")
            if effective == "image":
                return await self._build_summary_image(event, range_key)
            return self._build_summary_text(range_key)
        except Exception as e:
            logger.exception("[token_stats] tool query failed")
            return f"查询失败：{e}"

    @register.tool(
        name="query_token_usage",
        description="按维度聚合查询历史 Token 用量与费用（可组合过滤条件）。适合\"哪个模型/渠道/来源用得多、某渠道花了多少钱、按天趋势\"类问题；无参数=近7天·按渠道·前8行。",
        params={
            "type": "object",
            "properties": {
                "dim": {
                    "type": "string",
                    "enum": ["channel", "model", "source", "day"],
                    "description": "聚合维度：channel=渠道(默认)/model=模型/source=来源/day=按天",
                    "default": "channel",
                },
                "range": {
                    "type": "string",
                    "enum": ["d7", "today", "d30", "total"],
                    "description": "时间范围：d7=近7天(默认)/today/d30/total；也可用 from+to 指定区间",
                    "default": "d7",
                },
                "from_date": {"type": "string", "description": "起始日期 YYYY-MM-DD（须与 to_date 同用）"},
                "to_date": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                "model": {"type": "string", "description": "只统计模型名包含此关键字的记录，如 flash"},
                "channel": {"type": "string", "description": "只统计渠道名包含此关键字的记录"},
                "source": {"type": "string", "description": "只统计来源包含此关键字的记录，如 gm/dm"},
                "top": {"type": "integer", "description": "返回行数上限(1-20)，默认8，按Token降序"},
            },
            "required": [],
        },
    )
    async def query_token_usage(self, event: KiraMessageBatchEvent, dim: str = "", range: str = "",
                                from_date: str = "", to_date: str = "", model: str = "", channel: str = "",
                                source: str = "", top: int = 0) -> str:
        if not self.enabled:
            return "Token 统计未启用（插件配置页 → 基础设置）"
        try:
            return self._build_ai_usage(dim, range, from_date, to_date, model, channel, source, top)
        except Exception as e:
            logger.exception("[token_stats] tool usage failed")
            return f"查询失败：{e}"

    @register.tool(
        name="query_token_records",
        description="查询最近N轮对话的逐轮用量明细（时间/模型/来源/渠道/输入/输出/缓存/费用），按时间倒序，可组合过滤；也可用 minInput 查输入超过某 token 数的轮次（定位大上下文）。",
        params={
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "条数(1-30)，默认10"},
                "model": {"type": "string", "description": "只看模型名包含此关键字的轮次"},
                "channel": {"type": "string", "description": "只看渠道名包含此关键字的轮次"},
                "source": {"type": "string", "description": "只看来源包含此关键字的轮次"},
                "minInput": {"type": "integer", "description": "只看输入超过此 token 数的轮次，如 50000 查上下文最大的几轮"},
            },
            "required": [],
        },
    )
    async def query_token_records(self, event: KiraMessageBatchEvent, n: int = 0, model: str = "",
                                  channel: str = "", source: str = "", minInput: int = 0) -> str:
        if not self.enabled:
            return "Token 统计未启用（插件配置页 → 基础设置）"
        try:
            return self._build_ai_records(n or None, model, channel, source, minInput or None)
        except Exception as e:
            logger.exception("[token_stats] tool records failed")
            return f"查询失败：{e}"

    @register.tool(
        name="query_balance",
        description="查询已配置的 API 账户余额（DeepSeek/Kimi/硅基/智谱官方、One-API/New-API 中转站、估算型钱包/积分等）。用户问「余额多少/还剩多少钱/额度够不够/账户还有多少」时调用，只查余额不查用量。",
        params={
            "type": "object",
            "properties": {},
            "required": [],
        },
    )
    async def query_balance(self, event: KiraMessageBatchEvent) -> str:
        if not self.enabled:
            return "Token 统计未启用（插件配置页 → 基础设置）"
        if not self.enable_balance or not self.balance_sources:
            return "未启用余额监测或未配置余额源（插件配置页 → 余额监测）"
        try:
            await self._probe_all(wait=True)
            lines = ["💳 账户余额："]
            for src in self.balance_sources:
                if not src.get("enabled", True):
                    continue
                st = self._resolve_balance_state(src)
                name = src.get("name", "")
                if st["ok"]:
                    unit = self._src_unit(src)
                    lines.append(f"- {name}：{st['balance']:.4f}" + (f" {unit}" if unit else "") + f"（{st.get('msg', '')[:40]}）")
                else:
                    lines.append(f"- {name}：探测失败（{st['msg']}）")
            return "\n".join(lines)
        except Exception as e:
            logger.exception("[token_stats] tool balance failed")
            return f"查询失败：{e}"
    # ── WebUI API（FastAPI 参数注入：query 参数按签名自动解析）──

    @register.api(method="GET", path="/stats", auth=True)
    async def api_stats(self):
        today = datetime.now().strftime("%Y-%m-%d")
        d7 = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        d30 = (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")
        s = self._sess
        channel, model, _ = self._resolve_channel_model()

        def _with_dur(base: dict, b: dict) -> dict:
            """范围聚合 + 耗时（有数据才带 dur 字段，前端据此决定是否显示耗时行）"""
            a, mn, mx = _dur_stats(b)
            if a is not None:
                base["dur"] = {"avg": a, "min": mn, "max": mx}
            return base

        ranges = {
            "session": _with_dur({"v": s["v"], "i": s["i"], "o": s["o"], "c": s["c"], "r": s["r"], "e": s["e"]}, s),
        }
        for _k, _f, _t in (("today", today, today), ("d7", d7, today), ("d30", d30, today),
                           ("total", "0000-01-01", "9999-12-31")):
            _a = self._range_agg(_f, _t)
            ranges[_k] = _with_dur(_a, _a)
        # 费用：按 (币种, 显示单位) 分桶，前端按需展示
        def _cost_pair(k):
            units, matched = k
            arr = [{"unit": uu, "amt": f"{uamt:,.4f}"} for ukey, uamt in units.items() if uamt
                   for uu in [ukey.partition("|")[2]]]
            return {"units": arr, "matched": matched}

        # 兜底：内存 aggs 未匹配到规则时，直接遍历记录现算（保证历史费用一定显示）
        # H3：按 (frm,to,记录数,最新时间戳,规则hash) 缓存，轮询不再每秒全扫
        def _cost_pair_scan(frm, to):
            recs = self._read_records()
            fp = self._calc_fingerprint(recs)
            ck = (frm, to)
            hit = self._range_scan_cache.get(ck)
            if hit and hit[0] == fp:
                return hit[1]
            units, matched = {}, False
            for r in recs:
                try:
                    t = _parse_ts(r["t"])
                except Exception:
                    continue
                d = t.strftime("%Y-%m-%d")
                if d < frm or d > to:
                    continue
                rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
                if rule is None:
                    continue
                amt, cur = _rule_cost_ex(rule, r.get("i", 0), r.get("o", 0), r.get("c", 0), t)
                if amt is None:
                    continue
                ukey = f"{cur}|{_rule_unit(rule)}"
                units[ukey] = units.get(ukey, 0.0) + amt
                matched = True
            arr = [{"unit": uu, "amt": f"{uamt:,.4f}"} for ukey, uamt in units.items() if uamt
                   for uu in [ukey.partition("|")[2]]]
            result = {"units": arr, "matched": matched}
            self._range_scan_cache[ck] = (fp, result)
            if len(self._range_scan_cache) > 16:
                self._range_scan_cache.clear()  # 有界
            return result

        def _cost_any(k, frm, to):
            cp = _cost_pair(k)
            if cp["matched"] and cp["units"]:
                return cp
            return _cost_pair_scan(frm, to)

        costs = {
            "session": _cost_pair(self._session_cost_units()),
            "today": _cost_any(self._range_cost_units(today, today), today, today),
            "d7": _cost_any(self._range_cost_units(d7, today), d7, today),
            "d30": _cost_any(self._range_cost_units(d30, today), d30, today),
            "total": _cost_any(self._range_cost_units("0000-01-01", "9999-12-31"), "0000-01-01", "9999-12-31"),
        }
        errors = {
            "session": s.get("e", 0),
            "today": self._range_agg(today, today)["e"],
            "total": self._range_agg("0000-01-01", "9999-12-31")["e"],
            "last": self._last_err_text,
        }
        # 后台日志 ERROR 分类聚合（近7天）
        log_err = {}
        cutoff = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
        for day, cats in self._log_err_hist.items():
            if day < cutoff or day > today:
                continue
            for cat, n in cats.items():
                log_err[cat] = log_err.get(cat, 0) + n
        errors["log"] = log_err
        errors["logLast"] = self._log_err_last
        # 工具结果失败（近7天）
        tool_err_total = 0
        for day, n in self._tool_err_hist.items():
            if day < cutoff or day > today:
                continue
            tool_err_total += n
        errors["tool"] = tool_err_total
        errors["toolLast"] = self._tool_err_last
        # 余额摘要：当前渠道匹配的源
        bal_summary = {"sources": len(self.balance_sources), "ok": 0, "current": ""}
        for src in self.balance_sources:
            if not src.get("enabled", True):
                continue
            st = self._resolve_balance_state(src)
            if st["ok"]:
                bal_summary["ok"] += 1
                if not bal_summary["current"]:
                    unit = self._src_unit(src)
                    bal_summary["current"] = f"{src.get('name', '')} {st['balance']:.2f}" + (f" {unit}" if unit else "")

        return {
            "model": model,
            "channel": channel,
            "src": self._cur_source,
            "sess_sid": s.get("sid", ""),
            "sess_name": await self._resolve_sid_name(s.get("sid", "")) if s.get("sid") else "",
            "elapsed": max(0, int(time.time() - s["start"])),
            "rounds": s["r"],
            "total": s["v"], "input": s["i"], "output": s["o"], "cached": s["c"],
            "lastInput": self._last_round["i"],
            "lastOutput": self._last_round["o"],
            "lastCached": self._last_round["c"],
            "busy": False,
            "logFile": str(self._log_path),
            "ranges": ranges,
            "costs": costs,
            "errors": errors,
            "balance": bal_summary,
        }

    @register.api(method="GET", path="/history", auth=True)
    async def api_history(self, request: Request):
        """/history?day=YYYY-MM-DD → 单天按小时；否则全部按天"""
        day = request.query_params.get("day")
        if day and re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            hours = [{"h": i, **h} for i, h in enumerate(self._hours.get(day, []) or []) if h]
            return {"day": day, "hours": hours}
        days = [{"d": k, "r": v["r"], "v": v["v"], "i": v["i"], "o": v["o"], "c": v["c"], "e": v["e"]}
                for k, v in sorted(self._days.items())]
        return {"days": days}

    @register.api(method="GET", path="/records", auth=True)
    async def api_records(self, request: Request):
        """/records?n=15 → 最近 n 轮（含费用），倒序"""
        try:
            n = max(1, min(100, int(request.query_params.get("n", "15") or 15)))
        except Exception:
            n = 15
        recs = self._read_records()
        sid_filter = request.query_params.get("sid") or ""
        if sid_filter:
            recs = [r for r in recs if r.get("sid", "") == sid_filter]
        out = []
        for r in reversed(recs[-n:]):
            try:
                t = _parse_ts(r["t"])
            except Exception:
                continue
            rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
            amt, cur = _rule_cost_ex(rule, r.get("i", 0), r.get("o", 0), r.get("c", 0), t) if rule else (None, "CNY")
            out.append({
                "t": t.strftime("%Y-%m-%d %H:%M:%S"),
                "v": r.get("v", 0), "i": r.get("i", 0), "o": r.get("o", 0), "c": r.get("c", 0),
                "m": r.get("m", ""), "s": r.get("s", ""), "ch": r.get("ch", ""),
                "h": r.get("h", ""), "sid": r.get("sid", ""),
                "co": f"{amt:.4f}" if amt is not None else None,
                "cur": cur,
                "unit": _rule_unit(rule) if rule else "",
                "d": _rec_dur(r),
            })
        # 并发解析会话昵称（唯一 sid 去重，缓存命中后零开销）
        uniq = sorted({x["sid"] for x in out if x["sid"]})
        nmap = dict(zip(uniq, await asyncio.gather(*(self._resolve_sid_name(s) for s in uniq))))
        for x in out:
            x["sid_name"] = nmap.get(x["sid"], "")
            x["type"] = "dm" if ":dm:" in x["sid"] else ("gm" if ":gm:" in x["sid"] else "other")
        return {"recs": out}

    @register.api(method="GET", path="/analytics", auth=True)
    async def api_analytics(self, request: Request):
        """/analytics?range=today|d7|d30|total|custom&from=&to="""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        range_key = (request.query_params.get("range") or "today").lower()
        if range_key == "total":
            frm, to = "0000-01-01", "9999-12-31"
        elif range_key == "d7":
            frm, to = (now - timedelta(days=6)).strftime("%Y-%m-%d"), today
        elif range_key == "d30":
            frm, to = (now - timedelta(days=29)).strftime("%Y-%m-%d"), today
        else:
            frm, to = today, today
        frm = request.query_params.get("from") or frm
        to = request.query_params.get("to") or to

        by_source, by_channel, by_model, by_sid = {}, {}, {}, {}
        total = {"r": 0, "i": 0, "o": 0, "c": 0, "v": 0, "units": {}, "matched": False}
        for r in self._read_records():
            try:
                t = _parse_ts(r["t"])
            except Exception:
                continue
            day = t.strftime("%Y-%m-%d")
            if day < frm or day > to:
                continue
            i, o, c, v = r.get("i", 0), r.get("o", 0), r.get("c", 0), r.get("v", 0)
            dval = _rec_dur(r)
            rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
            amt, cur = _rule_cost_ex(rule, i, o, c, t) if rule else (None, "CNY")

            def add(d, k):
                a = d.setdefault(k, {"r": 0, "i": 0, "o": 0, "c": 0, "v": 0, "units": {}, "matched": False, "last_at": "",
                                     "ds": 0.0, "dc": 0, "dmin": None, "dmax": None})
                a["r"] += 1; a["i"] += i; a["o"] += o; a["c"] += c; a["v"] += v
                _dur_acc(a, dval)
                _ts = t.strftime("%Y-%m-%d %H:%M:%S")
                if _ts > a["last_at"]:
                    a["last_at"] = _ts
                if amt is not None:
                    ukey = f"{cur}|{_rule_unit(rule) if rule else ''}"
                    a["units"][ukey] = a["units"].get(ukey, 0.0) + amt
                    a["matched"] = True

            add(by_source, r.get("s", "") or "未知")
            add(by_channel, r.get("ch", "") or "未知")
            add(by_model, r.get("m", "") or "未知")
            add(by_sid, r.get("sid", "") or "未知")
            total["r"] += 1; total["i"] += i; total["o"] += o; total["c"] += c; total["v"] += v
            _dur_acc(total, dval)
            if amt is not None:
                ukey = f"{cur}|{_rule_unit(rule) if rule else ''}"
                total["units"][ukey] = total["units"].get(ukey, 0.0) + amt
                total["matched"] = True

        def fmt_cost(a):
            bits = []
            if a.get("matched"):
                for ukey, uamt in (a.get("units") or {}).items():
                    _, _, uu = ukey.partition("|")
                    bits.append(f"{uu} {uamt:,.4f}" if uu else f"{uamt:,.4f}")
            return " + ".join(bits) if bits else None

        def _dim_row(k, v):
            row = {"k": k, **{kk: vv for kk, vv in v.items() if kk not in ("ds", "dc", "dmin", "dmax")},
                   "cost": fmt_cost(v)}
            row["dur_avg"], row["dur_min"], row["dur_max"] = _dur_stats(v)
            return row

        def dim(d):
            arr = [_dim_row(k, v) for k, v in d.items()]
            arr.sort(key=lambda x: x["v"], reverse=True)
            return arr[:20]

        async def dim_sid(d):
            arr = [_dim_row(k, v) for k, v in d.items()]
            arr.sort(key=lambda x: x["v"], reverse=True)
            out = arr[:20]
            uniq = sorted({x["k"] for x in out if x["k"]})
            nmap = dict(zip(uniq, await asyncio.gather(*(self._resolve_sid_name(s) for s in uniq))))
            for x in out:
                x["name"] = nmap.get(x["k"], "")
            return out

        _ta, _tmin, _tmax = _dur_stats(total)
        return {
            "from": frm, "to": to,
            "total": {"r": total["r"], "i": total["i"], "o": total["o"], "c": total["c"], "v": total["v"],
                      "cost": fmt_cost(total), "dur_avg": _ta, "dur_min": _tmin, "dur_max": _tmax},
            "bySource": dim(by_source),
            "byChannel": dim(by_channel),
            "byModel": dim(by_model),
            "bySid": await dim_sid(by_sid),
        }

    # ── 会话昵称解析（OneBot，带 1 小时缓存，失败降级为 sid）──
    def _ensure_ada(self):
        if self._ada_obj:
            return
        try:
            if hasattr(self.ctx.adapter_mgr, 'get_adapters'):
                for name, ada in self.ctx.adapter_mgr.get_adapters().items():
                    if hasattr(ada, 'info') and str(getattr(ada.info, 'platform', '')).lower() == "qq":
                        self._ada_obj = ada
                        return
            if hasattr(self.ctx.adapter_mgr, '_adapters'):
                for name, ada in self.ctx.adapter_mgr._adapters.items():
                    if hasattr(ada, 'info') and str(getattr(ada.info, 'platform', '')).lower() == "qq":
                        self._ada_obj = ada
                        return
        except Exception:
            pass

    async def _call_onebot(self, action: str, params: dict, timeout: float = 4.0):
        self._ensure_ada()
        if not self._ada_obj:
            return None
        try:
            ob_client = self._ada_obj.get_client()
            res = await ob_client.send_action(action, params, timeout=timeout)
            return res
        except Exception:
            return None

    async def _resolve_sid_name(self, sid: str) -> str:
        """qq:dm:12345 → 周武(12345)；qq:gm:12345 → 群名(12345)；失败降级原样"""
        try:
            if ":dm:" in sid:
                uid = sid.split(":dm:", 1)[1]
                now = time.time()
                hit = self._name_cache.get(("u", uid))
                if hit and now - hit[1] < 3600:
                    name = hit[0]
                else:
                    name = ""
                    res = await self._call_onebot("get_stranger_info", {"user_id": int(uid)})
                    if res and res.get("status") == "ok":
                        name = (res.get("data") or {}).get("nickname", "") or ""
                    if name:
                        self._name_cache[("u", uid)] = (name, now)
                return f"{name}({uid})" if name else sid
            if ":gm:" in sid:
                gid = sid.split(":gm:", 1)[1]
                now = time.time()
                hit = self._name_cache.get(("g", gid))
                if hit and now - hit[1] < 3600:
                    name = hit[0]
                else:
                    name = ""
                    res = await self._call_onebot("get_group_info", {"group_id": int(gid)})
                    if res and res.get("status") == "ok":
                        name = (res.get("data") or {}).get("group_name", "") or ""
                    if name:
                        self._name_cache[("g", gid)] = (name, now)
                return f"{name}({gid})" if name else sid
        except Exception:
            pass
        return sid

    @register.api(method="GET", path="/sessions", auth=True)
    async def api_sessions(self, request: Request):
        """/sessions?range=today|d7|d30|total|custom&from=&to=
        会话级统计：细到每个 sid（如 qq:dm:12345 / qq:gm:12345），
        并按私聊(dm)/群聊(gm)/其他 整体归类汇总"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        range_key = (request.query_params.get("range") or "today").lower()
        if range_key == "total":
            frm, to = "0000-01-01", "9999-12-31"
        elif range_key == "d7":
            frm, to = (now - timedelta(days=6)).strftime("%Y-%m-%d"), today
        elif range_key == "d30":
            frm, to = (now - timedelta(days=29)).strftime("%Y-%m-%d"), today
        else:
            frm, to = today, today
        frm = request.query_params.get("from") or frm
        to = request.query_params.get("to") or to

        sessions = {}
        for r in self._read_records():
            try:
                t = _parse_ts(r["t"])
            except Exception:
                continue
            day = t.strftime("%Y-%m-%d")
            if day < frm or day > to:
                continue
            sid = r.get("sid", "") or "未知"
            a = sessions.setdefault(sid, {"r": 0, "i": 0, "o": 0, "c": 0, "v": 0, "units": {}, "matched": False, "last_at": "",
                                          "ds": 0.0, "dc": 0, "dmin": None, "dmax": None})
            i, o, c, v = r.get("i", 0), r.get("o", 0), r.get("c", 0), r.get("v", 0)
            a["r"] += 1; a["i"] += i; a["o"] += o; a["c"] += c; a["v"] += v
            _dur_acc(a, _rec_dur(r))
            _ts = t.strftime("%Y-%m-%d %H:%M:%S")
            if _ts > a["last_at"]:
                a["last_at"] = _ts
            rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
            amt, cur = _rule_cost_ex(rule, i, o, c, t) if rule else (None, "CNY")
            if amt is not None:
                ukey = f"{cur}|{_rule_unit(rule) if rule else ''}"
                a["units"][ukey] = a["units"].get(ukey, 0.0) + amt
                a["matched"] = True

        def _sess_type(sid: str) -> str:
            if ":dm:" in sid:
                return "dm"
            if ":gm:" in sid:
                return "gm"
            return "other"

        groups = {
            "dm": {"label": "私聊", "r": 0, "i": 0, "o": 0, "c": 0, "v": 0, "units": {}, "matched": False, "sessions": 0},
            "gm": {"label": "群聊", "r": 0, "i": 0, "o": 0, "c": 0, "v": 0, "units": {}, "matched": False, "sessions": 0},
            "other": {"label": "其他", "r": 0, "i": 0, "o": 0, "c": 0, "v": 0, "units": {}, "matched": False, "sessions": 0},
        }
        arr = []
        for sid, a in sessions.items():
            st = _sess_type(sid)
            g = groups[st]
            g["sessions"] += 1
            for k in ("r", "i", "o", "c", "v"):
                g[k] += a[k]
            for uk, uv in a["units"].items():
                g["units"][uk] = g["units"].get(uk, 0.0) + uv
            if a["matched"]:
                g["matched"] = True
            arr.append({"sid": sid, "type": st, **a})
        arr.sort(key=lambda x: x["v"], reverse=True)

        def fmt_cost(a):
            bits = []
            if a.get("matched"):
                for ukey, uamt in (a.get("units") or {}).items():
                    _, _, uu = ukey.partition("|")
                    bits.append(f"{uu} {uamt:,.4f}" if uu else f"{uamt:,.4f}")
            return " + ".join(bits) if bits else None

        names = await asyncio.gather(*(self._resolve_sid_name(x["sid"]) for x in arr[:50]))
        sess_out = []
        for x, name in zip(arr[:50], names):
            _a, _mn, _mx = _dur_stats(x)
            sess_out.append({
                "sid": x["sid"], "name": name,
                "type": x["type"], "r": x["r"], "i": x["i"], "o": x["o"],
                "c": x["c"], "v": x["v"], "cost": fmt_cost(x), "last_at": x["last_at"],
                "dur_avg": _a, "dur_min": _mn, "dur_max": _mx,
            })
        return {
            "from": frm, "to": to,
            "groups": {k: {**v, "cost": fmt_cost(v)} for k, v in groups.items()},
            "sessions": sess_out,
        }

    @register.api(method="GET", path="/trend", auth=True)
    async def api_trend(self, request: Request):
        """/trend?range=today|d7|d30|total  → 按天时间趋势；
        &day=YYYY-MM-DD → 单天按小时桶（含模型费用分色数据）；
        &day=...&hour=N → 单小时按 5 分钟桶（含模型费用分色数据）。
        每桶 {d, r, v, i, o, c, e, da/dmin/dmax(单轮耗时秒，无数据为 None), models:[{name,cny,pts}]}；顶层带 Top8 模型 topModels。"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        range_key = (request.query_params.get("range") or "d7").lower()
        day = (request.query_params.get("day") or "").strip()
        hour_s = (request.query_params.get("hour") or "").strip()

        if re.match(r"^\d{4}-\d{2}-\d{2}$", day) and day in self._hours and not hour_s:
            # 单天按小时（不带 hour 参数才走这里；带 hour 下钻到 5 分钟桶）
            hours = []
            for h, hr in enumerate(self._hours[day]):
                if not hr:
                    continue
                hours.append({
                    "d": f"{day} {h:02d}:00", "h": h,
                    "r": hr["r"], "v": hr["v"], "i": hr["i"], "o": hr["o"], "c": hr["c"], "e": hr["e"],
                    "models": self._bucket_fee_models(hr["aggs"]),
                    **_dur_fields(hr),
                })
            top = self._all_top_models(day, day)
            return {"from": day, "to": day, "day": day, "unit": "hour", "days": hours, "topModels": top}

        if day and hour_s.isdigit() and day in self._mins:
            h = int(hour_s)
            if 0 <= h < 24:
                buckets = []
                row = self._mins[day][h]
                if row:
                    for m5, mb in enumerate(row):
                        if not mb:
                            continue
                        buckets.append({
                            "d": f"{day} {h:02d}:{m5 * 5:02d}", "h": h, "m": m5,
                            "r": mb["r"], "v": mb["v"], "i": mb["i"], "o": mb["o"], "c": mb["c"], "e": mb["e"],
                            "models": self._bucket_fee_models(mb["aggs"]),
                            **_dur_fields(mb),
                        })
                top = self._all_top_models()
                return {"from": day, "to": day, "day": day, "hour": h, "unit": "min5",
                        "days": buckets, "topModels": top}

        # 按天（内存按天聚合 self._days 现算，零磁盘扫描；缺天补 0 桶；total 超 62 天按月聚合）
        if range_key == "total" or range_key == "all":
            if self._days:
                frm, to = min(self._days.keys()), max(self._days.keys())
            else:
                frm, to = today, today
        elif range_key == "d30":
            frm, to = (now - timedelta(days=29)).strftime("%Y-%m-%d"), today
        elif range_key == "today":
            frm, to = today, today
        else:
            frm, to = (now - timedelta(days=6)).strftime("%Y-%m-%d"), today

        day_keys = sorted(k for k in self._days if frm <= k <= to)
        span = len(day_keys)
        top_models = {}
        if span > 62:
            # 累计且跨度大 → 按月聚合（避免上百根柱）
            mbuckets = {}
            for dk in day_keys:
                mk2 = dk[:7]
                b = mbuckets.setdefault(mk2, {"d": mk2, "r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0,
                                              "_aggs": {}, "_models": {},
                                              "ds": 0.0, "dc": 0, "dmin": None, "dmax": None})
                src = self._days[dk]
                for kk in ("r", "v", "i", "o", "c", "e"):
                    b[kk] += src[kk]
                if src.get("dc"):
                    b["ds"] += src.get("ds", 0.0); b["dc"] += src["dc"]
                    if src.get("dmin") is not None and (b["dmin"] is None or src["dmin"] < b["dmin"]):
                        b["dmin"] = src["dmin"]
                    if src.get("dmax") is not None and (b["dmax"] is None or src["dmax"] > b["dmax"]):
                        b["dmax"] = src["dmax"]
                for m in self._bucket_fee_models(src["aggs"]):
                    bm = b["_models"].setdefault(m["name"], {"name": m["name"], "cny": 0.0, "pts": 0.0})
                    bm["cny"] += m["cny"]; bm["pts"] += m["pts"]
                    tm = top_models.setdefault(m["name"], {"name": m["name"], "cny": 0.0, "pts": 0.0})
                    tm["cny"] += m["cny"]; tm["pts"] += m["pts"]
            days = []
            for mk2 in sorted(mbuckets.keys()):
                b = mbuckets[mk2]
                b["models"] = sorted(b.pop("_models").values(),
                                     key=lambda x: x["cny"] + x["pts"] / 500, reverse=True)[:8]
                b.pop("_aggs", None)
                b.update(_dur_fields(b))
                for kk in ("ds", "dc", "dmin", "dmax"):
                    b.pop(kk, None)
                days.append(b)
            unit = "month"
        else:
            # 从 frm 到 to 逐日补齐 0 值桶（漏天不错位）
            days = []
            try:
                d0 = datetime.strptime(frm, "%Y-%m-%d")
                d1 = datetime.strptime(to, "%Y-%m-%d")
                cur_d = d0
                while cur_d <= d1:
                    dk = cur_d.strftime("%Y-%m-%d")
                    src = self._days.get(dk)
                    if src:
                        models_l = self._bucket_fee_models(src["aggs"])
                        days.append({"d": dk, "r": src["r"], "v": src["v"], "i": src["i"],
                                     "o": src["o"], "c": src["c"], "e": src["e"], "models": models_l,
                                     **_dur_fields(src)})
                        for m in models_l:
                            tm = top_models.setdefault(m["name"], {"name": m["name"], "cny": 0.0, "pts": 0.0})
                            tm["cny"] += m["cny"]; tm["pts"] += m["pts"]
                    else:
                        days.append({"d": dk, "r": 0, "v": 0, "i": 0, "o": 0, "c": 0, "e": 0, "models": [],
                                     "da": None, "dmin": None, "dmax": None})
                    cur_d += timedelta(days=1)
            except ValueError:
                for dk in day_keys:
                    src = self._days[dk]
                    days.append({"d": dk, "r": src["r"], "v": src["v"], "i": src["i"],
                                 "o": src["o"], "c": src["c"], "e": src["e"],
                                 "models": self._bucket_fee_models(src["aggs"]),
                                 **_dur_fields(src)})
            unit = "day"
        models = sorted(top_models.values(), key=lambda x: x["cny"] + x["pts"] / 500, reverse=True)[:8]
        return {"from": frm, "to": to, "unit": unit, "days": days, "topModels": models}

    def _bucket_fee_models(self, aggs: dict):
        """桶内按模型聚合费用（内存 aggs 现算，无磁盘扫描）→ [{name,cny,pts}] Top8"""
        per = {}
        for mkey, slots in (aggs or {}).items():
            parts = mkey.split("\u001F")
            model = parts[0] if len(parts) > 0 else ""
            channel = parts[1] if len(parts) > 1 else ""
            host = parts[2] if len(parts) > 2 else ""
            rule = _match_rule(self.rules, channel, model, host)
            if rule is None:
                continue
            cur = _rule_currency(rule)
            for is_peak, agg in ((False, slots[0]), (True, slots[1])):
                if agg is None:
                    continue
                pk = bool(rule.get("peak_enabled", True)) and is_peak
                hit = rule.get("hit_peak" if pk else "hit_off", 0) or 0
                miss = rule.get("miss_peak" if pk else "miss_off", 0) or 0
                out = rule.get("out_peak" if pk else "out_off", 0) or 0
                amt = (agg["c"] * hit + max(0, agg["i"] - agg["c"]) * miss + agg["o"] * out) / 1_000_000
                m = per.setdefault(model or "未知", {"name": model or "未知", "cny": 0.0, "pts": 0.0})
                if cur == "积分":
                    m["pts"] += amt
                else:
                    m["cny"] += amt
        arr = sorted(per.values(), key=lambda x: x["cny"] + x["pts"] / 500, reverse=True)[:8]
        return [x for x in arr if x["cny"] or x["pts"]]

    def _all_top_models(self, frm: str = "", to: str = ""):
        """Top8 模型（费用分色图例用）；frm/to 非空时只统计该日期范围（按天）"""
        top = {}
        for r in self._read_records():
            try:
                t = _parse_ts(r["t"])
            except Exception:
                continue
            if frm or to:
                d = t.strftime("%Y-%m-%d")
                if (frm and d < frm) or (to and d > to):
                    continue
            rule = _match_rule(self.rules, r.get("ch", ""), r.get("m", ""), r.get("h", ""))
            amt, cur = _rule_cost_ex(rule, r.get("i", 0), r.get("o", 0), r.get("c", 0), t) if rule else (None, "CNY")
            if amt is None:
                continue
            mk = r.get("m", "") or "未知"
            m = top.setdefault(mk, {"name": mk, "cny": 0.0, "pts": 0.0})
            if cur == "积分":
                m["pts"] += amt
            else:
                m["cny"] += amt
        return sorted(top.values(), key=lambda x: x["cny"] + x["pts"] / 500, reverse=True)[:8]

    @register.api(method="GET", path="/models", auth=True)
    async def api_models(self):
        """KiraAI 已配置的全部 LLM 模型列表（provider:model），供估算型余额源「关联模型」下拉选择"""
        models = []
        try:
            pm = self.ctx.provider_mgr
            if pm is not None:
                for pid in pm.get_all_providers():
                    try:
                        infos = pm.get_model_infos(pid)
                    except Exception:
                        continue
                    for info in infos:
                        try:
                            mt = getattr(info, "model_type", None)
                            if mt is not None and getattr(mt, "value", "") != "llm":
                                continue
                        except Exception:
                            pass
                        mid = getattr(info, "model_id", "") or ""
                        pname = getattr(info, "provider_name", "") or pid
                        if mid:
                            models.append({"key": f"{pid}:{mid}", "label": f"{pname} / {mid}"})
        except Exception:
            pass
        models.sort(key=lambda x: x["label"].lower())
        return {"models": models}

    @register.api(method="GET", path="/balance", auth=True)
    async def api_balance(self, request: Request):
        """/balance?refresh=1 → 先强制即时探测再返回；返回完整配置字段（含禁用源），
        展示与可视化编辑器共用同一数据源——编辑回填不丢配置、保存不丢禁用源"""
        if (request.query_params.get("refresh") or "") == "1":
            await self._probe_all(wait=True)
        sources = []
        for src in self.balance_sources:
            if not src.get("enabled", True):
                # 禁用源不探测，只取缓存状态
                st = self._bal_state_of(src.get("name", ""))
            else:
                st = self._resolve_balance_state(src)
            item = {
                "name": src.get("name", ""),
                "type": (src.get("type") or "auto").strip().lower(),
                "enabled": bool(src.get("enabled", True)),
                "est": self._is_est(src),
                "ok": st["ok"],
                "balance": f"{st['balance']:.4f}" if st["ok"] else "",
                "currency": st.get("currency", "CNY"),
                "unit": self._src_unit(src),
                "at": st.get("at", ""),
                "msg": st.get("msg", ""),
            }
            # 派生源（简易文本/平台快捷配置派生）带 origin 标记：前端据此禁用编辑/删除/开关
            if src.get("_origin"):
                item["origin"] = src["_origin"]
            # 配置字段（可视化编辑器回填用，含禁用源）；api_key 掩码回传防明文泄露
            for k in ("url", "api_key", "api_user", "json_path", "quota_conversion",
                      "daily_quota", "anchor_balance", "refresh_time", "anchor_at", "model_ref"):
                v = src.get(k)
                if v is not None:
                    if k == "api_key" and v:
                        v = _mask_api_key(str(v))
                    item[k] = v
            sources.append(item)
        return {"interval": max(60, self.balance_interval), "unit": self.balance_unit, "sources": sources}

    @register.api(method="POST", path="/bg-sync", auth=True)
    async def api_bg_sync(self, request: Request):
        """前端把自定义背景图（data URL）同步到服务端，供渲染图模式使用。
        body: {"data": "data:image/jpeg;base64,..."} 或 {"data": ""} 清除。"""
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "msg": "请求体不是合法 JSON"}
        data = body.get("data") or ""
        try:
            if data.startswith("data:image/"):
                b64 = data.split(",", 1)[1] if "," in data else ""
                raw = base64.b64decode(b64)
                if len(raw) > 2_000_000:
                    return {"ok": False, "msg": "图片超过 2MB 限制"}
                (self._data_dir / "bg_custom.jpg").write_bytes(raw)
                return {"ok": True, "msg": "背景已同步"}
            (self._data_dir / "bg_custom.jpg").unlink(missing_ok=True)
            return {"ok": True, "msg": "背景已清除"}
        except Exception as e:
            logger.warning(f"[token_stats] 背景同步失败: {e}")
            return {"ok": False, "msg": f"背景同步失败：{e}"}

    @register.api(method="POST", path="/balance-config", auth=True)
    async def api_balance_config(self, request: Request):
        """保存余额监测源（WebUI 可视化编辑器）：整体替换 balance_sources 并热重载"""
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "msg": "请求体不是合法 JSON"}
        new_sources = body.get("sources")
        if not isinstance(new_sources, list):
            return {"ok": False, "msg": "sources 必须是数组"}
        # 清洗：只保留合法字段，剔除空项；派生源（简易文本/平台快捷配置）不落盘到
        # balance_sources——它们始终只由各自原始配置项派生，避免整体写回后热重载重复增殖
        cleaned = []
        for s in new_sources:
            if not isinstance(s, dict):
                continue
            if s.get("_origin") or s.get("origin"):
                continue
            name = str(s.get("name") or "").strip()
            if not name:
                continue
            item = {"name": name, "type": (str(s.get("type") or "auto").strip().lower() or "auto"),
                    "enabled": bool(s.get("enabled", True))}
            for k in ("url", "api_key", "api_user", "json_path", "currency", "model_ref"):
                v = s.get(k)
                if v not in (None, ""):
                    item[k] = str(v).strip()
            if "unit" in s and s.get("unit") is not None:
                item["unit"] = str(s.get("unit") or "").strip()
            for k in ("quota_conversion", "daily_quota", "anchor_balance"):
                v = s.get(k)
                if v not in (None, ""):
                    try:
                        item[k] = float(v)
                    except (TypeError, ValueError):
                        pass
            if s.get("refresh_time"):
                item["refresh_time"] = str(s["refresh_time"]).strip()
            if s.get("anchor_at"):
                item["anchor_at"] = str(s["anchor_at"]).strip()
            elif (s.get("type") or "auto").strip().lower() in EST_TYPES and s.get("anchor_balance") not in (None, ""):
                # 仅估算型源：WebUI 表单无 anchor_at 输入，填了「当前余额(对表)」但没给时间时锚定取当前；
                # 编辑已有源时前端会回填原 anchor_at（见 balEditOpen），不会漂移
                item["anchor_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            # api_key 未改动（值等于掩码）时保留原值，防掩码覆盖真 key
            if "api_key" in item and "****" in item["api_key"]:
                for old in self.balance_sources:
                    if old.get("name") == name and old.get("api_key"):
                        item["api_key"] = old["api_key"]
                        break
            cleaned.append(item)
        try:
            pm = self.ctx.plugin_mgr
            if pm is None:
                return {"ok": False, "msg": "plugin_mgr 不可用"}
            cfg = pm.get_plugin_config("KiraAI_token_stats_plugin")
            sec = dict(cfg.get("section_balance") or {})
            sec["balance_sources"] = cleaned
            cfg["section_balance"] = sec
            await pm.update_plugin_config("KiraAI_token_stats_plugin", cfg)
            return {"ok": True, "count": len(cleaned)}
        except Exception as e:
            logger.warning(f"[token_stats] 保存余额配置失败: {e}")
            return {"ok": False, "msg": f"保存失败: {e}"}

    @register.api(method="POST", path="/balance-interval", auth=True)
    async def api_balance_interval(self, request: Request):
        """保存余额轮询间隔（秒，最小 5）"""
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "msg": "请求体不是合法 JSON"}
        try:
            interval = max(60, int(body.get("interval") or 600))
        except (TypeError, ValueError):
            return {"ok": False, "msg": "间隔必须是正整数（秒）"}
        try:
            pm = self.ctx.plugin_mgr
            if pm is None:
                return {"ok": False, "msg": "plugin_mgr 不可用"}
            cfg = pm.get_plugin_config("KiraAI_token_stats_plugin")
            sec = dict(cfg.get("section_balance") or {})
            sec["balance_interval"] = interval
            cfg["section_balance"] = sec
            await pm.update_plugin_config("KiraAI_token_stats_plugin", cfg)
            return {"ok": True, "interval": interval}
        except Exception as e:
            logger.warning(f"[token_stats] 保存轮询间隔失败: {e}")
            return {"ok": False, "msg": f"保存失败: {e}"}

    @register.api(method="GET", path="/alert", auth=True)
    async def api_alert(self):
        """预警配置读取：投递会话 + 规则（含已触发状态，供前端展示）。
        _alert_fired 值为 dict {count, at, day}（v1.4.2+）或旧格式 float 时间戳，兼容两者"""
        fired = {}
        for k, v in self._alert_fired.items():
            if isinstance(v, dict):
                at = v.get("at", 0)
            else:
                at = v
            try:
                fired[k] = datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError):
                fired[k] = ""
        return {
            "enable": self.enable_alert,
            "deliveries": self.alert_deliveries,
            "rules": self.alert_rules,
            "fired": fired,
        }

    @register.api(method="POST", path="/alert-config", auth=True)
    async def api_alert_config(self, request: Request):
        """保存预警配置（投递会话 + 规则整体替换）并热重载"""
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "msg": "请求体不是合法 JSON"}
        deliveries = body.get("deliveries")
        rules = body.get("rules")
        if not isinstance(deliveries, list) or not isinstance(rules, list):
            return {"ok": False, "msg": "deliveries 和 rules 必须是数组"}
        # 清洗投递会话
        clean_d = []
        for d in deliveries:
            if not isinstance(d, dict):
                continue
            did = str(d.get("id") or "").strip()
            sid = str(d.get("sid") or "").strip()
            if not did or not sid:
                continue
            clean_d.append({"id": did, "name": str(d.get("name") or did).strip(),
                            "sid": sid, "enabled": bool(d.get("enabled", True))})
        # 清洗规则
        clean_r = []
        for r in rules:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or "").strip()
            if not rid:
                continue
            rtype = (str(r.get("type") or "balance").strip().lower() or "balance")
            if rtype not in ("balance", "token", "cost"):
                continue
            item = {"id": rid, "enabled": bool(r.get("enabled", True)), "type": rtype,
                    "name": str(r.get("name") or "").strip(),
                    "trigger": (str(r.get("trigger") or "once").strip().lower() or "once"),
                    "target": str(r.get("target") or "").strip(),
                    "session": str(r.get("session") or "").strip(),
                    "op": (str(r.get("op") or "le").strip().lower() or "le"),
                    "mode": (str(r.get("mode") or "direct").strip().lower() or "direct"),
                    "message": str(r.get("message") or "").strip(),
                    "llm_message": str(r.get("llm_message") or "").strip(),
                    "send_image": bool(r.get("send_image", False)),
                    "llm_send_image": bool(r.get("llm_send_image", False)),
                    "currency": str(r.get("currency") or "").strip(),
                    "time_start": str(r.get("time_start") or "").strip(),
                    "time_end": str(r.get("time_end") or "").strip()}
            for k in ("threshold", "time_window", "max_alerts", "alert_interval"):
                v = r.get(k)
                if v not in (None, ""):
                    try:
                        item[k] = float(v) if k == "threshold" else int(v)
                    except (TypeError, ValueError):
                        pass
            dl = r.get("deliveries")
            if isinstance(dl, list):
                item["deliveries"] = [str(x) for x in dl if str(x).strip()]
            elif dl:
                item["deliveries"] = [str(dl)]
            clean_r.append(item)
        try:
            pm = self.ctx.plugin_mgr
            if pm is None:
                return {"ok": False, "msg": "plugin_mgr 不可用"}
            cfg = pm.get_plugin_config("KiraAI_token_stats_plugin")
            sec = dict(cfg.get("section_alert") or {})
            sec["alert_deliveries"] = clean_d
            sec["alert_rules"] = clean_r
            cfg["section_alert"] = sec
            await pm.update_plugin_config("KiraAI_token_stats_plugin", cfg)
            return {"ok": True, "deliveries": len(clean_d), "rules": len(clean_r)}
        except Exception as e:
            logger.warning(f"[token_stats] 保存预警配置失败: {e}")
            return {"ok": False, "msg": f"保存失败: {e}"}

    @register.api(method="POST", path="/alert-test", auth=True)
    async def api_alert_test(self, request: Request):
        """测试投递：按规则立即发一条提醒到目标会话（不改变已触发状态）"""
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "msg": "请求体不是合法 JSON"}
        rid = str(body.get("id") or "").strip()
        rule = None
        for r in self.alert_rules:
            if str(r.get("id") or "") == rid:
                rule = r
                break
        if rule is None:
            return {"ok": False, "msg": f"规则 {rid} 不存在"}
        sids = self._alert_delivery_sids(rule)
        if not sids:
            return {"ok": False, "msg": "该规则未配置可用的投递会话"}
        # 取当前值构造提醒文本（不触发、不改状态）
        rtype = (rule.get("type") or "balance").strip().lower()
        if rtype == "balance":
            got = self._alert_balance_value(rule)
            value, unit = (got[0], got[1]) if got else (0, "")
        else:
            got = self._alert_usage_value(rule)
            value, unit = (got[0], got[1]) if got else (0, "")
        text = self._alert_fmt_value(rule, value, unit, mode=(rule.get('mode') or 'direct'))
        text = f"[测试] {text}"
        mode = (rule.get("mode") or "direct").strip().lower()
        if mode == "llm":
            try:
                await self.ctx.publish_notice(sids[0], MessageChain([Text(text)]), is_mentioned=True)
            except Exception as e:
                return {"ok": False, "msg": f"llm 投递失败: {e}"}
        else:
            for sid in sids:
                try:
                    await self.ctx.message_processor.send_message_chain(sid, MessageChain([Text(text)]))
                except Exception as e:
                    return {"ok": False, "msg": f"直发失败 {sid}: {e}"}
        return {"ok": True, "msg": f"已投递到 {len(sids)} 个会话"}

    @register.api(method="POST", path="/alert-reset", auth=True)
    async def api_alert_reset(self, request: Request):
        """重置规则已触发状态（前端手动复位，如充值后想立即重新预警）"""
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "msg": "请求体不是合法 JSON"}
        rid = str(body.get("id") or "").strip()
        if rid:
            self._alert_fired.pop(rid, None)
            return {"ok": True, "msg": f"已重置规则 {rid}"}
        self._alert_fired.clear()
        return {"ok": True, "msg": "已重置全部规则"}

    @register.api(method="GET", path="/alert-sources", auth=True)
    async def api_alert_sources(self):
        """预警可选项：余额源名 / 来源标签 / 会话列表（前端下拉用）"""
        bal_names = [str(s.get("name") or "") for s in self.balance_sources if s.get("enabled", True) and s.get("name")]
        src_labels = []
        seen = set()
        for rec in self._read_records():
            s = str(rec.get("s", "") or "")
            if s and s not in seen:
                seen.add(s)
                src_labels.append(s)
        sessions = []
        for sid, s in self._sess_map.items():
            if sid and s.get("r"):
                sessions.append({"sid": sid, "name": sid})
        return {"balance_sources": bal_names, "source_labels": src_labels, "sessions": sessions}

    @register.api(method="GET", path="/pricing", auth=True)
    async def api_pricing(self):
        return {"rules": self.rules,
                "peak_profiles": [{"name": p["name"], "windows": [list(w) for w in p["windows"]]} for p in _PEAK_PROFILES]}

    @register.api(method="POST", path="/pricing-config", auth=True)
    async def api_pricing_config(self, request: Request):
        """保存价格规则（WebUI 可视化编辑器）：整体替换 rules 并热重载"""
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "msg": "请求体不是合法 JSON"}
        new_rules = body.get("rules")
        if not isinstance(new_rules, list):
            return {"ok": False, "msg": "rules 必须是数组"}
        # 清洗：只保留合法字段，剔除空项
        cleaned = []
        for r in new_rules:
            if not isinstance(r, dict):
                continue
            name = str(r.get("name") or "").strip()
            if not name:
                continue
            item = {"name": name, "peak_enabled": bool(r.get("peak_enabled", True)),
                    "enabled": r.get("enabled", True) != False}
            for k in ("url_match", "model_match", "channel_match", "currency", "peak_profile"):
                v = r.get(k)
                if v not in (None, ""):
                    item[k] = str(v).strip()
            if "unit" in r and r.get("unit") is not None:
                item["unit"] = str(r.get("unit") or "").strip()
            for k in ("hit_peak", "hit_off", "miss_peak", "miss_off", "out_peak", "out_off"):
                v = r.get(k)
                if v not in (None, ""):
                    try:
                        item[k] = float(v)
                    except (TypeError, ValueError):
                        pass
            cleaned.append(item)
        try:
            pm = self.ctx.plugin_mgr
            if pm is None:
                return {"ok": False, "msg": "plugin_mgr 不可用"}
            cfg = pm.get_plugin_config("KiraAI_token_stats_plugin")
            sec = dict(cfg.get("section_pricing") or {})
            sec["rules"] = cleaned
            cfg["section_pricing"] = sec
            await pm.update_plugin_config("KiraAI_token_stats_plugin", cfg)
            # 注意：update_plugin_config 触发热重载，旧实例已被替换，无需（也不能）改 self.rules
            return {"ok": True, "count": len(cleaned)}
        except Exception as e:
            logger.warning(f"[token_stats] 保存价格规则失败: {e}")
            return {"ok": False, "msg": f"保存失败: {e}"}

    @register.api(method="POST", path="/pricing-windows", auth=True)
    async def api_pricing_windows(self, request: Request):
        """保存全局峰谷时段（WebUI 价格规则页）：格式 [["09:00","12:00"],["14:00","18:00"]]"""
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "msg": "请求体不是合法 JSON"}
        pw = body.get("peak_windows")
        if not isinstance(pw, list) or not pw:
            return {"ok": False, "msg": "peak_windows 必须是数组"}
        cleaned = []
        for item in pw:
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                continue
            sh, sm = _parse_hhmm(item[0])
            eh, em = _parse_hhmm(item[1])
            if sh * 60 + sm < eh * 60 + em:
                cleaned.append([f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"])
        if not cleaned:
            return {"ok": False, "msg": "峰谷时段格式错误：需为 [['09:00','12:00'],...] 且结束晚于开始"}
        try:
            pm = self.ctx.plugin_mgr
            if pm is None:
                return {"ok": False, "msg": "plugin_mgr 不可用"}
            cfg = pm.get_plugin_config("KiraAI_token_stats_plugin")
            sec = dict(cfg.get("section_pricing") or {})
            sec["peak_windows"] = cleaned
            cfg["section_pricing"] = sec
            await pm.update_plugin_config("KiraAI_token_stats_plugin", cfg)
            return {"ok": True, "count": len(cleaned)}
        except Exception as e:
            logger.warning(f"[token_stats] 保存峰谷时段失败: {e}")
            return {"ok": False, "msg": f"保存失败: {e}"}

    @register.api(method="POST", path="/pricing-profiles", auth=True)
    async def api_pricing_profiles(self, request: Request):
        """保存峰谷方案库：格式 [{"name":"默认工作日","windows":[["09:00","12:00"],["14:00","18:00"]]}]"""
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "msg": "请求体不是合法 JSON"}
        profiles = body.get("profiles")
        if not isinstance(profiles, list) or not profiles:
            return {"ok": False, "msg": "profiles 必须是数组"}
        cleaned = []
        seen_names = set()
        for p in profiles:
            if not isinstance(p, dict) or not str(p.get("name") or "").strip():
                continue
            name = str(p["name"]).strip()
            if name in seen_names:
                return {"ok": False, "msg": f"方案名重复：{name}"}
            seen_names.add(name)
            w = p.get("windows")
            if not isinstance(w, list) or not w:
                continue
            cleaned_w = []
            for item in w:
                if not (isinstance(item, (list, tuple)) and len(item) == 2):
                    continue
                sh, sm = _parse_hhmm(item[0])
                eh, em = _parse_hhmm(item[1])
                if sh * 60 + sm < eh * 60 + em:
                    cleaned_w.append([f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"])
            if cleaned_w:
                cleaned.append({"name": name, "windows": cleaned_w})
        if not cleaned:
            return {"ok": False, "msg": "方案格式错误：需为 [{'name':'方案名','windows':[['09:00','12:00'],...]}] 且结束晚于开始"}
        # 删除引用校验：被计价规则引用的方案不允许删（裸调 API 也拦）
        old_names = {p.get("name") for p in _PEAK_PROFILES}
        new_names = {p["name"] for p in cleaned}
        removed = old_names - new_names
        if removed:
            for r in self.rules or []:
                if r.get("peak_profile") in removed:
                    return {"ok": False, "msg": f"方案「{r.get('peak_profile')}」正被计价规则「{r.get('name')}」引用，无法删除"}
        try:
            pm = self.ctx.plugin_mgr
            if pm is None:
                return {"ok": False, "msg": "plugin_mgr 不可用"}
            cfg = pm.get_plugin_config("KiraAI_token_stats_plugin")
            sec = dict(cfg.get("section_pricing") or {})
            sec["peak_profiles"] = cleaned
            cfg["section_pricing"] = sec
            await pm.update_plugin_config("KiraAI_token_stats_plugin", cfg)
            return {"ok": True, "count": len(cleaned)}
        except Exception as e:
            logger.warning(f"[token_stats] 保存峰谷方案库失败: {e}")
            return {"ok": False, "msg": f"保存失败: {e}"}

    # ── WebUI 侧边栏页面 ──

    @register.page("/stats", auth=True, menu=PageMenu(label={"zh": "Token 用量"}, icon="DataLine"))
    def page_stats(self):
        return PluginPage.from_html(_DASHBOARD_HTML)

    @register.page("/stats-widget", auth=True, menu=PageMenu(label={"zh": "Token 挂件"}, icon="Desktop"))
    def page_stats_widget(self):
        if not self.enable_widget:
            return PluginPage.from_html(
                f"<!DOCTYPE html><html lang=\"zh-CN\"><body style=\"background:#0f172a;color:#94a3b8;font-family:sans-serif;padding:24px;font-size:13px\">"
                f"<p style=\"color:#e2e8f0;font-weight:600;margin-bottom:8px\">Token 挂件已停用</p>"
                f"<p>此页面为迷你悬浮卡片（实时 tokens/费用/余额，可拖动、可折叠成小球，可弹独立小窗），当前在配置中被关闭。<br>"
                f"如需开启：插件管理 → KiraAI_token_stats_plugin → 配置 → 「挂件」→ 打开「启用挂件」。</p>"
                f"</body></html>")
        # 后端「紧凑模式」配置注入为前端默认值；localStorage 有记忆时以用户为准
        html = _WIDGET_HTML.replace(
            "let compact = localStorage.getItem('tsWidgetCompact')==='1';",
            "let _c = localStorage.getItem('tsWidgetCompact'); let compact = _c === null ? "
            + ("true" if self.widget_compact else "false") + " : _c === '1';",
        )
        return PluginPage.from_html(html)

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Token 用量统计</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--line:#334155;--fg:#e2e8f0;--dim:#a3b2c7;--acc:#38bdf8;--ok:#34d399;--warn:#fbbf24;--err:#f87171;--purple:#a78bfa;--pink:#f472b6;--inset:#0b1220}
*{margin:0;padding:0;box-sizing:border-box}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
@keyframes shimmer{from{background-position:-200px 0}to{background-position:200px 0}}
.panel.on{animation:fadeIn .25s ease}
.skel{height:92px;border-radius:12px;background:linear-gradient(90deg,var(--card) 25%,#263449 50%,var(--card) 75%);background-size:400px 100%;animation:shimmer 1.2s linear infinite;border:1px solid var(--line)}
.card{transition:transform .18s ease,box-shadow .18s ease}
.card::after{content:'';position:absolute;inset:0;background:linear-gradient(105deg,transparent 40%,rgba(255,255,255,.06) 50%,transparent 60%);transform:translateX(-130%);pointer-events:none;z-index:1}
.card:hover::after{transform:translateX(130%);transition:transform .8s ease}
.card:hover{transform:translateY(-2px);box-shadow:0 6px 18px rgba(0,0,0,.3)}
.card[data-k="session"],.card[data-k="today"]{grid-column:span 2}
@media(max-width:700px){.card[data-k="session"],.card[data-k="today"]{grid-column:span 1}}
.card.errbox{background:linear-gradient(rgba(248,113,113,.07),rgba(248,113,113,.07)),var(--card)}
body{background:var(--bg);color:var(--fg);font-family:"Segoe UI",system-ui,"Microsoft YaHei",sans-serif;padding:20px;font-size:14px;background-size:cover;background-position:center;background-attachment:fixed}
body.bg-on #app{background:rgba(15,23,42,.72);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-radius:16px;padding:20px;border:1px solid rgba(51,65,85,.5);box-shadow:0 8px 32px rgba(0,0,0,.35)}
body::before{content:'';position:fixed;inset:0;background-image:var(--userbg,none);background-size:cover;background-position:center;opacity:0;transition:opacity .4s ease;pointer-events:none;z-index:0}
body.hasbg::before{opacity:1;box-shadow:inset 0 0 0 9999px rgba(11,18,32,.45)}
#app{position:relative;z-index:1}
@media (prefers-reduced-motion: reduce){body::before{transition:none!important}}
#skinBtn,#bgBtn{position:fixed;right:14px;bottom:14px;width:34px;height:34px;border-radius:50%;border:1px solid var(--line);background:rgba(30,41,59,.7);color:var(--dim);cursor:pointer;font-size:16px;z-index:999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(6px)}
#bgBtn{right:56px}
#skinBtn:hover,#bgBtn:hover{color:var(--fg);border-color:var(--acc)}
.sw{position:relative;display:inline-block;width:34px;height:18px;vertical-align:middle;cursor:pointer}
.sw input{opacity:0;width:0;height:0}
.sw i{position:absolute;inset:0;background:#334155;border-radius:999px;transition:.2s}
.sw i:before{content:'';position:absolute;left:2px;top:2px;width:14px;height:14px;border-radius:50%;background:#94a3b8;transition:.2s}
.sw input:checked + i{background:var(--acc)}
.sw input:checked + i:before{transform:translateX(16px);background:#06283d}
h1{font-size:20px;margin-bottom:4px;display:flex;align-items:center;gap:10px}
h1 .dot{width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.tabs{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}
.tab{padding:6px 16px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--dim);cursor:pointer;font-size:13px}
.tab.on{background:var(--acc);color:#06283d;font-weight:600;border-color:var(--acc)}
.panel{display:none}.panel.on{display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;position:relative;overflow:hidden}
.card .k{color:var(--dim);font-size:11px;letter-spacing:.5px}
.card .topline{display:flex;justify-content:space-between;align-items:baseline}
.card .v{font-size:22px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}
.card .v.cost{color:var(--ok)}.card .v.in{color:var(--acc)}.card .v.out{color:var(--pink)}.card .v.cache{color:var(--warn)}.card .v.pts{color:#c084fc}
.card .d{color:var(--dim);font-size:11px;margin-top:3px;line-height:1.5}
.card .spark{position:absolute;right:10px;bottom:8px;width:44%;height:26px;opacity:.85}
.card .spark svg{width:100%;height:100%;display:block}
.card .delta{color:var(--dim);font-size:11px}
.card .delta.up{color:var(--ok)}.card .delta.down{color:var(--err)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.hours .h{background:var(--inset)}
.bar{background:var(--inset)}
.box h3{font-size:14px;margin-bottom:10px;color:var(--fg)}
.box h3 .seg{float:right;font-size:11px;color:var(--dim);font-weight:400;cursor:pointer;border:1px solid var(--line);border-radius:999px;padding:2px 10px;margin-left:6px}
.box h3 .seg.on{background:var(--acc);color:#06283d;border-color:var(--acc)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{color:var(--dim);text-align:left;font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line);font-size:11px;letter-spacing:.5px}
td{padding:6px 8px;border-bottom:1px solid rgba(51,65,85,.4);font-variant-numeric:tabular-nums}
tr:hover td{background:rgba(56,189,248,.05)}
tr.cur td{background:rgba(52,211,153,.07)}
.rate{color:var(--purple)}.ok{color:var(--ok)}.bad{color:var(--err)}
.bar{height:8px;border-radius:4px;background:var(--inset);overflow:hidden;margin-top:4px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--purple));border-radius:4px}
.hours{display:grid;grid-template-columns:repeat(12,1fr);gap:6px}
.hours .h{background:var(--inset);border-radius:6px;padding:6px;text-align:center;font-size:10px;color:var(--dim)}
.hours .h i{display:block;height:46px;background:var(--inset);border-radius:3px;margin:4px 0 2px;position:relative;overflow:hidden}
.hours .h i b{position:absolute;bottom:0;left:0;right:0;background:linear-gradient(180deg,var(--acc),#6366f1);border-radius:3px 3px 0 0}
.hours .h.clickable{cursor:pointer}.hours .h.clickable:hover{border:1px solid var(--acc)}
.btn{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:5px 14px;cursor:pointer;font-size:12px}
.btn:hover{border-color:var(--acc)}
.btn:disabled{opacity:.5;cursor:default}
.btn.on{background:var(--acc);color:#06283d;border-color:var(--acc);font-weight:600}
.snapshot{display:flex;gap:16px;align-items:center;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin-bottom:16px;font-size:12.5px}
.snapshot .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block;margin-right:6px;box-shadow:0 0 6px var(--ok)}
.snapshot .st{color:var(--dim)}
.snapshot b{color:var(--fg)}
.errbox{border-left:3px solid var(--err)}
.note{color:var(--dim);font-size:11.5px;margin-top:8px;line-height:1.6}
/* 时间趋势 */
.trendWrap{position:relative}
.trend{display:flex;align-items:flex-end;gap:3px;height:150px;padding:6px 0 0;position:relative}
.trend .col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%;position:relative;cursor:pointer;min-width:0}
.trend .col:hover .tip{opacity:1}
.trend .stk{display:flex;flex-direction:column;justify-content:flex-end;width:100%}
.trend .stack{border-radius:2px 2px 0 0;width:100%;transition:filter .15s}
.trend .col:hover .stack{filter:brightness(1.3)}
.trend .hitsvg{position:absolute;left:0;top:6px;width:100%;height:calc(100% - 22px);pointer-events:none;overflow:visible}
.trendWrap.hasDur{padding-right:36px}
.trendWrap .durlbl{position:absolute;right:2px;transform:translateY(-50%);font-size:9px;line-height:1;color:var(--warn);opacity:.85;pointer-events:none;font-variant-numeric:tabular-nums}
.trend .tlbl{text-align:center;font-size:10px;color:var(--dim);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trend .tip{position:absolute;bottom:100%;left:50%;transform:translateX(-50%);background:var(--inset);border:1px solid var(--line);border-radius:8px;padding:6px 9px;font-size:11px;white-space:nowrap;opacity:0;transition:.15s;z-index:20;pointer-events:none;color:var(--fg)}
.trend .tip.flip{left:auto;right:0;transform:none}
.legend{display:flex;gap:12px;flex-wrap:wrap;margin-top:8px;font-size:11px;color:var(--dim)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:-1px}
</style>
</head>
<body>
<button id="skinBtn" title="随机背景开关">👕</button>
<button id="bgBtn" title="自定义背景：单击选择图片（1-10 张），双击清除">🖼</button>
<div id="app">
<h1><span class="dot" id="dot"></span>Token 用量统计</h1>
<div class="sub" id="sub">加载中…</div>
<div class="tabs">
  <div class="tab on" data-p="ov">概览</div>
  <div class="tab" data-p="trend">时间趋势</div>
  <div class="tab" data-p="dim">维度分析</div>
  <div class="tab" data-p="sess">会话统计</div>
  <div class="tab" data-p="rec">最近记录</div>
  <div class="tab" data-p="price">价格规则</div>
  <div class="tab" data-p="bal">余额监测</div>
  <div class="tab" data-p="alert">预警提醒</div>
</div>

<div class="panel on" id="p-ov">
  <div class="snapshot" id="snap"></div>
  <div class="cards" id="cards"><div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div></div>
  <div class="grid2">
    <div class="box" id="histBox"><h3>按天历史</h3><div id="hist"></div></div>
    <div class="box" id="hourBox"><h3>今日按小时 <span class="note" style="float:right">点击小时柱下钻最近记录</span></h3><div id="hours"></div></div>
  </div>
</div>

<div class="panel" id="p-trend">
  <div class="box">
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px">
      <button class="btn on" data-tr="d7">近7天</button>
      <button class="btn" data-tr="d30">近30天</button>
      <button class="btn" data-tr="total">累计</button>
      <span style="color:var(--dim);font-size:12px;margin-left:6px" id="trendRange"></span>
      <span style="flex:1"></span>
      <button class="btn" data-tr="back" id="trendBack" style="display:none">← 返回按天</button>
    </div>
    <div class="trendWrap" id="trendWrap"><div class="trend" id="trend"></div></div>
    <div class="legend" id="trendLegend"></div>
    <div class="note" id="trendNote"></div>
  </div>
</div>

<div class="panel" id="p-dim">
  <div class="box" style="margin-bottom:12px">
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
      <button class="btn" data-r="today">今天</button>
      <button class="btn on" data-r="d7">近7天</button>
      <button class="btn" data-r="d30">近30天</button>
      <button class="btn" data-r="total">累计</button>
      <span style="color:var(--dim);font-size:12px" id="dimRange"></span>
    </div>
    <table><thead><tr><th>维度</th><th>值</th><th>轮数</th><th>输入</th><th>输出</th><th>缓存</th><th>总量</th><th title="单轮 LLM 响应耗时均值">均耗时</th><th title="最快单轮">最快</th><th title="最慢单轮">最慢</th><th>费用</th></tr></thead><tbody id="dimBody"></tbody></table>
  </div>
</div>

<div class="panel" id="p-sess">
  <div class="box" style="margin-bottom:12px">
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
      <button class="btn" data-sr="today">今天</button>
      <button class="btn on" data-sr="d7">近7天</button>
      <button class="btn" data-sr="d30">近30天</button>
      <button class="btn" data-sr="total">累计</button>
      <span style="color:var(--dim);font-size:12px" id="sessRange"></span>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px" id="sessGroups"></div>
    <table><thead><tr><th>会话</th><th>类型</th><th>轮数</th><th>输入</th><th>输出</th><th>缓存</th><th>总量</th><th title="单轮 LLM 响应耗时均值">均耗时</th><th title="最快单轮">最快</th><th title="最慢单轮">最慢</th><th>费用</th><th>最近活动</th></tr></thead><tbody id="sessBody"></tbody></table>
  </div>
</div>

<div class="panel" id="p-rec">
  <div class="box">
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
      <button class="btn" id="recRefresh">刷新</button>
      <span style="color:var(--dim);font-size:12px">最近 <span id="recCount">15</span> 轮（含工具步），费用按当前价格规则即时计算</span>
    </div>
    <table><thead><tr><th>时间</th><th>模型</th><th>来源</th><th>渠道</th><th>输入</th><th>输出</th><th>缓存</th><th>耗时</th><th>总量</th><th>费用</th></tr></thead><tbody id="recBody"></tbody></table>
  </div>
</div>

<div class="panel" id="p-price">
  <div class="box">
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      <button class="btn" id="priceAdd">＋ 添加规则</button>
      <span style="color:var(--dim);font-size:12px" id="priceInfo"></span>
    </div>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap;background:var(--inset);border:1px solid var(--line);border-radius:8px;padding:8px 12px">
      <span style="color:var(--dim);font-size:12px">峰谷方案库：</span>
      <select id="peakProfileSel" style="width:200px;background:var(--card);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:6px 10px;font-size:12px"></select>
      <input id="peakProfileName" style="width:130px;background:var(--card);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:6px 10px;font-size:12px" placeholder="方案名">
      <input id="peakWindows" style="width:240px;background:var(--card);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:6px 10px;font-size:12px" placeholder="09:00-12:00,14:00-18:00">
      <button class="btn" id="peakWindowsSave">保存方案</button>
      <button class="btn" id="peakProfileAdd">＋ 新建</button>
      <button class="btn" id="peakProfileDel">删除</button>
      <span style="color:var(--dim);font-size:11px">每条计价规则可选自己的峰谷方案；格式 开始-结束，逗号分隔多段</span>
    </div>
    <div id="priceBody"></div>
  </div>
</div>

<div class="panel" id="p-bal">
  <div class="box">
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      <button class="btn" id="balRefresh">立即探测</button>
      <button class="btn" id="balEdit">＋ 添加监测源</button>
      <span style="color:var(--dim);font-size:12px">轮询间隔</span>
      <input id="balInterval" type="number" min="60" style="width:64px;background:var(--card);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:5px 8px;font-size:12px" value="600">
      <span style="color:var(--dim);font-size:12px">秒</span>
      <button class="btn" id="balIntervalSave">保存</button>
      <span style="color:var(--dim);font-size:12px" id="balInfo"></span>
    </div>
    <table><thead><tr><th>启用</th><th>名称</th><th>类型</th><th>余额</th><th>更新时间</th><th>状态</th><th style="width:110px">操作</th></tr></thead><tbody id="balBody"></tbody></table>
    <div class="note">点击「＋ 添加监测源」可视化配置，保存后自动热重载。类型说明：auto=按 URL 自动探测官方端点或 One-API 中转站；custom=自定义接口多端点尝试；newapi=New-API 站点；preset=预设扣减（钱包型）；daily=每日重置积分；rolling=每日累计滚存积分。估算型填「当前余额(对表)」即以上游实际余额校准，此后按价格规则自动扣减；可再选「关联模型」只统计该模型的用量来估算。</div>
  </div>
</div>

<div id="balEditor" style="display:none;position:fixed;inset:0;background:rgba(2,6,23,.7);z-index:99;align-items:center;justify-content:center">
  <div style="background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;width:560px;max-width:94vw;max-height:88vh;overflow:auto">
    <div style="display:flex;align-items:center;margin-bottom:14px">
      <h3 style="margin:0;flex:1" id="balEditTitle">添加监测源</h3>
      <button class="btn" id="balEditClose">✕</button>
    </div>
    <div id="balEditForm"></div>
  </div>
</div>


<div class="panel" id="p-alert">
  <div class="box">
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
      <button class="btn" id="alertAddRule">＋ 添加规则</button>
      <button class="btn" id="alertAddDel">＋ 添加投递会话</button>
      <button class="btn" id="alertSaveBtn" style="background:var(--acc);border-color:var(--acc);color:#06283d;font-weight:600">保存配置</button>
      <span style="color:var(--dim);font-size:12px" id="alertInfo"></span>
      <span style="flex:1"></span>
      <span style="color:var(--dim);font-size:12px">监测余额 / 当日 tokens / 当日费用，达阈值自动提醒。同一站点可设多段阈值（如余额 10 元/5 元/1 元），各自独立触发互不影响。</span>
    </div>
    <div style="margin-bottom:14px">
      <div style="font-weight:600;font-size:13px;margin-bottom:6px">投递会话 <span style="color:var(--dim);font-weight:400">（提醒发到哪里，可配多个）</span></div>
      <div id="alertDelList"></div>
    </div>
    <div>
      <div style="font-weight:600;font-size:13px;margin-bottom:6px">预警规则 <span style="color:var(--dim);font-weight:400">（token/cost 按自然日重置；balance 余额回升超过阈值自动复位）</span></div>
      <div id="alertRuleList"></div>
    </div>
  </div>
</div>

<div id="alertEditor" style="display:none;position:fixed;inset:0;background:rgba(2,6,23,.7);z-index:99;align-items:center;justify-content:center">
  <div style="background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;width:680px;max-width:94vw;max-height:88vh;overflow:auto">
    <div style="display:flex;align-items:center;margin-bottom:14px">
      <h3 style="margin:0;flex:1" id="alertEditTitle">添加规则</h3>
      <button class="btn" id="alertEditClose">✕</button>
    </div>
    <div id="alertEditForm"></div>
  </div>
</div>

<div id="alertDelEditor" style="display:none;position:fixed;inset:0;background:rgba(2,6,23,.7);z-index:99;align-items:center;justify-content:center">
  <div style="background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;width:440px;max-width:94vw">
    <div style="display:flex;align-items:center;margin-bottom:14px">
      <h3 style="margin:0;flex:1" id="alertDelTitle">添加投递会话</h3>
      <button class="btn" id="alertDelClose">✕</button>
    </div>
    <div id="alertDelForm"></div>
  </div>
</div>
<div id="priceEditor" style="display:none;position:fixed;inset:0;background:rgba(2,6,23,.7);z-index:99;align-items:center;justify-content:center">
  <div style="background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;width:640px;max-width:94vw;max-height:88vh;overflow:auto">
    <div style="display:flex;align-items:center;margin-bottom:14px">
      <h3 style="margin:0;flex:1" id="priceEditTitle">添加价格规则</h3>
      <button class="btn" id="priceEditClose">✕</button>
    </div>
    <div id="priceEditForm"></div>
  </div>
</div>

<script>
const API = '/api/plugin/KiraAI_token_stats_plugin';
const $ = s => document.querySelector(s);
const fmt = n => Number(n||0).toLocaleString('zh-CN');
// 数字缩写：K(千)/M(百万)/亿(1e8)/B(十亿)，995 平滑进位（三处 fmt4 与 Python _fmt4 规则一致）
const fmt4 = v => { v=Math.max(0,Math.round(v||0)); if(v<1000)return ''+v;
  if(v<9950)return (v/1e3).toFixed(1).replace('.0','')+'K'; if(v<995000)return Math.round(v/1e3)+'K';
  if(v<9950000)return (v/1e6).toFixed(1).replace('.0','')+'M'; if(v<99500000)return Math.round(v/1e6)+'M';
  if(v<995000000)return (v/1e8).toFixed(1).replace('.0','')+'亿'; if(v<9950000000)return Math.round(v/1e8)+'亿';
  if(v<99500000000)return (v/1e9).toFixed(1).replace('.0','')+'B'; return Math.round(v/1e9)+'B'; };
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// 单轮耗时（秒）格式化：<10s→4.56s；<60s→23.4s；<60m→1m23s；≥1h→1h05m；无值→—（与 Python _fmt_dur、挂件 fmtDur 一致）
const fmtDur = v => { if(v==null||isNaN(+v)||+v<0) return '—'; v=+v;
  if(v<10) return v.toFixed(2)+'s';
  if(v<60) return v.toFixed(1)+'s';
  if(v<3600) return Math.floor(v/60)+'m'+String(Math.floor(v%60)).padStart(2,'0')+'s';
  return Math.floor(v/3600)+'h'+String(Math.floor(v%3600/60)).padStart(2,'0')+'m'; };
const localDate = () => { const d=new Date();
  return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0'); };
// 页内 toast（沙箱内 alert 不可用，一律用 toast）
function dtoast(msg){
  let t = document.getElementById('dToast');
  if(!t){
    t = document.createElement('div');
    t.id = 'dToast';
    t.style.cssText = 'position:fixed;left:50%;bottom:60px;transform:translateX(-50%);background:rgba(15,23,42,.95);border:1px solid var(--line);color:var(--fg);padding:8px 16px;border-radius:20px;font-size:13px;z-index:999;transition:opacity .3s;pointer-events:none;max-width:88vw';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.opacity = '1';
  clearTimeout(t._tm);
  t._tm = setTimeout(()=>{ t.style.opacity = '0'; }, 2200);
}
// 内联二次确认（沙箱内 confirm 不可用）：首次点击变「确认？」2.5s 内再点执行
function confirmBtn(btn, label, fn){
  if(btn._arm){ btn._arm=false; btn.textContent=btn._orig; btn.classList.remove('on'); fn(); return; }
  btn._arm = true; btn._orig = btn.textContent; btn.textContent = label; btn.classList.add('on');
  setTimeout(()=>{ if(btn._arm){ btn._arm=false; btn.textContent=btn._orig; btn.classList.remove('on'); } }, 2500);
}
// KPI 数字 rAF 300ms 插值滚动
function tweenNum(el, from, to){
  if(from===to){ el.textContent = fmt4(to); return; }
  const t0 = performance.now();
  const step = t => {
    const p = Math.min(1,(t-t0)/300);
    el.textContent = fmt4(Math.round(from+(to-from)*p));
    if(p<1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
const MODEL_COLORS = ['#38bdf8','#a78bfa','#f472b6','#34d399','#fbbf24','#fb7185','#22d3ee','#c084fc'];
const costText = c => c==null ? '—' : (c.indexOf('积分')>=0 ? c : '¥'+c);

document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('on'));
  t.classList.add('on'); $('#p-'+t.dataset.p).classList.add('on');
  if(t.dataset.p==='trend') loadTrend(curTr);
  if(t.dataset.p==='dim') loadDim(curDim);
  if(t.dataset.p==='sess') loadSess(curSessRange);
  if(t.dataset.p==='rec') loadRec();
  if(t.dataset.p==='price') loadPrice();
  if(t.dataset.p==='bal') loadBal(false);
  if(t.dataset.p==='alert') loadAlert();
});

async function jget(p){ const r = await fetch(API+p, {cache:'no-store'}); return r.json(); }

function sparkHtml(arr){
  if(!arr || !arr.length) return '';
  const max = Math.max(...arr,1), N = arr.length;
  const pts = arr.map((v,i)=>((i/(N-1||1))*44)+','+ (26-(v/max*24))).join(' ');
  const fill = arr.map((v,i)=>((i/(N-1||1))*44)+','+(26-(v/max*24))).join(' ');
  return '<svg viewBox="0 0 44 26" preserveAspectRatio="none"><polyline points="'+pts+'" fill="none" stroke="rgba(56,189,248,.85)" stroke-width="1.4"/>'+
    '<polygon points="0,26 '+fill+' 44,26" fill="rgba(56,189,248,.13)"/></svg>';
}

const RL = {session:'本次会话',today:'今天',d7:'近7天',d30:'近30天',total:'累计'};
const sessLabel = d => d.sess_name ? d.sess_name : (d.sess_sid ? d.sess_sid : '本次会话');
async function loadOv(){
  try{
    await _loadOvInner();
    const t = new Date();
    $('#updAt').textContent = '上次更新 '+String(t.getHours()).padStart(2,'0')+':'+String(t.getMinutes()).padStart(2,'0')+':'+String(t.getSeconds()).padStart(2,'0');
  }catch(e){
    $('#dot').style.background = 'var(--err)';
    $('#dot').style.boxShadow = '0 0 8px var(--err)';
    $('#updAt').textContent = '更新失败';
  }
}
async function _loadOvInner(){
  const d = await jget('/stats');
  $('#dot').style.boxShadow = '';
  $('#dot').style.background = d.busy ? '#60a5fa' : '#34d399';
  $('#sub').textContent = '模型 ' + (d.model||'—') + ' · 渠道 ' + (d.channel||'—') + ' · 日志 ' + (d.logFile||'');
  // 会话已进行时长：以服务端值校准基准，本地 1s ticker 递增（见 fmtElapsed/setInterval 底部）
  _elBase = {sec: d.elapsed||0, at: Date.now()};
  $('#snap').innerHTML = '<span><span class="dot"></span>' + esc(d.src||'—') + '</span>' +
    '<span class="st">会话 <b>'+d.rounds+'</b> 轮 · <b>'+fmt4(d.total)+'</b> Token · 已进行 <span id="sessElapsed">'+fmtElapsed(_elBase.sec)+'</span></span>' +
    '<span class="st">最近一轮：输入 <b>'+fmt(d.lastInput)+'</b> · 输出 <b>'+fmt(d.lastOutput)+'</b>'+(d.lastCached?' · 缓存 <b>'+fmt(d.lastCached)+'</b>':'')+'</span>';
  const hist = await jget('/history');
  const dayVols = (hist.days||[]).map(x=>x.v);
  const cards = [];
  for (const k of ['session','today','d7','d30','total']){
    const rg = (d.ranges||{})[k]||{}, co = (d.costs||{})[k]||{};
    const rate = rg.i>0 ? (rg.c/rg.i*100).toFixed(1)+'%' : '—';
    const errs = (d.errors||{})[k]||0;
    const costBits = [];
    if(co.matched && co.units){ co.units.forEach(u=>{ costBits.push(u.unit?('<span class="pts">'+u.amt+' '+esc(u.unit)+'</span>'):u.amt); }); }
    // 单轮耗时行（仅该范围有耗时数据时显示）
    const durLine = (rg.dur && rg.dur.avg!=null) ? '<div class="d">耗时 均'+fmtDur(rg.dur.avg)+'·快'+fmtDur(rg.dur.min)+'·慢'+fmtDur(rg.dur.max)+'</div>' : '';
    // 迷你走势（非本次会话）：近14天分布
    let sp = '';
    if(k!=='session') sp = '<div class="spark">'+sparkHtml(dayVols.slice(-14))+'</div>';
    cards.push({k:k, vnum:rg.v||0, html:'<div class="k">'+(k==='session'?sessLabel(d):RL[k])+'</div><div class="topline"><div class="v">'+fmt4(rg.v)+'</div>'+(sp||'')+'</div>'+
      '<div class="d">'+fmt(rg.r)+' 轮 · 输入 '+fmt(rg.i)+' · 输出 '+fmt(rg.o)+' · 缓存 '+fmt(rg.c)+' · 命中率 <span class="rate">'+rate+'</span></div>'+
      '<div class="d">费用 '+(costBits.length?costBits.join(' + '):'<span class="cost">—</span>')+(errs?' · 出错 <span class="bad">'+errs+'</span>':'')+'</div>'+durLine});
  }
  const er = d.errors||{};
  if (er.last) cards.push({k:'errLast', cls:'errbox', html:'<div class="k">最近出错</div><div class="d">'+esc(er.last)+'</div>'});
  const lg = er.log||{};
  const lgNames = {xml:'XML解析',model:'模型调用',tool:'工具执行',net:'网络/超时',traceback:'异常堆栈',other:'其他'};
  const lgBits = [];
  for (const c of ['xml','model','tool','net','traceback','other']){
    if (lg[c]) lgBits.push((lgNames[c]||c)+' '+lg[c]);
  }
  if (lgBits.length){
    let lgTxt = '后台日志错误（近7天）：'+lgBits.join(' · ');
    if (er.logLast && er.logLast.text) lgTxt += '｜最近：'+esc(er.logLast.text.slice(0,60));
    cards.push({k:'errLog', cls:'errbox', html:'<div class="k">后台日志 ERROR</div><div class="d">'+lgTxt+'</div>'});
  }
  if (er.tool){
    let tt = '工具结果失败（近7天）：'+er.tool+' 次';
    if (er.toolLast && er.toolLast.text) tt += '｜最近：'+esc(er.toolLast.text.slice(0,60));
    cards.push({k:'errTool', cls:'errbox', html:'<div class="k">工具结果失败</div><div class="d">'+tt+'</div>'});
  }
  renderCards(cards);
  loadHist(hist); loadHours();
}
// M1：cards 按 data-k 增量更新——内容签名未变不动 DOM，避免轮询全量 innerHTML 重建
let _cardEls = {};
function renderCards(cards){
  const box = $('#cards');
  // 首次真正渲染时清理初始骨架屏占位（幂等）
  box.querySelectorAll('.skel').forEach(e=>e.remove());
  const seen = {};
  cards.forEach(c=>{
    seen[c.k] = 1;
    const el0 = _cardEls[c.k];
    if(el0 && el0._sig === c.html) return;
    if(el0){ el0.className = 'card'+(c.cls?' '+c.cls:''); el0.innerHTML = c.html; el0._sig = c.html; return; }
    const el = document.createElement('div');
    el.className = 'card'+(c.cls?' '+c.cls:'');
    el.dataset.k = c.k;
    el.innerHTML = c.html;
    el._sig = c.html;
    box.appendChild(el);
    _cardEls[c.k] = el;
  });
  for(const k in _cardEls){
    if(!seen[k]){ _cardEls[k].remove(); delete _cardEls[k]; }
  }
}
async function loadHist(hist){
  const d = hist || await jget('/history');
  const days = (d.days||[]).slice(-14);
  if(!days.length){ $('#hist').innerHTML='<div class="note">暂无历史数据。开始对话后自动统计；想看到费用请先去 <button class="btn" onclick="showTab(\'price\')">配置价格规则</button></div>'; return; }
  const max = Math.max(...days.map(x=>x.v),1);
  const today = localDate();
  $('#hist').innerHTML = '<table><thead><tr><th>日期</th><th>总量</th><th>轮数</th><th>输入</th><th>输出</th><th>缓存</th><th style="width:30%">分布</th></tr></thead><tbody>'+
    days.map(x=>'<tr'+(x.d===today?' class="cur"':'')+'><td>'+x.d+(x.d===today?' ★':'')+'</td><td>'+fmt4(x.v)+'</td><td>'+x.r+'</td><td>'+fmt(x.i)+'</td><td>'+fmt(x.o)+'</td><td>'+fmt(x.c)+'</td>'+
    '<td><div class="bar"><i style="width:'+(x.v/max*100)+'%"></i></div></td></tr>').join('')+'</tbody></table>';
}
async function loadHours(){
  const d = await jget('/history?day='+localDate());
  const hs = d.hours||[];
  const max = Math.max(...hs.map(x=>x.v),1);
  const cells = [];
  for(let h=0;h<24;h++){
    const x = hs.find(y=>y.h===h);
    cells.push('<div class="h'+(x?' clickable':'')+'" data-h="'+h+'">'+h+'时<i>'+(x?'<b style="height:'+Math.max(4,x.v/max*100)+'%"></b>':'')+'</i>'+(x?fmt4(x.v):'—')+'</div>');
  }
  $('#hours').innerHTML = '<div class="hours">'+cells.join('')+'</div>';
  document.querySelectorAll('#hours .clickable').forEach(c=>c.onclick=()=>{
    const h = c.dataset.h;
    showTab('rec');
    filterRecByHour(h);
  });
}

/* ── 时间趋势（三级下钻：按天 → 单天按小时 → 单小时按5分钟）── */
let curTr = 'd7', curDay = null, curHour = null;
document.querySelectorAll('[data-tr]').forEach(b=>{
  if(b.id==='trendBack') return;
  b.onclick=()=>{ curDay=null; curHour=null; curTr=b.dataset.tr; document.querySelectorAll('[data-tr]').forEach(x=>{if(x.id!=='trendBack')x.classList.remove('on')}); b.classList.add('on'); loadTrend(curTr); };
});
$('#trendBack').onclick=()=>{
  if(curHour!==null){ curHour=null; loadTrend(); }
  else if(curDay){ curDay=null; loadTrend(); }
};
async function loadTrend(r){
  let url = '/trend?range='+(curTr||'d7');
  if(curDay && curHour===null) url = '/trend?day='+curDay;   // 后端按天(day=)返回小时桶
  if(curDay && curHour!==null) url = '/trend?day='+curDay+'&hour='+curHour; // 后端按小时(day+hour=)返回5分钟桶
  const d = await jget(url);
  const isDay = !!curDay && curHour===null, isMin = !!curDay && curHour!==null, isMonth = !curDay && d.unit==='month';
  $('#trendRange').textContent = isMin ? ('单小时 '+curDay+' '+String(curHour).padStart(2,'0')+':00 按5分钟（点柱 → 查看该时段记录）')
    : isDay ? ('单天 '+curDay+' 按小时（点柱 → 下钻5分钟/看记录）')
    : isMonth ? (d.from+' ~ '+d.to+'（按月聚合）')
    : (d.from+' ~ '+d.to+'（点柱 → 下钻该天按小时）');
  $('#trendBack').style.display = (curDay||curHour!==null) ? '' : 'none';
  const days = d.days||[], models = d.topModels||[];
  if(!days.length){ $('#trend').innerHTML='<div class="note" style="padding:20px">该范围暂无数据 <button class="btn" onclick="showTab(\'price\')">去配置价格规则</button></div>'; $('#trendLegend').innerHTML=''; $('#trendNote').textContent=''; return; }
  const maxV = Math.max(...days.map(x=>x.v),1);
  // 图例色与柱色统一：按 topModels 顺序建立 name→color 映射
  const colorOf = {};
  models.forEach((m,i)=>{ colorOf[m.name] = MODEL_COLORS[i%8]; });
  const legend = [];
  models.forEach(m=>{ if(m.cny||m.pts) legend.push('<span><i style="background:'+colorOf[m.name]+'"></i>'+esc(m.name)+'</span>'); });
  legend.push('<span><i style="background:#334155"></i>未计价</span>');
  legend.push('<span><i style="background:transparent;border-top:2px dashed var(--purple);border-radius:0;height:0;width:14px;vertical-align:2px"></i>缓存命中率</span>');
  if(days.some(x=>x.da!=null)) legend.push('<span><i style="background:transparent;border-top:2px solid var(--warn);border-radius:0;height:0;width:14px;vertical-align:2px"></i>单轮耗时</span>');
  $('#trendLegend').innerHTML = legend.join('');
  $('#trendNote').textContent = '柱高=总量，堆叠色=Top8 模型费用分色；紫色虚线=缓存命中率（右轴 0-100%）；橙色实线=单轮平均耗时（右轴 秒）。'+(isMin?'当前为单小时按5分钟桶。':(isDay?'当前为单天按小时桶。':(isMonth?'当前为按月聚合，不可下钻。':'')))+(isMonth?'':'点柱：'+(isMin?'→ 查看该时段记录':(isDay?'→ 下钻该小时按5分钟':'→ 下钻该天按小时'))+'；「← 返回」逐级回退。');
  const N = days.length;
  const rows = days.map((x,xi)=>{
    const stack = [];
    const bm = x.models||[];
    if(bm.length){
      const total = bm.reduce((s,m)=>s+m.cny+(m.pts||0)/500,0);
      if(total>0){
        bm.forEach(m=>{
          const w = (m.cny+(m.pts||0)/500)/total*100;
          if(w<0.3) return;
          stack.push('<div class="stack" style="height:'+w+'%;background:'+(colorOf[m.name]||'#334155')+'" title="'+esc(m.name)+'"></div>');
        });
      }
    }
    if(!stack.length) stack.push('<div class="stack" style="height:100%;background:#334155"></div>');
    const rate = x.i>0 ? (x.c/x.i*100) : 0;
    const costLine = bm.length ? bm.slice(0,3).map(m=>esc(m.name)+' ¥'+m.cny.toFixed(2)).join(' · ') : '';
    const tip = '<div class="tip'+(xi>=N*0.7?' flip':'')+'">'+(x.d)+(x.h!=null&&isDay?' 时':'')+(x.m!=null?':'+String(x.m*5).padStart(2,'0'):'')+'<br>'+(x.r)+' 轮 · '+fmt4(x.v)+' Token<br>输入 '+fmt(x.i)+' · 输出 '+fmt(x.o)+' · 缓存 '+fmt(x.c)+'<br>命中率 <span class="rate">'+rate.toFixed(1)+'%</span>'+(x.da!=null?' · 单轮耗时 <span style="color:var(--warn)">'+fmtDur(x.da)+'</span>':'')+(x.e?' · 出错 '+x.e:'')+(costLine?'<br>'+costLine:'')+'</div>';
    const hgt = x.v>0 ? Math.max(2, x.v/maxV*100) : 0;
    const lbl = isMonth ? x.d : (x.d.length>10?x.d.slice(x.d.length-5):x.d);
    return '<div class="col" data-d="'+esc(x.d)+'" data-h="'+(x.h!=null?x.h:'')+'" data-m="'+(x.m!=null?x.m:'')+'">'+tip+'<div class="stk" style="height:'+hgt+'%">'+
      (stack.join(''))+'</div>'+
      '<div class="tlbl">'+esc(lbl)+'</div></div>';
  }).join('');
  // 缓存命中率折线（内联 SVG，紫色虚线，右轴 0-100%）
  const hitPts = days.map((x,xi)=>{
    const rate = x.i>0 ? (x.c/x.i*100) : 0;
    return ((xi+0.5)/N*100).toFixed(2)+','+(100-Math.min(100,rate)).toFixed(2);
  }).join(' ');
  // 命中率节点（单点/多点都可见；SVG attribute 不支持 var()，颜色必须走 style）
  const hitDots = days.map((x,xi)=>{
    if(x.i<=0) return '';
    const rate = Math.min(100, x.c/x.i*100);
    return '<circle cx="'+((xi+0.5)/N*100).toFixed(2)+'" cy="'+(100-rate).toFixed(2)+'" r="1.6" style="fill:var(--purple)"/>';
  }).join('');
  const hitSvg = '<svg class="hitsvg" viewBox="0 0 100 100" preserveAspectRatio="none"><polyline points="'+hitPts+'" fill="none" style="stroke:var(--purple)" stroke-width="1.5" stroke-dasharray="3 2" vector-effect="non-scaling-stroke" opacity=".85"/>'+hitDots+'</svg>';
  // 单轮平均耗时折线（橙色实线，右轴 秒，独立量纲；无耗时数据的桶断开不连线）
  let durMax = 0;
  days.forEach(x=>{ if(x.da!=null && x.da>durMax) durMax=x.da; });
  let durSvg = '', durAxisHtml = '';
  if(durMax>0){
    let path = '', pen = false, dots = '';
    days.forEach((x,xi)=>{
      if(x.da==null){ pen=false; return; }
      const px = ((xi+0.5)/N*100).toFixed(2);
      const py = (100-Math.min(96,x.da/durMax*96)).toFixed(2);
      path += (pen?'L':'M')+px+' '+py;
      pen = true;
      dots += '<circle cx="'+px+'" cy="'+py+'" r="1.8" style="fill:var(--warn)"/>';
    });
    durSvg = '<svg class="hitsvg" viewBox="0 0 100 100" preserveAspectRatio="none"><path d="'+path+'" fill="none" style="stroke:var(--warn)" stroke-width="1.5" vector-effect="non-scaling-stroke" opacity=".9"/>'+dots+'</svg>';
    // 右轴刻度标签（柱子右侧小字）：0 / 半值 / 峰值；CSS 不支持 calc 乘法，换算为 px+% 组合
    durAxisHtml = [0, 0.5, 1].map(f=>{
      const top = 'calc('+(6-22*(1-f))+'px + '+((1-f)*100)+'%)';
      return '<span class="durlbl" style="top:'+top+'">'+fmtDur(durMax*f)+'</span>';
    }).join('');
  }
  $('#trendWrap').classList.toggle('hasDur', durMax>0);
  $('#trend').innerHTML = rows + hitSvg + durSvg;
  const oldAxis = $('#trendWrap').querySelectorAll('.durlbl'); oldAxis.forEach(e=>e.remove());
  if(durAxisHtml) $('#trendWrap').insertAdjacentHTML('beforeend', durAxisHtml);
  document.querySelectorAll('#trend .col').forEach(c=>{
    if(isMonth) return; // 按月聚合不可下钻
    c.onclick=()=>{
      const d = c.dataset.d, h = c.dataset.h, m = c.dataset.m;
      if(!curDay){ curDay = d.slice(0,10); loadTrend(); }
      else if(curDay && curHour===null && h!==''){ curHour = parseInt(h,10); loadTrend(); }
      else { // 5分钟桶 → 跳记录并过滤该时段
        showTab('rec');
        filterRecBySlot(d.slice(0,10), h===''?null:parseInt(h,10), m===''?null:parseInt(m,10));
      }
    };
  });
}

let recSlotFilter = null; // {day, h, m5|null}
function filterRecByHour(h){ recSlotFilter = {day:null, h:h, m5:null}; $('#recCount').textContent = h+'时'; loadRec(); }
function filterRecBySlot(day, h, m5){ recSlotFilter = {day:day, h:h, m5:m5}; $('#recCount').textContent = (m5!=null? (h+':'+String(m5*5).padStart(2,'0')) : (h+'时')); loadRec(); }
function showTab(name){
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('on'));
  document.querySelector('.tab[data-p="'+name+'"]').classList.add('on');
  $('#p-'+name).classList.add('on');
}

let curDim='d7';
document.querySelectorAll('[data-r]').forEach(b=>b.onclick=()=>{curDim=b.dataset.r;document.querySelectorAll('[data-r]').forEach(x=>x.classList.remove('on'));b.classList.add('on');loadDim(curDim)});
async function loadDim(r){
  const d = await jget('/analytics?range='+r);
  $('#dimRange').textContent = d.from + ' ~ ' + d.to + '（' + RL[r] + '）';
  const rows = [];
  const durTd = x => '<td>'+fmtDur(x.dur_avg)+'</td><td>'+fmtDur(x.dur_min)+'</td><td>'+fmtDur(x.dur_max)+'</td>';
  const push = (name, arr) => (arr||[]).forEach(x=>{
    rows.push('<tr><td>'+esc(name)+'</td><td>'+esc(x.k)+'</td><td>'+x.r+'</td><td>'+fmt(x.i)+'</td><td>'+fmt(x.o)+'</td><td>'+fmt(x.c)+'</td><td>'+fmt(x.v)+'</td>'+durTd(x)+'<td>'+(x.cost||'—')+'</td></tr>');
  });
  push('来源', d.bySource); push('渠道', d.byChannel); push('模型', d.byModel);
  (d.bySid||[]).forEach(x=>{ rows.push('<tr><td>会话</td><td>'+esc(x.name||x.k)+'</td><td>'+x.r+'</td><td>'+fmt(x.i)+'</td><td>'+fmt(x.o)+'</td><td>'+fmt(x.c)+'</td><td>'+fmt(x.v)+'</td>'+durTd(x)+'<td>'+(x.cost||'—')+'</td></tr>'); });
  $('#dimBody').innerHTML = rows.join('') || '<tr><td colspan="11" class="note">该范围暂无数据</td></tr>';
}
let curSessRange='d7';
document.querySelectorAll('[data-sr]').forEach(b=>b.onclick=()=>{curSessRange=b.dataset.sr;document.querySelectorAll('[data-sr]').forEach(x=>x.classList.remove('on'));b.classList.add('on');loadSess(curSessRange)});
async function loadSess(r){
  const d = await jget('/sessions?range='+r);
  $('#sessRange').textContent = d.from + ' ~ ' + d.to + '（' + RL[r] + '）';
  const g = d.groups||{};
  const gOrder = [['dm','私聊'],['gm','群聊'],['other','其他']];
  $('#sessGroups').innerHTML = gOrder.map(([k,label])=>{
    const x = g[k]||{};
    return '<div class="box" style="flex:1;min-width:150px;padding:10px 14px;margin:0"><div style="font-size:12px;color:var(--dim)">'+label+' · '+x.sessions+' 个会话</div><div style="font-size:18px;font-weight:700;margin-top:4px">'+fmt4(x.v)+' <span style="font-size:11px;color:var(--dim)">tokens</span></div><div style="font-size:11px;color:var(--dim);margin-top:2px">'+x.r+' 轮'+(x.cost?' · '+esc(x.cost):'')+'</div></div>';
  }).join('');
  const rows = (d.sessions||[]).map(x=>{
    const t = x.type==='dm'?'私聊':(x.type==='gm'?'群聊':'其他');
    return '<tr style="cursor:pointer" data-sid="'+esc(x.sid)+'" onclick="sessRecs(this.dataset.sid)"><td>'+esc(x.name||x.sid)+'</td><td>'+t+'</td><td>'+x.r+'</td><td>'+fmt(x.i)+'</td><td>'+fmt(x.o)+'</td><td>'+fmt(x.c)+'</td><td>'+fmt(x.v)+'</td><td>'+fmtDur(x.dur_avg)+'</td><td>'+fmtDur(x.dur_min)+'</td><td>'+fmtDur(x.dur_max)+'</td><td>'+(x.cost||'—')+'</td><td>'+esc(x.last_at||'')+'</td></tr>';
  }).join('');
  $('#sessBody').innerHTML = rows || '<tr><td colspan="12" class="note">该范围暂无会话数据</td></tr>';
}
function sessRecs(sid){
  showTab('rec');
  recSlotFilter = null;
  $('#recCount').textContent = '会话 '+sid;
  loadRecBySid(sid);
}
async function loadRecBySid(sid){
  const d = await jget('/records?n=100&sid='+encodeURIComponent(sid));
  $('#recBody').innerHTML = (d.recs||[]).map(x=>
    '<tr><td>'+esc(x.t)+'</td><td>'+esc(x.m)+'</td><td>'+(x.sid?('<span class="note">['+(x.type==='dm'?'私聊':(x.type==='gm'?'群聊':'其他'))+']</span> '+esc(x.sid_name||x.sid)):esc(x.s))+'</td><td>'+esc(x.ch)+'</td>'+
    '<td>'+fmt(x.i)+'</td><td>'+fmt(x.o)+'</td><td>'+fmt(x.c)+'</td><td>'+fmtDur(x.d)+'</td><td>'+fmt(x.v)+'</td><td>'+(x.co?(x.unit?x.co+' '+esc(x.unit):x.co):'—')+'</td></tr>').join('') ||
    '<tr><td colspan="10" class="note">该会话暂无记录</td></tr>';
}
async function loadRec(){
  const d = await jget('/records?n='+(recSlotFilter?100:15));
  let recs = d.recs||[];
  if(recSlotFilter){
    const sf = recSlotFilter;
    recs = recs.filter(x=>{
      const mt = x.t.match(/^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})/);
      if(!mt) return false;
      if(sf.day && mt[1]!==sf.day) return false;
      if(parseInt(mt[2],10)!==sf.h) return false;
      if(sf.m5!=null && Math.floor(parseInt(mt[3],10)/5)!==sf.m5) return false;
      return true;
    });
  }
  $('#recBody').innerHTML = recs.map(x=>
    '<tr><td>'+esc(x.t)+'</td><td>'+esc(x.m)+'</td><td>'+(x.sid?('<span class="note">['+(x.type==='dm'?'私聊':(x.type==='gm'?'群聊':'其他'))+']</span> '+esc(x.sid_name||x.sid)):esc(x.s))+'</td><td>'+esc(x.ch)+'</td>'+
    '<td>'+fmt(x.i)+'</td><td>'+fmt(x.o)+'</td><td>'+fmt(x.c)+'</td><td>'+fmtDur(x.d)+'</td><td>'+fmt(x.v)+'</td><td>'+(x.co?(x.unit?x.co+' '+esc(x.unit):x.co):'—')+'</td></tr>').join('') ||
    '<tr><td colspan="10" class="note">'+(recSlotFilter?'该时段暂无记录':'暂无记录')+'</td></tr>';
  recSlotFilter = null;
}
let priceRules = [];
let priceEditIdx = -1;
let peakProfiles = [];
let peakProfileIdx = -1;
async function loadPrice(){
  const d = await jget('/pricing');
  priceRules = d.rules||[];
  peakProfiles = d.peak_profiles || [{name:'默认工作日', windows:[['09:00','12:00'],['14:00','18:00']]}];
  $('#priceInfo').textContent = '共 ' + priceRules.length + ' 条规则 · ' + peakProfiles.length + ' 套峰谷方案';
  const sel = $('#peakProfileSel');
  sel.innerHTML = peakProfiles.map((p,i)=>'<option value="'+i+'">'+esc(p.name)+'</option>').join('');
  if(peakProfileIdx<0 || peakProfileIdx>=peakProfiles.length) peakProfileIdx = 0;
  sel.value = peakProfileIdx;
  $('#peakProfileName').value = peakProfiles[peakProfileIdx].name;
  $('#peakWindows').value = peakProfiles[peakProfileIdx].windows.map(x=>x[0]+'-'+x[1]).join(',');
  $('#priceBody').innerHTML = priceRules.length ? '<table><thead><tr><th>启用</th><th>名称</th><th>币种</th><th>单位</th><th>URL 匹配</th><th>模型匹配</th><th>渠道匹配</th><th>峰谷方案</th><th>缓存命中(峰/谷)</th><th>未命中(峰/谷)</th><th>输出(峰/谷)</th><th style="width:110px">操作</th></tr></thead><tbody>'+
    priceRules.map((r,i)=>'<tr'+(r.enabled===false?' style="opacity:.55"':'')+'><td><label class="sw"><input type="checkbox" '+(r.enabled!==false?'checked':'')+' onchange="priceToggle('+i+',this.checked)"><i></i></label></td><td>'+esc(r.name||'')+'</td><td>'+(r.currency==='积分'?'积分':'¥元')+'</td><td>'+esc(r.unit!==undefined?r.unit:(r.currency==='积分'?'积分':'¥'))+'</td><td>'+esc(r.url_match||'')+'</td><td>'+esc(r.model_match||'')+'</td><td>'+esc(r.channel_match||'')+'</td>'+
    '<td>'+(r.peak_enabled===false?'恒谷':esc(r.peak_profile||'默认工作日'))+'</td><td>'+r.hit_peak+' / '+r.hit_off+'</td><td>'+r.miss_peak+' / '+r.miss_off+'</td><td>'+r.out_peak+' / '+r.out_off+'</td>'+
    '<td><button class="btn" onclick="priceEditOpen('+i+')">编辑</button> <button class="btn" onclick="priceDel('+i+',this)">删除</button></td></tr>').join('')+'</tbody></table>'+
    '<div class="note">匹配加权 URL=4 分、模型=2 分、渠道名=1 分取最高；价格单位 元（或积分）/百万 tokens；每条规则可选自己的峰谷方案（上方方案库管理）。双币种分别累计，不与 ¥ 混算。改价/改方案后全历史费用即时重算。</div>'
    : '<div class="note">暂无价格规则，费用显示「—」。点「＋ 添加规则」配置第一条。</div>';
}
const PRICE_FIELDS = [
  ['name','规则名称','如 DeepSeek V4-Flash（官方价）'],
  ['url_match','URL 匹配（可选）','api.deepseek.com'],
  ['model_match','模型匹配（可选）','flash'],
  ['channel_match','渠道匹配（可选）','deepseek'],
  ['currency','币种','CNY=人民币计价；填 积分 则按积分计价（与 ¥ 分开累计，永不混算）'],
  ['unit','显示单位(留空=无单位)','仅影响展示：如 ¥ / 元 / $ / 积分。币种决定计价口径，单位只是显示符号，两者独立'],
  ['hit_peak','缓存命中·峰时价','0.10'],
  ['hit_off','缓存命中·谷时价','0.05'],
  ['miss_peak','未命中·峰时价','3.0'],
  ['miss_off','未命中·谷时价','1.5'],
  ['out_peak','输出·峰时价','9.0'],
  ['out_off','输出·谷时价','4.5']
];
function priceFieldHtml(f){
  return '<div style="margin-bottom:10px"><label style="display:block;font-size:12px;color:var(--dim);margin-bottom:4px">'+esc(f[1])+'</label>'+
    '<input id="pf_'+f[0]+'" style="width:100%;box-sizing:border-box;background:var(--inset);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:7px 10px;font-size:13px" placeholder="'+esc(f[2])+'"></div>';
}
function priceEditOpen(i){
  priceEditIdx = i;
  const r = (i>=0 && priceRules[i]) ? priceRules[i] : {name:'',currency:'CNY',peak_enabled:true};
  const pe = r.peak_enabled !== undefined ? r.peak_enabled : true; // 旧规则无该字段按启用处理
  $('#priceEditTitle').textContent = i>=0 ? '编辑价格规则' : '添加价格规则';
  let html = priceFieldHtml(['name','规则名称',r.name||'']);
  html += '<div style="margin-bottom:10px"><label style="display:block;font-size:12px;color:var(--dim);margin-bottom:4px">峰谷价</label><select id="pf_peak_enabled" style="width:100%;background:var(--inset);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:7px 10px;font-size:13px">'+
    '<option value="1"'+(pe?' selected':'')+'>启用峰谷价</option>'+
    '<option value="0"'+(pe?'':' selected')+'>不启用（全天按谷价）</option></select></div>';
  html += '<div style="margin-bottom:10px"><label style="display:block;font-size:12px;color:var(--dim);margin-bottom:4px">峰谷方案</label><select id="pf_peak_profile" style="width:100%;background:var(--inset);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:7px 10px;font-size:13px">'+
    peakProfiles.map(p=>'<option value="'+esc(p.name)+'"'+(r.peak_profile===p.name?' selected':'')+'>'+esc(p.name)+'</option>').join('')+'</select></div>';
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">';
  PRICE_FIELDS.slice(1).forEach(f=>{ html += priceFieldHtml(f); });
  html += '</div>';
  html += '<div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn" id="pf_cancel">取消</button><button class="btn on" id="pf_save">保存</button></div>';
  $('#priceEditForm').innerHTML = html;
  $('#pf_name').value = r.name||'';
  PRICE_FIELDS.slice(1).forEach(f=>{
    const v = r[f[0]];
    if(v!=null && v!=='') $('#pf_'+f[0]).value = v;
  });
  $('#pf_cancel').onclick = ()=>{ $('#priceEditor').style.display='none'; };
  $('#pf_save').onclick = async ()=>{
    const item = {name:$('#pf_name').value.trim(), peak_enabled:$('#pf_peak_enabled').value==='1'};
    const pp = $('#pf_peak_profile');
    if(pp && pp.value) item.peak_profile = pp.value;
    if(!item.name){ dtoast('请填写规则名称'); return; }
    PRICE_FIELDS.slice(1).forEach(f=>{
      const el = $('#pf_'+f[0]);
      if(!el) return;
      const v = el.value.trim();
      if(f[0]==='unit'){ item.unit = v; return; }
      if(v==='') return;
      if(f[0]==='currency') item.currency = v;
      else if(f[0]==='url_match'||f[0]==='model_match'||f[0]==='channel_match') item[f[0]] = v;
      else { const n = parseFloat(v); if(!isNaN(n)) item[f[0]] = n; }
    });
    if(priceEditIdx>=0 && priceRules[priceEditIdx]) priceRules[priceEditIdx] = item;
    else priceRules.push(item);
    const r2 = await fetch(API+'/pricing-config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rules:priceRules})}).then(x=>x.json());
    if(r2.ok){ $('#priceEditor').style.display='none'; loadPrice(); loadOv(); }
    else dtoast('保存失败：'+(r2.msg||'未知错误'));
  };
  $('#priceEditor').style.display='flex';
}
function priceDel(i, btn){
  confirmBtn(btn, '确认删除？', async ()=>{
    priceRules.splice(i,1);
    const r = await fetch(API+'/pricing-config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rules:priceRules})}).then(x=>x.json());
    if(r.ok){ loadPrice(); loadOv(); }
    else dtoast('删除失败：'+(r.msg||'未知错误'));
  });
}
$('#priceAdd').onclick = ()=>priceEditOpen(-1);
$('#priceEditClose').onclick = ()=>{ $('#priceEditor').style.display='none'; };
function parseWindows(raw){
  const parts = raw.split(',').map(s=>s.trim()).filter(Boolean);
  const windows = [];
  let bad = false;
  parts.forEach(p=>{
    const m = p.match(/^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$/);
    if(!m){ bad = true; return; }
    const sh=+m[1], sm=+m[2], eh=+m[3], em=+m[4];
    if(sh>23||sm>59||eh>23||em>59||sh*60+sm>=eh*60+em){ bad = true; return; }
    windows.push([String(sh).padStart(2,'0')+':'+String(sm).padStart(2,'0'), String(eh).padStart(2,'0')+':'+String(em).padStart(2,'0')]);
  });
  return bad ? null : windows;
}
async function saveProfiles(){
  const r = await fetch(API+'/pricing-profiles', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({profiles:peakProfiles})}).then(x=>x.json());
  if(r.ok){ loadPrice(); loadOv(); }
  else dtoast('保存失败：'+(r.msg||'未知错误'));
}
$('#peakProfileSel').onchange = ()=>{
  peakProfileIdx = parseInt($('#peakProfileSel').value,10);
  $('#peakProfileName').value = peakProfiles[peakProfileIdx].name;
  $('#peakWindows').value = peakProfiles[peakProfileIdx].windows.map(x=>x[0]+'-'+x[1]).join(',');
};
$('#peakWindowsSave').onclick = async ()=>{
  const w = parseWindows($('#peakWindows').value);
  if(!w){ dtoast('格式错误：请用 09:00-12:00,14:00-18:00 格式，且结束须晚于开始'); return; }
  peakProfiles[peakProfileIdx].windows = w;
  await saveProfiles();
};
$('#peakProfileAdd').onclick = ()=>{
  const name = ($('#peakProfileName').value.trim() || ('方案'+(peakProfiles.length+1)));
  if(peakProfiles.some(p=>p.name===name)){ dtoast('方案名已存在'); return; }
  peakProfiles.push({name:name, windows:[['09:00','12:00'],['14:00','18:00']]});
  peakProfileIdx = peakProfiles.length-1;
  $('#peakProfileSel').innerHTML = peakProfiles.map((p,i)=>'<option value="'+i+'">'+esc(p.name)+'</option>').join('');
  $('#peakProfileSel').value = peakProfileIdx;
  $('#peakProfileName').value = name;
  $('#peakWindows').value = '09:00-12:00,14:00-18:00';
  saveProfiles();
};
$('#peakProfileDel').onclick = async (ev)=>{
  if(peakProfiles.length<=1){ dtoast('至少保留一套方案'); return; }
  const name = peakProfiles[peakProfileIdx].name;
  if(priceRules.some(r=>r.peak_profile===name)){ dtoast('有计价规则正在使用「'+name+'」，请先改掉那些规则的方案再删'); return; }
  confirmBtn(ev.currentTarget, '确认删除？', async ()=>{
    peakProfiles.splice(peakProfileIdx,1);
    peakProfileIdx = 0;
    await saveProfiles();
  });
};
async function priceToggle(i,on){
  priceRules[i].enabled = on;
  const r = await fetch(API+'/pricing-config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rules:priceRules})}).then(x=>x.json());
  if(r.ok){ loadPrice(); loadOv(); }
  else dtoast('保存失败：'+(r.msg||'未知错误'));
}
let balSources = [];
let balEditIdx = -1;
async function loadBal(refresh){
  const d = await jget('/balance'+(refresh?'?refresh=1':''));
  balSources = d.sources||[];
  $('#balInterval').value = d.interval;
  $('#balInfo').textContent = balSources.length + ' 个源';
  $('#balBody').innerHTML = balSources.map((x,i)=>{
    const unit = x.unit!==undefined ? x.unit : (x.currency==='积分' ? '积分' : (d.unit||'元'));
    if(x.origin){
      // 派生源（简易文本/平台快捷配置派生）：配置项管理，不提供编辑/删除/开关，避免改完存不住
      const oLabel = x.origin==='simple' ? '由「New-API 简易配置」文本行管理' : '由平台快捷配置（'+esc(x.origin.replace('quick:',''))+'）管理';
      return '<tr'+(x.enabled===false?' style="opacity:.55"':'')+'><td><label class="sw" title="'+oLabel+'"><input type="checkbox" disabled '+(x.enabled!==false?'checked':'')+'><i></i></label></td><td>'+esc(x.name)+'</td><td>'+esc(x.type)+(x.est?' <span class="note">(估算)</span>':'')+'</td><td class="'+(x.ok?'ok':'bad')+'">'+(x.ok?(x.balance+(unit?' '+esc(unit):'')):'失败')+'</td>'+
      '<td>'+esc(x.at||'—')+'</td><td class="note">'+esc(x.msg||'—')+'</td>'+
      '<td class="note" title="请在插件配置中修改对应配置项">由配置项管理</td></tr>';
    }
    return '<tr'+(x.enabled===false?' style="opacity:.55"':'')+'><td><label class="sw"><input type="checkbox" '+(x.enabled!==false?'checked':'')+' onchange="balToggle('+i+',this.checked)"><i></i></label></td><td>'+esc(x.name)+'</td><td>'+esc(x.type)+(x.est?' <span class="note">(估算)</span>':'')+'</td><td class="'+(x.ok?'ok':'bad')+'">'+(x.ok?(x.balance+(unit?' '+esc(unit):'')):'失败')+'</td>'+
    '<td>'+esc(x.at||'—')+'</td><td class="note">'+esc(x.msg||'—')+'</td>'+
    '<td><button class="btn" onclick="balEditOpen('+i+')">编辑</button> <button class="btn" onclick="balDel('+i+',this)">删除</button></td></tr>';
  }).join('') ||
    '<tr><td colspan="7" class="note">未配置余额监测源，点「＋ 添加监测源」开始</td></tr>';
}
async function balToggle(i,on){
  balSources[i].enabled = on;
  const r = await fetch(API+'/balance-config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({sources:balSources})}).then(x=>x.json());
  if(r.ok){ loadBal(false); }
  else dtoast('保存失败：'+(r.msg||'未知错误'));
}
$('#balIntervalSave').onclick = async ()=>{
  const v = parseInt($('#balInterval').value,10);
  if(!v || v<5){ dtoast('间隔须为 ≥5 的整数（秒）'); return; }
  const r = await fetch(API+'/balance-interval', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({interval:v})}).then(x=>x.json());
  if(r.ok){ loadBal(false); }
  else dtoast('保存失败：'+(r.msg||'未知错误'));
};
const BAL_TYPES = [
  ['auto','auto · 自动探测（官方端点/One-API 中转站）'],
  ['custom','custom · 自定义接口'],
  ['newapi','newapi · New-API 站点'],
  ['preset','preset · 预设扣减（钱包型）'],
  ['daily','daily · 每日重置积分'],
  ['rolling','rolling · 每日累计滚存积分']
];
const BAL_FIELDS = {
  auto: [['url','站点/接口地址','https://api.deepseek.com'],['api_key','API Key',''],['unit','显示单位(留空=无单位)','元']],
  custom: [['url','接口地址','https://myproxy.example.com'],['api_key','API Key',''],['json_path','余额字段路径(可选)','balance_infos.0.total_balance'],['unit','显示单位(留空=无单位)','元']],
  newapi: [['url','站点地址','https://newapi.example.com'],['api_key','系统访问令牌',''],['api_user','用户ID(纯数字)',''],['quota_conversion','换算比例(默认500000)','500000'],['unit','显示单位(留空=无单位)','元']],
  preset: [['anchor_balance','当前余额(对表)',''],['model_ref','关联模型(可选)',''],['currency','币种(CNY/积分)','CNY'],['unit','显示单位(留空=无单位)','元']],
  daily: [['daily_quota','每日额度','1000'],['refresh_time','刷新时刻 HH:mm','00:00'],['anchor_balance','当前余额(对表,可选)',''],['model_ref','关联模型(可选)',''],['currency','币种','积分'],['unit','显示单位(留空=无单位)','积分']],
  rolling: [['daily_quota','每日额度','1000'],['refresh_time','刷新时刻 HH:mm','00:00'],['anchor_balance','当前余额(对表)',''],['model_ref','关联模型(可选)',''],['currency','币种','积分'],['unit','显示单位(留空=无单位)','积分']]
};
function balFieldHtml(f){
  const tp = (f[0]==='api_key') ? ' type="password" autocomplete="off"' : '';
  return '<div style="margin-bottom:10px"><label style="display:block;font-size:12px;color:var(--dim);margin-bottom:4px">'+esc(f[1])+'</label>'+
    '<input id="bf_'+f[0]+'"'+tp+' style="width:100%;box-sizing:border-box;background:var(--inset);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:7px 10px;font-size:13px" placeholder="'+esc(f[2])+'"></div>';
}
function balEditOpen(i){
  balEditIdx = i;
  const src = (i>=0 && balSources[i]) ? balSources[i] : {name:'',type:'auto',enabled:true};
  $('#balEditTitle').textContent = i>=0 ? '编辑监测源' : '添加监测源';
  let html = balFieldHtml(['name','名称',src.name||'']);
  html += '<div style="margin-bottom:10px"><label style="display:block;font-size:12px;color:var(--dim);margin-bottom:4px">类型</label><select id="bf_type" style="width:100%;background:var(--inset);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:7px 10px;font-size:13px">'+
    BAL_TYPES.map(t=>'<option value="'+t[0]+'"'+(src.type===t[0]?' selected':'')+'>'+esc(t[1])+'</option>').join('')+'</select></div>';
  html += '<div id="bf_dyn"></div>';
  html += '<div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim)"><input type="checkbox" id="bf_enabled"'+(src.enabled===false?'':' checked')+'> 启用</label></div>';
  html += '<div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn" id="bf_cancel">取消</button><button class="btn on" id="bf_save">保存</button></div>';
  $('#balEditForm').innerHTML = html;
  $('#bf_name').value = src.name||'';
  const renderDyn = ()=>{
    const t = $('#bf_type').value;
    $('#bf_dyn').innerHTML = (BAL_FIELDS[t]||[]).map(f=>{
      if(f[0]==='model_ref') return '<div style="margin-bottom:10px"><label style="display:block;font-size:12px;color:var(--dim);margin-bottom:4px">'+esc(f[1])+'</label><select id="bf_model_ref" style="width:100%;background:var(--inset);border:1px solid var(--line);border-radius:8px;color:var(--fg);padding:7px 10px;font-size:13px"><option value="">（不关联，按渠道/URL 全算）</option></select><div class="note">从 KiraAI 已配置的 LLM 模型中选择，估算消耗只统计该模型的用量（模型名+渠道匹配）</div></div>';
      return balFieldHtml(f);
    }).join('');
    (BAL_FIELDS[t]||[]).forEach(f=>{
      const v = src[f[0]];
      if(v!=null && v!=='') {
        if(f[0]==='model_ref') $('#bf_model_ref').value = v;
        else $('#bf_'+f[0]).value = v;
      }
    });
    if(t==='preset'||t==='daily'||t==='rolling'){
      fetch(API+'/models').then(r=>r.json()).then(d=>{
        const sel = $('#bf_model_ref');
        if(!sel) return;
        (d.models||[]).forEach(m=>{
          const op = document.createElement('option');
          op.value = m.key; op.textContent = m.label;
          sel.appendChild(op);
        });
        if(src.model_ref) sel.value = src.model_ref;
      });
    }
  };
  renderDyn();
  // 编辑已有源：回填 anchor_at（表单无输入框，用隐藏字段原样保留，避免锚定基准漂移）
  if(i>=0 && src.anchor_at) $('#bf_dyn').insertAdjacentHTML('beforeend', '<input type="hidden" id="bf_anchor_at" value="'+esc(src.anchor_at)+'">');
  $('#bf_type').onchange = renderDyn;
  $('#bf_cancel').onclick = ()=>{ $('#balEditor').style.display='none'; };
  $('#bf_save').onclick = async ()=>{
    const t = $('#bf_type').value;
    const item = {name:$('#bf_name').value.trim(), type:t, enabled:$('#bf_enabled').checked};
    if(!item.name){ dtoast('请填写名称'); return; }
    (BAL_FIELDS[t]||[]).forEach(f=>{
      const el = $('#bf_'+f[0]);
      if(!el) return;
      const v = el.value.trim();
      if(f[0]==='unit'){ item.unit = v; return; }
      if(v!=='') item[f[0]] = v;
    });
    const ha = $('#bf_anchor_at');
    if(ha && ha.value) item.anchor_at = ha.value;
    if(balEditIdx>=0 && balSources[balEditIdx]) balSources[balEditIdx] = item;
    else balSources.push(item);
    const r = await fetch(API+'/balance-config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({sources:balSources})}).then(x=>x.json());
    if(r.ok){ $('#balEditor').style.display='none'; loadBal(true); }
    else dtoast('保存失败：'+(r.msg||'未知错误'));
  };
  $('#balEditor').style.display='flex';
}
function balDel(i, btn){
  confirmBtn(btn, '确认删除？', async ()=>{
    balSources.splice(i,1);
    const r = await fetch(API+'/balance-config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({sources:balSources})}).then(x=>x.json());
    if(r.ok) loadBal(false);
    else dtoast('删除失败：'+(r.msg||'未知错误'));
  });
}
$('#balRefresh').onclick = ()=>{ $('#balRefresh').disabled=true; loadBal(true).finally(()=>$('#balRefresh').disabled=false); };
$('#balEdit').onclick = ()=>balEditOpen(-1);
$('#balEditClose').onclick = ()=>{ $('#balEditor').style.display='none'; };
$('#recRefresh').onclick = ()=>{ recSlotFilter=null; $('#recCount').textContent='15'; loadRec(); };

// 已进行时长本地 1s 计时：/stats 轮询到校准基准（_elBase），页面不可见时暂停省 CPU，
// 恢复可见时立即重新拉取校准，避免漂移
let _elBase = null;
function fmtElapsed(sec){
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec/86400), h = Math.floor(sec%86400/3600), m = Math.floor(sec%3600/60), s = sec%60;
  return (d>0?d+'天':'')+(d>0||h>0?h+'时':'')+(d>0||h>0||m>0?m+'分':'')+s+'秒';
}
setInterval(()=>{
  if(document.hidden || !_elBase) return;
  const e = $('#sessElapsed');
  if(e) e.textContent = fmtElapsed(_elBase.sec + (Date.now()-_elBase.at)/1000);
}, 1000);
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) loadOv(); });

loadOv();
setInterval(loadOv, 4000);
// 随机背景（默认开，右下角 👕 点击关闭）+ 自定义背景池（🖼 单击选图、双击清除；与挂件共享 localStorage）
(function(){
  const BG_KEY = 'tsWidgetSkinBg', CBG_KEY = 'tsWidgetCustomBg';
  let bgTimer = null;
  const customPool = ()=>{ try{ return JSON.parse(localStorage.getItem(CBG_KEY)||'[]'); }catch(e){ return []; } };
  // 经 body::before 叠层绘制（400ms 淡入过渡）
  const paintBg = url => {
    document.body.style.setProperty('--userbg', url ? 'url("'+url+'")' : 'none');
    document.body.classList.toggle('hasbg', !!url);
  };
  const applyBg = on => {
    clearInterval(bgTimer); bgTimer = null;
    document.body.classList.toggle('bg-on', on);
    if(!on){ paintBg(''); return; }
    const pool = customPool();
    if(pool.length){
      // 自定义池：加载随机选一张 + 每 60s 随机轮换
      paintBg(pool[Math.floor(Math.random()*pool.length)]);
      bgTimer = setInterval(()=>{
        const p = customPool();
        if(p.length) paintBg(p[Math.floor(Math.random()*p.length)]);
      }, 60000);
    } else {
      // 线上随机图；加载失败静默回退
      const img = new Image();
      img.onload = ()=>paintBg(img.src);
      img.onerror = ()=>{ paintBg(''); document.body.classList.remove('bg-on'); };
      img.src = 'https://image.astrdark.cyou/random?type=img&dir=image&orientation=auto&t=' + Date.now();
    }
  };
  const toast = msg => {
    let t = document.getElementById('skinToast');
    if(!t){
      t = document.createElement('div');
      t.id = 'skinToast';
      t.style.cssText = 'position:fixed;left:50%;bottom:60px;transform:translateX(-50%);background:rgba(15,23,42,.92);border:1px solid var(--line);color:var(--fg);padding:8px 16px;border-radius:20px;font-size:13px;z-index:999;transition:opacity .3s;pointer-events:none';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.opacity = '1';
    clearTimeout(t._tm);
    t._tm = setTimeout(()=>{ t.style.opacity = '0'; }, 1600);
  };
  const syncIcon = on => { $('#skinBtn').textContent = on ? '👕' : '🚫'; $('#skinBtn').title = on ? '随机背景：开（点击关闭）' : '随机背景：关（点击开启）'; };
  // 自定义背景：选 1-10 张图，canvas 压缩（长边≤1280 JPEG q0.8，超限降档 960/q0.7 再逐张减少）存 localStorage
  const compressImg = (file, maxSide, q) => new Promise((res, rej)=>{
    const r = new FileReader();
    r.onerror = rej;
    r.onload = ()=>{
      const img = new Image();
      img.onerror = rej;
      img.onload = ()=>{
        let w = img.naturalWidth, h = img.naturalHeight;
        const sc = Math.min(1, maxSide/Math.max(w,h));
        w = Math.max(1, Math.round(w*sc)); h = Math.max(1, Math.round(h*sc));
        const cv = document.createElement('canvas');
        cv.width = w; cv.height = h;
        cv.getContext('2d').drawImage(img, 0, 0, w, h);
        res(cv.toDataURL('image/jpeg', q));
      };
      img.src = r.result;
    };
    r.readAsDataURL(file);
  });
  const fileIn = document.createElement('input');
  fileIn.type = 'file'; fileIn.accept = 'image/*'; fileIn.multiple = true; fileIn.style.display = 'none';
  document.body.appendChild(fileIn);
  const bgOn = ()=>localStorage.getItem(BG_KEY) !== '0';
  const trySave = arr => { try{ localStorage.setItem(CBG_KEY, JSON.stringify(arr)); return true; }catch(e){ return false; } };
  // 把当前生效背景同步到服务端（渲染图模式用；data URL 直接 POST）
  const syncBgToServer = url => {
    try{
      fetch(API+'/bg-sync', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({data:url||''})}).catch(()=>{});
    }catch(e){}
  };
  fileIn.onchange = async ()=>{
    const files = Array.from(fileIn.files||[]);
    fileIn.value = '';
    if(!files.length) return;
    if(files.length > 10) toast('最多 10 张，仅取前 10 张');
    const list = files.slice(0, 10);
    let saved = 0;
    outer:
    for(const [ms, q] of [[1280, 0.8], [960, 0.7]]){
      const urls = [];
      for(const f of list){ try{ urls.push(await compressImg(f, ms, q)); }catch(e){} }
      while(urls.length){
        if(trySave(urls)){ saved = urls.length; break outer; }
        urls.pop();
      }
    }
    if(saved){
      applyBg(bgOn());
      const pool = customPool();
      if(pool.length) syncBgToServer(pool[Math.floor(Math.random()*pool.length)]);
      toast('自定义背景已保存（'+saved+' 张）' + (saved < list.length ? '，图片过大仅保存前 '+saved+' 张' : ''));
    }else{
      toast('图片过大，无法保存自定义背景');
    }
  };
  let clickTm = null;
  $('#bgBtn').onclick = ()=>{ clearTimeout(clickTm); clickTm = setTimeout(()=>fileIn.click(), 260); };
  $('#bgBtn').ondblclick = ()=>{
    clearTimeout(clickTm);
    localStorage.removeItem(CBG_KEY);
    applyBg(bgOn());
    syncBgToServer('');
    toast('已清除自定义背景，恢复线上随机图');
  };
  applyBg(bgOn());
  syncIcon(bgOn());
  $('#skinBtn').onclick = ()=>{
    const now = bgOn();
    localStorage.setItem(BG_KEY, now ? '0' : '1');
    applyBg(!now);
    syncIcon(!now);
    toast('随机背景：' + (!now ? '开' : '关'));
  };
})();

// ═══ 预警提醒 tab ═══
let ALERT = {enable:true, deliveries:[], rules:[], fired:{}};
let alertEditId = null, alertDelEditId = null;
const ALERT_TYPE = {balance:'余额', token:'tokens', cost:'费用'};
const ALERT_TAG = {balance:'#38bdf8', token:'#a78bfa', cost:'#f472b6'};

async function loadAlert(){
  try{
    ALERT = await jget('/alert');
    renderAlert();
  }catch(e){ $('#alertRuleList').innerHTML = '<div style="color:var(--err)">加载失败: '+e+'</div>'; }
}
function renderAlert(){
  $('#alertInfo').textContent = ALERT.enable ? '' : '（预警提醒已停用）';
  const dl = $('#alertDelList');
  dl.innerHTML = ALERT.deliveries.length ? ALERT.deliveries.map(d=>`
    <div style="display:flex;align-items:center;gap:10px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;margin-bottom:6px;background:var(--inset);${d.enabled?'':'opacity:.5'}">
      <span style="font-weight:600">${aesc(d.name)}</span>
      <span style="color:var(--dim);font-family:monospace;font-size:12px;background:var(--card);padding:2px 8px;border-radius:6px;border:1px solid var(--line)">${aesc(d.sid)}</span>
      ${d.enabled?'':'<span style="color:var(--dim);font-size:11px">已停用</span>'}
      <span style="flex:1"></span>
      <button class="btn" onclick="alertEditDel('${d.id}')">编辑</button>
      <button class="btn" onclick="alertDelDel('${d.id}', this)">删除</button>
    </div>`).join('') : '<div style="color:var(--dim);text-align:center;padding:16px;border:1px dashed var(--line);border-radius:8px">还没有投递会话，点「＋ 添加投递会话」</div>';
  const rl = $('#alertRuleList');
  rl.innerHTML = ALERT.rules.length ? ALERT.rules.map(r=>{
    const t = ALERT_TYPE[r.type]||r.type;
    const opTxt = r.op==='le'?'≤':'≥';
    const target = r.target?`<b>${aesc(r.target)}</b>`:'<b>全部</b>';
    const sess = r.session?` · 会话 <b>${aesc(r.session)}</b>`:'';
    const cur = r.currency?`（${aesc(r.currency)}）`:'（任一币种）';
    const win = (r.time_start||r.time_end||r.time_window)?` · 时段 <b>${aesc(r.time_start||'00:00')}${r.time_window?'+'+aesc(r.time_window)+'min':''}${r.time_end?'~'+aesc(r.time_end):''}</b>`:'';
    const cd = (r.max_alerts&&r.max_alerts>1)?` · 连续提醒 <b>${r.max_alerts}次/${r.alert_interval||300}s</b>`:'';
    const trg = r.trigger==='step' ? ` · 每新增 <b>${r.threshold}</b> 提醒一次` : '';
    const img = (r.mode==='llm'?r.llm_send_image:r.send_image)?' · 📷 补发概况图':'';
    const fired = ALERT.fired[r.id]?`<span style="color:var(--ok);font-size:11px;background:rgba(52,211,153,.1);border:1px solid rgba(52,211,153,.3);padding:2px 8px;border-radius:6px">已触发 ${aesc(ALERT.fired[r.id])}</span>`:'';
    const dnames = (r.deliveries||[]).map(id=>{const d=ALERT.deliveries.find(x=>x.id===id);return d?(d.enabled?d.name:`<span style="color:var(--dim);text-decoration:line-through">${d.name}</span>`):id}).join('、')||'<span style="color:var(--err)">未配置投递</span>';
    return `<div style="background:var(--inset);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:8px;${r.enabled?'':'opacity:.5'}">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span style="font-size:11px;padding:2px 8px;border-radius:6px;font-weight:600;color:${ALERT_TAG[r.type]};background:${ALERT_TAG[r.type]}22;border:1px solid ${ALERT_TAG[r.type]}44">${t}</span>
        <span style="font-size:11px;padding:2px 8px;border-radius:6px;font-weight:600;color:${r.mode==='llm'?'#fbbf24':'#34d399'};background:${r.mode==='llm'?'rgba(251,191,36,.12)':'rgba(52,211,153,.12)'};border:1px solid ${r.mode==='llm'?'rgba(251,191,36,.3)':'rgba(52,211,153,.3)'}">${r.mode==='llm'?'🤖 发给 LLM':'⚡ 机械直发'}</span>
        <label class="sw" title="${r.enabled?'停用该规则':'启用该规则'}"><input type="checkbox" ${r.enabled?'checked':''} onchange="alertToggle('${r.id}',this.checked)"><i></i></label>
        <span style="font-weight:600;font-size:13px">${aesc(r.name||r.id)}</span>
        ${fired}
        <span style="flex:1"></span>
        <button class="btn" onclick="alertTest('${r.id}')">测试</button>
        <button class="btn" onclick="alertReset('${r.id}')">复位</button>
        <button class="btn" onclick="alertEditRule('${r.id}')">编辑</button>
        <button class="btn" onclick="alertDelRule('${r.id}', this)">删除</button>
      </div>
      <div style="color:var(--dim);font-size:12px;margin-top:6px;line-height:1.7">
        ${r.type==='balance'
          ? `监测 <b style="color:var(--fg)">余额</b>：${target} ${opTxt} <b style="color:var(--warn)">${r.threshold}</b>${win}${cd}${img}`
          : `监测 <b style="color:var(--fg)">${t}</b>：${target}${sess} ${opTxt} <b style="color:var(--warn)">${r.threshold}</b>${cur}${trg}${win}${cd}${img}`}
        <br>投递到：<b style="color:var(--fg)">${dnames}</b>
        ${r.mode==='llm'?(r.llm_message?`<br>LLM 素材：<span style="color:var(--dim)">${aesc(r.llm_message)}</span>`:''):(r.message?`<br>话语：<span style="color:var(--dim)">${aesc(r.message)}</span>`:'')}
      </div>
    </div>`;
  }).join('') : '<div style="color:var(--dim);text-align:center;padding:16px;border:1px dashed var(--line);border-radius:8px">还没有预警规则，点「＋ 添加规则」</div>';
}
function aesc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function alertUid(p){return p+Date.now().toString(36)+Math.random().toString(36).slice(2,5)}

// ── 投递会话 ──
function alertEditDel(id){
  alertDelEditId = id||null;
  const d = id?ALERT.deliveries.find(x=>x.id===id):null;
  $('#alertDelTitle').textContent = d?'编辑投递会话':'添加投递会话';
  $('#alertDelForm').innerHTML = `
    <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">名称 *</label>
      <input id="ad_name" type="text" value="${d?aesc(d.name):''}" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px" placeholder="如：主群"></div>
    <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">sid *</label>
      <input id="ad_sid" type="text" value="${d?aesc(d.sid):''}" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px" placeholder="qq:gm:123456 或 qq:dm:10001">
      <div style="color:var(--dim);font-size:11px;margin-top:4px">qq:gm:群号 = 群聊 · qq:dm:QQ号 = 私聊</div></div>
    <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">启用</label>
      <div style="margin-top:4px"><input id="ad_enabled" type="checkbox" ${d?(d.enabled?'checked':''):'checked'}> 投递到此会话</div></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
      <button class="btn" onclick="$('#alertDelEditor').style.display='none'">取消</button>
      <button class="btn" style="background:var(--acc);border-color:var(--acc);color:#06283d;font-weight:600" onclick="alertSaveDel()">保存会话</button>
    </div>`;
  $('#alertDelEditor').style.display='flex';
}
async function saveAlertConfig(){
  try{
    const r = await fetch(API+'/alert-config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({deliveries: ALERT.deliveries, rules: ALERT.rules})});
    const d = await r.json();
    if(d.ok){ toast('配置已保存 ✓'); return true; }
    toast('保存失败: '+(d.msg||'未知错误')); return false;
  }catch(e){ toast('保存失败: '+e); return false; }
}
function alertSaveDel(){
  const name = $('#ad_name').value.trim(), sid = $('#ad_sid').value.trim();
  if(!name||!sid){ toast('名称和 sid 必填'); return; }
  if(alertDelEditId){
    const d = ALERT.deliveries.find(x=>x.id===alertDelEditId);
    d.name=name; d.sid=sid; d.enabled=$('#ad_enabled').checked;
  } else {
    ALERT.deliveries.push({id:alertUid('d'), name, sid, enabled:$('#ad_enabled').checked});
  }
  $('#alertDelEditor').style.display='none';
  renderAlert(); saveAlertConfig();
}
function alertDelDel(id, btn){
  confirmBtn(btn, '确认删除？', async ()=>{
    ALERT.deliveries = ALERT.deliveries.filter(d=>d.id!==id);
    ALERT.rules.forEach(r=>{r.deliveries=(r.deliveries||[]).filter(x=>x!==id)});
    renderAlert(); saveAlertConfig();
  });
}

// ── 规则 ──
function alertEditRule(id){
  alertEditId = id||null;
  const r = id?ALERT.rules.find(x=>x.id===id):null;
  $('#alertEditTitle').textContent = r?'编辑规则':'添加规则';
  const type = r?r.type:'balance';
  const mode = r?r.mode:'direct';
  const targetOpts = type==='balance'
    ? ['DeepSeek','硅基流动','NewAPI 中转','Kimi']
    : ['gm','dm','system','全部'];
  const delChk = ALERT.deliveries.length
    ? ALERT.deliveries.map(d=>{
        const disabled = !d.enabled;
        return `<label style="display:inline-flex;align-items:center;gap:5px;background:var(--inset);border:1px solid var(--line);border-radius:8px;padding:5px 10px;cursor:${disabled?'not-allowed':'pointer'};font-size:12px;margin-right:6px;margin-bottom:6px;${disabled?'opacity:.45':''}"><input type="checkbox" value="${d.id}" ${(r&&(r.deliveries||[]).includes(d.id))?'checked':''} ${disabled?'disabled':''} style="accent-color:var(--acc)"> ${aesc(d.name)}${disabled?' <span style="color:var(--dim);font-size:10px">(已停用)</span>':''}</label>`;
      }).join('')
    : '<span style="color:var(--err)">请先添加投递会话</span>';
  $('#alertEditForm').innerHTML = `
    <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">规则名称 *</label>
      <input id="ar_name" type="text" value="${r?aesc(r.name||''):''}" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px" placeholder="如：余额低于 10 元提醒"></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:0 12px">
      <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">监测类型 *</label>
        <select id="ar_type" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px">
          <option value="balance" ${type==='balance'?'selected':''}>余额</option>
          <option value="token" ${type==='token'?'selected':''}>当日 tokens</option>
          <option value="cost" ${type==='cost'?'selected':''}>当日费用</option>
        </select></div>
      <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">比较方式</label>
        <select id="ar_op" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px">
          <option value="le" ${(r?r.op:'le')==='le'?'selected':''}>低于等于（≤）</option>
          <option value="ge" ${(r?r.op:'le')==='ge'?'selected':''}>达到（≥）</option>
        </select></div>
      <div id="ar_trigger_row" style="margin-bottom:12px;display:${type==='balance'?'none':''}"><label style="font-size:12px;color:var(--dim);font-weight:600">触发模式</label>
        <select id="ar_trigger" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px">
          <option value="once" ${(r?r.trigger:'once')==='once'?'selected':''}>当天累计达阈值提醒一次</option>
          <option value="step" ${(r?r.trigger:'once')==='step'?'selected':''}>每新增达阈值提醒一次（如每 10M）</option>
        </select>
        <div style="color:var(--dim);font-size:11px;margin-top:4px">「每新增」= 每跨过一个阈值整数倍提醒一次（如每 10M 提醒），受生效时段限制，跨天重置</div></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:0 12px">
      <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600" id="ar_target_lab">${type==='balance'?'目标（余额源名，留空=任一源）':'目标（来源标签，留空=全部）'}</label>
        <input id="ar_target" type="text" list="arTargetList" value="${r?aesc(r.target||''):''}" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px" placeholder="${type==='balance'?'如：DeepSeek':'如：gm / dm'}">
        <datalist id="arTargetList">${targetOpts.map(o=>`<option value="${o}">`).join('')}</datalist>
        <div id="ar_target_hint" style="color:var(--dim);font-size:11px;margin-top:4px">${type==='balance'?'填余额源名称（包含匹配，如 DeepSeek 可匹配「DeepSeek 官方」）；留空=任一源达阈值即触发':'填来源标签（gm=群聊 / dm=私聊 / 自定义关键词规则标签）；留空=全部来源'}</div></div>
      <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">阈值 *</label>
        <input id="ar_threshold" type="text" inputmode="decimal" value="${r?r.threshold:''}" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px" placeholder="如：10 或 1M">
        <div id="ar_thr_btns" style="display:${type==='token'?'flex':'none'};gap:6px;margin-top:6px">
          <button type="button" class="btn sm" onclick="thrMul(1000)">K</button>
          <button type="button" class="btn sm" onclick="thrMul(1000000)">M</button>
          <button type="button" class="btn sm" onclick="thrMul(100000000)">亿</button>
          <button type="button" class="btn sm" onclick="thrMul(1000000000)">B</button>
        </div>
        <div id="ar_thr_conv" style="color:var(--acc);font-size:11px;margin-top:4px;display:${type==='token'?'':'none'}"></div></div>
    </div>
    <div id="ar_session_row" style="display:${type==='balance'?'none':''};grid-template-columns:1fr 1fr;gap:0 12px">
      <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">限定会话 sid（留空=全部）</label>
        <input id="ar_session" type="text" value="${r?aesc(r.session||''):''}" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px" placeholder="如：qq:gm:123456">
        <div style="color:var(--dim);font-size:11px;margin-top:4px">与「目标」同时填时取交集：只统计「来源匹配 且 会话匹配」的记录</div></div>
      <div id="ar_currency_row" style="margin-bottom:12px;display:${type==='cost'?'':'none'}"><label style="font-size:12px;color:var(--dim);font-weight:600">费用币种（留空=任一币种达阈值即触发）</label>
        <input id="ar_currency" type="text" list="arCurList" value="${r?aesc(r.currency||''):''}" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px" placeholder="如：CNY / 积分">
        <datalist id="arCurList"><option value="CNY"><option value="积分"></datalist></div>
    </div>
    <div style="font-size:12px;font-weight:700;color:var(--acc);margin:14px 0 8px;padding-top:12px;border-top:1px dashed var(--line)">投递方式</div>
    <div style="display:flex;gap:16px;margin-bottom:12px">
      <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="radio" name="ar_mode" value="direct" ${mode==='direct'?'checked':''} style="accent-color:var(--acc)"> 机械直发（不经过 LLM，零消耗）</label>
      <label style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="radio" name="ar_mode" value="llm" ${mode==='llm'?'checked':''} style="accent-color:var(--acc)"> 发给 LLM（bot 感知后主动告知）</label>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:0 12px">
      <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">机械直发话语（direct 模式）</label>
        <textarea id="ar_message" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:12px;margin-top:4px;resize:vertical;min-height:52px;font-family:monospace" placeholder="⚠️ {name} 余额仅剩 {value} {unit}，阈值 {threshold}">${r?aesc(r.message||''):''}</textarea>
        <div style="color:var(--dim);font-size:11px;margin-top:4px">可用：<code>{name}</code> 目标名 · <code>{value}</code> 当前值 · <code>{unit}</code> 单位 · <code>{threshold}</code> 阈值 · <code>{type}</code> 类型</div></div>
      <div id="ar_llm_msg_row" style="margin-bottom:12px;display:${mode==='llm'?'':'none'}"><label style="font-size:12px;color:var(--dim);font-weight:600">LLM 提醒素材（llm 模式，bot 感知后主动告知用户）</label>
        <textarea id="ar_llm_message" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:12px;margin-top:4px;resize:vertical;min-height:52px;font-family:monospace" placeholder="⚠️ {name} 余额仅剩 {value} {unit}。请告知用户情况">${r?aesc(r.llm_message||''):''}</textarea>
        <div style="color:var(--dim);font-size:11px;margin-top:4px">留空=默认「…请告知用户情况」；可用占位符同上</div></div>
      <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">最大连续提醒次数</label>
        <input id="ar_max_alerts" type="number" min="1" value="${r?(r.max_alerts!=null?r.max_alerts:1):1}" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px">
        <div style="color:var(--dim);font-size:11px;margin-top:4px">默认 1 = 只提醒 1 次。在未回升/未超过阈值前最多连续提醒 N 次，达到上限后不再提醒（等余额回升 / 跨天 / 手动复位）</div></div>
      <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">连续提醒间隔（秒）</label>
        <input id="ar_alert_interval" type="number" min="0" value="${r?(r.alert_interval!=null?r.alert_interval:300):300}" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px">
        <div style="color:var(--dim);font-size:11px;margin-top:4px">默认 300 秒（5 分钟）。两次连续提醒的最小间隔；多条规则同时触发互不影响</div></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:0 12px">
      <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">直发模式补发概况图</label>
        <div style="margin-top:4px"><input id="ar_send_image" type="checkbox" ${r&&r.send_image?'checked':''} style="accent-color:var(--acc)"> 消息先发，图异步构建好后补发</div></div>
      <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">LLM 模式补发概况图</label>
        <div style="margin-top:4px"><input id="ar_llm_send_image" type="checkbox" ${r&&r.llm_send_image?'checked':''} style="accent-color:var(--acc)"> 独立开关，bot 告知后异步补发</div></div>
    </div>
    <div style="font-size:12px;font-weight:700;color:var(--acc);margin:14px 0 8px;padding-top:12px;border-top:1px dashed var(--line)">生效时段（可选）</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0 12px">
      <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">开始 HH:mm</label>
        <input id="ar_time_start" type="time" value="${r?aesc(r.time_start||''):''}" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px"></div>
      <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">结束 HH:mm</label>
        <input id="ar_time_end" type="time" value="${r?aesc(r.time_end||''):''}" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px"></div>
      <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">窗口（分钟）</label>
        <input id="ar_time_window" type="number" min="0" value="${r?(r.time_window!=null?r.time_window:''):''}" style="width:100%;background:var(--inset);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:13px;margin-top:4px" placeholder="如 60 / 30"></div>
    </div>
    <div style="color:var(--dim);font-size:11px;margin-bottom:12px">窗口语义：从「开始」起 N 分钟内生效（如 9:00 + 60min = 9:00-10:00）；与「结束」同时给时取交集；全留空=全天生效</div>
    <div style="font-size:12px;font-weight:700;color:var(--acc);margin:14px 0 8px;padding-top:12px;border-top:1px dashed var(--line)">投递到</div>
    <div style="margin-bottom:12px">${delChk}</div>
    <div style="margin-bottom:12px"><label style="font-size:12px;color:var(--dim);font-weight:600">提醒效果预览</label>
      <div id="ar_preview" style="background:var(--inset);border:1px dashed var(--line);border-radius:8px;padding:10px 12px;font-size:12px;color:var(--dim);margin-top:4px;white-space:pre-wrap;line-height:1.6"></div></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
      <button class="btn" onclick="$('#alertEditor').style.display='none'">取消</button>
      <button class="btn" style="background:var(--acc);border-color:var(--acc);color:#06283d;font-weight:600" onclick="alertSaveRule()">保存规则</button>
    </div>`;
  // 初始化比较方式：balance 才允许 le（编辑已有 token/cost 规则时禁用）
  const leOpt0 = $('#ar_op').querySelector('option[value="le"]');
  if(leOpt0) leOpt0.disabled = type!=='balance';
  if(type!=='balance' && $('#ar_op').value==='le') $('#ar_op').value='ge';
  $('#alertEditor').style.display='flex';
  alertPreview();
  $('#ar_type').onchange = ()=>{
    const t = $('#ar_type').value;
    $('#ar_target_lab').textContent = t==='balance' ? '目标（余额源名，留空=任一源）' : '目标（来源标签，留空=全部）';
    $('#ar_target_hint').textContent = t==='balance' ? '填余额源名称（包含匹配，如 DeepSeek 可匹配「DeepSeek 官方」）；留空=任一源达阈值即触发' : '填来源标签（gm=群聊 / dm=私聊 / 自定义关键词规则标签）；留空=全部来源';
    $('#ar_target').placeholder = t==='balance' ? '如：DeepSeek' : '如：gm / dm';
    $('#ar_session_row').style.display = t==='balance' ? 'none' : 'grid';
    $('#ar_currency_row').style.display = t==='cost' ? '' : 'none';
    $('#ar_trigger_row').style.display = t==='balance' ? 'none' : '';
    // 比较方式：balance 才允许「低于等于」；token/cost 只允许「达到」
    const opSel = $('#ar_op');
    const leOpt = opSel.querySelector('option[value="le"]');
    if(leOpt) leOpt.disabled = t!=='balance';
    if(t!=='balance' && opSel.value==='le') opSel.value='ge';
    // token 模式显示 K/M/亿/B 按钮与实时换算
    const isToken = t==='token';
    $('#ar_thr_btns').style.display = isToken ? 'flex' : 'none';
    $('#ar_thr_conv').style.display = isToken ? '' : 'none';
    const opts = t==='balance' ? ['DeepSeek','硅基流动','NewAPI 中转','Kimi'] : ['gm','dm','system','全部'];
    $('#arTargetList').innerHTML = opts.map(o=>`<option value="${o}">`).join('');
    alertPreview();
  };
  ['ar_name','ar_op','ar_target','ar_threshold','ar_session','ar_currency','ar_message','ar_llm_message','ar_max_alerts','ar_alert_interval','ar_time_start','ar_time_end','ar_time_window'].forEach(id=>{
    const el = $('#'+id); if(el) el.addEventListener('input', alertPreview);
  });
  document.querySelectorAll('input[name=ar_mode]').forEach(x=>x.addEventListener('change', ()=>{
    const isLlm = document.querySelector('input[name=ar_mode]:checked').value==='llm';
    $('#ar_llm_msg_row').style.display = isLlm ? '' : 'none';
    alertPreview();
  }));
}
function parseThreshold(v){
  if(v==null||String(v).trim()==='') return NaN;
  const s = String(v).trim().toLowerCase();
  const m = s.match(/^([0-9]*\.?[0-9]+)\s*(k|m|亿|b)?$/);
  if(!m) return NaN;
  let n = parseFloat(m[1]);
  const suf = m[2]||'';
  if(suf==='k') n*=1000;
  else if(suf==='m') n*=1000000;
  else if(suf==='亿') n*=100000000;
  else if(suf==='b') n*=1000000000;
  return n;
}
function thrMul(mul){
  // 预填写：点按钮直接填该单位的值（如点 M 填 1000000）；已有值时乘以该单位
  const cur = parseThreshold($('#ar_threshold').value);
  const base = isNaN(cur) ? 0 : cur;
  $('#ar_threshold').value = base ? base*mul : mul;
  alertPreview();
}
function thrConv(v){
  if(!(v>0)) return '';
  const units = [['B',1e9],['亿',1e8],['M',1e6],['K',1e3]];
  for(const [u,div] of units){
    if(v/div >= 1) return '= '+(v/div).toFixed(v/div>=100?0:2).replace(/\.?0+$/,'')+u;
  }
  if(v >= 100) return '= '+(v/1000).toFixed(2).replace(/\.?0+$/,'')+'K';
  return '';
}
function alertPreview(){
  const type = $('#ar_type').value;
  const thRaw = $('#ar_threshold').value;
  const th = parseThreshold(thRaw);
  const thDisp = isNaN(th) ? (thRaw||'?') : th;
  const target = $('#ar_target').value.trim()||'全部';
  const unit = type==='balance'?'元':(type==='token'?'tokens':'元');
  const isLlm = document.querySelector('input[name=ar_mode]:checked').value==='llm';
  const msg = (isLlm ? $('#ar_llm_message').value.trim() : $('#ar_message').value.trim())
    || `⚠️ ${ALERT_TYPE[type]}预警：${target} 已达 {value} {unit}` + (isLlm?'。请告知用户情况':'');
  const pv = msg.replace(/\{name\}/g,target).replace(/\{value\}/g,thDisp).replace(/\{unit\}/g,unit)
    .replace(/\{threshold\}/g,thDisp).replace(/\{type\}/g,ALERT_TYPE[type]).replace(/\{target\}/g,target);
  $('#ar_preview').textContent = pv;
  const conv = $('#ar_thr_conv');
  if(conv){
    const c = thrConv(th);
    conv.textContent = c ? ('换算：'+c) : '';
  }
}
function alertSaveRule(){
  const name = $('#ar_name').value.trim();
  const thRaw = $('#ar_threshold').value;
  if(String(thRaw).trim().startsWith('-')){ toast('阈值不能为负数'); return; }
  const threshold = parseThreshold(thRaw);
  if(!name){ toast('规则名称必填'); return; }
  if(isNaN(threshold)){ toast('阈值必填且为数字（支持 K/M/亿/B 后缀，如 1M）'); return; }
  const deliveries = [...document.querySelectorAll('#alertEditForm input[type=checkbox][value]')].filter(x=>x.checked).map(x=>x.value);
  const data = {
    name, type:$('#ar_type').value, op:$('#ar_op').value,
    trigger:$('#ar_trigger').value,
    target:$('#ar_target').value.trim(), session:$('#ar_session').value.trim(),
    threshold, currency:$('#ar_currency').value.trim(),
    mode:document.querySelector('input[name=ar_mode]:checked').value,
    message:$('#ar_message').value.trim(),
    llm_message:$('#ar_llm_message').value.trim(),
    send_image:$('#ar_send_image').checked, llm_send_image:$('#ar_llm_send_image').checked,
    max_alerts:parseInt($('#ar_max_alerts').value)||1,
    alert_interval:parseInt($('#ar_alert_interval').value)||300,
    time_start:$('#ar_time_start').value, time_end:$('#ar_time_end').value,
    time_window:parseInt($('#ar_time_window').value)||'',
    deliveries
  };
  if(alertEditId){ Object.assign(ALERT.rules.find(x=>x.id===alertEditId), data); }
  else { ALERT.rules.push({id:alertUid('r'), enabled:true, ...data}); }
  $('#alertEditor').style.display='none';
  renderAlert(); saveAlertConfig();
}
function alertToggle(id, on){
  const r = ALERT.rules.find(x=>x.id===id);
  if(!r) return;
  r.enabled = on;
  renderAlert(); saveAlertConfig();
}
function alertDelRule(id, btn){
  confirmBtn(btn, '确认删除？', async ()=>{
    ALERT.rules = ALERT.rules.filter(r=>r.id!==id);
    delete ALERT.fired[id];
    renderAlert(); saveAlertConfig();
  });
}
async function alertTest(id){
  const r = ALERT.rules.find(x=>x.id===id);
  if(!r) return;
  try{
    const resp = await fetch(API+'/alert-test', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})});
    const d = await resp.json();
    if(d.ok) toast('测试投递成功：'+d.msg);
    else toast('测试投递失败：'+(d.msg||'未知错误'));
  }catch(e){ toast('测试投递失败：'+e); }
}
async function alertReset(id){
  delete ALERT.fired[id];
  renderAlert();
  try{
    await fetch(API+'/alert-reset', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})});
    toast('已复位该规则（可再次触发）');
  }catch(e){ toast('复位失败: '+e); }
}
$('#alertSaveBtn').onclick = ()=>saveAlertConfig();
$('#alertAddRule').onclick = ()=>alertEditRule(null);
$('#alertAddDel').onclick = ()=>alertEditDel(null);
$('#alertEditClose').onclick = ()=>$('#alertEditor').style.display='none';
$('#alertDelClose').onclick = ()=>$('#alertDelEditor').style.display='none';
</script>
</body>
</html>
"""


_WIDGET_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Token 挂件</title>
<style>
:root{--bg:rgba(15,23,42,.92);--card:#1e293b;--line:#334155;--fg:#e2e8f0;--dim:#94a3b8;--acc:#38bdf8;--ok:#34d399;--err:#f87171;--warn:#fbbf24;--pink:#f472b6;--purple:#a78bfa}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{background:transparent;font-family:"Segoe UI",system-ui,"Microsoft YaHei",sans-serif;overflow:hidden;background-size:cover;background-position:center;background-attachment:fixed}
body::before{content:'';position:fixed;inset:0;background-image:var(--userbg,none);background-size:cover;background-position:center;opacity:0;transition:opacity .4s ease;pointer-events:none;z-index:0}
body.hasbg::before{opacity:1}
@media (prefers-reduced-motion: reduce){ #w,#w.ball,#skinBtn{backdrop-filter:none!important;-webkit-backdrop-filter:none!important} *{transition:none!important;animation:none!important} body::before{transition:none!important} }
#skinBtn,#bgBtn{position:fixed;right:10px;bottom:10px;width:28px;height:28px;border-radius:50%;border:1px solid #334155;background:rgba(30,41,59,.7);color:#94a3b8;cursor:pointer;font-size:14px;z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(6px)}
#bgBtn{right:46px}
#skinBtn:hover,#bgBtn:hover{color:#e2e8f0;border-color:#38bdf8}
#w{position:fixed;left:16px;top:16px;width:300px;background:var(--bg);border:1px solid var(--line);border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.45);backdrop-filter:blur(8px);user-select:none;z-index:9999}
#w.ball{width:56px;height:56px;border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:move;background:var(--bg);box-shadow:0 6px 20px rgba(0,0,0,.45);border:1px solid var(--line)}
#w.ball .head,#w.ball .body,#w.ball .foot{display:none}
.ballv{display:none;font-size:14px;font-weight:700;color:var(--fg);font-variant-numeric:tabular-nums}
#w.ball .ballv{display:block}
#w.dragging{opacity:.85;border-color:var(--acc)}
body.grabbing,body.grabbing *{cursor:grabbing!important}
.head{display:flex;align-items:center;gap:8px;padding:10px 12px;cursor:move;border-bottom:1px solid rgba(51,65,85,.5)}
.head .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 6px var(--ok);flex:none;transition:background .3s,box-shadow .3s}
.head .dot.err{background:var(--err);box-shadow:0 0 6px var(--err)}
.head .t{font-size:12px;font-weight:600;color:var(--fg);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.head .hbtn{width:22px;height:22px;border:none;background:rgba(51,65,85,.4);color:var(--dim);border-radius:6px;cursor:pointer;font-size:12px;line-height:1;flex:none}
.head .hbtn:hover{color:var(--fg);background:var(--line)}
.body{padding:10px 12px 12px}
.row{display:flex;align-items:center;justify-content:space-between;padding:4px 0;font-size:12px}
.row .k{color:var(--dim)}
.row .v{font-variant-numeric:tabular-nums;font-weight:600;color:var(--fg);transition:color .15s,text-shadow .15s}
.row .v.cost{color:var(--ok)}.row .v.pts{color:var(--purple)}.row .v.in{color:var(--acc)}.row .v.out{color:var(--pink)}
.row .v.flash{color:#fff;text-shadow:0 0 8px var(--acc)}
.sep{height:1px;background:rgba(51,65,85,.5);margin:6px 0}
.bal{font-size:11.5px}
.bal .bname{color:var(--dim);max-width:120px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.foot{padding:8px 12px;border-top:1px solid rgba(51,65,85,.5);display:flex;gap:6px;align-items:center;justify-content:flex-end}
.foot .st{font-size:10px;color:var(--dim);margin-right:auto}
.foot .go{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:3px 10px;cursor:pointer;font-size:11px}
.foot .go:hover{border-color:var(--acc)}
</style>
</head>
<body>
<button id="skinBtn" title="随机背景开关">👕</button>
<button id="bgBtn" title="自定义背景：单击选择图片（1-10 张），双击清除">🖼</button>
<div id="w">
  <div class="head" id="hd">
    <span class="dot" id="dot"></span>
    <span class="t" id="title">Token 挂件</span>
    <button class="hbtn" id="pip" title="置顶浮窗（PiP）" style="display:none">📌</button>
    <button class="hbtn" id="pop" title="独立小窗">⧉</button>
    <button class="hbtn" id="fold" title="折叠">—</button>
  </div>
  <div class="body" id="body">
    <div class="row"><span class="k">本次会话</span><span class="v" id="v_sess">—</span></div>
    <div class="row"><span class="k">今日</span><span class="v" id="v_today">—</span></div>
    <div class="row"><span class="k">近7天</span><span class="v" id="v_d7">—</span></div>
    <div class="row"><span class="k">费用(今日)</span><span class="v cost" id="v_cost">—</span></div>
    <div class="row"><span class="k">费用(累计)</span><span class="v cost" id="v_cost_total">—</span></div>
    <div class="row" id="row_dur" style="display:none" title="单轮 LLM 响应耗时均值（本次会话）"><span class="k">轮均耗时</span><span class="v" id="v_dur">—</span></div>
    <div class="sep"></div>
    <div class="bal" id="bal"></div>
  </div>
  <div class="foot">
    <span class="st" id="st">—</span>
    <button class="go" id="go">打开看板</button>
  </div>
  <div class="ballv" id="ballv">0</div>
</div>
<script>
const API = '/api/plugin/KiraAI_token_stats_plugin';
const PAGE = '/page/plugin/KiraAI_token_stats_plugin';
const $ = s=>document.querySelector(s);
// #w 可能被移入 PiP 窗口的 document，保存直接元素引用，所有内部读写经 wEl/w$（跨 document 仍有效）
const wEl = document.getElementById('w');
const w$ = s=>wEl.querySelector(s);
let pipWinRef = null; // 活动中的 PiP 窗口（无则 null）
// 数字缩写：K(千)/M(百万)/亿(1e8)/B(十亿)，995 平滑进位（三处 fmt4 与 Python _fmt4 规则一致）
const fmt4 = v => { v=Math.max(0,Math.round(v||0)); if(v<1000)return ''+v;
  if(v<9950)return (v/1e3).toFixed(1).replace('.0','')+'K'; if(v<995000)return Math.round(v/1e3)+'K';
  if(v<9950000)return (v/1e6).toFixed(1).replace('.0','')+'M'; if(v<99500000)return Math.round(v/1e6)+'M';
  if(v<995000000)return (v/1e8).toFixed(1).replace('.0','')+'亿'; if(v<9950000000)return Math.round(v/1e8)+'亿';
  if(v<99500000000)return (v/1e9).toFixed(1).replace('.0','')+'B'; return Math.round(v/1e9)+'B'; };
// 单轮耗时（秒）格式化：与看板 fmtDur / Python _fmt_dur 一致（<10s→4.56s；<60s→23.4s；<60m→1m23s；≥1h→1h05m）
const fmtDur = v => { if(v==null||isNaN(+v)||+v<0) return '—'; v=+v;
  if(v<10) return v.toFixed(2)+'s';
  if(v<60) return v.toFixed(1)+'s';
  if(v<3600) return Math.floor(v/60)+'m'+String(Math.floor(v%60)).padStart(2,'0')+'s';
  return Math.floor(v/3600)+'h'+String(Math.floor(v%3600/60)).padStart(2,'0')+'m'; };
// 双模式：URL ?pop=1 → 独立弹窗模式（window.moveBy/resizeTo 控制真实窗口）
const IS_POP = new URLSearchParams(location.search).get('pop')==='1';
const POS_KEY = IS_POP ? 'tsWidgetPopPos' : 'tsWidgetPos';
let compact = localStorage.getItem('tsWidgetCompact')==='1';
let pos = null;
try{ pos = JSON.parse(localStorage.getItem(POS_KEY)||'null'); }catch(e){}
if(pos && !IS_POP){ wEl.style.left=pos.x+'px'; wEl.style.top=pos.y+'px'; }
if(compact){ wEl.style.width='180px'; }
if(IS_POP){ w$('#pop').style.display='none'; document.title='Token 挂件 · 小窗'; }

function toast(msg){
  let t = document.getElementById('skinToast');
  if(!t){
    t = document.createElement('div');
    t.id = 'skinToast';
    t.style.cssText = 'position:fixed;left:50%;bottom:50px;transform:translateX(-50%);background:rgba(15,23,42,.95);border:1px solid #334155;color:#e2e8f0;padding:6px 14px;border-radius:16px;font-size:12px;z-index:99999;transition:opacity .3s;pointer-events:none;max-width:90%';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.opacity = '1';
  clearTimeout(t._tm);
  t._tm = setTimeout(()=>{ t.style.opacity = '0'; }, 2000);
}

// 数值变化 150ms 闪动
function setVal(id, txt){
  const el = w$(id);
  if(el.textContent !== txt){
    el.textContent = txt;
    el.classList.add('flash');
    setTimeout(()=>el.classList.remove('flash'), 150);
  }
}
async function refresh(){
  try{
    const d = await fetch(API+'/stats',{cache:'no-store'}).then(r=>r.json());
    w$('#dot').classList.remove('err');
    w$('#dot').style.background = d.busy ? '#60a5fa' : '#34d399';
    w$('#title').textContent = 'Token · ' + (d.model||'—');
    setVal('#v_sess', fmt4(d.total));
    w$('#ballv').textContent = fmt4(d.total);
    setVal('#v_today', fmt4((d.ranges||{}).today ? d.ranges.today.v : 0));
    setVal('#v_d7', fmt4((d.ranges||{}).d7 ? d.ranges.d7.v : 0));
    const co = (d.costs||{}).today||{}, coT = (d.costs||{}).total||{};
    const fmtCost = c => { if(!c.matched || !c.units || !c.units.length) return '—'; return c.units.map(u=>u.unit?(u.amt+' '+u.unit):u.amt).join(' + '); };
    setVal('#v_cost', fmtCost(co));
    setVal('#v_cost_total', fmtCost(coT));
    const dur = (d.ranges||{}).session && d.ranges.session.dur;
    w$('#row_dur').style.display = (dur && dur.avg!=null) ? '' : 'none';
    if(dur && dur.avg!=null) setVal('#v_dur', fmtDur(dur.avg));
    const b = d.balance||{};
    w$('#bal').innerHTML = b.current ? '<div class="row"><span class="k">余额</span><span class="v">'+b.current+'</span></div>' : '';
    w$('#st').textContent = '会话 '+d.rounds+' 轮 · '+(d.src||'');
  }catch(e){
    w$('#dot').classList.add('err');
    w$('#dot').style.background = '';
    w$('#st').textContent = '加载失败';
  }
}
refresh();
setInterval(refresh, 5000);
// 随机背景（默认开，右下角 👕 点击关闭）+ 自定义背景池（🖼 单击选图、双击清除；localStorage 共享，PiP 同步）
const BG_KEY = 'tsWidgetSkinBg', CBG_KEY = 'tsWidgetCustomBg';
let bgTimer = null;
const customPool = ()=>{ try{ return JSON.parse(localStorage.getItem(CBG_KEY)||'[]'); }catch(e){ return []; } };
// 经 body::before 叠层绘制（400ms 淡入过渡），PiP 活动窗口同步
function paintBg(url){
  const val = url ? 'url("'+url+'")' : 'none';
  document.body.style.setProperty('--userbg', val);
  document.body.classList.toggle('hasbg', !!url);
  if(pipWinRef){
    const pb = pipWinRef.document.body;
    pb.style.setProperty('--userbg', val);
    pb.classList.toggle('hasbg', !!url);
  }
}
function applyBg(on){
  clearInterval(bgTimer); bgTimer = null;
  if(!on){ paintBg(''); return; }
  const pool = customPool();
  if(pool.length){
    // 自定义池：加载随机选一张 + 每 60s 随机轮换
    paintBg(pool[Math.floor(Math.random()*pool.length)]);
    bgTimer = setInterval(()=>{
      const p = customPool();
      if(p.length) paintBg(p[Math.floor(Math.random()*p.length)]);
    }, 60000);
  } else {
    // 线上随机图；加载失败静默回退
    const img = new Image();
    img.onload = ()=>paintBg(img.src);
    img.onerror = ()=>paintBg('');
    img.src = 'https://image.astrdark.cyou/random?type=img&dir=image&orientation=auto&t=' + Date.now();
  }
}
(function(){
  const syncIcon = on => { $('#skinBtn').textContent = on ? '👕' : '🚫'; $('#skinBtn').title = on ? '随机背景：开（点击关闭）' : '随机背景：关（点击开启）'; };
  const bgOn = localStorage.getItem(BG_KEY) !== '0';
  applyBg(bgOn);
  syncIcon(bgOn);
  $('#skinBtn').onclick = ()=>{
    const now = localStorage.getItem(BG_KEY) !== '0';
    localStorage.setItem(BG_KEY, now ? '0' : '1');
    applyBg(!now);
    syncIcon(!now);
    toast('随机背景：' + (!now ? '开' : '关'));
  };
})();
// 自定义背景：选 1-10 张图，canvas 压缩（长边≤1280 JPEG q0.8，超限降档 960/q0.7 再逐张减少）存 localStorage
function compressImg(file, maxSide, q){
  return new Promise((res, rej)=>{
    const r = new FileReader();
    r.onerror = rej;
    r.onload = ()=>{
      const img = new Image();
      img.onerror = rej;
      img.onload = ()=>{
        let w = img.naturalWidth, h = img.naturalHeight;
        const sc = Math.min(1, maxSide/Math.max(w,h));
        w = Math.max(1, Math.round(w*sc)); h = Math.max(1, Math.round(h*sc));
        const cv = document.createElement('canvas');
        cv.width = w; cv.height = h;
        cv.getContext('2d').drawImage(img, 0, 0, w, h);
        res(cv.toDataURL('image/jpeg', q));
      };
      img.src = r.result;
    };
    r.readAsDataURL(file);
  });
}
(function(){
  const fileIn = document.createElement('input');
  fileIn.type = 'file'; fileIn.accept = 'image/*'; fileIn.multiple = true; fileIn.style.display = 'none';
  document.body.appendChild(fileIn);
  const bgOn = ()=>localStorage.getItem(BG_KEY) !== '0';
  const trySave = arr => { try{ localStorage.setItem(CBG_KEY, JSON.stringify(arr)); return true; }catch(e){ return false; } };
  // 把当前生效背景同步到服务端（渲染图模式用；data URL 直接 POST）
  const syncBgToServer = url => {
    try{
      fetch(API+'/bg-sync', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({data:url||''})}).catch(()=>{});
    }catch(e){}
  };
  fileIn.onchange = async ()=>{
    const files = Array.from(fileIn.files||[]);
    fileIn.value = '';
    if(!files.length) return;
    if(files.length > 10) toast('最多 10 张，仅取前 10 张');
    const list = files.slice(0, 10);
    let saved = 0;
    outer:
    for(const [ms, q] of [[1280, 0.8], [960, 0.7]]){
      const urls = [];
      for(const f of list){ try{ urls.push(await compressImg(f, ms, q)); }catch(e){} }
      while(urls.length){
        if(trySave(urls)){ saved = urls.length; break outer; }
        urls.pop();
      }
    }
    if(saved){
      applyBg(bgOn());
      const pool = customPool();
      if(pool.length) syncBgToServer(pool[Math.floor(Math.random()*pool.length)]);
      toast('自定义背景已保存（'+saved+' 张）' + (saved < list.length ? '，图片过大仅保存前 '+saved+' 张' : ''));
    }else{
      toast('图片过大，无法保存自定义背景');
    }
  };
  let clickTm = null;
  $('#bgBtn').onclick = ()=>{ clearTimeout(clickTm); clickTm = setTimeout(()=>fileIn.click(), 260); };
  $('#bgBtn').ondblclick = ()=>{
    clearTimeout(clickTm);
    localStorage.removeItem(CBG_KEY);
    applyBg(bgOn());
    syncBgToServer('');
    toast('已清除自定义背景，恢复线上随机图');
  };
})();
// 拖动（标题栏；球模式下整个球可拖）：弹窗模式优先 window.moveBy 移动真实窗口，失败退化页内拖拽
// move/up 监听在 mousedown 时动态挂到 wEl.ownerDocument —— #w 移入 PiP 窗口后仍能拖动
let drag=false, sx=0, sy=0, ox=0, oy=0, popMove=true, dragDist=0, dragDoc=null;
wEl.addEventListener('mousedown',e=>{
  if(e.target.classList.contains('hbtn') || e.target.classList.contains('go')) return;
  if(!wEl.classList.contains('ball') && !e.target.closest('#hd')) return;
  drag=true; dragDist=0; sx=e.screenX; sy=e.screenY;
  const r=wEl.getBoundingClientRect(); ox=r.left; oy=r.top;
  wEl.classList.add('dragging');
  (wEl.ownerDocument.body||document.body).classList.add('grabbing');
  dragDoc = wEl.ownerDocument;
  dragDoc.addEventListener('mousemove', onDragMove);
  dragDoc.addEventListener('mouseup', onDragUp);
});
function onDragMove(e){
  if(!drag) return;
  dragDist += Math.abs(e.screenX-sx)+Math.abs(e.screenY-sy);
  if(IS_POP && popMove){
    try{
      window.moveBy(e.screenX-sx, e.screenY-sy);
      sx=e.screenX; sy=e.screenY;
      return;
    }catch(err){ popMove=false; sx=e.screenX; sy=e.screenY; }
  }
  wEl.style.left=Math.max(0,ox+(e.screenX-sx))+'px';
  wEl.style.top=Math.max(0,oy+(e.screenY-sy))+'px';
}
function onDragUp(){
  if(!drag) return;
  drag=false;
  if(dragDoc){ dragDoc.removeEventListener('mousemove', onDragMove); dragDoc.removeEventListener('mouseup', onDragUp); dragDoc=null; }
  wEl.classList.remove('dragging');
  (wEl.ownerDocument.body||document.body).classList.remove('grabbing');
  // 球模式下位移 <5px 判定为单击 → 展开
  if(wEl.classList.contains('ball') && dragDist<5){ setFold(false); return; }
  if(IS_POP && popMove){
    try{ localStorage.setItem(POS_KEY, JSON.stringify({x:window.screenX,y:window.screenY})); }catch(e){}
  }else{
    const r=wEl.getBoundingClientRect();
    localStorage.setItem(POS_KEY, JSON.stringify({x:r.left,y:r.top}));
  }
}
// 折叠（球模式：收成 56px 圆球显示总量，球体单击展开）；弹窗模式同步 resizeTo 真实窗口并检测是否被钳制
function setFold(toBall){
  wEl.classList.toggle('ball', toBall);
  wEl.classList.toggle('popball', toBall && IS_POP);
  w$('#fold').textContent = toBall ? '+' : '—';
  if(IS_POP){
    try{
      if(toBall){
        window.resizeTo(140,150);
        // 浏览器对 popup 外框有最小尺寸钳制，未生效则退回卡片态
        if(window.outerWidth > 220){
          toast('浏览器限制了小窗最小尺寸，无法折叠成球');
          wEl.classList.remove('ball');
          wEl.classList.remove('popball');
          w$('#fold').textContent = '—';
        }
      }else{
        window.resizeTo(340,360);
      }
    }catch(e){}
  }
}
w$('#fold').onclick=()=>setFold(!wEl.classList.contains('ball'));
// 独立小窗：同源真实 URL + 命名窗口单例 + 记忆位置；被拦截时页内 toast 提示
function openPop(){
  let left = Math.max(0,(screen.availWidth||screen.width)-380), top = 80;
  try{
    const p = JSON.parse(localStorage.getItem('tsWidgetPopPos')||'null');
    if(p){ left=p.x; top=p.y; }
  }catch(e){}
  const url = PAGE + '/stats-widget?pop=1';
  const w = window.open(url, 'tsWidgetPop', 'popup=yes,width=340,height=360,left='+left+',top='+top);
  if(!w){ toast('浏览器拦截了弹窗，请允许本站弹窗后重试'); return; }
  try{ w.focus(); }catch(e){}
}
w$('#pop').onclick=openPop;
// PiP 置顶浮窗：复制样式 + 移动 #w + pagehide 归还（侧边栏/弹窗两条路径共用）
async function openPip(){
  const pipWin = await documentPictureInPicture.requestWindow({width:340, height:360});
  // 复制样式
  let css = '';
  for(const sh of document.styleSheets){
    try{ for(const r of sh.cssRules) css += r.cssText; }catch(e){}
  }
  const st = pipWin.document.createElement('style');
  st.textContent = css;
  pipWin.document.head.appendChild(st);
  pipWin.document.body.style.cssText = document.body.style.cssText;
  pipWin.document.body.className = document.body.className;
  pipWin.document.body.appendChild(wEl);
  pipWinRef = pipWin;
  const stEl = w$('#st'); if(stEl) stEl.textContent = '已置顶（OS 级浮窗）';
  pipWin.addEventListener('pagehide', ()=>{
    pipWinRef = null;
    document.body.appendChild(wEl);
  }, {once:true});
}
// 📌：浏览器支持即显示。侧边栏模式直接在当前 iframe 调 PiP（成功即一步到位，浮窗归属主标签页）；
// 被 SecurityError/NotAllowedError 拒绝时回退：toast 提示并自动弹出独立小窗；弹窗模式保持手动点
if('documentPictureInPicture' in window){
  w$('#pip').style.display = '';
  w$('#pip').onclick = async ()=>{
    try{
      await openPip();
    }catch(e){
      if(!IS_POP){
        toast('无法直接置顶：需先弹出独立小窗，已为你打开，请在小窗中点 📌');
        openPop();
      }
      /* 弹窗模式下失败（含用户取消）静默 */
    }
  };
}
// 打开看板：同源真实地址新开标签；失败时 toast + 可复制链接
w$('#go').onclick=()=>{
  const u = PAGE + '/stats';
  const w = window.open(u, '_blank');
  if(!w){
    toast('弹窗被拦截：' + location.origin + u);
  }
};
</script>
</body>
</html>
"""
