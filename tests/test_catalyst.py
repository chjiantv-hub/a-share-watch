"""公告/催化抓取与分类测试（离线 fixture，不访问网络）。"""
import os, json, time
import pytest
import catalyst as C
from pool_select import build_observe_pool

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
def fx(name, mode="r"):
    return open(os.path.join(FX, name), mode)

@pytest.mark.parametrize("title,cls,label", [
    ("关于签订重大合同的公告", "正面", "订单/重大合同"),
    ("关于中标某项目的公告", "正面", "订单/重大合同"),
    ("2026年前三季度业绩预告（预增）", "正面", "业绩增长"),
    ("关于以集中竞价方式回购公司股份的方案", "正面", "增持/回购"),
    ("关于控股股东拟变更暨控制权变更的提示性公告", "正面", "产业事件"),
    ("关于收到深圳证券交易所关注函的公告", "负面", "监管问询/立案"),
    ("关于公司被中国证监会立案调查的公告", "负面", "监管问询/立案"),
    ("2026年度业绩预告：预计净利润亏损", "负面", "业绩预亏"),
    ("关于持股5%以上股东减持股份预披露的公告", "负面", "股东减持"),
    ("关于公司股票被实施退市风险警示的公告", "负面", "ST/退市风险"),
    ("关于首次公开发行限售股上市流通的提示性公告", "风险", "限售股解禁（规模待核验）"),
    ("股票交易严重异常波动公告", "风险", "交易风险提示"),
    ("关于媒体报道的澄清公告", "风险", "澄清/否认相关业务"),
    ("关于控股股东减持计划实施完毕的公告", "中性", None),
    ("关于回购注销部分限制性股票的公告", "中性", None),
    ("关于会计政策变更的公告", "中性", None),
    ("关于2021年限制性股票激励计划第三个解除限售期解除限售股份上市流通的提示性公告", "中性", None),
    ("股票交易异常波动公告", "中性", None),
    ("关于签订募集资金监管协议的公告", "中性", None),
    ("公司控股股东关于股票交易异常波动问询函的回复", "中性", None),
    ("关于深圳证券交易所《审核问询函》回复的公告", "中性", None),
    ("关于对深圳证券交易所问询函回复的公告", "负面", "监管问询/立案"),
    ("东吴证券关于某公司详式权益变动报告书之持续督导意见", "中性", None),
    ("第十一届董事会三十次会议决议公告", "中性", None),
])
def test_classify_title(title, cls, label):
    r = C.classify_title(title)
    assert r["cls"] == cls and r["label"] == label, (title, r)

def test_news_negative_downgraded_and_positive_needs_mention():
    assert C.classify_title("某公司被立案调查", kind="资讯")["cls"] == "风险"
    assert C.classify_title("政策持续发力", kind="资讯", name="示例股份", code="000001")["cls"] == "中性"
    assert C.classify_title("示例股份获千万元订单", kind="资讯", name="示例股份", code="000001")["cls"] == "正面"

def test_parse_fixtures():
    em = C.parse_em_ann(json.load(fx("em_ann.json")), "000001", "20261016")
    assert [x["date"] for x in em] == ["2026-10-30", "2026-10-28"]           # 窗口外的被过滤
    assert em[0]["url"].endswith("/000001/AN202610291831169034.html")
    cn = C.parse_cninfo(json.load(fx("cninfo.json")), "20261016")
    assert cn[0]["title"] == "关于收到上海证券交易所问询函的公告"          # 去除 <em>
    assert cn[0]["url"].startswith("https://static.cninfo.com.cn/")
    sina = C.parse_sina(fx("sina.html", "rb").read().decode("gbk"), "20261016")
    assert len(sina) == 2 and sina[0]["url"].startswith("https://vip.stock.finance.sina.com.cn/")
    news = C.parse_em_news(fx("em_news.txt").read(), "20261016")
    assert news[0]["title"] == "示例股份获千万元订单" and news[0]["kind"] == "资讯"

def _src(name):
    def f(sess, code, since):
        if name == "em": return C.parse_em_ann(json.load(fx("em_ann.json")), code, since)
        if name == "cninfo": return C.parse_cninfo(json.load(fx("cninfo.json")), since)
        if name == "sina": return C.parse_sina(fx("sina.html", "rb").read().decode("gbk"), since)
        if name == "news": return C.parse_em_news(fx("em_news.txt").read(), since)
    return f
def _fail(msg):
    def f(sess, code, since): raise RuntimeError(msg)
    return f

def test_fetch_positive_pass():
    r = C.fetch_one("000001", "示例股份", "20261030", sess=object(),
                    ann_sources=[("东方财富公告", _src("em"))], news_sources=[("东方财富资讯", _src("news"))])
    assert r["flag_kind"] == "pass" and r["flag"].startswith("已核验（自动：订单/重大合同")
    assert r["ann_source"] == "东方财富公告"
    assert 1 <= len(r["top"]) <= 3 and r["top"][0]["cls"] == "正面"
    # 大盘综述（未提及本股）被过滤
    assert all("周末要闻" not in x["title"] for x in r["items"])

def test_fetch_negative_excludes_and_fallback_order():
    health = {}
    r = C.fetch_one("600001", "示例", "20261030", sess=object(), health=health,
                    ann_sources=[("东方财富公告", _fail("HTTP 500")), ("巨潮资讯", _src("cninfo"))], news_sources=[])
    assert r["flag_kind"] == "exclude" and r["flag"].startswith("排除（监管问询/立案")
    assert r["ann_source"] == "巨潮资讯"
    assert health["东方财富公告"]["fail"] == 1 and health["巨潮资讯"]["ok"] == 1

def test_fetch_risk_keeps_unverified():
    r = C.fetch_one("000002", "示例", "20261030", sess=object(), ann_sources=[("新浪财经公告", _src("sina"))], news_sources=[])
    # 同时有预增（正面）和解禁（风险）：风险优先，保持未核验
    assert r["flag_kind"] == "unverified" and "限售股解禁" in r["flag"]

def test_fetch_all_fail_unverified_with_reason():
    r = C.fetch_one("000003", "示例", "20261030", sess=object(),
                    ann_sources=[("东方财富公告", _fail("超时")), ("巨潮资讯", _fail("HTTP 500")), ("新浪财经公告", _fail("连接失败"))],
                    news_sources=[("东方财富资讯", _fail("超时"))])
    assert r["flag_kind"] == "unverified" and r["flag"].startswith("未核验（公告源均失败")
    assert "巨潮资讯" in r["flag"] and r["status"] == "fail" and not r["top"]

def test_news_only_positive_not_pass_when_ann_failed():
    r = C.fetch_one("000001", "示例股份", "20261030", sess=object(),
                    ann_sources=[("东方财富公告", _fail("超时"))], news_sources=[("东方财富资讯", _src("news"))])
    assert r["flag_kind"] == "unverified" and "公告源失败" in r["flag"]

def test_service_cache_pending_and_timeout(tmp_path):
    calls = []
    def slow(code, name, day, health=None):
        calls.append(code); time.sleep(0.5)
        return C.summarize(code, name, [{"title": "关于中标的公告", "date": "2026-10-30", "source": "x", "url": None, "kind": "公告"}], ann_ok=True)
    svc = C.CatalystService(str(tmp_path), workers=2, fetcher=slow)
    r = svc.ensure("20261030", [("000001", "a")], wait_s=0)
    assert r["000001"]["status"] == "pending" and "抓取中" in r["000001"]["flag"]
    r = svc.ensure("20261030", [("000001", "a")], wait_s=0.05)
    assert "抓取超时" in r["000001"]["flag"]
    r = svc.ensure("20261030", [("000001", "a")], wait_s=3)
    assert r["000001"]["flag_kind"] == "pass"
    assert os.path.exists(tmp_path / "20261030" / "000001.json")
    svc.ensure("20261030", [("000001", "a")], wait_s=1)
    assert calls == ["000001"]                       # 缓存命中，不重复抓取
    svc2 = C.CatalystService(str(tmp_path), workers=1, fetcher=slow)
    assert svc2.peek("20261030", "000001")["flag_kind"] == "pass"   # 磁盘缓存

def _zt(code, name, industry, amount_yi=20.0, first_seal="093000"):
    return {"code": code, "name": name, "industry": industry, "lb": 1, "amount": amount_yi * 1e8, "amount_yi": amount_yi,
            "first_seal": first_seal, "last_seal": "093500", "zb_times": 0, "seal_yi": 1.0}

def _cat(code, kind, flag):
    return {"code": code, "flag": flag, "flag_kind": kind, "reason": "2026-10-30 测试：标题", "status": "ok",
            "top": [{"title": "标题", "date": "2026-10-30", "source": "测试", "url": None, "kind": "公告", "cls": "负面", "label": "x"}]}

def test_observe_pool_uses_catalyst_and_excludes_negative():
    rows = [_zt(f"00500{i}", f"C{i}", "元件", first_seal=f"0930{i:02d}") for i in range(5)]
    cats = {"005000": _cat("005000", "exclude", "排除（监管问询/立案：关注函）"),
            "005001": _cat("005001", "pass", "已核验（自动：订单/重大合同）"),
            "005002": _cat("005002", "unverified", "未核验（抓取中）")}
    out = build_observe_pool(rows, stage_mode="主升期模式", tier="主升", sectors=None,
                             float_caps={r["code"]: 100 for r in rows}, watchlist=[], pool_day="20261030", catalysts=cats)
    codes = {r["code"] for r in out["observe"]}
    assert "005000" not in codes
    rej = next(r for r in out["rejected"] if r["code"] == "005000")
    assert rej["conclusion_for_observe"] == "剔除" and "公告/资讯负面" in rej["reasons"][0] and rej["catalyst"]
    by = {r["code"]: r for r in out["observe"]}
    assert by["005001"]["pass_flags"]["catalytic"] == "已核验（自动：订单/重大合同）"
    assert by["005001"]["catalyst"]["top"]
    assert by["005002"]["pass_flags"]["catalytic"] == "未核验（抓取中）"
    assert by["005003"]["pass_flags"]["catalytic"] == "未核验（尚未抓取）"
    assert ("005000", "C0") in out["candidates_ranked"]

def test_user_override_wins_over_auto_exclude():
    rows = [_zt(f"00600{i}", f"D{i}", "元件", first_seal=f"0930{i:02d}") for i in range(5)]
    cats = {"006000": _cat("006000", "exclude", "排除（股东减持：减持预披露）")}
    out = build_observe_pool(rows, stage_mode="主升期模式", tier="主升", sectors=None,
                             float_caps={r["code"]: 100 for r in rows}, watchlist=[], pool_day="20261030",
                             catalysts=cats, user_notes={"006000": {"catalytic_verified": True}})
    by = {r["code"]: r for r in out["observe"]}
    assert by["006000"]["pass_flags"]["catalytic"] == "已核验（用户）"
    assert any("以用户核验为准" in x for x in by["006000"]["reasons"])
