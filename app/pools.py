"""涨停/跌停/炸板/昨日涨停 pools with retry + fallback.

Primary : 东方财富 push2ex (same endpoints akshare stock_zt_pool_*_em uses), longer timeout + retries + backoff.
Fallback: 同花顺 data.10jqka.com.cn/dataapi/limit_up/* (涨停池 / 跌停池 lower_limit_pool / 炸板池 open_limit_pool /
          连板天梯 continuous_limit_up), used only when Eastmoney fails. Every row/pool carries its real source label;
          fields a source does not provide are None (never guessed).
"""
import os, time, threading, datetime as dt
import requests

BJ = dt.timezone(dt.timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
S = requests.Session(); S.headers.update(UA)

EM_HOSTS = [h.strip() for h in os.environ.get("EM_POOL_HOSTS", "https://push2ex.eastmoney.com").split(",") if h.strip()]
EM_TIMEOUT = (8, float(os.environ.get("EM_POOL_READ_TIMEOUT", "25")))   # (connect, read) seconds
EM_TRIES = int(os.environ.get("EM_POOL_TRIES", "3"))
EM_COOLDOWN = int(os.environ.get("EM_POOL_COOLDOWN", "600"))            # after a full failure, skip EM for this long
EM_EP = {"zt": ("getTopicZTPool", "fbt:asc", 10000), "dt": ("getTopicDTPool", "fund:asc", 10000),
         "zb": ("getTopicZBPool", "fbt:asc", 5000), "prev": ("getYesterdayZTPool", "zs:desc", 5000)}
EM_LABEL = "东方财富 push2ex"
THS_LABEL = "同花顺 data.10jqka.com.cn（备用）"
_em_down = {"until": 0.0, "why": None}
_lock = threading.Lock()

class PoolError(RuntimeError): pass

def _f(x, k=1.0):
    try: return None if x in (None, "", "-") else float(x) / k
    except Exception: return None
def _i(x):
    try: return None if x in (None, "", "-") else int(x)
    except Exception: return None
def _hms(x):  # EM 92500 -> "092500"
    return None if x in (None, "", "-") else str(int(x)).zfill(6)
def _ts_hms(x):  # THS unix seconds -> "HHMMSS" Beijing
    try: return dt.datetime.fromtimestamp(int(x), BJ).strftime("%H%M%S")
    except Exception: return None
def _r(x, n):
    return None if x is None else round(x, n)

# ---------------- Eastmoney ----------------
def em_cooling():
    return time.time() < _em_down["until"]
def em_raw(kind, date):
    if em_cooling():
        raise PoolError(f"东财 push2ex 冷却中（{int(_em_down['until'] - time.time())}s 后重试；上次：{_em_down['why']}）")
    ep, sort, size = EM_EP[kind]
    params = {"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt", "Pageindex": "0", "pagesize": str(size), "sort": sort, "date": date}
    last = None; banned = False
    for attempt in range(EM_TRIES):
        if banned: break
        if attempt: time.sleep(min(2 ** attempt, 8))          # backoff 2s, 4s
        for host in EM_HOSTS:
            try:
                r = S.get(f"{host}/{ep}", params=params, timeout=EM_TIMEOUT)
                if r.status_code != 200: last = f"{host} HTTP {r.status_code}"; continue
                j = r.json()
                d = j.get("data")
                if d is None: return [], None                     # EM: no pool for this date
                qd = str(d.get("qdate") or "")
                return d.get("pool") or [], qd
            except requests.exceptions.ConnectionError as e:
                last = f"{host} ConnectionError: {str(e)[:80]}"
                if "RemoteDisconnected" in repr(e) or "Connection aborted" in repr(e):
                    banned = True; break                           # push2 rate-limit: retrying only extends the ban
            except Exception as e:
                last = f"{host} {type(e).__name__}: {str(e)[:80]}"
    with _lock:
        _em_down.update(until=time.time() + EM_COOLDOWN, why=last)
    raise PoolError(f"东财 push2ex {kind} 失败（{EM_TRIES}次，读超时{EM_TIMEOUT[1]:.0f}s）：{last}")

def em_pool(kind, date):
    pool, qd = em_raw(kind, date)
    if qd and qd != date:
        raise PoolError(f"东财返回日期 {qd} ≠ 请求 {date}")
    out = []
    for x in pool:
        tj = x.get("zttj") or {}
        stat = f"{tj.get('days')}/{tj.get('ct')}" if tj else None
        if kind == "zt":
            out.append({"code": x["c"], "name": x["n"], "lb": _i(x.get("lbc")), "stat": stat, "first_seal": _hms(x.get("fbt")),
                        "last_seal": _hms(x.get("lbt")), "zb_times": _i(x.get("zbc")), "seal_yi": _r(_f(x.get("fund"), 1e8), 2),
                        "amount": _f(x.get("amount")), "amount_yi": _r(_f(x.get("amount"), 1e8), 2), "turnover": _r(_f(x.get("hs")), 1),
                        "mcap_yi": _r(_f(x.get("ltsz"), 1e8), 1), "price": _f(x.get("p"), 1000), "pct": _r(_f(x.get("zdp")), 2),
                        "industry": x.get("hybk")})
        elif kind == "zb":
            out.append({"code": x["c"], "name": x["n"], "price": _f(x.get("p"), 1000), "pct": _r(_f(x.get("zdp")), 2),
                        "limit": _f(x.get("ztp"), 1000), "first_seal": _hms(x.get("fbt")), "zb_times": _i(x.get("zbc")),
                        "amount": _f(x.get("amount")), "amount_yi": _r(_f(x.get("amount"), 1e8), 2),
                        "amplitude": _r(_f(x.get("zf")), 1), "industry": x.get("hybk")})
        elif kind == "dt":
            out.append({"code": x["c"], "name": x["n"], "price": _f(x.get("p"), 1000), "pct": _r(_f(x.get("zdp")), 2),
                        "cont": _i(x.get("days")), "open_times": _i(x.get("oc")), "seal_yi": _r(_f(x.get("fund"), 1e8), 3),
                        "amount": _f(x.get("amount")), "amount_yi": _r(_f(x.get("amount"), 1e8), 2), "industry": x.get("hybk")})
        elif kind == "prev":
            out.append({"code": x["c"], "name": x["n"], "price": _f(x.get("p"), 1000), "limit": _f(x.get("ztp"), 1000),
                        "pct": _f(x.get("zdp")), "ylb": _i(x.get("ylbc"))})
    return out

# ---------------- 同花顺 (fallback) ----------------
THS_BASE = "https://data.10jqka.com.cn/dataapi/limit_up/"
THS_FIELDS = "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004"
THS_EP = {"zt": "limit_up_pool", "dt": "lower_limit_pool", "zb": "open_limit_pool"}
THS_TIMEOUT = (8, 25)
def _ths_get(ep, params, tries=3):
    last = None
    for a in range(tries):
        if a: time.sleep(2 * a)
        try:
            r = S.get(THS_BASE + ep, params=params, timeout=THS_TIMEOUT, headers={"Referer": "https://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html"})
            j = r.json()
            if j.get("status_code") == 0: return j.get("data")
            last = f"status {j.get('status_code')} {j.get('status_msg')}"
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
    raise PoolError(f"同花顺 {ep} 失败：{last}")

def ths_raw(kind, date):
    rows, page = [], 1
    while True:
        d = _ths_get(THS_EP[kind], {"page": page, "limit": 200, "field": THS_FIELDS, "filter": "HS,GEM2STAR",
                                    "order_field": "330324", "order_type": "0", "date": date})
        if str(d.get("date") or date) != date: raise PoolError(f"同花顺返回日期 {d.get('date')} ≠ 请求 {date}")
        info = d.get("info") or []; rows += info
        total = int((d.get("page") or {}).get("total") or 0)
        if not info or len(rows) >= total or page >= 20: break
        page += 1
    if total and len(rows) < total: raise PoolError(f"同花顺 {kind} 只取到 {len(rows)}/{total}")
    return rows

def ths_lianban(date):
    """code -> 连板数 from 同花顺 连板天梯 (stocks absent = 首板 = 1)."""
    d = _ths_get("continuous_limit_up", {"filter": "HS,GEM2STAR", "date": date})
    return {c["code"]: int(c.get("continue_num") or lvl.get("height")) for lvl in (d or []) for c in lvl.get("code_list") or []}

def ths_pool(kind, date):
    raw = ths_raw(kind, date)
    out = []
    if kind == "zt":
        try: lbm, lb_err = ths_lianban(date), None
        except Exception as e: lbm, lb_err = None, str(e)
        for x in raw:
            hv = _i(x.get("high_days_value")); boards, days = (hv >> 16, hv & 0xFFFF) if hv else (None, None)
            lb = (lbm.get(x["code"], 1) if lbm is not None else (boards if boards and boards == days else None))
            out.append({"code": x["code"], "name": x.get("name"), "lb": lb, "stat": f"{days}/{boards}" if hv else None,
                        "first_seal": _ts_hms(x.get("first_limit_up_time")), "last_seal": _ts_hms(x.get("last_limit_up_time")),
                        "zb_times": _i(x.get("open_num")), "seal_yi": _r(_f(x.get("order_amount"), 1e8), 2),
                        "amount": None, "amount_yi": None, "turnover": _r(_f(x.get("turnover_rate")), 1),
                        "mcap_yi": _r(_f(x.get("currency_value"), 1e8), 1), "price": _f(x.get("latest")), "pct": _r(_f(x.get("change_rate")), 2),
                        "industry": None, "reason": x.get("reason_type"), "ths_tag": x.get("high_days")})
        return out, lb_err
    for x in raw:
        base = {"code": x["code"], "name": x.get("name"), "price": _f(x.get("latest")), "pct": _r(_f(x.get("change_rate")), 2),
                "amount": None, "amount_yi": None, "industry": None}
        if kind == "zb":
            out.append({**base, "limit": None, "first_seal": _ts_hms(x.get("first_limit_up_time")), "zb_times": _i(x.get("open_num")), "amplitude": None})
        else:  # dt
            oa = _f(x.get("order_amount"))
            out.append({**base, "cont": None, "open_times": _i(x.get("open_num")), "seal_yi": _r(oa / 1e8, 3) if oa else None})
    return out, None

def get_pool(kind, date):
    """-> (rows, source_label, notes). Raises PoolError with both errors if all sources fail."""
    try:
        return em_pool(kind, date), EM_LABEL, None
    except Exception as e:
        em_err = str(e)[:200]
    if kind == "prev":
        raise PoolError(em_err)          # caller derives 昨日涨停表现 from 同花顺 + 腾讯 itself
    try:
        rows, note = ths_pool(kind, date)
        return rows, THS_LABEL, "; ".join(x for x in (f"东财失败：{em_err}", note and f"连板天梯未取到：{note}") if x)
    except Exception as e2:
        raise PoolError(f"东财：{em_err} | 同花顺：{str(e2)[:200]}")

def probe():
    """Connectivity check for /api/diag."""
    out = {}
    for name, fn in (("eastmoney_push2ex", lambda: S.get(EM_HOSTS[0] + "/getTopicZTPool", params={"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt", "Pageindex": "0", "pagesize": "1", "sort": "fbt:asc", "date": dt.datetime.now(BJ).strftime("%Y%m%d")}, timeout=EM_TIMEOUT)),
                     ("ths_limit_up", lambda: S.get(THS_BASE + "limit_up_pool", params={"page": 1, "limit": 1, "field": THS_FIELDS, "filter": "HS,GEM2STAR", "order_field": "330324", "order_type": "0"}, timeout=THS_TIMEOUT)),
                     ("tencent_qt", lambda: S.get("https://qt.gtimg.cn/q=sh000001", timeout=10))):
        t0 = time.time()
        try: r = fn(); out[name] = {"ok": r.status_code == 200, "http": r.status_code, "secs": round(time.time() - t0, 2), "bytes": len(r.content)}
        except Exception as e: out[name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}", "secs": round(time.time() - t0, 2)}
    out["em_cooldown_s"] = max(0, int(_em_down["until"] - time.time())); out["em_last_fail"] = _em_down["why"]
    return out
