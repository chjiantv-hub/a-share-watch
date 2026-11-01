"""Build a FiveCore MarketPayload from a-share-watch's in-process caches (no HTTP self-push).

Every field carries its own source and Beijing fetch time. A field that is not available is None and
listed in missing_fields; nothing is filled with 0 or estimated."""
import datetime as dt

BJ = dt.timezone(dt.timedelta(hours=8))
PUBLIC_QUALITY = 'public_web_unlicensed'
FIELDS = ['advancers', 'decliners', 'unchanged', 'limit_up', 'limit_down', 'broken_limit',
          'yesterday_limit_premium_pct', 'turnover_yi', 'turnover_vs_prev_pct', 'indices', 'sectors']


def iso_bj(s):
    """'2026-11-01 15:33:18' (Beijing, from a-share-watch) -> ISO 8601 with +08:00."""
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=BJ).isoformat()
    except ValueError:
        return s


def _pool_src(label, em_label):
    if not label:
        return None
    return '东方财富 push2ex 涨停/跌停/炸板池' if label == em_label else f'同花顺备用（{label}）'


def build_public_payload(get, ref_day, em_label, sentiment=None):
    """get(key) -> a-share-watch ST entry {ok, data, fetched_at, source, error}. Returns payload dict or None."""
    p = {k: None for k in FIELDS}
    src, at, notes = {}, {}, {}
    data_time = {}

    # ---- breadth: 东方财富 index breadth, else 乐咕 legu ----
    b = get('breadth')
    bd = b.get('data') or {}
    leg = bd.get('legu') or {}
    if 'up' in bd:
        p['advancers'], p['decliners'], p['unchanged'] = bd.get('up'), bd.get('down'), bd.get('flat')
        s = '东方财富 指数涨跌家数（沪A+深A+北证，push2 镜像）'
        data_time['breadth'] = bd.get('data_time')
    elif leg.get('上涨') is not None:
        p['advancers'], p['decliners'], p['unchanged'] = leg.get('上涨'), leg.get('下跌'), leg.get('平盘')
        s = '乐咕乐股 legu 市场活跃度（东财镜像失败时备用）'
        data_time['breadth'] = leg.get('统计日期')
    else:
        s = None
    for k in ('advancers', 'decliners', 'unchanged'):
        if p[k] is not None:
            p[k] = int(float(p[k])); src[k] = s; at[k] = iso_bj(b.get('fetched_at'))
    if s and not b.get('ok') and b.get('error'):
        notes['breadth'] = f"最近一次刷新失败，沿用 {b.get('fetched_at')} 的数据：{b.get('error')}"[:200]

    # ---- limit pools: 涨停 / 跌停 / 炸板 / 昨涨停今表现 ----
    lu_e = get('limitup')
    lu = lu_e.get('data') or {}
    rd = ref_day()
    trade_date = lu.get('date')
    if lu and trade_date != rd:
        notes['limit_pools'] = f'涨停池日期 {trade_date} ≠ 当前交易日 {rd}，已不采用'
        lu = {}
    lsrc = lu.get('sources') or {}
    for fk, pk in (('limit_up', 'zt'), ('limit_down', 'dt'), ('broken_limit', 'zb')):
        if lu.get(pk) is not None:
            p[fk] = len(lu[pk]); src[fk] = _pool_src(lsrc.get(pk), em_label); at[fk] = iso_bj(lu_e.get('fetched_at'))
        elif (lu.get('errors') or {}).get(pk):
            notes[fk] = lu['errors'][pk][:200]
    prev = lu.get('prev') or {}
    if prev.get('avg_pct') is not None:
        p['yesterday_limit_premium_pct'] = prev['avg_pct']
        ps = lsrc.get('prev')
        src['yesterday_limit_premium_pct'] = ('东方财富 push2ex 昨日涨停股今日表现（平均涨幅）' if ps == em_label else f'备用：{ps}')
        at['yesterday_limit_premium_pct'] = iso_bj(lu_e.get('fetched_at'))
        notes.setdefault('yesterday_limit_premium_pct', f"样本 {prev.get('n')} 只，今日仍涨停 {prev.get('still_zt')} 只")
    elif (lu.get('errors') or {}).get('prev'):
        notes['yesterday_limit_premium_pct'] = lu['errors']['prev'][:200]

    # ---- turnover + indices: 腾讯 qt.gtimg.cn / ifzq ----
    ix_e = get('index')
    ix = ix_e.get('data') or {}
    if ix.get('turnover_yi') is not None:
        p['turnover_yi'] = ix['turnover_yi']
        src['turnover_yi'] = '腾讯 qt.gtimg.cn（沪A指数000002 + 深A指数399107 成交额合计，不含北交所）'
        at['turnover_yi'] = iso_bj(ix_e.get('fetched_at'))
    tc = ix.get('turnover_cmp') or {}
    if tc.get('chg_pct') is not None:
        p['turnover_vs_prev_pct'] = tc['chg_pct']
        src['turnover_vs_prev_pct'] = f"腾讯 ifzq 分时：截至 {tc.get('hhmm')} 与上一交易日 {tc.get('prev_day')} 同时刻对比"
        at['turnover_vs_prev_pct'] = iso_bj(ix_e.get('fetched_at'))
    elif tc.get('error'):
        notes['turnover_vs_prev_pct'] = f"同时刻对比未取到：{tc['error']}"
    if ix.get('rows'):
        p['indices'] = {r['name']: {'price': r.get('price'), 'pct': r.get('pct'), 'time': r.get('time')} for r in ix['rows']}
        src['indices'] = '腾讯 qt.gtimg.cn 指数快照'; at['indices'] = iso_bj(ix_e.get('fetched_at'))
        data_time['indices'] = ix.get('data_time')

    # ---- sectors: only what the 涨停池 itself says (industry counts); no new detection ----
    if lu.get('zt'):
        cnt = {}
        for r in lu['zt']:
            if r.get('industry'):
                cnt[r['industry']] = cnt.get(r['industry'], 0) + 1
        if cnt:
            p['sectors'] = [{'name': k, 'limit_up_count': v} for k, v in sorted(cnt.items(), key=lambda kv: -kv[1])[:10]]
            src['sectors'] = '涨停池行业分布（按涨停池个股所属行业计数，非板块涨幅）'; at['sectors'] = at.get('limit_up')

    if not src:
        return None
    missing = [k for k in FIELDS if p[k] is None]
    fetch_times = [v for v in at.values() if v]
    srcs = []
    if any(k in src for k in ('limit_up', 'limit_down', 'broken_limit', 'yesterday_limit_premium_pct')):
        srcs.append('东方财富 push2ex' if any('东方财富' in (src.get(k) or '') for k in ('limit_up', 'limit_down', 'broken_limit')) else '同花顺备用')
    if any('同花顺' in (v or '') for v in src.values()) and '同花顺备用' not in srcs:
        srcs.append('同花顺备用')
    if 'advancers' in src:
        srcs.append('东方财富 涨跌家数' if '东方财富' in src['advancers'] else '乐咕乐股')
    if 'turnover_yi' in src or 'indices' in src:
        srcs.append('腾讯 qt.gtimg.cn / ifzq')
    ref = None
    if sentiment:
        try:
            s = sentiment()
            ref = {'label': '盯盘情绪分档（a-share-watch 规则，系统判定）', 'tier': s.get('tier'), 'stage_mode': s.get('stage_mode'),
                   'one_line': s.get('one_line'), 'position': (s.get('position') or {}).get('text'), 'must_empty': s.get('must_empty'),
                   'buy_points_active': s.get('buy_points_active'), 'buy_points_paused': s.get('buy_points_paused')}
        except Exception as e:  # reference only; never blocks the feed
            ref = {'label': '盯盘情绪分档', 'error': f'{type(e).__name__}: {e}'[:120]}
    return {
        **p,
        'source': ' / '.join(srcs)[:200],
        'timestamp': max(fetch_times) if fetch_times else None,
        'data_quality': PUBLIC_QUALITY,
        'trade_date': trade_date or (data_time.get('indices') or '')[:10].replace('-', '') or None,
        'data_time': data_time,
        'field_sources': src,
        'field_fetched_at': at,
        'field_notes': notes,
        'missing_fields': missing,
        'reference': ref,
    }
