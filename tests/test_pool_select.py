"""选股入池纯逻辑测试（不访问网络）。"""
from pool_select import (
    identify_mainlines, build_observe_pool, planned_buy_point,
    open_confirm_from_minutes, is_lanban_or_tail, MIN_AMOUNT_YI,
)

def _zt(code, name, industry, lb=1, amount_yi=8.0, first_seal='093000', last_seal='093500',
        zb_times=0, seal_yi=1.0):
    return {"code": code, "name": name, "industry": industry, "lb": lb,
            "amount": amount_yi * 1e8, "amount_yi": amount_yi,
            "first_seal": first_seal, "last_seal": last_seal, "zb_times": zb_times, "seal_yi": seal_yi}

def test_st_excluded():
    rows = [_zt("000001", "ST示例", "白酒Ⅱ", amount_yi=20)] * 5
    # need 5 in industry - use unique codes
    rows = [_zt(f"00000{i}", "ST示例", "白酒Ⅱ", amount_yi=20) for i in range(1, 6)]
    out = build_observe_pool(rows, stage_mode="修复期模式", tier="修复", sectors=None,
                             float_caps={r["code"]: 100 for r in rows}, watchlist=[], pool_day="20261030")
    assert out["observe"] == []
    assert all("ST" in (x["reasons"][0] if x["reasons"] else "") or "ST" in "".join(x["reasons"]) for x in out["rejected"])

def test_amount_filter_hard():
    rows = [_zt(f"60000{i}", f"票{i}", "元件", amount_yi=3.0 if i < 4 else 12.0,
                first_seal=f"09300{i}") for i in range(5)]
    # 5 in 元件 so mainline; only last has amount>=5
    out = build_observe_pool(rows, stage_mode="修复期模式", tier="修复", sectors=None,
                             float_caps={r["code"]: 120 for r in rows}, watchlist=[], pool_day="20261030")
    codes = {r["code"] for r in out["observe"]}
    assert "600004" in codes
    assert "600000" not in codes
    assert any("成交额" in "".join(r["reasons"]) and "剔除" == r["conclusion_for_observe"] for r in out["rejected"])

def test_observe_ranking_mainline_leader_first():
    # 主线 industry A with 5 zt; leader earliest seal should rank high
    rows = []
    for i in range(5):
        rows.append(_zt(f"00100{i}", f"龙{i}", "休闲食品", lb=1 if i else 2,
                        amount_yi=10 + i, first_seal=f"0930{i:02d}", seal_yi=2 - i * 0.1))
    # noise industry
    rows.append(_zt("002000", "散", "光学光电", amount_yi=30, first_seal="093000"))
    out = build_observe_pool(rows, stage_mode="修复期模式", tier="修复", sectors=None,
                             float_caps={r["code"]: 80 for r in rows}, watchlist=[], pool_day="20261030")
    assert out["mainlines"] and out["mainlines"][0]["industry"] == "休闲食品"
    assert out["mainlines"][0]["mainline_ok"] is True
    assert len(out["observe"]) <= 10
    # 散 not in mainline
    assert all(r["code"] != "002000" for r in out["observe"])

def test_retreat_buy_point_restriction():
    r = _zt("001219", "青岛食品", "休闲食品", lb=1, zb_times=2, last_seal="143000")
    assert planned_buy_point("退潮期模式", r, True, False) == "首板次日弱转强"
    r2 = _zt("001219", "青岛食品", "休闲食品", lb=3, zb_times=0, last_seal="100000")
    assert planned_buy_point("退潮期模式", r2, True, False) == "—"
    # 修复期高位龙头 -> 分歧转一致
    assert planned_buy_point("修复期模式", r2, True, False) == "分歧转一致"

def test_partial_mainline_when_lt5():
    rows = [_zt(f"00300{i}", f"A{i}", "白酒Ⅱ", amount_yi=10, first_seal=f"09400{i}") for i in range(3)]
    main = identify_mainlines(rows, None, "退潮期模式")
    assert main["mainlines"]
    assert main["mainlines"][0]["mainline_ok"] == "partial"
    assert main["mainlines"][0]["mainline_days"] == 1
    assert "连续2天" not in (main["mainlines"][0]["inflow_note"] or "") or "未核验" in main["mainlines"][0]["inflow_note"]

def test_open_confirm_replay_helper():
    # standing above open
    bars = [f"093{i} {10+i*0.01} {1000*(i+1)} {1e7*(i+1)}" for i in range(0, 11)]  # 0930-0940
    # fix times properly
    bars = []
    px = 10.0
    for m in range(0, 11):
        t = 930 + m
        hhmm = f"{t:04d}"
        px = 10.0 + 0.05  # stay above open
        bars.append(f"{hhmm} {px} {1000*(m+1)} {1e7}")
    bars[0] = "0930 10.0 1000 1e7"
    yest = [f"{930+m:04d} 9.5 {500*(m+1)} {5e6}" for m in range(0, 11)]
    v = open_confirm_from_minutes("000001", "测试", bars, pre=9.5, yest_bars=yest,
                                  buy_point="首板次日弱转强", replay=True)
    assert v["conclusion"] == "可建仓"
    assert v["replay"] is True
    assert "回放" in v["reason"]

    # break open
    bars2 = [f"{930+m:04d} {10 - m*0.05} {1000} {1e7}" for m in range(0, 11)]
    v2 = open_confirm_from_minutes("000001", "测试", bars2, pre=9.5, replay=True)
    assert v2["conclusion"] == "不可建仓"

def test_lanban_detect():
    assert is_lanban_or_tail(_zt("1", "a", "x", zb_times=1))
    assert is_lanban_or_tail(_zt("1", "a", "x", zb_times=0, last_seal="140000"))
    assert not is_lanban_or_tail(_zt("1", "a", "x", zb_times=0, last_seal="103000"))

def test_catalytic_never_auto_pass():
    rows = [_zt(f"00400{i}", f"B{i}", "元件", amount_yi=20, first_seal=f"0930{i:02d}") for i in range(5)]
    out = build_observe_pool(rows, stage_mode="主升期模式", tier="主升", sectors=None,
                             float_caps={r["code"]: 100 for r in rows}, watchlist=[], pool_day="20261030")
    assert out["observe"]
    for r in out["observe"]:
        assert r["pass_flags"]["catalytic"] == "未核验"
