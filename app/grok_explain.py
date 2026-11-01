"""xAI Grok 行情解读（后端调用；密钥仅来自环境变量，永不下发前端）。

默认模型：grok-4.20-0309-non-reasoning（xAI 文档中的非推理快速档，适合结构化中文解读；
可用环境变量 XAI_MODEL 覆盖。未设置 XAI_API_KEY 时服务仍可启动，接口返回「Grok 未配置」。）
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable

import requests

XAI_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_MODEL = "grok-4.20-0309-non-reasoning"
TIMEOUT_S = 45.0
MSG_UNCONFIGURED = "Grok 未配置：请在 Render/ClawCloud 环境变量设置 XAI_API_KEY 后重新部署（或 Manual Deploy）。密钥不会出现在前端。"
MSG_BUSY = "同一标的/盘面解读进行中，请稍后再试（免费实例避免并发打爆）。"

SYSTEM_PROMPT = """你是 A 股短线交易助手，严格按「情绪分档 + 阶段模式 + 选股条件 + 四大买点」框架解读。

硬性规则：
1. 只能引用用户消息里 JSON 上下文中已有的数字与字段；禁止编造价格、涨跌幅、家数、成交额、连板数。
2. 上下文缺失的字段必须写「未取到」，不得猜测补全。
3. 结论只能三选一：可建仓 / 继续观察 / 不可建仓（盘面解读用「可进攻 / 继续观察 / 防守或空仓」对应表达，并说明仓位上限来自上下文）。
4. 输出必须使用以下小标题（纯文本，可用 Markdown 标题）：
   ## 结论
   ## 依据
   ## 风险与缺失数据
   ## 下一步观察点
5. 「依据」中逐条引用上下文字段名与数值（如 sent.tier、observe.conclusion_for_observe、catalyst.flag）。
6. 文末固定一行：仅供研究参考，不构成投资建议。
7. 使用正式中文，简洁可核对；不要输出与框架无关的闲聊。"""

_LOCKS_MU = threading.Lock()
_KEY_LOCKS: dict[str, threading.Lock] = {}
_INFLIGHT: set[str] = set()


def api_key() -> str:
    return (os.environ.get("XAI_API_KEY") or "").strip()


def configured() -> bool:
    return bool(api_key())


def model_name() -> str:
    return (os.environ.get("XAI_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def status() -> dict:
    return {"configured": configured(), "model": model_name()}


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_MU:
        if key not in _KEY_LOCKS:
            _KEY_LOCKS[key] = threading.Lock()
        return _KEY_LOCKS[key]


def try_begin(key: str) -> bool:
    """非阻塞：同一 key 仅允许一个进行中的请求。"""
    lk = _lock_for(key)
    if not lk.acquire(blocking=False):
        return False
    with _LOCKS_MU:
        _INFLIGHT.add(key)
    return True


def end(key: str) -> None:
    with _LOCKS_MU:
        _INFLIGHT.discard(key)
    try:
        _lock_for(key).release()
    except RuntimeError:
        pass


def _spot_view(q: dict | None) -> dict | None:
    if not q:
        return None
    keys = ("code", "name", "price", "pct", "open", "high", "low", "pre", "amount_yi",
            "vol_ratio", "turnover_pct", "limit_up", "limit_down", "at_zt", "at_dt", "time", "board")
    return {k: q.get(k) for k in keys}


def _catalyst_view(c: dict | None) -> dict | None:
    if not c:
        return None
    top = []
    for x in (c.get("top") or [])[:3]:
        top.append({k: x.get(k) for k in ("cls", "label", "title", "date", "source")})
    return {
        "flag": c.get("flag"), "flag_kind": c.get("flag_kind"), "status": c.get("status"),
        "reason": c.get("reason"), "ann_source": c.get("ann_source"), "window": c.get("window"),
        "fetched_at": c.get("fetched_at"), "top": top,
    }


def _sent_slim(sent: dict) -> dict:
    if not sent:
        return {"tier": "未取到"}
    return {
        "date": sent.get("date"), "tier": sent.get("tier"), "stage_mode": sent.get("stage_mode"),
        "why": sent.get("why"), "one_line": sent.get("one_line"),
        "position": sent.get("position"), "must_empty": sent.get("must_empty"),
        "must_empty_reasons": sent.get("must_empty_reasons"),
        "must_empty_unchecked": sent.get("must_empty_unchecked"),
        "buy_points_active": sent.get("buy_points_active"),
        "buy_points_paused": sent.get("buy_points_paused"),
        "panic_day": sent.get("panic_day"), "tips": sent.get("tips"),
        "stats": sent.get("stats"),
        "breadth": {k: (sent.get("breadth") or {}).get(k) for k in ("up", "down", "flat", "src", "data_time", "activity")},
    }


def build_context(
    *,
    scope: str,
    code: str | None = None,
    get: Callable[[str], dict],
    sentiment: Callable[[], dict],
    read_watch: Callable[[], list],
    cat_peek: Callable[[str, str], dict | None] | None = None,
    cat_day: str | None = None,
    trade_pool_file_load: Callable[[], dict] | None = None,
    nows: Callable[[], str],
    phase_cn: str,
    ref_day: str | None = None,
    last_close_day: str | None = None,
) -> tuple[dict, list[str]]:
    """从现有 ST/缓存组装上下文；不主动打东财。返回 (context, used_keys)。"""
    used: list[str] = []
    sent = sentiment() or {}
    used.append("sentiment")
    ctx: dict[str, Any] = {
        "scope": scope,
        "now": nows(),
        "phase_cn": phase_cn,
        "ref_day": ref_day,
        "last_close_day": last_close_day,
        "sent": _sent_slim(sent),
        "framework": {
            "情绪分档": ["冰点/退潮", "修复", "主升", "过渡", "未取到"],
            "阶段模式": ["退潮期模式", "修复期模式", "主升期模式", "过渡期模式（偏谨慎）"],
            "四大买点": ["分歧转一致", "首板次日弱转强", "趋势中军回踩", "恐慌日尾盘低吸"],
            "选股硬条件摘要": "主线涨停≥5（不足则≥3候选）；成交额≥5亿；流通市值约50–500亿软条件；催化未核验不视为通过",
        },
    }

    obs_st = get("observe"); used.append("observe")
    obs = (obs_st or {}).get("data") or {}
    au_st = get("auction"); used.append("auction")
    au = (au_st or {}).get("data") or {}
    oc_st = get("open_confirm"); used.append("open_confirm")
    oc = (oc_st or {}).get("data") or {}
    tp_st = get("trade_pool"); used.append("trade_pool")
    tp = (tp_st or {}).get("data")
    if not tp and trade_pool_file_load:
        tp = trade_pool_file_load() or {}
    tp = tp or {}

    day = cat_day or obs.get("day") or last_close_day

    if scope == "market":
        ix = get("index"); used.append("index")
        lu = get("limitup"); used.append("limitup")
        ctx["observe_summary"] = {
            "ok": obs_st.get("ok"), "fetched_at": obs_st.get("fetched_at"), "error": obs_st.get("error"),
            "day": obs.get("day"), "replay": obs.get("replay"),
            "mainlines": [
                {k: m.get(k) for k in ("industry", "zt_n", "kind", "mainline_ok", "mainline_days", "main_net_yi_today")}
                for m in (obs.get("mainlines") or [])[:8]
            ],
            "mainline_note": obs.get("mainline_note"),
            "sector_inflow_note": obs.get("sector_inflow_note"),
            "observe_count": len(obs.get("observe") or []),
            "observe_codes": [
                {"code": r.get("code"), "name": r.get("name"), "conclusion": r.get("conclusion_for_observe"),
                 "planned_buy_point": r.get("planned_buy_point"), "industry": r.get("industry"), "amount_yi": r.get("amount_yi")}
                for r in (obs.get("observe") or [])[:10]
            ],
            "w2s_count": len(obs.get("w2s_candidates") or []),
            "rejected_count": len(obs.get("rejected") or []),
        }
        ctx["auction_summary"] = {
            "ok": au_st.get("ok"), "fetched_at": au_st.get("fetched_at"), "error": au_st.get("error"),
            "summary": au.get("summary"), "stage_mode": au.get("stage_mode"), "tier": au.get("tier"),
            "top": [
                {k: r.get(k) for k in ("code", "name", "conclusion", "buy_point", "pct", "reason", "tag", "src")}
                for r in (au.get("rows") or [])[:12]
            ],
        }
        ctx["open_confirm_summary"] = {
            "ok": oc_st.get("ok"), "fetched_at": oc_st.get("fetched_at"), "error": oc_st.get("error"),
            "day": oc.get("day"), "pending": oc.get("pending"), "note": oc.get("note"), "replay": oc.get("replay"),
            "summary": oc.get("summary"),
            "rows": [
                {k: r.get(k) for k in ("code", "name", "conclusion", "buy_point", "reason", "from")}
                for r in (oc.get("rows") or [])[:12]
            ],
        }
        ctx["trade_pool_summary"] = {
            "day": tp.get("day"),
            "confirmed": [
                {k: r.get(k) for k in ("code", "name", "buy_point", "reason")}
                for r in (tp.get("confirmed") or [])[:10]
            ],
            "focus_count": len(tp.get("focus") or []),
            "notes_count": len(tp.get("notes") or {}),
        }
        ctx["index"] = (ix or {}).get("data")
        ctx["limitup_meta"] = {
            "ok": (lu or {}).get("ok"), "fetched_at": (lu or {}).get("fetched_at"),
            "error": (lu or {}).get("error"),
            "date": ((lu or {}).get("data") or {}).get("date"),
            "errors": ((lu or {}).get("data") or {}).get("errors"),
        }
        return ctx, used

    # ---- stock ----
    code = (code or "").strip()
    spot_st = get("spot"); used.append("spot")
    rows = ((spot_st or {}).get("data") or {}).get("rows") or {}
    q = rows.get(code)
    name = (q or {}).get("name")
    wl = next((w for w in (read_watch() or []) if w.get("code") == code), None)
    if wl and not name:
        name = wl.get("name")
    used.append("watchlist")

    o = next((x for x in (obs.get("observe") or []) if x.get("code") == code), None)
    if not o:
        o = next((x for x in (obs.get("w2s_candidates") or []) if x.get("code") == code), None)
    if not name and o:
        name = o.get("name")
    a = next((x for x in (au.get("rows") or []) if x.get("code") == code), None)
    if not name and a:
        name = a.get("name")
    c_row = next((x for x in (oc.get("rows") or []) if x.get("code") == code), None)
    if not name and c_row:
        name = c_row.get("name")

    cat = None
    if cat_peek and day:
        try:
            cat = cat_peek(day, code)
            used.append("catalyst")
        except Exception:
            cat = None

    note = (tp.get("notes") or {}).get(code)
    in_confirmed = next((x for x in (tp.get("confirmed") or []) if x.get("code") == code), None)
    in_focus = next((x for x in (tp.get("focus") or []) if x.get("code") == code), None)

    ctx.update({
        "code": code,
        "name": name or "未取到",
        "in_watchlist": bool(wl),
        "spot": _spot_view(q),
        "spot_meta": {"ok": spot_st.get("ok"), "fetched_at": spot_st.get("fetched_at"), "error": spot_st.get("error"),
                      "source": spot_st.get("source")},
        "observe": None if not o else {
            k: o.get(k) for k in (
                "code", "name", "industry", "theme", "lb", "amount_yi", "conclusion_for_observe",
                "planned_buy_point", "reasons", "lanban", "watch_prices", "pass_flags",
            )
        },
        "auction": None if not a else {
            k: a.get(k) for k in (
                "code", "name", "conclusion", "conclusion_cls", "buy_point", "reason", "tag", "cls",
                "pct", "price", "ratio_pct", "src", "theme", "industry",
            )
        },
        "open_confirm": None if not c_row else {
            k: c_row.get(k) for k in (
                "code", "name", "conclusion", "conclusion_cls", "buy_point", "reason", "from", "metrics",
            )
        },
        "trade_pool": {
            "day": tp.get("day"),
            "confirmed": in_confirmed,
            "focus": in_focus,
            "note": note,
        },
        "catalyst": _catalyst_view(cat) if cat else (
            {"flag": "未取到", "flag_kind": "unverified", "status": "missing",
             "reason": "催化缓存未命中（未抓取或已关闭）"} if day else {"flag": "未取到", "reason": "池日未取到"}
        ),
        "observe_day": obs.get("day"),
        "observe_meta": {"ok": obs_st.get("ok"), "fetched_at": obs_st.get("fetched_at"), "error": obs_st.get("error")},
    })
    return ctx, used


def call_xai(messages: list[dict], *, model: str | None = None, key: str | None = None,
             timeout: float = TIMEOUT_S, session: requests.Session | None = None) -> dict:
    """POST OpenAI-compatible chat completions. Raises RuntimeError on HTTP/协议错误。"""
    key = (key if key is not None else api_key())
    if not key:
        raise RuntimeError(MSG_UNCONFIGURED)
    model = model or model_name()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1800,
    }
    sess = session or requests
    try:
        r = sess.post(XAI_URL, headers=headers, json=body, timeout=timeout)
    except requests.Timeout as e:
        raise RuntimeError(f"xAI 接口超时（{timeout:.0f}s）：{e}") from e
    except requests.RequestException as e:
        raise RuntimeError(f"xAI 接口请求失败：{type(e).__name__}: {e}") from e
    if r.status_code >= 400:
        detail = (r.text or "")[:400]
        raise RuntimeError(f"xAI HTTP {r.status_code}：{detail}")
    try:
        data = r.json()
    except Exception as e:
        raise RuntimeError(f"xAI 返回非 JSON：{e}") from e
    try:
        text = data["choices"][0]["message"]["content"]
    except Exception as e:
        raise RuntimeError(f"xAI 返回结构异常：{e}; body={str(data)[:300]}") from e
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("xAI 返回空文本")
    return {"text": text.strip(), "model": data.get("model") or model, "raw_id": data.get("id")}


def explain(
    *,
    scope: str,
    code: str | None = None,
    get: Callable,
    sentiment: Callable,
    read_watch: Callable,
    cat_peek: Callable | None = None,
    cat_day: str | None = None,
    trade_pool_file_load: Callable | None = None,
    nows: Callable,
    phase_cn: str,
    ref_day: str | None = None,
    last_close_day: str | None = None,
    session: requests.Session | None = None,
) -> dict:
    """完整解读流程。未配置时 ok=False 且不访问网络。"""
    scope = (scope or "").strip().lower()
    if scope not in ("stock", "market"):
        return {"ok": False, "error": "scope 仅支持 stock 或 market", "at": nows()}
    if scope == "stock":
        code = (code or "").strip()
        if not re.fullmatch(r"\d{6}", code or ""):
            return {"ok": False, "error": "个股解读需要 6 位数字代码", "at": nows()}
        lock_key = f"stock:{code}"
    else:
        code = None
        lock_key = "market"

    if not configured():
        return {
            "ok": False, "error": MSG_UNCONFIGURED, "configured": False,
            "model": model_name(), "at": nows(), "scope": scope, "code": code,
        }

    if not try_begin(lock_key):
        return {"ok": False, "error": MSG_BUSY, "at": nows(), "scope": scope, "code": code, "busy": True}

    t0 = time.time()
    try:
        ctx, used = build_context(
            scope=scope, code=code, get=get, sentiment=sentiment, read_watch=read_watch,
            cat_peek=cat_peek, cat_day=cat_day, trade_pool_file_load=trade_pool_file_load,
            nows=nows, phase_cn=phase_cn, ref_day=ref_day, last_close_day=last_close_day,
        )
        user_content = (
            ("请解读当前盘面情绪与可执行仓位/买点（scope=market）。\n" if scope == "market"
             else f"请解读股票 {code}（scope=stock）。\n")
            + "上下文 JSON 如下（只能引用其中数字）：\n"
            + json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        out = call_xai(messages, session=session)
        return {
            "ok": True,
            "text": out["text"],
            "model": out["model"],
            "used_context_keys": used,
            "at": nows(),
            "scope": scope,
            "code": code,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "configured": True,
        }
    except Exception as e:
        return {
            "ok": False, "error": str(e)[:500], "model": model_name(),
            "at": nows(), "scope": scope, "code": code, "configured": True,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    finally:
        end(lock_key)
