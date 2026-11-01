"""Grok 解读：未配置路径、上下文组装、已配置 mock（不访问网络）。"""
import os
import json
import threading
from unittest.mock import patch, MagicMock

import pytest
import grok_explain as G


def _now():
    return "2026-11-01 18:40:00"


def _get_factory(st):
    def get(k):
        return st.get(k) or {"ok": False, "data": None, "error": "尚未取到"}
    return get


def base_state():
    return {
        "spot": {"ok": True, "fetched_at": _now(), "source": "腾讯", "data": {
            "rows": {"002636": {"code": "002636", "name": "金安国纪", "price": 12.3, "pct": 5.2,
                     "open": 11.8, "high": 12.5, "low": 11.7, "amount_yi": 8.1, "time": "15:00:00"}}
        }},
        "observe": {"ok": True, "fetched_at": _now(), "data": {
            "day": "20261030", "mainlines": [{"industry": "印制电路板", "zt_n": 4, "kind": "候选新方向"}],
            "observe": [{"code": "002636", "name": "金安国纪", "industry": "印制电路板", "theme": "PCB",
                         "lb": 1, "amount_yi": 40.8, "conclusion_for_observe": "入观察池",
                         "planned_buy_point": "首板次日弱转强", "reasons": ["成交额≥5亿"],
                         "pass_flags": {"catalytic": "未核验", "catalytic_kind": "unverified"}}],
            "w2s_candidates": [], "rejected": [],
        }},
        "auction": {"ok": True, "data": {"summary": {"n": 1}, "rows": [
            {"code": "002636", "name": "金安国纪", "conclusion": "只看不追", "buy_point": "首板次日弱转强",
             "pct": 3.1, "reason": "竞价额未放大", "src": ["观察池"]}
        ]}},
        "open_confirm": {"ok": True, "data": {"summary": {"n": 1, "can_buy": 0}, "rows": [
            {"code": "002636", "name": "金安国纪", "conclusion": "继续观察", "reason": "冲高回落", "buy_point": "—"}
        ]}},
        "trade_pool": {"ok": True, "data": {"day": "20261030", "confirmed": [], "focus": [], "notes": {}}},
        "index": {"ok": True, "data": {"turnover_yi": 16000, "rows": []}},
        "limitup": {"ok": True, "data": {"date": "20261030", "zt": [], "dt": [], "zb": []}},
    }


def test_unconfigured_explain_no_network(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert G.configured() is False
    st = base_state()
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not call network")

    with patch.object(G.requests, "post", boom):
        r = G.explain(
            scope="stock", code="002636", get=_get_factory(st),
            sentiment=lambda: {"tier": "冰点/退潮", "stage_mode": "退潮期模式", "one_line": "x",
                               "position": {"total": "0–20%"}, "stats": {"zt": 10}},
            read_watch=lambda: [{"code": "002636", "name": "金安国纪"}],
            nows=_now, phase_cn="休市日", ref_day="20261030", last_close_day="20261030",
        )
    assert r["ok"] is False and "Grok 未配置" in r["error"]
    assert called["n"] == 0
    assert r["configured"] is False


def test_status_and_default_model(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_MODEL", raising=False)
    s = G.status()
    assert s["configured"] is False
    assert s["model"] == G.DEFAULT_MODEL == "grok-4.20-0309-non-reasoning"
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    monkeypatch.setenv("XAI_MODEL", "grok-4.3")
    assert G.configured() is True
    assert G.model_name() == "grok-4.3"


def test_build_context_stock_and_market():
    st = base_state()
    sent = {"tier": "冰点/退潮", "stage_mode": "退潮期模式", "one_line": "冰点 · 退潮期模式 · 总仓0–20%",
            "position": {"total": "0–20%", "single": "≤10%"}, "must_empty": False,
            "buy_points_active": ["首板次日弱转强"], "buy_points_paused": ["分歧转一致"],
            "stats": {"zt": 47, "dt": 82}, "breadth": {"up": 800, "down": 4500}}

    def peek(day, code):
        assert day == "20261030" and code == "002636"
        return {"flag": "未核验", "flag_kind": "unverified", "status": "ok",
                "top": [{"cls": "风险", "label": "澄清", "title": "澄清公告", "date": "2026-10-28", "source": "东财"}]}

    ctx, used = G.build_context(
        scope="stock", code="002636", get=_get_factory(st), sentiment=lambda: sent,
        read_watch=lambda: [{"code": "002636", "name": "金安国纪"}],
        cat_peek=peek, cat_day="20261030", nows=_now, phase_cn="休市日",
        ref_day="20261030", last_close_day="20261030",
    )
    assert ctx["code"] == "002636" and ctx["spot"]["price"] == 12.3
    assert ctx["observe"]["conclusion_for_observe"] == "入观察池"
    assert ctx["auction"]["conclusion"] == "只看不追"
    assert ctx["catalyst"]["top"][0]["title"] == "澄清公告"
    assert "sentiment" in used and "catalyst" in used and "spot" in used

    mctx, mused = G.build_context(
        scope="market", get=_get_factory(st), sentiment=lambda: sent,
        read_watch=lambda: [], nows=_now, phase_cn="休市日",
        ref_day="20261030", last_close_day="20261030",
    )
    assert mctx["scope"] == "market"
    assert mctx["observe_summary"]["observe_count"] == 1
    assert mctx["sent"]["tier"] == "冰点/退潮"
    assert "index" in mused and "limitup" in mused


def test_configured_path_mocked_requests(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    st = base_state()
    fake = MagicMock()
    fake.post.return_value.status_code = 200
    fake.post.return_value.json.return_value = {
        "id": "chatcmpl-test",
        "model": "grok-4.20-0309-non-reasoning",
        "choices": [{"message": {"content": "## 结论\n继续观察\n## 依据\nsent.tier=冰点/退潮\n## 风险与缺失数据\n无\n## 下一步观察点\n看竞价\n仅供研究参考，不构成投资建议"}}],
    }
    with patch.object(G, "requests", fake):
        r = G.explain(
            scope="stock", code="002636", get=_get_factory(st),
            sentiment=lambda: {"tier": "冰点/退潮", "stage_mode": "退潮期模式", "stats": {}},
            read_watch=lambda: [], nows=_now, phase_cn="休市日",
            ref_day="20261030", last_close_day="20261030",
            session=fake,
        )
    assert r["ok"] is True and "继续观察" in r["text"]
    assert r["model"] == "grok-4.20-0309-non-reasoning"
    assert "observe" in r["used_context_keys"]
    # Authorization header must carry key; URL is xAI
    args, kwargs = fake.post.call_args
    assert args[0] == G.XAI_URL
    assert kwargs["headers"]["Authorization"] == "Bearer xai-test-key"
    assert "xai-test-key" not in r["text"]


def test_inflight_lock_rejects_second(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    st = base_state()
    entered = threading.Event()
    release = threading.Event()

    class SlowSess:
        def post(self, *a, **k):
            entered.set()
            release.wait(2)
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {"model": "m", "choices": [{"message": {"content": "## 结论\n不可建仓\n仅供研究参考，不构成投资建议"}}]}
            return m

    results = [None, None]

    def run(i):
        results[i] = G.explain(
            scope="market", get=_get_factory(st),
            sentiment=lambda: {"tier": "过渡", "stats": {}},
            read_watch=lambda: [], nows=_now, phase_cn="休市日",
            session=SlowSess(),
        )

    t1 = threading.Thread(target=run, args=(0,))
    t1.start()
    assert entered.wait(1)
    run(1)  # second should be busy
    assert results[1]["ok"] is False and results[1].get("busy")
    release.set()
    t1.join(3)
    assert results[0]["ok"] is True


def test_api_routes_unconfigured(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    import app as A
    c = TestClient(A.app)
    tok = os.environ["ACCESS_TOKEN"]
    assert c.get("/api/grok/status").status_code == 401
    s = c.get("/api/grok/status", headers={"X-Token": tok}).json()
    assert s["configured"] is False and s["model"] == G.DEFAULT_MODEL
    r = c.post("/api/grok/explain", headers={"X-Token": tok}, json={"scope": "market"}).json()
    assert r["ok"] is False and "Grok 未配置" in r["error"]
    bad = c.post("/api/grok/explain", headers={"X-Token": tok}, json={"scope": "stock"}).json()
    assert bad["ok"] is False and "6 位" in bad["error"]


def test_api_routes_configured_mock(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    from fastapi.testclient import TestClient
    import app as A
    # seed a bit of cache so context is non-empty
    A.put("observe", {"day": "20261030", "observe": [], "mainlines": [], "w2s_candidates": [], "rejected": []}, source="test")
    fake = MagicMock()
    fake.post.return_value.status_code = 200
    fake.post.return_value.json.return_value = {
        "model": "grok-4.20-0309-non-reasoning",
        "choices": [{"message": {"content": "## 结论\n防守或空仓\n## 依据\n未取到完整盘面\n## 风险与缺失数据\n—\n## 下一步观察点\n—\n仅供研究参考，不构成投资建议"}}],
    }
    c = TestClient(A.app)
    tok = os.environ["ACCESS_TOKEN"]
    with patch.object(G.requests, "post", fake.post):
        r = c.post("/api/grok/explain", headers={"X-Token": tok}, json={"scope": "market"}).json()
    assert r["ok"] is True and "防守" in r["text"]
    st = c.get("/api/grok/status", headers={"X-Token": tok}).json()
    assert st["configured"] is True
