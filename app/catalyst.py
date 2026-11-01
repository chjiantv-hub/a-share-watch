#!/usr/bin/env python3
"""公告/催化自动抓取与关键词分类。

来源（按顺序尝试，任一成功即止）：
  公告：东方财富公告 np-anotice-stock.eastmoney.com → 巨潮资讯 cninfo hisAnnouncement → 新浪财经公告列表
  资讯：东方财富搜索 search-api-web.eastmoney.com（补充；失败不影响公告结论）
原则：不编造。抓取失败/超时一律「未核验（原因）」；负面命中不静默放行。
"""
from __future__ import annotations
import datetime as dt
import html
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as fwait
from typing import Any, Callable

import requests

BJ = dt.timezone(dt.timedelta(hours=8))
LOOKBACK_DAYS = 14            # 自然日，约 10 个交易日
HTTP_TIMEOUT = 6
OK_TTL_S = 3 * 3600           # 成功结果 3 小时内复用
FAIL_TTL_S = 10 * 60          # 失败 10 分钟后重试
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

# ---------------- 分类规则 ----------------
# 顺序：负面排除 → 风险（需人工核验）→ 正面催化 → 中性。仅依据标题关键词。
NEG_RULES = [
    ("监管问询/立案", r"立案|行政处罚|处罚决定|监管函|警示函|问询函|关注函|纪律处分|通报批评|公开谴责|责令改正"),
    ("ST/退市风险", r"退市风险警示|其他风险警示|\*ST|终止上市"),
    ("业绩预亏", r"预亏|首亏|续亏|由盈转亏|预计.{0,8}亏损|业绩预告.{0,12}(预减|下降|亏损)|业绩.{0,4}大幅下(降|滑)"),
    ("股东减持", r"减持"),
]
NEG_EXEMPT = {
    # 异常波动时公司向控股股东发出的问询函回复、重组/发行的「审核问询函」属常规流程，不视为监管问询
    "监管问询/立案": r"异常波动.{0,8}问询|审核问询|问询函.{0,6}回复.{0,10}异常波动",
    "股东减持": r"不减持|终止减持|不存在减持|减持计划.{0,10}(期限届满|实施完毕|结果|完成)|提前终止.{0,6}减持|增持",
    "ST/退市风险": r"撤销",
}
RISK_RULES = [
    ("限售股解禁（规模待核验）", r"解除限售|限售股.{0,10}上市流通|解禁"),
    ("交易风险提示", r"严重异常波动|风险提示"),
    ("重大事项终止", r"终止.{0,10}(重组|收购|并购|发行|合同|协议|投资)"),
    ("澄清/否认相关业务", r"澄清|未与.{0,20}(接触|合作)|不涉及|无相关业务|尚未开展"),
    ("诉讼/冻结", r"诉讼|仲裁|司法冻结|股份冻结|被冻结|强制平仓|违约"),
]
RISK_EXEMPT = {
    "限售股解禁（规模待核验）": r"激励",          # 股权激励解锁：通常规模较小，记为中性
}
POS_RULES = [
    ("业绩增长", r"预增|扭亏|预盈|净利润.{0,10}增长|大幅增长|同比增长|业绩.{0,6}增长|业绩快报.{0,12}增"),
    ("订单/重大合同", r"中标|重大合同|日常经营.{0,4}合同|(签订|签署).{0,16}(合同|协议)|订单|战略合作"),
    ("政策", r"政策|政府补助|获得.{0,6}补助|纳入.{0,10}目录|试点|国务院|发改委|工信部|产业规划"),
    ("产业事件", r"控制权.{0,4}变更|拟入主|详式权益变动|重大资产重组|资产注入|(?<!回购)收购|并购|获批|获得.{0,10}(批准|批复|注册证|许可)|注册证|临床试验|新产品|新药|投产|量产|扩产|扩建|技术突破|首发|发布会|成功研制"),
    ("增持/回购", r"增持|回购"),
]
POS_EXEMPT = {
    "增持/回购": r"注销|减持",
    "订单/重大合同": r"募集资金.{0,6}监管协议|三方监管|托管协议|保荐协议",
    "政策": r"会计政策|会计估计|分红政策|利润分配政策|股东回报",
}
# 中介机构文件（法律意见、核查意见、持续督导等）不作为正面催化
POS_GLOBAL_EXEMPT = r"法律意见|核查意见|持续督导|独立财务顾问|保荐机构|鉴证报告|审计报告"
SNIPPET_STRIP = re.compile(r"<[^>]+>")

def _clean_title(t: str | None) -> str:
    t = SNIPPET_STRIP.sub("", html.unescape(t or "")).strip()
    return re.sub(r"\s+", " ", t)

def _strip_name_prefix(t: str) -> str:
    return re.sub(r"^[^:：]{1,12}[:：]", "", t)

def classify_title(title: str, kind: str = "公告", name: str | None = None, code: str | None = None) -> dict:
    """返回 {"cls": 负面|风险|正面|中性, "label": 规则名或None}。
    资讯（新闻）的负面命中降为「风险」（需人工核验），正面命中须标题含本股名称/代码。"""
    t = _strip_name_prefix(_clean_title(title))
    def hit(rules, exempt):
        for label, pat in rules:
            if re.search(pat, t) and not (exempt.get(label) and re.search(exempt[label], t)):
                return label
        return None
    lab = hit(NEG_RULES, NEG_EXEMPT)
    if lab:
        return {"cls": "负面" if kind == "公告" else "风险", "label": lab}
    lab = hit(RISK_RULES, RISK_EXEMPT)
    if lab:
        return {"cls": "风险", "label": lab}
    lab = None if re.search(POS_GLOBAL_EXEMPT, t) else hit(POS_RULES, POS_EXEMPT)
    if lab:
        if kind != "公告":
            mentions = (name and name in t) or (code and code in t)
            if not mentions:
                return {"cls": "中性", "label": None}
        return {"cls": "正面", "label": lab}
    return {"cls": "中性", "label": None}

_ORDER = {"负面": 0, "风险": 1, "正面": 2, "中性": 3}

def summarize(code: str, name: str | None, items: list[dict], *, ann_ok: bool,
              errors: list[dict] | None = None, news_ok: bool | None = None,
              lookback_days: int = LOOKBACK_DAYS) -> dict:
    """由已抓取条目得出催化结论。items 需含 title/date/source/url/kind。"""
    errors = errors or []
    out_items = []
    for it in items:
        c = classify_title(it.get("title"), it.get("kind") or "公告", name, code)
        out_items.append({**it, "title": _clean_title(it.get("title")), **c})
    # 去重（同标题同日）
    seen, uniq = set(), []
    for it in sorted(out_items, key=lambda x: (x.get("date") or ""), reverse=True):
        k = (_strip_name_prefix(it["title"]), it.get("date"))
        if k in seen: continue
        seen.add(k); uniq.append(it)
    neg = [x for x in uniq if x["cls"] == "负面"]
    risk = [x for x in uniq if x["cls"] == "风险"]
    pos = [x for x in uniq if x["cls"] == "正面"]
    # uniq 已按日期倒序；稳定排序：负面→风险→正面→中性，同类公告优先
    top = sorted(uniq, key=lambda x: (_ORDER[x["cls"]], 0 if x.get("kind") == "公告" else 1))[:3]
    err_txt = "；".join(f"{e.get('src')} {e.get('err')}" for e in errors)[:200]
    if not ann_ok and not uniq:
        kind, flag = "unverified", f"未核验（公告源均失败：{err_txt or '无响应'}）"
        reason = flag[4:-1]
    elif neg:
        x = neg[0]
        kind, flag = "exclude", f"排除（{x['label']}：{_strip_name_prefix(x['title'])[:40]}）"
        reason = f"{x['date']} {x['source']}：{x['title']}"
    elif risk:
        labs = "、".join(dict.fromkeys(x["label"] for x in risk))
        kind, flag = "unverified", f"未核验（存在风险项需人工核验：{labs}）"
        reason = f"{risk[0]['date']} {risk[0]['source']}：{risk[0]['title']}"
    elif pos:
        labs = "、".join(dict.fromkeys(x["label"] for x in pos))
        only_news = all(x.get("kind") != "公告" for x in pos)
        kind, flag = "pass", f"已核验（自动：{labs}{'；仅资讯来源' if only_news else ''}）"
        reason = f"{pos[0]['date']} {pos[0]['source']}：{pos[0]['title']}"
    elif uniq:
        kind, flag = "unverified", f"未核验（近{lookback_days}日公告/资讯未见正面催化关键词）"
        reason = flag[4:-1]
    else:
        kind, flag = "unverified", f"未核验（近{lookback_days}日无公告/资讯）"
        reason = flag[4:-1]
    if not ann_ok and uniq and kind == "pass":
        # 公告源失败、仅资讯命中正面：不足以确认无负面公告
        kind, flag = "unverified", f"未核验（仅资讯命中正面，公告源失败：{err_txt[:60]}）"
    return {"code": code, "name": name, "flag": flag, "flag_kind": kind, "reason": reason,
            "status": "ok" if ann_ok else ("partial" if uniq else "fail"),
            "ann_ok": ann_ok, "news_ok": news_ok, "errors": errors,
            "top": [{k: x.get(k) for k in ("title", "date", "source", "url", "kind", "cls", "label")} for x in top],
            "counts": {"正面": len(pos), "负面": len(neg), "风险": len(risk), "total": len(uniq)},
            "items": [{k: x.get(k) for k in ("title", "date", "source", "url", "kind", "cls", "label")} for x in uniq[:30]]}

def stub(code: str, name: str | None, reason: str) -> dict:
    return {"code": code, "name": name, "flag": f"未核验（{reason}）", "flag_kind": "unverified",
            "reason": reason, "status": "pending", "top": [], "items": [], "counts": {}, "errors": []}

# ---------------- 解析（纯函数，便于离线测试） ----------------
def parse_em_ann(js: dict, code: str, since: str) -> list[dict]:
    out = []
    for x in ((js or {}).get("data") or {}).get("list") or []:
        d = (x.get("notice_date") or x.get("display_time") or "")[:10]
        if not d or d.replace("-", "") < since: continue
        art = x.get("art_code") or ""
        cols = "、".join(c.get("column_name") or "" for c in x.get("columns") or [])
        out.append({"title": x.get("title_ch") or x.get("title") or "", "date": d, "source": "东方财富公告",
                    "url": f"https://data.eastmoney.com/notices/detail/{code}/{art}.html" if art else None,
                    "kind": "公告", "column": cols})
    return out

def parse_cninfo(js: dict, since: str) -> list[dict]:
    out = []
    for x in (js or {}).get("announcements") or []:
        ms = x.get("announcementTime")
        try: d = dt.datetime.fromtimestamp(int(ms) / 1000, BJ).strftime("%Y-%m-%d")
        except Exception: continue
        if d.replace("-", "") < since: continue
        adj = x.get("adjunctUrl")
        out.append({"title": _clean_title(x.get("announcementTitle")), "date": d, "source": "巨潮资讯",
                    "url": f"https://static.cninfo.com.cn/{adj}" if adj else None, "kind": "公告"})
    return out

_SINA_RE = re.compile(r"(\d{4}-\d{2}-\d{2})&nbsp;<a[^>]*href=['\"]([^'\"]+)['\"][^>]*>([^<]+)</a>")
def parse_sina(text: str, since: str) -> list[dict]:
    out = []
    for d, href, t in _SINA_RE.findall(text or ""):
        if d.replace("-", "") < since: continue
        url = href if href.startswith("http") else "https://vip.stock.finance.sina.com.cn" + href
        out.append({"title": _clean_title(t), "date": d, "source": "新浪财经公告", "url": url, "kind": "公告"})
    return out

def parse_em_news(text: str, since: str) -> list[dict]:
    m = re.search(r"^[^(]*\((.*)\)\s*;?\s*$", (text or "").strip(), re.S)
    js = json.loads(m.group(1) if m else text)
    out = []
    for x in ((js.get("result") or {}).get("cmsArticleWebOld") or []):
        d = (x.get("date") or "")[:10]
        if not d or d.replace("-", "") < since: continue
        out.append({"title": _clean_title(x.get("title")), "date": d,
                    "source": "东方财富资讯" + (f"·{x['mediaName']}" if x.get("mediaName") else ""),
                    "url": x.get("url"), "kind": "资讯"})
    return out

# ---------------- 网络抓取 ----------------
def _req(sess, method, url, **kw):
    last = None
    for i in range(2):           # 1 次重试
        try:
            r = sess.request(method, url, timeout=kw.pop("timeout", HTTP_TIMEOUT) if i == 0 else HTTP_TIMEOUT, **kw)
            if r.status_code != 200: raise RuntimeError(f"HTTP {r.status_code}")
            return r
        except Exception as e:
            last = e
            if i == 0: time.sleep(0.6)
    raise last

def _err(e: Exception) -> str:
    if isinstance(e, requests.Timeout): return "超时"
    if isinstance(e, requests.ConnectionError): return "连接失败"
    return f"{type(e).__name__}: {e}"[:80]

def fetch_em_ann(sess, code, since):
    r = _req(sess, "GET", "https://np-anotice-stock.eastmoney.com/api/security/ann",
             params={"sr": -1, "page_size": 30, "page_index": 1, "ann_type": "A", "client_source": "web",
                     "stock_list": code, "f_node": 0, "s_node": 0})
    js = r.json()
    if not isinstance(js, dict) or "data" not in js: raise RuntimeError("返回格式异常")
    return parse_em_ann(js, code, since)

_ORG: dict[str, str] = {}
def _cninfo_org(sess, code):
    col = "sse" if code.startswith("6") else "szse"
    if code in _ORG: return _ORG[code], col
    r = _req(sess, "POST", "https://www.cninfo.com.cn/new/information/topSearch/query", data={"keyWord": code, "maxNum": 5})
    for x in r.json() or []:
        if x.get("code") == code and x.get("orgId"):
            _ORG[code] = x["orgId"]; return x["orgId"], col
    raise RuntimeError("未找到 orgId")

def fetch_cninfo(sess, code, since):
    org, col = _cninfo_org(sess, code)
    s = f"{since[:4]}-{since[4:6]}-{since[6:]}"
    e = dt.datetime.now(BJ).strftime("%Y-%m-%d")
    r = _req(sess, "POST", "https://www.cninfo.com.cn/new/hisAnnouncement/query",
             headers={"X-Requested-With": "XMLHttpRequest"},
             data={"stock": f"{code},{org}", "tabName": "fulltext", "pageSize": 30, "pageNum": 1, "column": col,
                   "category": "", "plate": "", "seDate": f"{s}~{e}", "searchkey": "", "secid": "",
                   "sortName": "", "sortType": "", "isHLtitle": "true"})
    return parse_cninfo(r.json(), since)

def fetch_sina(sess, code, since):
    r = _req(sess, "GET", f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllBulletin/stockid/{code}.phtml")
    text = r.content.decode("gbk", errors="ignore")
    if "datelist" not in text: raise RuntimeError("页面结构异常")
    return parse_sina(text, since)

def fetch_em_news(sess, code, since):
    param = {"uid": "", "keyword": code, "type": ["cmsArticleWebOld"], "client": "web", "clientType": "web",
             "clientVersion": "curr", "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                                                      "pageIndex": 1, "pageSize": 15, "preTag": "", "postTag": ""}}}
    r = _req(sess, "GET", "https://search-api-web.eastmoney.com/search/jsonp",
             params={"cb": "jQuery_cb", "param": json.dumps(param, ensure_ascii=False)})
    return parse_em_news(r.text, since)

ANN_SOURCES: list[tuple[str, Callable]] = [("东方财富公告", fetch_em_ann), ("巨潮资讯", fetch_cninfo), ("新浪财经公告", fetch_sina)]
NEWS_SOURCES: list[tuple[str, Callable]] = [("东方财富资讯", fetch_em_news)]

def since_of(day: str, lookback_days: int = LOOKBACK_DAYS) -> str:
    return (dt.datetime.strptime(day, "%Y%m%d") - dt.timedelta(days=lookback_days)).strftime("%Y%m%d")

def fetch_one(code: str, name: str | None, day: str, *, sess=None, ann_sources=None, news_sources=None,
              health: dict | None = None, lookback_days: int = LOOKBACK_DAYS) -> dict:
    sess = sess or _session()
    since = since_of(day, lookback_days)
    items, errors, ann_ok, news_ok, used = [], [], False, None, None
    def note(src, ok, err=None):
        if health is not None:
            h = health.setdefault(src, {"ok": 0, "fail": 0, "last_err": None, "last_ok_at": None})
            if ok: h["ok"] += 1; h["last_ok_at"] = dt.datetime.now(BJ).strftime("%F %T")
            else: h["fail"] += 1; h["last_err"] = err
    for src, fn in (ann_sources if ann_sources is not None else ANN_SOURCES):
        try:
            got = fn(sess, code, since); items += got; ann_ok = True; note(src, True); used = src
            break
        except Exception as e:
            errors.append({"src": src, "err": _err(e)}); note(src, False, _err(e))
    for src, fn in (news_sources if news_sources is not None else NEWS_SOURCES):
        try:
            got = fn(sess, code, since); news_ok = True; note(src, True)
            # 仅保留标题提及本股（名称/代码）的资讯，避免大盘综述干扰
            items += [x for x in got if (code in x["title"]) or (name and name in x["title"])]
        except Exception as e:
            news_ok = False; errors.append({"src": src, "err": _err(e)}); note(src, False, _err(e))
    res = summarize(code, name, items, ann_ok=ann_ok, errors=errors, news_ok=news_ok, lookback_days=lookback_days)
    res["ann_source"] = used if ann_ok else None
    res["window"] = f"{since}~{dt.datetime.now(BJ).strftime('%Y%m%d')}"
    return res

def _session():
    s = requests.Session(); s.headers.update(UA); return s

# ---------------- 后台服务：缓存 + 有界并发 ----------------
class CatalystService:
    def __init__(self, cache_dir: str, workers: int = 4, on_update: Callable[[], None] | None = None,
                 fetcher: Callable[..., dict] | None = None):
        self.dir = cache_dir; os.makedirs(cache_dir, exist_ok=True)
        self.ex = ThreadPoolExecutor(workers, thread_name_prefix="catalyst")
        self.mem: dict[tuple[str, str], dict] = {}
        self.inflight: dict[tuple[str, str], Any] = {}
        self.lock = threading.Lock()
        self.on_update = on_update
        self.fetcher = fetcher or fetch_one
        self.health: dict[str, dict] = {}

    def _path(self, day, code): return os.path.join(self.dir, day, f"{code}.json")

    def peek(self, day: str, code: str) -> dict | None:
        k = (day, code)
        with self.lock:
            if k in self.mem: return self.mem[k]
        try:
            d = json.load(open(self._path(day, code), encoding="utf-8"))
            with self.lock: self.mem[k] = d
            return d
        except Exception:
            return None

    def _stale(self, d: dict | None) -> bool:
        if not d: return True
        age = time.time() - (d.get("_ts") or 0)
        return age > (OK_TTL_S if d.get("status") == "ok" else FAIL_TTL_S)

    def _run(self, day, code, name):
        try:
            res = self.fetcher(code, name, day, health=self.health)
        except Exception as e:
            res = summarize(code, name, [], ann_ok=False, errors=[{"src": "内部", "err": _err(e)}])
        res["_ts"] = time.time(); res["fetched_at"] = dt.datetime.now(BJ).strftime("%F %T"); res["day"] = day
        try:
            os.makedirs(os.path.join(self.dir, day), exist_ok=True)
            p = self._path(day, code); tmp = p + ".tmp"
            json.dump(res, open(tmp, "w", encoding="utf-8"), ensure_ascii=False); os.replace(tmp, p)
        except Exception:
            pass
        with self.lock:
            self.mem[(day, code)] = res; self.inflight.pop((day, code), None)
        if self.on_update:
            try: self.on_update()
            except Exception: pass
        return res

    def ensure(self, day: str, pairs: list[tuple[str, str | None]], wait_s: float = 0.0) -> dict[str, dict]:
        """为缺失/过期的代码提交后台抓取；最多等待 wait_s 秒。返回 code->结果（未完成为「抓取中/超时」占位）。"""
        futs = []
        for code, name in pairs:
            cur = self.peek(day, code)
            if not self._stale(cur): continue
            with self.lock:
                if (day, code) in self.inflight: futs.append(self.inflight[(day, code)]); continue
                f = self.ex.submit(self._run, day, code, name); self.inflight[(day, code)] = f; futs.append(f)
        if futs and wait_s > 0:
            fwait(futs, timeout=wait_s)
        out = {}
        for code, name in pairs:
            cur = self.peek(day, code)
            if cur: out[code] = cur
            else:
                out[code] = stub(code, name, "抓取中" if wait_s <= 0 else "抓取超时，后台继续")
        return out

    def status(self) -> dict:
        with self.lock:
            return {"inflight": len(self.inflight), "cached": len(self.mem), "sources": dict(self.health)}

def public_view(res: dict | None) -> dict | None:
    """接口输出用：去掉内部字段，保留 ≤3 条摘要。"""
    if not res: return None
    return {k: res.get(k) for k in ("flag", "flag_kind", "reason", "status", "top", "counts", "fetched_at",
                                     "ann_source", "window", "errors")}
