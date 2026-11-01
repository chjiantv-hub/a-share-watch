#!/usr/bin/env python3
"""选股入池纯逻辑：情绪阶段下的主线识别、盘前观察池、开盘确认。
不编造价格/家数；缺数据标「未取到/未核验/未知」。"""
from __future__ import annotations
import re
from collections import Counter, defaultdict
from typing import Any

MIN_ZT_MAINLINE = 5
MIN_ZT_CANDIDATE = 3          # 当日无≥5时，用≥3作「候选主线」
MIN_AMOUNT_YI = 5.0
CAP_MIN_YI, CAP_MAX_YI = 50.0, 500.0
OBSERVE_CAP = 10

def _amt_yi(r: dict) -> float | None:
    if r.get("amount_yi") is not None:
        try: return float(r["amount_yi"])
        except Exception: pass
    if r.get("amount") is not None:
        try: return round(float(r["amount"]) / 1e8, 2)
        except Exception: pass
    return None

def _is_st(name: str | None) -> bool:
    return "ST" in (name or "").upper()

def _hms(s: str | None) -> str:
    s = (s or "").strip()
    if re.fullmatch(r"\d{6}", s): return s
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        parts = s.split(":")
        return f"{int(parts[0]):02d}{int(parts[1]):02d}{int(parts[2]) if len(parts)>2 else 0:02d}"
    return s or ""

def is_lanban_or_tail(r: dict) -> bool:
    """烂板或尾盘板：炸板次数>0，或末封≥14:00。"""
    zb = r.get("zb_times") or 0
    last = _hms(r.get("last_seal"))
    return bool(zb and zb > 0) or (last >= "140000" if last else False)

def identify_mainlines(zt_rows: list[dict], sectors: dict | None, stage_mode: str) -> dict:
    """主线：行业涨停≥5；若无，则≥3为候选主线（partial）。
    连续流入天数：仅当 sectors 带来历史时才宣称；默认 mainline_days=1。"""
    zt_rows = zt_rows or []
    ind_n = Counter((r.get("industry") or "").strip() or "未标注行业" for r in zt_rows)
    full = [{"industry": k, "zt_n": n, "mainline_ok": True, "mainline_days": 1,
             "inflow_note": "仅有当日板块快照，未核验连续2天资金流入", "kind": "主线"}
            for k, n in ind_n.most_common() if n >= MIN_ZT_MAINLINE and k != "未标注行业"]
    partial = []
    if not full:
        partial = [{"industry": k, "zt_n": n, "mainline_ok": "partial", "mainline_days": 1,
                    "inflow_note": f"当日无行业涨停≥{MIN_ZT_MAINLINE}家；此为涨停相对集中方向（≥{MIN_ZT_CANDIDATE}），仅候选主线",
                    "kind": "候选主线"}
                   for k, n in ind_n.most_common() if n >= MIN_ZT_CANDIDATE and k != "未标注行业"]

    # 板块当日流入（单日）：用于标注，不宣称连续
    inflow_map = {}
    bottom_names = set()
    if isinstance(sectors, dict):
        top = sectors.get("industry_inflow_top")
        if isinstance(top, list):
            for x in top:
                if isinstance(x, dict) and x.get("board"):
                    inflow_map[x["board"]] = x.get("main_net_yi")
        bot = sectors.get("industry_bottom")
        if isinstance(bot, list):
            bottom_names = {x.get("board") for x in bot if isinstance(x, dict)}

    def enrich(m):
        yi = inflow_map.get(m["industry"])
        m = dict(m)
        m["main_net_yi_today"] = yi
        if yi is not None:
            m["inflow_note"] = f"当日主力净流入 {yi} 亿（单日快照，非连续2天核验）" + (
                "；净流出" if yi < 0 else "")
        # 退潮期：弱板块不作为主攻方向
        m["is_weak_board"] = m["industry"] in bottom_names
        m["new_direction"] = bool(stage_mode == "退潮期模式" and not m["is_weak_board"])
        if stage_mode == "退潮期模式" and m["is_weak_board"]:
            m["kind"] = "退潮方向（慎做）"
        elif stage_mode == "退潮期模式":
            m["kind"] = "新方向" if m["mainline_ok"] is True else "候选新方向"
        return m

    mainlines = [enrich(m) for m in (full or partial)]
    note = None
    if not full and not partial:
        note = f"当日涨停池无行业达到≥{MIN_ZT_CANDIDATE}家，主线未识别"
    elif not full:
        note = f"当日无行业涨停≥{MIN_ZT_MAINLINE}家，仅列出候选主线"
    return {"mainlines": mainlines, "industry_zt_counts": dict(ind_n),
            "mainline_threshold": MIN_ZT_MAINLINE, "note": note,
            "sector_inflow_history_days": 1,
            "sector_inflow_note": "目前仅缓存当日板块主力净流入，不能宣称「连续2天流入」"}

def _辨识度(rows_in_ind: list[dict], r: dict) -> tuple[bool, list[str]]:
    reasons = []
    ok = False
    # 最先涨停
    seals = [(_hms(x.get("first_seal")) or "999999", x["code"]) for x in rows_in_ind]
    seals.sort()
    if seals and seals[0][1] == r["code"] and seals[0][0] != "999999":
        ok = True; reasons.append(f"板块内最先涨停（首封 {seals[0][0][:2]}:{seals[0][0][2:4]}）")
    # 最高连板
    max_lb = max((x.get("lb") or 1) for x in rows_in_ind)
    if (r.get("lb") or 1) == max_lb and max_lb >= 1:
        leaders = [x for x in rows_in_ind if (x.get("lb") or 1) == max_lb]
        if len(leaders) == 1 or r["code"] == max(leaders, key=lambda x: _amt_yi(x) or 0)["code"]:
            ok = True; reasons.append(f"板块内最高连板 {max_lb}")
    # 成交额最大（中军）
    amts = [( _amt_yi(x) or -1, x["code"]) for x in rows_in_ind]
    amts.sort(reverse=True)
    if amts and amts[0][0] > 0 and amts[0][1] == r["code"]:
        ok = True; reasons.append(f"板块内涨停成交额最大 {_amt_yi(r)} 亿")
    # 封单最强
    seals_f = [(x.get("seal_yi") if x.get("seal_yi") is not None else -1, x["code"]) for x in rows_in_ind]
    seals_f.sort(reverse=True)
    if seals_f and seals_f[0][0] > 0 and seals_f[0][1] == r["code"]:
        ok = True; reasons.append(f"板块内封单最强 {r.get('seal_yi')} 亿")
    return ok, reasons

def _cat_view(cat: dict | None) -> dict | None:
    if not cat: return None
    return {k: cat.get(k) for k in ("flag", "flag_kind", "reason", "status", "top", "counts", "fetched_at", "ann_source", "window")}

def planned_buy_point(stage_mode: str, r: dict, is_leader: bool, is_mid: bool) -> str:
    lb = r.get("lb") or 1
    lan = is_lanban_or_tail(r)
    retreat = stage_mode == "退潮期模式"
    if retreat:
        # 退潮期主攻：仅首板（含烂板/尾盘板首板）；高位连板不标此买点
        if lb == 1:
            return "首板次日弱转强"
        return "—"
    # 修复/主升/过渡：能对上的才写
    if lan and lb == 1:
        return "首板次日弱转强"
    if lb >= 2 and is_leader:
        return "分歧转一致"
    if is_mid and lb == 1 and not lan:
        return "趋势中军回踩"
    if lb == 1:
        return "首板次日弱转强"
    return "—"

def build_observe_pool(zt_rows: list[dict], *, stage_mode: str, tier: str | None,
                       sectors: dict | None, float_caps: dict[str, float | None],
                       watchlist: list[dict], user_notes: dict | None = None,
                       pool_day: str, position: dict | None = None,
                       catalysts: dict[str, dict] | None = None) -> dict:
    """盘前观察池。float_caps: code -> 流通市值亿元 or None。
    catalysts: code -> catalyst.summarize() 结果；None 表示未启用自动抓取（一律「未核验」）。"""
    user_notes = user_notes or {}
    cat_excluded: list[tuple[str, str]] = []
    main = identify_mainlines(zt_rows, sectors, stage_mode)
    theme_inds = {m["industry"] for m in main["mainlines"] if m.get("kind") != "退潮方向（慎做）"}
    weak_inds = {m["industry"] for m in main["mainlines"] if m.get("is_weak_board")}
    kind_of = {m["industry"]: m for m in main["mainlines"]}

    by_ind = defaultdict(list)
    for r in zt_rows or []:
        by_ind[(r.get("industry") or "").strip() or "未标注行业"].append(r)

    observe, rejected, w2s = [], [], []
    for r in zt_rows or []:
        code, name = r.get("code"), r.get("name") or ""
        ind = (r.get("industry") or "").strip() or "未标注行业"
        note = user_notes.get(code) or {}
        if note.get("action") == "剔除":
            rejected.append({"code": code, "name": name, "industry": ind,
                             "reasons": ["用户标记剔除"], "conclusion_for_observe": "剔除"})
            continue
        reasons, flags = [], {}
        # ST hard
        if _is_st(name):
            rejected.append({"code": code, "name": name, "industry": ind,
                             "reasons": ["名称含ST，排除"], "conclusion_for_observe": "剔除"})
            continue
        amt = _amt_yi(r)
        flags["amount_yi"] = amt
        if amt is None:
            flags["amount_ok"] = "unknown"; reasons.append("成交额未取到")
        elif amt < MIN_AMOUNT_YI:
            rejected.append({"code": code, "name": name, "industry": ind, "amount_yi": amt,
                             "reasons": [f"日成交额 {amt} 亿 < {MIN_AMOUNT_YI} 亿"], "conclusion_for_observe": "剔除"})
            continue
        else:
            flags["amount_ok"] = True; reasons.append(f"成交额 {amt} 亿 ≥ {MIN_AMOUNT_YI} 亿")

        # 主线 / 新方向
        in_theme = ind in theme_inds
        flags["theme_ok"] = in_theme
        if not in_theme:
            # 不在主线/候选：硬性不入观察池（可进 rejected 仅参考列表外）
            rejected.append({"code": code, "name": name, "industry": ind, "amount_yi": amt,
                             "reasons": [f"所属行业非主线/候选主线（该行业涨停 {main['industry_zt_counts'].get(ind, 0)} 家）"],
                             "conclusion_for_observe": "剔除"})
            continue
        km = kind_of.get(ind) or {}
        theme_label = km.get("kind") or "主线"
        reasons.append(f"{theme_label}·{ind}（涨停 {km.get('zt_n', main['industry_zt_counts'].get(ind, 0))} 家）")
        if km.get("mainline_ok") == "partial":
            flags["mainline_ok"] = "partial"
        else:
            flags["mainline_ok"] = True
        flags["mainline_days"] = km.get("mainline_days", 1)
        if stage_mode == "退潮期模式" and ind in weak_inds:
            rejected.append({"code": code, "name": name, "industry": ind, "amount_yi": amt,
                             "reasons": ["退潮期不做当日最弱板块"], "conclusion_for_observe": "剔除"})
            continue

        # 辨识度
        is_id, id_reasons = _辨识度(by_ind[ind], r)
        flags["ident_ok"] = is_id
        reasons.extend(id_reasons or ["板块内辨识度一般（非最先封/最高板/最大额/最强封单）"])
        is_mid = bool(amt and amt >= 15 and (r.get("lb") or 1) == 1 and not is_lanban_or_tail(r))

        # 流通市值
        cap = float_caps.get(code) if float_caps is not None else None
        flags["float_cap_yi"] = cap
        if cap is None:
            flags["cap_ok"] = "unknown"; reasons.append("流通市值未取到")
        elif CAP_MIN_YI <= cap <= CAP_MAX_YI:
            flags["cap_ok"] = True; reasons.append(f"流通市值 {cap} 亿（约 {CAP_MIN_YI}-{CAP_MAX_YI}）")
        else:
            flags["cap_ok"] = False; reasons.append(f"流通市值 {cap} 亿，超出约 {CAP_MIN_YI}-{CAP_MAX_YI} 亿区间（软条件，仍可观察）")

        # 催化/解禁/监管：用户标注优先；其次自动抓取结果；未抓取则「未核验」
        cat = (catalysts or {}).get(code) if catalysts is not None else None
        cat_view = _cat_view(cat)
        if note.get("catalytic_verified"):
            flags["catalytic"] = "已核验（用户）"; flags["catalytic_kind"] = "user"
            extra = f"（自动结果：{cat['flag']}，以用户核验为准）" if cat and cat.get("flag_kind") != "pass" else ""
            reasons.append("催化/解禁/监管：用户已核验" + extra)
        elif cat and cat.get("flag_kind") == "exclude":
            cat_excluded.append((code, name))
            rejected.append({"code": code, "name": name, "industry": ind, "amount_yi": amt,
                             "reasons": [f"公告/资讯负面：{cat.get('flag')}", cat.get("reason") or ""],
                             "conclusion_for_observe": "剔除", "catalyst": cat_view})
            continue
        elif cat:
            flags["catalytic"] = cat.get("flag") or "未核验"; flags["catalytic_kind"] = cat.get("flag_kind") or "unverified"
            reasons.append(f"催化/解禁/监管：{flags['catalytic']}")
        elif catalysts is not None:
            flags["catalytic"] = "未核验（尚未抓取）"; flags["catalytic_kind"] = "unverified"
            reasons.append("催化/解禁/监管：未核验（尚未抓取）")
        else:
            flags["catalytic"] = "未核验"; flags["catalytic_kind"] = "unverified"
            reasons.append("催化/解禁/监管：未核验")

        bp = planned_buy_point(stage_mode, r, is_id, is_mid)
        lan = is_lanban_or_tail(r)
        # 结论
        if not is_id:
            conc = "仅参考"
        elif flags.get("cap_ok") is False and not note.get("pin"):
            conc = "仅参考"
        else:
            conc = "入观察池"

        row = {
            "code": code, "name": name, "industry": ind, "theme": theme_label,
            "theme_detail": f"{ind}（涨停 {km.get('zt_n')} 家）",
            "lb": r.get("lb") or 1, "amount_yi": amt,
            "seal_yi": r.get("seal_yi"), "first_seal": r.get("first_seal"), "last_seal": r.get("last_seal"),
            "zb_times": r.get("zb_times") or 0, "lanban": lan,
            "reasons": reasons, "planned_buy_point": bp,
            "watch_prices": {"昨收": None, "昨低": None},  # filled by caller if known
            "pass_flags": flags, "stage_mode": stage_mode, "tier": tier,
            "conclusion_for_observe": conc,
            "amount": r.get("amount"),
            "pinned": bool(note.get("pin")),
            "catalyst": cat_view,
            "rank_key": (
                0 if note.get("pin") else 1,
                0 if theme_label in ("主线", "新方向") and is_id else (1 if "候选" in theme_label and (r.get("lb") or 1) == 1 else 2),
                0 if is_id else 1,
                0 if (r.get("lb") or 1) == 1 and lan else 1,
                -(amt or 0),
            ),
        }
        observe.append(row)
        # 弱转强候选：主线/新方向下昨日烂板或尾盘板的首板
        if (r.get("lb") or 1) == 1 and lan and in_theme:
            w2s.append({"code": code, "name": name, "industry": ind, "theme": theme_label,
                        "amount_yi": amt, "first_seal": r.get("first_seal"), "last_seal": r.get("last_seal"),
                        "zb_times": r.get("zb_times") or 0, "planned_buy_point": "首板次日弱转强",
                        "note": "供 09:26 竞价验证弱转强"})

    observe.sort(key=lambda x: x["rank_key"])
    candidates_ranked = [(r["code"], r["name"]) for r in observe] + cat_excluded
    # cap 10 for 入观察池+仅参考 ranked; keep conclusion
    top = observe[:OBSERVE_CAP]
    overflow = observe[OBSERVE_CAP:]
    for r in overflow:
        rejected.append({"code": r["code"], "name": r["name"], "industry": r["industry"], "amount_yi": r.get("amount_yi"),
                         "reasons": ["未进入观察池前10（排序靠后）"] + (r.get("reasons") or [])[:2],
                         "conclusion_for_observe": "剔除"})

    # strip internal rank_key
    for r in top:
        r.pop("rank_key", None)

    wl_out = []
    top_codes = {r["code"] for r in top}
    for w in watchlist or []:
        c = w.get("code")
        hit = next((x for x in top if x["code"] == c), None)
        wl_out.append({"code": c, "name": w.get("name") or (hit or {}).get("name") or c,
                       "board": w.get("board"), "note": w.get("note"),
                       "in_observe": c in top_codes,
                       "observe_conclusion": (hit or {}).get("conclusion_for_observe"),
                       "planned_buy_point": (hit or {}).get("planned_buy_point")})

    one = f"{tier or '—'} · {stage_mode} · 仓位{(position or {}).get('total') or '—'} · 主线 " + (
        "、".join(f"{m['industry']}({m['zt_n']})" for m in main["mainlines"][:3]) or "未识别")
    return {
        "day": pool_day, "one_line": one, "tier": tier, "stage_mode": stage_mode,
        "position": position, "mainlines": main["mainlines"],
        "mainline_note": main.get("note"), "sector_inflow_note": main.get("sector_inflow_note"),
        "industry_zt_counts": main["industry_zt_counts"],
        "observe": top, "w2s_candidates": w2s, "rejected": rejected,
        "watchlist": wl_out, "observe_cap": OBSERVE_CAP,
        "candidates_ranked": candidates_ranked,
        "rules": {
            "mainline": f"行业涨停≥{MIN_ZT_MAINLINE}为完整主线；不足时≥{MIN_ZT_CANDIDATE}为候选",
            "liquidity": f"成交额≥{MIN_AMOUNT_YI}亿（硬条件）",
            "cap": f"流通市值约{CAP_MIN_YI}-{CAP_MAX_YI}亿（软条件）",
            "catalytic": ("公告/资讯自动抓取（近约10个交易日）：负面（监管问询/立案、ST/退市风险、业绩预亏、股东减持）剔除；"
                          "风险项（解禁、风险提示、澄清、诉讼冻结、重大事项终止）保持未核验；正面（政策/业绩增长/订单合同/产业事件/增持回购）"
                          "标「已核验（自动）」；抓取失败为未核验并注明原因；用户「催化已核验」优先"
                          if catalysts is not None else "催化/解禁/监管默认未核验，不自动通过"),
            "retreat": "退潮期观察池偏向新方向首板；暂停分歧转一致/趋势中军回踩建仓",
        },
    }

def open_confirm_from_minutes(code: str, name: str, bars: list[str], *,
                              pre: float | None, yest_bars: list[str] | None = None,
                              buy_point: str | None = None, replay: bool = False) -> dict:
    """用开盘至 09:40 的分钟线判断。bars: 'HHMM price vol cum_amount' 文本行。"""
    base = {"code": code, "name": name, "buy_point": buy_point or "—",
            "conclusion": "未取到", "conclusion_cls": "na", "reason": "分时未取到",
            "replay": replay, "metrics": {}}
    if not bars:
        return base
    parsed = []
    for b in bars:
        parts = b.split()
        if len(parts) < 2: continue
        t = parts[0].replace(":", "")
        if len(t) == 4: t = t  # HHMM
        try: p = float(parts[1]); cv = float(parts[2]) if len(parts) > 2 else None
        except Exception: continue
        amt = float(parts[3]) if len(parts) > 3 else None
        parsed.append({"t": t, "p": p, "cv": cv, "amt": amt})
    win = [x for x in parsed if "0930" <= x["t"] <= "0940"]
    if not win:
        # 若只有更早/更晚，尽量取开盘后前约10根
        win = [x for x in parsed if x["t"] >= "0930"][:11]
    if not win:
        return {**base, "reason": "09:30-09:40 分时为空"}
    open_p = win[0]["p"]
    last = win[-1]
    mean_p = sum(x["p"] for x in win) / len(win)
    above_open = last["p"] >= open_p - 1e-6
    broke_open = any(x["p"] < open_p - 1e-6 for x in win) and last["p"] < open_p - 1e-6
    # 冲高回落：最高远高于末价且末价回落近开盘或以下
    hi = max(x["p"] for x in win)
    chase_fade = hi > open_p * 1.02 and last["p"] <= open_p * 1.005
    vol_ratio = None
    if yest_bars:
        ywin = []
        for b in yest_bars:
            parts = b.split()
            if len(parts) < 3: continue
            t = parts[0].replace(":", "")
            if "0930" <= t <= "0940":
                try: ywin.append(float(parts[2]))
                except Exception: pass
        if ywin and last.get("cv") is not None and ywin[-1]:
            vol_ratio = round(last["cv"] / ywin[-1], 2)
    metrics = {"open": open_p, "last_0940": last["p"], "high": hi, "mean": round(mean_p, 3),
               "above_open": above_open, "vol_vs_yest": vol_ratio,
               "window": f"{win[0]['t']}-{win[-1]['t']}", "pre": pre,
               "pct_from_open": round((last["p"] / open_p - 1) * 100, 2) if open_p else None}
    prefix = "回放·" if replay else ""
    if broke_open or (chase_fade and not above_open):
        return {**base, "conclusion": "不可建仓", "conclusion_cls": "no", "metrics": metrics,
                "reason": prefix + ("跌破开盘价" if broke_open else "冲高回落且未站稳开盘价")}
    if above_open and (vol_ratio is None or vol_ratio >= 0.8) and not chase_fade:
        bp = buy_point if buy_point and buy_point != "—" else "—"
        return {**base, "conclusion": "可建仓", "conclusion_cls": "yes", "metrics": metrics,
                "buy_point": bp,
                "reason": prefix + f"09:40 仍站上开盘价（开{open_p} 现{last['p']}）" + (
                    f"，量能较昨同时刻 {vol_ratio} 倍" if vol_ratio is not None else "，较昨同时刻量能未取到")}
    if above_open and chase_fade:
        return {**base, "conclusion": "暂不可建仓，继续观察", "conclusion_cls": "hold", "metrics": metrics,
                "reason": prefix + "虽仍高于开盘价但冲高回落，等待再次承接"}
    return {**base, "conclusion": "暂不可建仓，继续观察", "conclusion_cls": "hold", "metrics": metrics,
            "reason": prefix + f"未稳站开盘价或量能不足（开{open_p} 现{last['p']}" + (
                f"，量比昨{vol_ratio}" if vol_ratio is not None else "") + "）"}

def merge_trade_pool(day: str, observe: dict | None, auction: dict | None,
                     open_confirm: dict | None, prev: dict | None = None) -> dict:
    obs_rows = (observe or {}).get("observe") or []
    au_rows = (auction or {}).get("rows") or []
    oc_rows = (open_confirm or {}).get("rows") or []
    focus = []
    for r in au_rows:
        if r.get("conclusion") in ("可建仓", "只看不追") or r.get("cls") == "w2s" or r.get("is_watchlist") or "观察池" in (r.get("src") or []):
            focus.append({"code": r["code"], "name": r.get("name"), "conclusion": r.get("conclusion"),
                          "buy_point": r.get("buy_point"), "tag": r.get("tag"), "src": r.get("src")})
    # dedupe focus preserving order
    seen = set(); focus2 = []
    for f in focus:
        if f["code"] in seen: continue
        seen.add(f["code"]); focus2.append(f)
    confirmed = [r for r in oc_rows if r.get("conclusion") == "可建仓"]
    rejected = list((observe or {}).get("rejected") or [])
    notes = (prev or {}).get("notes") or {}
    return {"day": day, "observe": [{"code": r["code"], "name": r.get("name"),
                                     "conclusion": r.get("conclusion_for_observe"),
                                     "planned_buy_point": r.get("planned_buy_point"),
                                     "theme": r.get("theme"), "industry": r.get("industry"),
                                     "catalytic": (r.get("pass_flags") or {}).get("catalytic")} for r in obs_rows],
            "focus": focus2, "confirmed": confirmed,
            "rejected": [{"code": r.get("code"), "name": r.get("name"), "reasons": r.get("reasons")} for r in rejected[:50]],
            "notes": notes, "updated_at": None}
