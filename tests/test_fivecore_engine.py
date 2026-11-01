from fivecore.engine import analyze_market, analyze_stock, quality_cn, INSUFFICIENT

# Real 2026-10-30 public-web numbers (a-share-watch): breadth 893/4602/76, 涨停47 跌停82 炸板31, 昨涨停今 -0.68%
REAL_1030 = {'advancers': 893, 'decliners': 4602, 'unchanged': 76, 'limit_up': 47, 'limit_down': 82, 'broken_limit': 31,
             'yesterday_limit_premium_pct': -0.68, 'turnover_yi': 16739.2, 'turnover_vs_prev_pct': 7.6,
             'data_quality': 'public_web_unlicensed', 'timestamp': '2026-11-01T15:41:05+08:00'}


def test_market_missing_data_is_not_falsely_bullish():
    """All-zero breadth is missing data: neither 偏强 nor a firm 偏弱/震荡 label."""
    result = analyze_market({'advancers': 0, 'decliners': 0, 'limit_up': 0, 'limit_down': 0, 'broken_limit': 0})
    assert result['environment'] == INSUFFICIENT
    assert result['sentiment'] == INSUFFICIENT
    assert result['breadth_pct'] is None
    assert result['limit_up'] is None and result['broken_limit_rate_pct'] is None   # all-zero pools = missing, not 0
    assert 'breadth' in result['missing_inputs']


def test_unacceptable_t1_risk_blocks_candidate():
    market = {'environment': '偏强', 'sentiment': '赚钱效应扩散'}
    stock = {'code': '600000', 'is_mainline': True, 'is_leader_or_capacity_core': True,
             'absorption_improving': True, 'sector_confirmation': True,
             'entry_trigger': True, 'unacceptable_t1_risk': True}
    result = analyze_stock(stock, market)
    assert result['status'] == '风险回避'
    assert result['checks']['t1_risk'] is False


def test_missing_evidence_does_not_count_as_pass():
    # stock-level evidence missing -> 未提供 (None), never True; T+1 not stated -> unknown
    result = analyze_stock({'code': '000001'}, {'environment': '震荡', 'sentiment': '分歧/修复观察'})
    for k in ('mainline', 'leader', 'absorption', 'sector_validation', 'entry', 't1_risk'):
        assert result['checks'][k] is None
    assert result['score'] == 2            # only market + sentiment, which the snapshot supports
    assert result['status'] == '暂不关注'
    # when the market snapshot itself is 数据不足, nothing scores at all
    result = analyze_stock({'code': '000001'}, {'environment': INSUFFICIENT, 'sentiment': INSUFFICIENT})
    assert result['score'] == 0
    assert result['checks']['market'] is None and result['checks']['sentiment'] is None
    assert result['status'] == '暂不关注'
    assert set(result['unknown']) == set(result['checks'])


def test_null_fields_are_unknown_not_zero():
    m = analyze_market({'advancers': 3000, 'decliners': 2000, 'unchanged': 100, 'limit_up': None, 'limit_down': None,
                        'broken_limit': None, 'yesterday_limit_premium_pct': None, 'turnover_yi': None})
    assert m['environment'] == INSUFFICIENT          # breadth 60% but 跌停/溢价 unknown -> cannot label
    assert m['sentiment'] == INSUFFICIENT
    assert m['limit_up'] is None and m['limit_down'] is None and m['broken_limit_rate_pct'] is None
    assert m['liquidity'] == '成交额数据缺失'
    assert {'limit_up', 'limit_down', 'broken_limit', 'yesterday_limit_premium_pct', 'turnover_yi'} <= set(m['missing_inputs'])


def test_known_negative_still_detected_with_partial_data():
    m = analyze_market({'advancers': 1000, 'decliners': 4000, 'unchanged': 50, 'limit_down': 30})
    assert m['environment'] == '偏弱'
    assert m['sentiment'] == '亏钱效应占优'


def test_real_snapshot_and_public_label():
    m = analyze_market(REAL_1030)
    assert m['environment'] == '偏弱' and m['sentiment'] == '亏钱效应占优'
    assert m['limit_up'] == 47 and m['broken_limit_rate_pct'] == 39.7 and m['liquidity'] == '放量'
    assert m['data_quality'] == 'public_web_unlicensed'
    assert '非授权' in m['data_quality_cn'] and 'Level-1' in m['data_quality_cn']
    assert quality_cn('licensed_level1').startswith('授权行情')
    s = analyze_stock({'code': '002343', 'unacceptable_t1_risk': False}, m)
    assert s['status'] == '风险回避' and s['checks']['market'] is False
