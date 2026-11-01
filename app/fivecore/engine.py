"""Transparent, deterministic Five-Core rules. No LLM-generated trading decisions.

Semantics (v0.2):
- Missing input (None) is *unknown*, never 0 and never a pass.
- advancers + decliners + unchanged == 0 is treated as missing breadth.
- limit_up + limit_down + broken_limit == 0 (all three zero) is treated as missing pools.
- If key inputs are missing, environment / sentiment = "数据不足" (unless a known
  value already proves the negative side, e.g. 跌停≥20 → 偏弱).
- Stock checks are three-state: True (data supports), False (data contradicts), None (未知/未提供).
  Only True scores. When the market is "数据不足", market/sentiment checks are None.
"""
from datetime import datetime, timezone

INSUFFICIENT = '数据不足'
LICENSED_PREFIX = 'licensed'
QUALITY_CN = {
    'public_web_unlicensed': '公开网页行情（非授权 Level-1），延迟数秒至数分钟，仅供研究参考',
    'provider_unverified': '数据源未核验',
}


def quality_cn(q: str | None) -> str:
    if not q:
        return '未标注'
    if q.startswith(LICENSED_PREFIX):
        return f'授权行情（{q}）'
    return QUALITY_CN.get(q, q)


def _num(x):
    """float or None. Never substitutes 0 for a missing value."""
    if x is None or isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def analyze_market(m: dict) -> dict:
    adv, dec, unch = _num(m.get('advancers')), _num(m.get('decliners')), _num(m.get('unchanged'))
    missing = []
    breadth = None
    if adv is None or dec is None or (adv + dec + (unch or 0)) <= 0:
        missing.append('breadth')
    else:
        breadth = adv / max(adv + dec, 1)

    limit_up, limit_down, broken = _num(m.get('limit_up')), _num(m.get('limit_down')), _num(m.get('broken_limit'))
    if limit_up is not None and limit_down is not None and broken is not None and limit_up + limit_down + broken == 0:
        limit_up = limit_down = broken = None          # an all-zero pool snapshot is not a real market
        missing.append('limit_pools')
    for k, v in (('limit_up', limit_up), ('limit_down', limit_down), ('broken_limit', broken)):
        if v is None and 'limit_pools' not in missing:
            missing.append(k)
    break_rate = broken / max(limit_up + broken, 1) if limit_up is not None and broken is not None else None
    prev_premium = _num(m.get('yesterday_limit_premium_pct'))
    if prev_premium is None:
        missing.append('yesterday_limit_premium_pct')
    amount = _num(m.get('turnover_yi'))
    vs_prev = _num(m.get('turnover_vs_prev_pct'))

    # --- environment: breadth is mandatory ---
    if breadth is None:
        environment = INSUFFICIENT
    elif breadth <= .38 or (limit_down is not None and limit_down >= 20):
        environment = '偏弱'
    elif prev_premium is None or limit_down is None:
        environment = INSUFFICIENT
    elif breadth >= .62 and prev_premium > 0 and limit_down <= 8:
        environment = '偏强'
    else:
        environment = '震荡'

    # --- sentiment: breadth + pools + premium are key inputs ---
    if breadth is None:
        sentiment = INSUFFICIENT
    elif (prev_premium is not None and prev_premium < -1) or (break_rate is not None and break_rate >= .50) \
            or (limit_down is not None and limit_down >= 15):
        sentiment = '亏钱效应占优'
    elif prev_premium is None or limit_up is None or break_rate is None or limit_down is None:
        sentiment = INSUFFICIENT
    elif prev_premium > 1 and limit_up >= 50 and break_rate < .30:
        sentiment = '赚钱效应扩散'
    else:
        sentiment = '分歧/修复观察'

    if amount is None or amount <= 0:
        liquidity = '成交额数据缺失'
        missing.append('turnover_yi')
    elif vs_prev is None:
        liquidity = '同比数据缺失'
        missing.append('turnover_vs_prev_pct')
    else:
        liquidity = '放量' if vs_prev > 3 else ('缩量' if vs_prev < -3 else '量能平稳')

    def _i(v):
        return int(v) if v is not None else None

    return {
        'environment': environment,
        'sentiment': sentiment,
        'breadth_pct': round(breadth * 100, 1) if breadth is not None else None,
        'advancers': _i(adv), 'decliners': _i(dec), 'unchanged': _i(unch),
        'limit_up': _i(limit_up),
        'limit_down': _i(limit_down),
        'broken_limit': _i(broken),
        'broken_limit_rate_pct': round(break_rate * 100, 1) if break_rate is not None else None,
        'yesterday_limit_premium_pct': prev_premium,
        'liquidity': liquidity,
        'turnover_yi': amount,
        'turnover_vs_prev_pct': vs_prev,
        'missing_inputs': missing,
        'timestamp': m.get('timestamp') or datetime.now(timezone.utc).isoformat(),
        'data_quality': m.get('data_quality') or 'provider_unverified',
        'data_quality_cn': quality_cn(m.get('data_quality') or 'provider_unverified'),
    }


CHECK_LABELS = {
    'market': '市场环境', 'sentiment': '情绪周期', 'mainline': '属于当前主线', 'leader': '龙头/容量核心',
    'absorption': '承接改善', 'sector_validation': '板块交叉验证', 'entry': '预设买点触发', 't1_risk': 'T+1 风险可接受',
}


def _tri(v):
    return None if v is None else bool(v)


def analyze_stock(stock: dict, market: dict) -> dict:
    """Only score supplied facts. Missing evidence (None) is 未提供/未知 and never counts as a pass."""
    env, sent = market.get('environment'), market.get('sentiment')
    t1 = stock.get('unacceptable_t1_risk')
    checks = {
        'market': None if env in (None, INSUFFICIENT) else env != '偏弱',
        'sentiment': None if sent in (None, INSUFFICIENT) else sent != '亏钱效应占优',
        'mainline': _tri(stock.get('is_mainline')),
        'leader': _tri(stock.get('is_leader_or_capacity_core')),
        'absorption': _tri(stock.get('absorption_improving')),
        'sector_validation': _tri(stock.get('sector_confirmation')),
        'entry': _tri(stock.get('entry_trigger')),
        't1_risk': None if t1 is None else not bool(t1),
    }
    passed = sum(1 for v in checks.values() if v is True)
    failed = [k for k, v in checks.items() if v is False]
    unknown = [k for k, v in checks.items() if v is None]
    if checks['market'] is False or checks['sentiment'] is False or checks['t1_risk'] is False:
        status = '风险回避'
    elif passed >= 7:
        status = '允许进入人工决策'
    elif passed >= 4:
        status = '观察等待确认'
    else:
        status = '暂不关注'
    notes = []
    if checks['market'] is None or checks['sentiment'] is None:
        notes.append('市场快照数据不足，市场环境/情绪两项记为未知，不计分。')
    if checks['t1_risk'] is None:
        notes.append('未提供 T+1 风险判断，记为未知，不计分。')
    return {
        'code': stock.get('code', ''), 'name': stock.get('name', ''),
        'price': stock.get('price'), 'change_pct': stock.get('change_pct'),
        'score': passed, 'max_score': len(checks), 'status': status,
        'checks': checks,
        'check_labels': CHECK_LABELS,
        'failed': failed,
        'unknown': unknown,
        'missing_evidence': failed + unknown,
        'notes': notes,
        'reason': '规则引擎仅评估已提供且有来源的数据；未提供或未知的项目一律不计分，不构成自动买入指令。',
    }
