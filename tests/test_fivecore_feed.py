"""Feed mapping from a-share-watch caches -> FiveCore payload, and auth through the real a-share-watch middleware."""
import os
from fivecore.feed import build_public_payload, PUBLIC_QUALITY

EM = '东方财富 push2ex'


def mk(st):
    return lambda k: st.get(k) or {'ok': False, 'data': None}


def full_state(rd='20261030'):
    return {
        'breadth': {'ok': True, 'fetched_at': '2026-11-01 15:33:19', 'data': {'up': 893, 'down': 4602, 'flat': 76, 'data_time': '2026-10-30 16:12:00'}},
        'limitup': {'ok': True, 'fetched_at': '2026-11-01 15:33:18', 'data': {
            'date': rd, 'errors': {}, 'sources': {'zt': EM, 'dt': EM, 'zb': EM, 'prev': EM},
            'zt': [{'code': f'{i:06d}', 'industry': '白酒Ⅱ' if i < 4 else '元件'} for i in range(47)],
            'dt': [{}] * 82, 'zb': [{}] * 31, 'prev': {'avg_pct': -0.68, 'n': 39, 'still_zt': 9}}},
        'index': {'ok': True, 'fetched_at': '2026-11-01 15:27:46', 'data': {
            'rows': [{'name': '上证指数', 'price': 3599.02, 'pct': -1.44, 'time': '2026-10-30 16:14:00'}],
            'turnover_yi': 16739.2, 'turnover_cmp': {'hhmm': '1500', 'prev_day': '20261029', 'chg_pct': 7.6}, 'data_time': '2026-10-30 16:14:00'}},
    }


def test_full_mapping_matches_watch_app():
    p = build_public_payload(mk(full_state()), lambda: '20261030', EM)
    assert p['data_quality'] == PUBLIC_QUALITY
    assert (p['advancers'], p['decliners'], p['unchanged']) == (893, 4602, 76)
    assert (p['limit_up'], p['limit_down'], p['broken_limit']) == (47, 82, 31)
    assert p['yesterday_limit_premium_pct'] == -0.68
    assert p['turnover_yi'] == 16739.2 and p['turnover_vs_prev_pct'] == 7.6
    assert p['missing_fields'] == []
    assert p['timestamp'] == '2026-11-01T15:33:19+08:00'          # real fetch time, Beijing
    assert p['field_fetched_at']['limit_up'] == '2026-11-01T15:33:18+08:00'
    assert '东方财富' in p['field_sources']['limit_up'] and '腾讯' in p['field_sources']['turnover_yi']
    assert p['sectors'][0] == {'name': '元件', 'limit_up_count': 43}


def test_missing_pools_are_null_not_zero():
    st = full_state()
    st['limitup']['data'].update({'errors': {'dt': 'PoolError: timeout', 'prev': 'PoolError: x'}, 'sources': {'zt': '同花顺 data.10jqka.com.cn（备用）', 'zb': EM}})
    del st['limitup']['data']['dt']; del st['limitup']['data']['prev']
    del st['index']['data']['turnover_cmp']
    p = build_public_payload(mk(st), lambda: '20261030', EM)
    assert p['limit_down'] is None and p['yesterday_limit_premium_pct'] is None and p['turnover_vs_prev_pct'] is None
    assert {'limit_down', 'yesterday_limit_premium_pct', 'turnover_vs_prev_pct'} <= set(p['missing_fields'])
    assert '同花顺' in p['field_sources']['limit_up']
    assert 'timeout' in p['field_notes']['limit_down']


def test_stale_pool_date_is_rejected_and_nothing_is_none():
    p = build_public_payload(mk(full_state(rd='20261029')), lambda: '20261030', EM)
    assert p['limit_up'] is None and p['limit_down'] is None and '20261029' in p['field_notes']['limit_pools']
    assert build_public_payload(mk({}), lambda: '20261030', EM) is None


def test_legu_fallback_for_breadth():
    st = full_state()
    st['breadth']['data'] = {'legu': {'上涨': 900.0, '下跌': 4590.0, '平盘': 80.0, '统计日期': '2026-10-30 15:00:00'}}
    p = build_public_payload(mk(st), lambda: '20261030', EM)
    assert p['advancers'] == 900 and '乐咕' in p['field_sources']['advancers']


def test_auth_required_and_ingest_via_real_app():
    from fastapi.testclient import TestClient
    import app as A          # a-share-watch app (startup/poller not run without context manager)
    c = TestClient(A.app)
    tok = os.environ['ACCESS_TOKEN']
    assert c.get('/fivecore/api/market/latest').status_code == 401
    assert c.get('/fivecore/').status_code == 401
    assert c.post('/fivecore/api/analyze/stock', json={'code': '002343'}).status_code == 401
    r = c.get('/fivecore/api/health', headers={'X-Token': tok})
    assert r.status_code == 200 and r.json()['market_feed'] in ('not_configured', 'public_web_unlicensed')
    page = c.get(f'/fivecore/?t={tok}')
    assert page.status_code == 200 and '<script>' in page.text and 'static/app.js' not in page.text
    # ingest: own token, constant-time compare; no ACCESS_TOKEN needed
    body = {'source': '测试夹具', 'timestamp': '2026-11-01T15:00:00+08:00', 'data_quality': 'licensed_test', 'limit_up': 1}
    assert c.post('/fivecore/api/ingest/market', json=body, headers={'X-Ingest-Token': 'bad'}).status_code == 401
    assert c.post('/fivecore/api/ingest/market', json=body, headers={'X-Ingest-Token': os.environ['FIVECORE_INGEST_TOKEN']}).status_code == 200
    h = c.get('/fivecore/api/health', headers={'X-Token': tok}).json()
    assert h['market_feed'] == 'licensed'
    m = c.get('/fivecore/api/market/latest', headers={'X-Token': tok}).json()
    assert m['environment'] == '数据不足' and m['advancers'] is None and m['banner'] is None
