#!/usr/bin/env python3
"""A股盯盘 v1 — FastAPI backend + background poller.
Data via bundled quotes.py helpers (Tencent qt / ifzq, Eastmoney mirrors, akshare push2ex pools, legu).
NOT Level-2. All times Beijing (UTC+8)."""
import os, sys, json, time, uuid, hmac, threading, traceback, re, datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from concurrent.futures import ThreadPoolExecutor

APP_DIR = os.path.dirname(os.path.abspath(__file__))
POOL_DIR = os.environ.get("POOL_DIR", APP_DIR)          # dir holding quotes.py
sys.path.insert(0, POOL_DIR); sys.path.insert(0, APP_DIR) if APP_DIR != POOL_DIR else None
_argv = sys.argv; sys.argv = [sys.argv[0]]          # quotes.py parses argv at import
import quotes as Q                                   # noqa: E402
import pools as P                                    # noqa: E402
sys.argv = _argv
import requests                                      # noqa: E402
from fastapi import FastAPI, Request, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response  # noqa: E402

DATA = os.environ.get("DATA_DIR", os.path.join(APP_DIR, "data")); STATIC = os.path.join(APP_DIR, "static")
os.makedirs(DATA, exist_ok=True)
WATCH_MD = os.environ.get("WATCHLIST_MD", os.path.join(DATA, "watchlist.md"))
SEED_DIR = os.environ.get("SEED_DIR", os.path.join(os.path.dirname(APP_DIR), "seed"))
if not os.path.exists(WATCH_MD) and os.path.exists(os.path.join(SEED_DIR, "watchlist.md")):   # first boot / ephemeral disk
    import shutil; shutil.copy(os.path.join(SEED_DIR, "watchlist.md"), WATCH_MD)
RULES_F = os.path.join(DATA, "rules.json"); EVENTS_F = os.path.join(DATA, "alert_log.json")
TOKEN_F = os.path.join(DATA, "token.txt")
BJ = Q.BJ
LOCK = threading.RLock()
AMP_PCT = 5.0   # 竞价额占昨日成交额 ≥ 5% 视为“竞价额放大”

def now(): return dt.datetime.now(BJ)
def nows(): return now().strftime("%F %T")
def log(*a): print(nows(), *a, flush=True)

def token():
    if os.environ.get("ACCESS_TOKEN", "").strip(): return os.environ["ACCESS_TOKEN"].strip()
    if not os.path.exists(TOKEN_F):
        import secrets; open(TOKEN_F, "w").write(secrets.token_urlsafe(18)); os.chmod(TOKEN_F, 0o600)
    return open(TOKEN_F).read().strip()
TOKEN = token()

def jload(p, d):
    try: return json.load(open(p))
    except Exception: return d
def jsave(p, obj):
    tmp = p + ".tmp"; json.dump(obj, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, p)

# ---------------- calendar ----------------
CAL = {"days": [], "loaded": None}
def load_cal():
    if CAL["loaded"] == now().date() and CAL["days"]: return
    try:
        import akshare as ak
        CAL["days"] = sorted(str(x)[:10].replace("-", "") for x in ak.tool_trade_date_hist_sina()["trade_date"])
        CAL["loaded"] = now().date()
    except Exception as e:
        log("calendar fail", e)
def is_trade_day(d=None):
    d = d or now().strftime("%Y%m%d")
    if CAL["days"]: return d in set(CAL["days"])
    return dt.datetime.strptime(d, "%Y%m%d").weekday() < 5
def prev_trade_day(d):
    days = CAL["days"] or []
    p = [x for x in days if x < d]
    if p: return p[-1]
    x = dt.datetime.strptime(d, "%Y%m%d") - dt.timedelta(days=1)
    while x.weekday() >= 5: x -= dt.timedelta(days=1)
    return x.strftime("%Y%m%d")
def ref_day():
    """Trading day whose data is 'current': today if trading day and >=09:15, else last trading day."""
    n = now(); d = n.strftime("%Y%m%d")
    if is_trade_day(d) and n.hour * 100 + n.minute >= 915: return d
    return prev_trade_day(d)
def phase():
    n = now(); hm = n.hour * 100 + n.minute
    if not is_trade_day(): return "closed_day"
    if hm < 915: return "pre"
    if hm < 925: return "auction_call"      # 09:15-09:25 可撤单阶段
    if hm < 930: return "auction_done"      # 09:25-09:30 撮合结果已出
    if hm < 1130: return "am"
    if hm < 1300: return "noon"
    if hm < 1500: return "pm"
    if hm < 1530: return "post"
    return "after"
PHASE_CN = {"closed_day": "休市日", "pre": "盘前", "auction_call": "集合竞价(9:15-9:25)", "auction_done": "竞价结束待开盘(9:25-9:30)",
            "am": "上午连续竞价", "noon": "午间休市", "pm": "下午连续竞价", "post": "收盘后", "after": "收盘后"}
def live(): return phase() in ("auction_call", "auction_done", "am", "pm")

# ---------------- helpers ----------------
def board_of(code, name=""):
    if code.startswith(("30",)): return "创业板20cm", 0.20
    if code.startswith(("68",)): return "科创板20cm", 0.20
    if code.startswith(("8", "4", "92")): return "北交所30cm", 0.30
    if "ST" in (name or "").upper(): return ("沪主板ST5%" if code.startswith("6") else "深主板ST5%"), 0.05
    return ("沪主板10cm" if code.startswith("6") else "深主板10cm"), 0.10
def r2(x): return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
def limits(code, name, pre):
    if not pre: return None, None
    _, r = board_of(code, name)
    return r2(pre * (1 + r)), r2(pre * (1 - r))
def valid_code(c): return bool(re.fullmatch(r"\d{6}", c or ""))

# ---------------- watchlist.md sync ----------------
def read_watch():
    rows = []
    try:
        for ln in open(WATCH_MD, encoding="utf-8"):
            m = re.match(r"\|\s*(\d{6})\s*\|([^|]*)\|([^|]*)\|([^|]*)\|", ln)
            if m: rows.append({"code": m.group(1), "name": m.group(2).strip(), "board": m.group(3).strip(), "note": m.group(4).strip()})
    except FileNotFoundError: pass
    return rows
def write_watch(rows):
    head = []
    try:
        for ln in open(WATCH_MD, encoding="utf-8"):
            if ln.startswith("|"): break
            head.append(ln)
    except FileNotFoundError: pass
    if not head: head = ["# 用户自选股（只判断能否建仓，不给止盈止损价）\n"]
    out = "".join(head) + "| 代码 | 名称 | 板块 | 备注 |\n|---|---|---|---|\n"
    out += "".join(f"| {r['code']} | {r['name']} | {r['board']} | {r['note']} |\n" for r in rows)
    tmp = WATCH_MD + ".tmp"; open(tmp, "w", encoding="utf-8").write(out); os.replace(tmp, WATCH_MD)
def watch_codes(): return [r["code"] for r in read_watch()]

# ---------------- state ----------------
ST = {}   # key -> {"data", "ok", "fetched_at", "error", "source"}
def put(key, data=None, error=None, source=None):
    with LOCK:
        cur = ST.get(key, {})
        if error is None:
            ST[key] = {"ok": True, "data": data, "fetched_at": nows(), "source": source, "error": None}
        else:
            ST[key] = {**cur, "ok": False, "error": str(error)[:300], "error_at": nows()}
def get(key): return ST.get(key) or {"ok": False, "data": None, "error": "尚未取到（后台首次拉取中）"}

# ---------------- fetchers ----------------
IDX = [("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指"), ("sh000688", "科创50")]
def job_index():
    q = Q.tx_quotes([s for s, _ in IDX] + ["sh000002", "sz399107"])
    rows = [{**q[s], "name": n} for s, n in IDX if s in q]
    if not rows: raise RuntimeError("腾讯指数行情为空")
    tv = None; parts = {}
    for s in ("sh000002", "sz399107"):
        if s in q and q[s]["amount_yi"] is not None: parts[q[s]["name"]] = q[s]["amount_yi"]
    if len(parts) == 2: tv = round(sum(parts.values()), 1)
    # same-time comparison vs previous trading day from Tencent minute data
    cmp_ = None
    try:
        rd = ref_day(); pd_ = prev_trade_day(rd); cur = prev = 0.0; hhmm = None
        for s in ("sh000002", "sz399107"):
            rj = Q.S.get("https://web.ifzq.gtimg.cn/appstock/app/day/query", params={"code": s}, timeout=8).json()
            days = {d["date"]: d["data"] for d in rj["data"][s]["data"]}
            if rd not in days or pd_ not in days or not days[rd]: raise RuntimeError("no minute data")
            last = days[rd][-1].split(); hhmm = last[0]; cur += float(last[3])
            pb = [b.split() for b in days[pd_] if b.split()[0] <= hhmm]
            prev += float(pb[-1][3]) if pb else 0
        if prev: cmp_ = {"hhmm": hhmm, "today_yi": round(cur / 1e8, 1), "prev_yi": round(prev / 1e8, 1), "prev_day": pd_,
                         "chg_pct": round((cur / prev - 1) * 100, 1)}
    except Exception as e:
        cmp_ = {"error": str(e)[:120]}
    put("index", {"rows": rows, "turnover_yi": tv, "turnover_parts": parts, "turnover_cmp": cmp_,
                  "data_time": max(r["time"] for r in rows)}, source="腾讯行情 qt.gtimg.cn（成交额=沪A指数000002+深A指数399107；同比=ifzq分时）")

def job_breadth():
    b = Q.cmd_breadth()
    put("breadth", b, source="东方财富指数涨跌家数（镜像）+ 乐咕乐股 legu" + ("" if "up" in b else "（东财镜像本次失败，仅乐咕）"))

def _fill_tx(rows, date):
    """Fill 成交额 for fallback rows from Tencent quotes, only when Tencent's quote date == pool date."""
    need = [r for r in rows if r.get("amount") is None]
    if not need: return 0
    q = {}
    for i in range(0, len(need), 60):
        try: q.update(Q.tx_quotes([Q.tx_sym(r["code"]) for r in need[i:i + 60]]))
        except Exception as e: log("tx fill fail", e)
    n = 0
    for r in need:
        t = q.get(Q.tx_sym(r["code"]))
        if t and (t.get("time") or "")[:10].replace("-", "") == date and t.get("amount_yi") is not None:
            r["amount_yi"] = round(t["amount_yi"], 2); r["amount"] = t["amount_yi"] * 1e8; n += 1
            if r.get("limit") is None and "limit" in r:
                r["limit"] = limits(r["code"], r.get("name") or t.get("name"), t.get("pre_close"))[0]
    return n

def _prev_perf_fallback(rd):
    """昨日涨停今日表现 without Eastmoney: 同花顺 previous-day 涨停池 + 连板天梯, today's price from Tencent qt
    (only if Tencent's quote date == rd)."""
    pd_ = prev_trade_day(rd)
    rows, _ = P.ths_pool("zt", pd_)
    q = {}
    for i in range(0, len(rows), 60): q.update(Q.tx_quotes([Q.tx_sym(r["code"]) for r in rows[i:i + 60]]))
    out = []
    for r in rows:
        t = q.get(Q.tx_sym(r["code"]))
        if not t or (t.get("time") or "")[:10].replace("-", "") != rd or t.get("price") is None or t.get("pre_close") is None: continue
        up = limits(r["code"], t.get("name") or r["name"], t["pre_close"])[0]
        out.append({"code": r["code"], "pct": t["pct"], "price": t["price"], "limit": up, "ylb": r["lb"]})
    if len(out) < len(rows) * 0.9: raise P.PoolError(f"腾讯行情只对上 {len(out)}/{len(rows)} 只昨日涨停股（行情日期≠{rd}？）")
    return out, f"同花顺 {pd_} 涨停池 + 腾讯 {rd} 行情（备用）"

def _prev_summary(prev):
    if not prev: return None
    pc = [r for r in prev if r.get("pct") is not None]
    still = [r for r in pc if r.get("price") is not None and r.get("limit") and r["price"] >= r["limit"] - 0.005]
    lb = [r for r in pc if (r.get("ylb") or 0) >= 2]
    lb_still = [r for r in lb if r in still]
    n = len(pc)
    return {"n": n, "avg_pct": round(sum(r["pct"] for r in pc) / n, 2) if n else None, "up": sum(r["pct"] > 0 for r in pc),
            "down": sum(r["pct"] < 0 for r in pc), "still_zt": len(still), "promo_rate": round(len(still) / n * 100, 1) if n else None,
            "lb_n": len(lb), "lb_still": len(lb_still), "lb_promo_rate": round(len(lb_still) / len(lb) * 100, 1) if lb else None,
            "lb_avg_pct": round(sum(r["pct"] for r in lb) / len(lb), 2) if lb else None}

def job_limitup():
    rd = ref_day()
    with ThreadPoolExecutor(4) as ex:
        futs = {k: ex.submit(P.get_pool, k, rd) for k in ("zt", "dt", "zb", "prev")}
        res = {}
        for k, fu in futs.items():
            try: res[k] = fu.result()
            except Exception as e: res[k] = e
    if isinstance(res["prev"], Exception):
        em_err = res["prev"]
        try: rows, src = _prev_perf_fallback(rd); res["prev"] = (rows, src, f"东财失败：{em_err}")
        except Exception as e: res["prev"] = P.PoolError(f"东财：{em_err} | 备用：{e}")
    errs = {k: f"{type(v).__name__}: {v}"[:300] for k, v in res.items() if isinstance(v, Exception)}
    if "zt" in errs and "dt" in errs: raise RuntimeError(f"涨停/跌停池均失败 {errs}")
    out = {"date": rd, "errors": errs, "sources": {}, "notes": {}}
    names = {"zt": "涨停", "dt": "跌停", "zb": "炸板", "prev": "昨涨停表现"}
    for k, v in res.items():
        if isinstance(v, Exception): continue
        rows, src, note = v
        out["sources"][k] = src
        if note: out["notes"][k] = note[:300]
        if k == "prev": out["prev"] = _prev_summary(rows); continue
        if src != P.EM_LABEL and k in ("zt", "zb", "dt"): _fill_tx(rows, rd)
        if k == "zt": rows = sorted(rows, key=lambda r: (-(r["lb"] or 0), r["first_seal"] or "999999"))
        out[k] = rows
    srcs = sorted(set(out["sources"].values()))
    put("limitup", out, source="；".join(f"{names[k]}={'东财' if v == P.EM_LABEL else '同花顺备用'}" for k, v in out["sources"].items())
        + ("（" + " / ".join(srcs) + "）"))

def job_prevpools():
    """Previous trading day's pools: auction candidates + 跌停 two-day check."""
    rd = ref_day(); pd_ = prev_trade_day(rd)
    cur = get("prevpools").get("data") or {}
    if cur.get("date") == pd_ and cur.get("zt") is not None and cur.get("src_zt") == P.EM_LABEL: return
    if cur.get("date") == pd_ and cur.get("zt") is not None and P.em_cooling(): return
    zt, src_zt, _ = P.get_pool("zt", pd_); time.sleep(1)
    try: zb, src_zb, _ = P.get_pool("zb", pd_)
    except Exception as e: zb, src_zb = None, f"未取到：{e}"[:200]
    time.sleep(1)
    try: dtn = len(P.get_pool("dt", pd_)[0])
    except Exception: dtn = None
    put("prevpools", {"date": pd_, "for_day": rd, "dt_count": dtn, "zt_count": len(zt), "src_zt": src_zt, "src_zb": src_zb,
                      "zt": [{"code": r["code"], "name": r["name"], "lb": r["lb"] if r["lb"] is not None else 1, "zb_times": r["zb_times"] or 0,
                              "last_seal": r["last_seal"] or "", "first_seal": r["first_seal"] or "", "amount": r["amount"],
                              "industry": r["industry"]} for r in zt],
                      "zb": [{"code": r["code"], "name": r["name"], "zb_times": r["zb_times"] or 0, "amount": r["amount"],
                              "industry": r["industry"]} for r in (zb or [])]},
        source=f"涨停/炸板池 {pd_}：涨停={src_zt}；炸板={src_zb}")

# ---- spot + alerts ----
def rule_codes(): return sorted({r["code"] for r in jload(RULES_F, [])})
def job_spot():
    codes = list(dict.fromkeys(watch_codes() + rule_codes()))
    if not codes: put("spot", {"rows": {}}, source="腾讯行情 qt.gtimg.cn"); return
    q = Q.tx_quotes([Q.tx_sym(c) for c in codes])
    rows = {}
    for c in codes:
        r = q.get(Q.tx_sym(c))
        if not r: continue
        up, dn = limits(c, r["name"], r["pre_close"])
        r = {**r, "limit_up": up, "limit_down": dn, "board": board_of(c, r["name"])[0],
             "at_zt": bool(up and r["price"] and r["price"] >= up - 0.001), "at_dt": bool(dn and r["price"] and r["price"] <= dn + 0.001)}
        rows[c] = r
    put("spot", {"rows": rows, "missing": [c for c in codes if c not in rows]}, source="腾讯行情 qt.gtimg.cn（快照，延迟数秒）")
    check_alerts(rows)
    # live 量比 spike events
    if live():
        for c, r in rows.items():
            if (r.get("vol_ratio") or 0) >= 3: add_event(c, r["name"], "量比≥3", f"量比 {r['vol_ratio']}", r["time"][11:16] if r.get("time") else "", "vr3")

RULE_T = {"price_ge": ("价格 ≥", "price"), "price_le": ("价格 ≤", "price"), "pct_ge": ("涨幅 ≥", "pct"), "pct_le": ("涨幅 ≤", "pct")}
def rule_met(rule, r):
    f = RULE_T[rule["type"]][1]; v = r.get(f)
    if v is None: return None
    return v >= rule["value"] if rule["type"].endswith("ge") else v <= rule["value"]
def fresh(r):
    try:
        t = dt.datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ)
        return live() and (now() - t).total_seconds() < 180
    except Exception: return False
def check_alerts(rows):
    with LOCK:
        rules = jload(RULES_F, []); log_ = jload(EVENTS_F, []); changed = False
        for ru in rules:
            r = rows.get(ru["code"])
            if not r or not ru.get("enabled", True): continue
            met = rule_met(ru, r)
            if met is None: continue
            if met and fresh(r) and not ru.get("armed_off"):
                lab, f = RULE_T[ru["type"]]
                log_.insert(0, {"id": uuid.uuid4().hex[:10], "rule_id": ru["id"], "code": ru["code"], "name": r["name"], "at": nows(),
                                "msg": f"{r['name']}({ru['code']}) {lab}{ru['value']}{'%' if f == 'pct' else ''} 已触发：现价 {r['price']}，涨幅 {r['pct']}%",
                                "data_time": r["time"]})
                ru["last_triggered"] = nows(); changed = True
                if ru.get("repeat"): ru["armed_off"] = True
                else: ru["enabled"] = False
            elif not met and ru.get("armed_off"):
                ru["armed_off"] = False; changed = True
        if changed: jsave(RULES_F, rules); jsave(EVENTS_F, log_[:300])

# ---- 异动 from minute bars ----
EVENTS = {}   # day -> {key: event}
def add_event(code, name, kind, detail, hhmm, key, day=None, src="live"):
    day = day or ref_day()
    with LOCK:
        d = EVENTS.setdefault(day, {})
        k = f"{code}:{key}"
        if k in d: return
        d[k] = {"id": uuid.uuid4().hex[:10], "day": day, "code": code, "name": name, "kind": kind, "detail": detail, "time": hhmm, "src": "live" if day == now().strftime("%Y%m%d") else "replay", "at": nows()}
def scan_minutes(code, name, bars, pre, day):
    up, dn = limits(code, name, pre)
    px = []; vols = []; prev_cum = 0; state_zt = state_dt = False; cool = {"up": -99, "dn": -99}
    for i, b in enumerate(bars):
        t, p, cv = b.split()[:3]; p = float(p); cv = float(cv); v = cv - prev_cum; prev_cum = cv
        hm = f"{t[:2]}:{t[2:]}"; px.append(p); vols.append(v)
        if i >= 5 and px[i - 5]:
            ch = (p / px[i - 5] - 1) * 100
            if ch >= 2 and i - cool["up"] >= 10:
                add_event(code, name, "5分钟急涨", f"{px[i-5]}→{p}（{ch:+.2f}%）", hm, f"up{t}", day); cool["up"] = i
            if ch <= -2 and i - cool["dn"] >= 10:
                add_event(code, name, "5分钟急跌", f"{px[i-5]}→{p}（{ch:+.2f}%）", hm, f"dn{t}", day); cool["dn"] = i
        if i >= 20 and i > 0:
            base = sum(vols[i - 20:i]) / 20
            if base > 0 and v >= 5 * base and v >= 2000:
                add_event(code, name, "分钟放量", f"该分钟 {int(v)} 手，约前20分钟均量 {v / base:.1f} 倍", hm, f"vol{t}", day)
        if up:
            if p >= up - 0.001 and not state_zt: add_event(code, name, "触及涨停", f"涨停价 {up}", hm, f"zt{t}", day); state_zt = True
            elif p < up - 0.001 and state_zt: add_event(code, name, "涨停开板", f"现价 {p} < 涨停价 {up}", hm, f"kb{t}", day); state_zt = False
        if dn:
            if p <= dn + 0.001 and not state_dt: add_event(code, name, "触及跌停", f"跌停价 {dn}", hm, f"dt{t}", day); state_dt = True
            elif p > dn + 0.001 and state_dt: add_event(code, name, "跌停开板", f"现价 {p} > 跌停价 {dn}", hm, f"dk{t}", day); state_dt = False
def job_minutes():
    codes = watch_codes(); rd = ref_day(); names = {r["code"]: r["name"] for r in read_watch()}
    done, errs = [], {}
    def one(c):
        try:
            days = {d["date"]: d for d in Q.tx_days(c)}
            if rd not in days: return c, "无该日分时（停牌或未开盘）"
            d = days[rd]; scan_minutes(c, names.get(c, c), d["data"], Q.num(d.get("prec")), rd); return c, None
        except Exception as e: return c, f"{type(e).__name__}: {e}"[:120]
    with ThreadPoolExecutor(4) as ex:
        for c, e in ex.map(one, codes):
            (errs.__setitem__(c, e) if e else done.append(c))
    put("minutes", {"day": rd, "scanned": done, "errors": errs}, source="腾讯分时 web.ifzq.gtimg.cn（1分钟收盘价口径）")

# ---- auction ----
def auction_candidates():
    pp = get("prevpools").get("data") or {}
    cand = {}
    for c in watch_codes(): cand[c] = {"code": c, "src": ["自选"], "prev_amount": None}
    for r in pp.get("zt", []):
        lan = r["zb_times"] > 0 or r["last_seal"] >= "140000"
        tag = f"昨日{r['lb']}板" + ("·烂板/尾盘板" if lan else "")
        e = cand.setdefault(r["code"], {"code": r["code"], "src": [], "prev_amount": None}); e["src"].append(tag)
        e["prev_amount"] = r["amount"]; e["name"] = r["name"]; e["industry"] = r["industry"]
    for r in pp.get("zb", []):
        e = cand.setdefault(r["code"], {"code": r["code"], "src": [], "prev_amount": None}); e["src"].append(f"昨日炸板{r['zb_times']}次")
        e["prev_amount"] = r["amount"]; e["name"] = r["name"]; e["industry"] = r["industry"]
    return pp, cand
def tag_auction(pct, ratio, is_yz, code, name):
    if pct is None: return "未取到", "na"
    if is_yz: return "一字/竞价涨停·只看不追", "watch"
    if pct > 7: return "高开>7%·只看不追", "watch"
    if 2 <= pct <= 5:
        if ratio is not None and ratio >= AMP_PCT: return "弱转强候选", "w2s"
        return "高开2–5%·竞价额未放大", "weak"
    if pct <= -3: return "低开>3%·等分歧转一致", "fqzy"
    return "—", "none"

def _yest_lb(src):
    for s in src or []:
        m = re.search(r"昨日(\d+)板", s)
        if m: return int(m.group(1))
    return None

def _is_lanban(src):
    return any(("烂板" in s or "尾盘板" in s) for s in (src or []))

def auction_verdict(row, stage_mode, ind_zt=None):
    """按选股/买点规则给出买点、结论、一句话理由。退潮期暂停分歧转一致建仓。"""
    src = row.get("src") or []; cls = row.get("cls"); pct = row.get("pct"); ratio = row.get("ratio_pct")
    ind = row.get("industry"); lb = _yest_lb(src); lan = _is_lanban(src)
    retreat = stage_mode == "退潮期模式"
    ind_n = (ind_zt or {}).get(ind) if ind else None
    theme = f"{ind}（昨涨停{ind_n}家）" if ind and ind_n is not None else (ind or "未取到")
    base = {"buy_point": "—", "conclusion": "未取到", "conclusion_cls": "na", "reason": "竞价数据未取到", "theme": theme, "yest_lb": lb, "lanban": lan}

    if cls == "na" or pct is None:
        return base
    if cls == "watch":
        return {**base, "buy_point": "—", "conclusion": "只看不追", "conclusion_cls": "watch",
                "reason": ("竞价一字/涨停，只看不追" if "一字" in (row.get("tag") or "") else f"高开{pct}%超过7%，只看不追")}
    if cls == "w2s":
        bp = "首板次日弱转强"
        # 退潮期：只做新方向首板（优先昨日1板+烂板/尾盘板）；高位连板接力一律不可
        if retreat and lb is not None and lb >= 3:
            return {**base, "buy_point": bp, "conclusion": "不可建仓", "conclusion_cls": "no",
                    "reason": f"退潮期不做{lb}板及以上高位连板接力"}
        if retreat and not (lan or lb == 1):
            return {**base, "buy_point": bp, "conclusion": "暂不可建仓", "conclusion_cls": "hold",
                    "reason": f"高开{pct}%且竞价额占昨{ratio}%，但非昨日首板/烂板，退潮期慎做"}
        return {**base, "buy_point": bp, "conclusion": "可建仓", "conclusion_cls": "yes",
                "reason": (f"退潮期主攻：高开{pct}%且竞价额占昨{ratio}%，昨日{'烂板/尾盘板' if lan else '首板'}候选"
                           if retreat else f"高开{pct}%且竞价额放大（占昨{ratio}%）→弱转强")}
    if cls == "fqzy":
        bp = "分歧转一致"
        if retreat:
            return {**base, "buy_point": bp, "conclusion": "继续观察", "conclusion_cls": "hold",
                    "reason": "退潮期暂停「分歧转一致」买点，标继续观察"}
        return {**base, "buy_point": bp, "conclusion": "暂不可建仓", "conclusion_cls": "hold",
                "reason": f"低开{abs(pct)}%，等开盘后快速翻红/回封再确认"}
    if cls == "weak":
        return {**base, "buy_point": "首板次日弱转强", "conclusion": "暂不可建仓", "conclusion_cls": "hold",
                "reason": f"高开{pct}%但竞价额未放大（占昨{ratio if ratio is not None else '—'}%，阈值{AMP_PCT}%）"}
    # none — 无明确买点
    if retreat and lb is not None and lb >= 3:
        return {**base, "buy_point": "—", "conclusion": "不可建仓", "conclusion_cls": "no",
                "reason": f"退潮期不做{lb}板及以上高位连板"}
    return {**base, "buy_point": "—", "conclusion": "继续观察", "conclusion_cls": "hold",
            "reason": f"竞价涨幅{pct}%未落入弱转强/只看不追/低开分歧区间"}
def job_auction():
    ph = phase(); rd = ref_day(); today = now().strftime("%Y%m%d")
    pp, cand = auction_candidates()
    if not pp: raise RuntimeError("昨日涨停/炸板池尚未取到")
    codes = list(cand)
    rows = []
    if ph == "auction_done":
        mode = "live"; snap = {}
        for i in range(0, len(codes), 60): snap.update(Q.tx_quotes([Q.tx_sym(c) for c in codes[i:i + 60]]))
        for c in codes:
            s = snap.get(Q.tx_sym(c)); e = cand[c]
            if not s: rows.append({**e, "error": "未取到"}); continue
            rows.append({**e, "name": s["name"], "price": s["price"], "pre": s["pre_close"], "amount": (s["amount_yi"] or 0) * 1e8, "time": s["time"]})
    elif ph in ("pre", "auction_call") and today == rd:
        cur = get("auction").get("data") or {}
        put("auction", {**cur, "note": "09:25 前无撮合结果；09:25 后自动刷新"}, source=get("auction").get("source")); return
    else:
        cur = get("auction").get("data") or {}
        # 旧缓存无 conclusion 字段时强制重算（规则升级）
        if cur.get("day") == rd and cur.get("mode") == "first_bar" and cur.get("complete") and cur.get("rows") and cur["rows"][0].get("conclusion"): return
        mode = "first_bar"
        def one(c):
            e = cand[c]
            try:
                days = {d["date"]: d for d in Q.tx_days(c)}
                d = days.get(rd)
                if not d or not d["data"]: return {**e, "error": "当日无分时（停牌？）"}
                b = d["data"][0].split()
                pa = e["prev_amount"]
                if pa is None:
                    pd_ = prev_trade_day(rd)
                    if pd_ in days and days[pd_]["data"]: pa = float(days[pd_]["data"][-1].split()[3])
                return {**e, "prev_amount": pa, "price": float(b[1]), "pre": Q.num(d.get("prec")), "amount": float(b[3]), "time": f"{rd} {b[0][:2]}:{b[0][2:]}（首根分时）"}
            except Exception as ex_: return {**e, "error": f"{type(ex_).__name__}"}
        with ThreadPoolExecutor(6) as ex: rows = list(ex.map(one, codes))
        if not rows[0:1] and not rows: pass
    out = []
    names = {}
    if any(not r.get("name") for r in rows):
        try:
            q = Q.tx_quotes([Q.tx_sym(r["code"]) for r in rows if not r.get("name")]); names = {v["code"]: v["name"] for v in q.values()}
        except Exception: pass
    # 情绪档位（影响退潮期买点是否可用）；行业昨涨停家数作「所属主线」参考
    sent = sentiment()
    stage = sent.get("stage_mode") or "过渡期模式"
    ind_zt = {}
    for zr in (pp.get("zt") or []):
        if zr.get("industry"): ind_zt[zr["industry"]] = ind_zt.get(zr["industry"], 0) + 1
    # 若当日涨停池已取到，优先用当日行业涨停数（主线更贴近）
    ind_zt_today = {}
    for zr in ((get("limitup").get("data") or {}).get("zt") or []):
        if zr.get("industry"): ind_zt_today[zr["industry"]] = ind_zt_today.get(zr["industry"], 0) + 1
    if ind_zt_today: ind_zt = ind_zt_today

    for r in rows:
        name = r.get("name") or names.get(r["code"]) or r["code"]
        if r.get("error"):
            row = {"code": r["code"], "name": name, "src": r["src"], "industry": r.get("industry"), "error": r["error"], "tag": "未取到", "cls": "na"}
            row.update(auction_verdict(row, stage, ind_zt)); out.append(row); continue
        pre, price = r.get("pre"), r.get("price")
        pct = round((price / pre - 1) * 100, 2) if price and pre else None
        ratio = round(r["amount"] / r["prev_amount"] * 100, 2) if r.get("amount") and r.get("prev_amount") else None
        up, _ = limits(r["code"], name, pre)
        yz = bool(up and price and price >= up - 0.001)
        tag, cls = tag_auction(pct, ratio, yz, r["code"], name)
        row = {"code": r["code"], "name": name, "src": r["src"], "industry": r.get("industry"), "price": price, "pre": pre, "pct": pct,
               "amount_wan": round(r["amount"] / 1e4, 1) if r.get("amount") is not None else None,
               "prev_amount_yi": round(r["prev_amount"] / 1e8, 2) if r.get("prev_amount") else None, "ratio_pct": ratio,
               "tag": tag, "cls": cls, "time": r.get("time"), "is_watchlist": "自选" in (r.get("src") or [])}
        row.update(auction_verdict(row, stage, ind_zt)); out.append(row)
    order = {"yes": 0, "watch": 1, "hold": 2, "no": 3, "na": 4}
    cls_ord = {"w2s": 0, "watch": 1, "fqzy": 2, "weak": 3, "none": 4, "na": 5}
    out.sort(key=lambda x: (order.get(x.get("conclusion_cls"), 9), cls_ord.get(x["cls"], 9), -(x.get("pct") or -99)))
    zt_rows = [x for x in out if any(s.startswith("昨日") and "板" in s and "炸板" not in s for s in x["src"]) and x.get("pct") is not None]
    summ = {"n": len(out), "ok": sum(1 for x in out if x.get("pct") is not None),
            "prev_zt_avg_pct": round(sum(x["pct"] for x in zt_rows) / len(zt_rows), 2) if zt_rows else None,
            "yz": sum(1 for x in out if x["cls"] == "watch" and (x.get("tag") or "").startswith("一字")),
            "w2s": sum(1 for x in out if x["cls"] == "w2s"), "fqzy": sum(1 for x in out if x["cls"] == "fqzy"),
            "can_buy": sum(1 for x in out if x.get("conclusion") == "可建仓"),
            "watch_only": sum(1 for x in out if x.get("conclusion") == "只看不追"),
            "observe": sum(1 for x in out if x.get("conclusion") in ("继续观察", "暂不可建仓")),
            "no_buy": sum(1 for x in out if x.get("conclusion") == "不可建仓")}
    complete = summ["ok"] >= max(1, int(0.9 * summ["n"]))
    put("auction", {"day": rd, "mode": mode, "candidates_from": pp.get("date"), "rows": out, "summary": summ, "complete": complete,
                    "amp_threshold_pct": AMP_PCT, "replay": rd != today, "stage_mode": stage, "tier": sent.get("tier"),
                    "must_empty": sent.get("must_empty"), "position": sent.get("position"),
                    "buy_points_active": sent.get("buy_points_active"), "buy_points_paused": sent.get("buy_points_paused")},
        source=("腾讯快照 qt.gtimg.cn（9:25-9:30 竞价撮合价/额）" if mode == "live" else "腾讯分时首根 09:30 bar（=开盘集合竞价撮合）")
               + f"；候选=自选+{pp.get('date')}涨停/炸板池（{pp.get('src_zt') or '东方财富'}）")

# ---------------- sentiment ----------------
def sentiment():
    lu = get("limitup").get("data") or {}; br = get("breadth").get("data") or {}; ix = get("index").get("data") or {}
    pp = get("prevpools").get("data") or {}
    zt = len(lu["zt"]) if lu.get("zt") is not None else None
    dtn = len(lu["dt"]) if lu.get("dt") is not None else None
    zbn = len(lu["zb"]) if lu.get("zb") is not None else None
    zb_rate = round(zbn / (zt + zbn) * 100, 1) if zt is not None and zbn is not None and (zt + zbn) else None
    max_lb = max((r["lb"] for r in lu.get("zt", [])), default=None) if lu.get("zt") is not None else None
    prev = lu.get("prev") or {}
    promo = prev.get("promo_rate"); avg = prev.get("avg_pct")
    tc = (ix.get("turnover_cmp") or {})
    vol_up = (tc.get("chg_pct") > 0) if tc.get("chg_pct") is not None else None
    def c(label, val, cond):
        return {"label": label, "value": val, "met": (None if val is None else bool(cond))}
    ice = [c("跌停≥50 或 跌停多于涨停", f"跌停{dtn} / 涨停{zt}" if dtn is not None and zt is not None else None, dtn is not None and zt is not None and (dtn >= 50 or dtn > zt)),
           c("最高板≤5", max_lb, max_lb is not None and max_lb <= 5),
           c("炸板率高（≥30%）", f"{zb_rate}%" if zb_rate is not None else None, zb_rate is not None and zb_rate >= 30),
           c("昨日涨停今日平均表现为负", f"{avg}%" if avg is not None else None, avg is not None and avg < 0)]
    rep = [c("涨停≥60 且 跌停≤20", f"涨停{zt} / 跌停{dtn}" if zt is not None and dtn is not None else None, zt is not None and dtn is not None and zt >= 60 and dtn <= 20),
           c("晋级率>30%", f"{promo}%" if promo is not None else None, promo is not None and promo > 30),
           c("成交放量（较前日同时刻）", f"{tc.get('chg_pct'):+.1f}%" if tc.get("chg_pct") is not None else None, vol_up)]
    main = [c("涨停≥80", zt, zt is not None and zt >= 80), c("最高板≥7", max_lb, max_lb is not None and max_lb >= 7),
            {"label": "主线清晰且持续3天以上", "value": "需人工判断", "met": None}]
    def allm(L, ks=None): L2 = [x for x in L if x["value"] != "需人工判断"]; return all(x["met"] for x in L2) if all(x["met"] is not None for x in L2) else None
    ice_n = sum(1 for x in ice if x["met"]); ice_known = sum(1 for x in ice if x["met"] is not None)
    if zt is None or dtn is None: tier, why = "未取到", "涨停/跌停池未取到，无法判定"
    elif allm(main): tier, why = "主升", "主升硬性条件全部满足（主线持续性需人工确认）"
    elif allm(rep[:2]): tier, why = "修复", "涨停≥60且跌停≤20、晋级率>30% 均满足" + ("" if rep[2]["met"] else "（成交未放量，偏弱修复）")
    elif ice[0]["met"] or ice_n >= 2: tier, why = "冰点/退潮", f"冰点/退潮条件满足 {ice_n}/{len(ice)} 项" + ("（含跌停条件）" if ice[0]["met"] else "")
    else: tier, why = "过渡", "未完整满足任一档全部条件，按偏谨慎处理"
    POS_MAP = {"冰点/退潮": {"total": "0–20%", "single": "≤10%", "max_hold": 2, "text": "总仓位 0–20%，单票≤10%，同时最多持有2只"},
               "修复": {"total": "30–50%", "single": "≤20%", "max_hold": None, "text": "总仓位 30–50%，单票≤20%"},
               "主升": {"total": "50–80%", "single": "≤30%", "max_hold": None, "text": "总仓位 50–80%，单票≤30%"},
               "过渡": {"total": "偏谨慎（参考≤20%）", "single": "≤10%", "max_hold": 2, "text": "过渡档：偏谨慎，参考退潮仓位 0–20%/单票≤10%"},
               "未取到": {"total": "未取到", "single": "未取到", "max_hold": None, "text": "情绪未取到，仓位上限无法判定"}}
    pos = POS_MAP.get(tier, POS_MAP["未取到"])
    stage_mode = {"冰点/退潮": "退潮期模式", "修复": "修复期模式", "主升": "主升期模式", "过渡": "过渡期模式（偏谨慎）", "未取到": "未取到"}.get(tier, "未取到")
    # 买点启用状态
    if stage_mode == "退潮期模式":
        bp_active = ["首板次日弱转强（主攻）", "恐慌日尾盘低吸（辅助，≤10%）"]
        bp_paused = ["分歧转一致（退潮期暂停）", "趋势中军回踩（退潮期暂停）"]
    elif stage_mode in ("修复期模式", "主升期模式"):
        bp_active = ["分歧转一致", "首板次日弱转强", "趋势中军回踩", "恐慌日尾盘低吸"]
        bp_paused = []
    else:
        bp_active = ["首板次日弱转强"]
        bp_paused = ["分歧转一致（过渡期慎用）", "趋势中军回踩（过渡期慎用）"]

    must_empty = False; must_reasons = []; must_unchecked = []
    # 条件1：跌停连续两天 ≥50
    if dtn is not None and pp.get("dt_count") is not None and pp.get("for_day") == lu.get("date"):
        if dtn >= 50 and pp["dt_count"] >= 50:
            must_empty = True
            must_reasons.append(f"跌停连续两天≥50家（{pp['date']} {pp['dt_count']} 家，{lu.get('date')} {dtn} 家）")
    elif dtn is not None and dtn >= 50 and pp.get("dt_count") is None:
        must_unchecked.append(f"今日跌停{dtn}家≥50，但上一交易日跌停数未取到，无法确认「连续两天」")
    # 条件2：指数放量破位且主线龙头集体跌停 — 本工具无自动判定，标未取到
    must_unchecked.append("指数放量破位且主线龙头集体跌停：未取到（需人工判断）")

    warn = list(must_reasons)
    # 恐慌日提示（跌停集中爆发）
    panic_day = bool(dtn is not None and dtn >= 50)
    tips = []
    if panic_day and not must_empty:
        tips.append(f"跌停{dtn}家，接近/达到恐慌日特征；尾盘低吸仅作辅助且≤10%，须属新方向核心")
    if must_empty:
        tips.append("触发必须空仓：今日不做新建仓")

    ladder = {}
    for r in lu.get("zt", []) or []: ladder.setdefault(r["lb"], []).append({k: r[k] for k in ("code", "name", "industry", "first_seal", "zb_times", "seal_yi")})
    leg = br.get("legu") or {}
    breadth = {"up": br.get("up", leg.get("上涨")), "down": br.get("down", leg.get("下跌")), "flat": br.get("flat", leg.get("平盘")),
               "src": "东方财富" if "up" in br else ("乐咕乐股" if leg else None), "data_time": br.get("data_time") or leg.get("统计日期"),
               "legu_zt": leg.get("涨停"), "legu_dt": leg.get("跌停"), "activity": leg.get("活跃度")}

    # 一句话可执行摘要
    if tier == "未取到":
        one = "情绪未取到，暂不给出仓位结论"
    elif must_empty:
        one = f"{tier} · {stage_mode} · 必须空仓（{must_reasons[0] if must_reasons else '条件触发'}）"
    else:
        one = f"{tier} · {stage_mode} · 总仓{pos['total']} 单票{pos['single']}" + (f" · 最多{pos['max_hold']}只" if pos.get("max_hold") else "")
        if panic_day: one += " · 恐慌日特征（尾盘低吸辅助）"

    return {"date": lu.get("date"), "tier": tier, "why": why, "position": pos, "stage_mode": stage_mode,
            "must_empty": must_empty, "must_empty_reasons": must_reasons, "must_empty_unchecked": must_unchecked,
            "buy_points_active": bp_active, "buy_points_paused": bp_paused, "panic_day": panic_day, "tips": tips,
            "one_line": one, "warnings": warn,
            "criteria": {"冰点/退潮": ice, "修复": rep, "主升": main},
            "stats": {"zt": zt, "dt": dtn, "zb": zbn, "zb_rate": zb_rate, "max_lb": max_lb, "promo_rate": promo,
                      "lb_promo_rate": prev.get("lb_promo_rate"), "lb_n": prev.get("lb_n"), "lb_still": prev.get("lb_still"),
                      "prev_n": prev.get("n"), "prev_still": prev.get("still_zt"), "prev_avg_pct": avg, "prev_up": prev.get("up"), "prev_down": prev.get("down"),
                      "prev_lb_avg_pct": prev.get("lb_avg_pct")},
            "breadth": breadth, "ladder": [{"lb": k, "stocks": ladder[k]} for k in sorted(ladder, reverse=True)]}


# ---------------- charts (腾讯 ifzq：分时 / 日K) ----------------
_CHART_CACHE = {}  # key -> (expires_at, payload)
_CHART_LOCK = threading.Lock()

def _chart_cache_get(key):
    with _CHART_LOCK:
        hit = _CHART_CACHE.get(key)
        if not hit: return None
        exp, payload = hit
        if time.time() >= exp:
            _CHART_CACHE.pop(key, None); return None
        return payload

def _chart_cache_put(key, payload, ttl):
    with _CHART_LOCK:
        _CHART_CACHE[key] = (time.time() + ttl, payload)
    return payload

def _num(x):
    try:
        if x in ("-", "", None): return None
        return float(x)
    except Exception:
        return None

def fetch_minute_chart(code):
    """腾讯 day/query 分时。bar: HHMM price cumvol cumamt。"""
    sym = Q.tx_sym(code)
    r = Q.S.get("https://web.ifzq.gtimg.cn/appstock/app/day/query", params={"code": sym}, timeout=10)
    r.raise_for_status()
    j = r.json()
    block = ((j.get("data") or {}).get(sym) or {})
    days = block.get("data") or []
    if not days:
        return {"code": code, "kind": "minute", "day": None, "prec": None, "bars": [],
                "source": "腾讯 ifzq day/query", "error": "无分时数据"}
    # 优先当日（交易日），否则取有 bar 的最近交易日（腾讯返回多为新→旧）
    today = now().strftime("%Y%m%d")
    day = None
    for d in days:
        if d.get("date") == today and d.get("data"):
            day = d; break
    if day is None:
        cands = [d for d in days if d.get("data")]
        if cands:
            day = max(cands, key=lambda d: str(d.get("date") or ""))
    if day is None:
        return {"code": code, "kind": "minute", "day": None, "prec": None, "bars": [],
                "source": "腾讯 ifzq day/query", "error": "无分时数据"}
    prec = _num(day.get("prec"))
    bars = []
    prev_vol = 0.0
    for raw in day["data"]:
        parts = raw.split()
        if len(parts) < 2: continue
        t = parts[0]
        price = _num(parts[1])
        if price is None: continue
        cumvol = _num(parts[2]) if len(parts) > 2 else None
        vol = None
        if cumvol is not None:
            vol = max(0.0, cumvol - prev_vol)
            prev_vol = cumvol
        bars.append({"t": t, "p": price, "v": vol})
    return {"code": code, "kind": "minute", "day": day.get("date"), "prec": prec, "bars": bars,
            "source": "腾讯 ifzq day/query（web.ifzq.gtimg.cn）", "error": None if bars else "分时为空"}

def fetch_day_chart(code, n=60):
    """腾讯 fqkline 前复权日K。字段顺序: date, open, close, high, low, volume。"""
    n = max(5, min(int(n or 60), 250))
    sym = Q.tx_sym(code)
    param = f"{sym},day,,,{n},qfq"
    urls = [
        ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", {"param": param}),
        ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get", {"param": param}),
    ]
    last_err = None
    j = None; src = None
    for url, params in urls:
        try:
            r = Q.S.get(url, params=params, timeout=12)
            r.raise_for_status()
            j = r.json()
            if (j.get("data") or {}).get(sym):
                src = url.split("/")[2] + " fqkline qfqday"
                break
            last_err = f"{url} empty"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            j = None
    if not j or not (j.get("data") or {}).get(sym):
        return {"code": code, "kind": "day", "bars": [], "adjust": "qfq", "n": n,
                "source": "腾讯 fqkline", "error": f"日K未取到（{last_err}）"}
    block = j["data"][sym]
    raw = block.get("qfqday") or block.get("day") or []
    bars = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 5: continue
        d, o, c, h, l = row[0], _num(row[1]), _num(row[2]), _num(row[3]), _num(row[4])
        v = _num(row[5]) if len(row) > 5 else None
        if None in (o, c, h, l): continue
        bars.append({"d": str(d)[:10], "o": o, "h": h, "l": l, "c": c, "v": v})
    return {"code": code, "kind": "day", "bars": bars, "adjust": "qfq", "n": n,
            "source": f"腾讯 {src}（前复权）", "error": None if bars else "日K为空"}

# ---------------- poller ----------------
JOBS = {  # name: (fn, live_interval_s, idle_interval_s)
    "spot": (job_spot, 10, 300), "index": (job_index, 15, 600), "limitup": (job_limitup, 60, 1800),
    "breadth": (job_breadth, 60, 1800), "minutes": (job_minutes, 30, 900), "prevpools": (job_prevpools, 300, 1800),
    "auction": (job_auction, 15, 900),
}
NEXT = {k: 0 for k in JOBS}; RUNNING = set(); LASTRUN = {}
POOLX = ThreadPoolExecutor(4)
def interval(name):
    fn, li, idle = JOBS[name]; ph = phase()
    if name == "auction":
        if ph == "auction_done": return 15
        if ph in ("am",): return 60
        return idle
    if ph in ("auction_call", "auction_done", "am", "pm"): return li
    if ph == "post": return max(li, 60)
    return idle
def run_job(name):
    t0 = time.time()
    try: JOBS[name][0]()
    except Exception as e:
        put(name, error=f"{type(e).__name__}: {e}"); log("job", name, "FAIL", repr(e)[:200])
    finally:
        LASTRUN[name] = {"at": nows(), "secs": round(time.time() - t0, 1)}
        with LOCK: RUNNING.discard(name)
def poller():
    load_cal()
    # order: prevpools first (auction depends on it)
    while True:
        try:
            if now().minute == 0 and now().second < 2: load_cal()
            for name in JOBS:
                if time.time() >= NEXT[name] and name not in RUNNING:
                    if name == "auction" and not (get("prevpools").get("data")): NEXT[name] = time.time() + 5; continue
                    with LOCK: RUNNING.add(name)
                    NEXT[name] = time.time() + interval(name)
                    POOLX.submit(run_job, name)
        except Exception: traceback.print_exc()
        time.sleep(1)

# ---------------- web ----------------
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
PUBLIC = {"/healthz", "/favicon.ico"}
@app.middleware("http")
async def auth(request: Request, call_next):
    p = request.url.path
    if p in PUBLIC: return await call_next(request)
    t = request.query_params.get("t") or request.headers.get("x-token") or request.cookies.get("asw_t") or ""
    if not hmac.compare_digest(t.encode(), TOKEN.encode()):
        return Response("<h3 style='font-family:sans-serif'>需要访问令牌（请使用带 ?t= 的完整链接打开）</h3>", status_code=401, media_type="text/html; charset=utf-8")
    resp = await call_next(request)
    if request.query_params.get("t"):
        resp.set_cookie("asw_t", TOKEN, max_age=3600 * 24 * 365, httponly=True, secure=True, samesite="lax")
    resp.headers["Cache-Control"] = "no-store"; resp.headers["X-Robots-Tag"] = "noindex"
    return resp

@app.get("/healthz")
def healthz(): return {"ok": True, "at": nows()}
@app.get("/", response_class=HTMLResponse)
def index(): return HTMLResponse(open(os.path.join(STATIC, "index.html"), encoding="utf-8").read())
@app.get("/sw.js")
def sw(): return FileResponse(os.path.join(STATIC, "sw.js"), media_type="application/javascript")
@app.get("/icon.png")
def icon(): return FileResponse(os.path.join(STATIC, "icon.png"), media_type="image/png")
@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({"name": "A股盯盘", "short_name": "A股盯盘", "start_url": f"/?t={TOKEN}", "scope": "/", "display": "standalone",
                         "background_color": "#0b0f14", "theme_color": "#0b0f14",
                         "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}]}, media_type="application/manifest+json")

def meta(key):
    s = get(key)
    return {"ok": s.get("ok", False), "fetched_at": s.get("fetched_at"), "source": s.get("source"), "error": s.get("error"), "error_at": s.get("error_at")}
@app.get("/api/status")
def api_status():
    return {"now": nows(), "phase": phase(), "phase_cn": PHASE_CN[phase()], "trade_day": is_trade_day(), "ref_day": ref_day(),
            "jobs": {k: {**meta(k), "last_run": LASTRUN.get(k), "interval_s": interval(k)} for k in JOBS}}
@app.get("/api/diag")
def api_diag(): return {"now": nows(), "probe": P.probe()}
@app.get("/api/watch")
def api_watch():
    s = get("spot"); rows = (s.get("data") or {}).get("rows", {}); wl = read_watch()
    items = [{**w, "q": rows.get(w["code"])} for w in wl]
    rd = ref_day(); ev = sorted((EVENTS.get(rd) or {}).values(), key=lambda e: (e["time"], e["at"]), reverse=True)
    return {"items": items, "spot": meta("spot"), "events": ev, "events_day": rd, "minutes": {**meta("minutes"), **(get("minutes").get("data") or {})},
            "phase_cn": PHASE_CN[phase()], "now": nows()}
@app.post("/api/watchlist")
async def add_watch(req: Request):
    b = await req.json(); c = str(b.get("code", "")).strip()
    if not valid_code(c): raise HTTPException(400, "代码须为6位数字")
    rows = read_watch()
    if any(r["code"] == c for r in rows): return {"ok": True, "exists": True}
    try:
        q = Q.tx_quotes([Q.tx_sym(c)]).get(Q.tx_sym(c))
    except Exception: q = None
    if not q: raise HTTPException(404, f"未查到 {c} 的行情，请核对代码")
    rows.append({"code": c, "name": q["name"], "board": board_of(c, q["name"])[0], "note": f"盯盘App添加 {now():%m/%d}"})
    write_watch(rows); NEXT["spot"] = NEXT["minutes"] = 0
    return {"ok": True, "name": q["name"]}
@app.delete("/api/watchlist/{code}")
def del_watch(code: str):
    rows = read_watch(); n = [r for r in rows if r["code"] != code]
    if len(n) == len(rows): raise HTTPException(404, "不在自选中")
    write_watch(n); return {"ok": True}

@app.get("/api/alerts")
def api_alerts(since: str = ""):
    rules = jload(RULES_F, []); rows = (get("spot").get("data") or {}).get("rows", {})
    for r in rules:
        q = rows.get(r["code"]); r["now_met"] = rule_met(r, q) if q else None
        r["q"] = {k: q.get(k) for k in ("name", "price", "pct", "time")} if q else None
    log_ = jload(EVENTS_F, [])
    return {"rules": rules, "log": log_[:100], "spot": meta("spot"), "now": nows(), "live": live()}
@app.get("/api/poll")
def api_poll():
    """Lightweight: latest alert + 异动 ids for client notifications."""
    rd = ref_day(); ev = sorted((EVENTS.get(rd) or {}).values(), key=lambda e: e["at"], reverse=True)[:30]
    return {"now": nows(), "phase_cn": PHASE_CN[phase()], "live": live(), "alerts": jload(EVENTS_F, [])[:20], "events": ev}
@app.post("/api/rules")
async def add_rule(req: Request):
    b = await req.json(); c = str(b.get("code", "")).strip(); t = b.get("type")
    if not valid_code(c): raise HTTPException(400, "代码须为6位数字")
    if t not in RULE_T: raise HTTPException(400, "规则类型错误")
    try: v = float(b.get("value"))
    except Exception: raise HTTPException(400, "数值错误")
    try: q = Q.tx_quotes([Q.tx_sym(c)]).get(Q.tx_sym(c))
    except Exception: q = None
    if not q: raise HTTPException(404, f"未查到 {c} 的行情")
    with LOCK:
        rules = jload(RULES_F, [])
        ru = {"id": uuid.uuid4().hex[:8], "code": c, "name": q["name"], "type": t, "value": v, "repeat": bool(b.get("repeat")),
              "enabled": True, "created": nows(), "last_triggered": None}
        rules.append(ru); jsave(RULES_F, rules)
    NEXT["spot"] = 0
    return {"ok": True, "rule": ru}
@app.patch("/api/rules/{rid}")
async def patch_rule(rid: str, req: Request):
    b = await req.json()
    with LOCK:
        rules = jload(RULES_F, [])
        for r in rules:
            if r["id"] == rid:
                if "enabled" in b: r["enabled"] = bool(b["enabled"]); r["armed_off"] = False
                jsave(RULES_F, rules); return {"ok": True}
    raise HTTPException(404)
@app.delete("/api/rules/{rid}")
def del_rule(rid: str):
    with LOCK:
        rules = jload(RULES_F, []); n = [r for r in rules if r["id"] != rid]; jsave(RULES_F, n)
    return {"ok": True}

@app.get("/api/limitup")
def api_limitup():
    d = get("limitup").get("data") or {}
    return {**meta("limitup"), "data": d, "minutes": None}
@app.get("/api/auction")
def api_auction():
    return {**meta("auction"), "data": get("auction").get("data"), "phase": phase(), "phase_cn": PHASE_CN[phase()], "now": nows()}
@app.get("/api/sentiment")
def api_sentiment():
    return {"sent": sentiment(), "index": {**meta("index"), "data": get("index").get("data")}, "limitup": meta("limitup"),
            "breadth": meta("breadth"), "prevpools": meta("prevpools"), "now": nows(), "phase_cn": PHASE_CN[phase()]}


@app.get("/api/chart/{code}")
def api_chart(code: str, kind: str = "minute", n: int = 60):
    """分时 / 日K（腾讯公开接口，短缓存）。kind=minute|day。"""
    code = (code or "").strip()
    if not valid_code(code):
        raise HTTPException(400, "代码须为6位数字")
    kind = (kind or "minute").strip().lower()
    if kind not in ("minute", "day", "min", "k", "dk"):
        raise HTTPException(400, "kind 仅支持 minute 或 day")
    if kind in ("min",): kind = "minute"
    if kind in ("k", "dk"): kind = "day"
    n = max(5, min(int(n or 60), 250))
    ttl = 25 if kind == "minute" else 480
    ck = f"{kind}:{code}:{n if kind=='day' else ''}"
    hit = _chart_cache_get(ck)
    if hit is not None:
        out = dict(hit); out["cached"] = True; return out
    try:
        if kind == "minute":
            payload = fetch_minute_chart(code)
        else:
            payload = fetch_day_chart(code, n)
    except Exception as e:
        payload = {"code": code, "kind": kind, "bars": [], "source": "腾讯 ifzq",
                   "error": f"{type(e).__name__}: {e}"[:200]}
        if kind == "day":
            payload["adjust"] = "qfq"; payload["n"] = n
        if kind == "minute":
            payload.update({"day": None, "prec": None})
    if not payload.get("error"):
        _chart_cache_put(ck, payload, ttl)
    out = dict(payload); out["cached"] = False; out["fetched_at"] = nows()
    return out

def seed_watch_from_env():
    """WATCHLIST_SEED="600000,000001" (optional): fill an empty watchlist on first boot, so personal picks need not live in the repo/image."""
    codes = [c.strip() for c in re.split(r"[,\s;]+", os.environ.get("WATCHLIST_SEED", "")) if valid_code(c.strip())]
    if not codes or read_watch(): return
    try: qs = Q.tx_quotes([Q.tx_sym(c) for c in codes])
    except Exception as e: qs = {}; log("seed quotes fail", e)
    rows = []
    for c in dict.fromkeys(codes):
        q = qs.get(Q.tx_sym(c)) or {}; name = q.get("name", "")
        rows.append({"code": c, "name": name, "board": board_of(c, name)[0], "note": "WATCHLIST_SEED"})
    write_watch(rows); log("watchlist seeded from env:", ",".join(codes))

@app.on_event("startup")
def _start():
    if not os.path.exists(RULES_F): jsave(RULES_F, [])
    try: seed_watch_from_env()
    except Exception as e: log("seed fail", e)
    threading.Thread(target=poller, daemon=True).start()
    log("started; token from", "env ACCESS_TOKEN" if os.environ.get("ACCESS_TOKEN") else TOKEN_F, "data", DATA)
