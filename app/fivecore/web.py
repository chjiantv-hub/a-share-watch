"""FiveCore mounted inside a-share-watch under /fivecore/ (same ACCESS_TOKEN middleware protects it)."""
import hmac, json, os, sqlite3, threading, datetime as dt
from pathlib import Path
from typing import Any
from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from pydantic import BaseModel, Field
from .engine import analyze_market, analyze_stock, quality_cn, LICENSED_PREFIX, INSUFFICIENT
from .feed import build_public_payload, PUBLIC_QUALITY, BJ

VERSION = '0.2.0'
STATIC = Path(__file__).resolve().parent / 'static'
BANNER_PUBLIC = '公开网页行情（非授权 Level-1），仅供研究参考'
INGEST_PATH = '/fivecore/api/ingest/market'      # exempt from ACCESS_TOKEN; guarded by its own token


class MarketPayload(BaseModel):
    """External ingest (for a future licensed feed). Missing numbers must be null, not 0."""
    source: str = Field(min_length=1, max_length=120)
    timestamp: str
    data_quality: str = 'provider_unverified'
    advancers: int | None = None
    decliners: int | None = None
    unchanged: int | None = None
    limit_up: int | None = None
    limit_down: int | None = None
    broken_limit: int | None = None
    yesterday_limit_premium_pct: float | None = None
    turnover_yi: float | None = None
    turnover_vs_prev_pct: float | None = None
    indices: dict[str, Any] | None = None
    sectors: list[dict[str, Any]] | None = None
    field_sources: dict[str, str] | None = None


class StockIn(BaseModel):
    """Judgment fields are tri-state: true / false / null(未提供)."""
    code: str
    name: str | None = None
    price: float | None = None
    change_pct: float | None = None
    is_mainline: bool | None = None
    is_leader_or_capacity_core: bool | None = None
    absorption_improving: bool | None = None
    sector_confirmation: bool | None = None
    entry_trigger: bool | None = None
    unacceptable_t1_risk: bool | None = None


def create_router(ctx: dict) -> APIRouter:
    """ctx: get, ref_day, em_label, sentiment, data_dir, quote(code)->dict|None, valid_code, board_of, nows, today."""
    r = APIRouter()
    db_path = os.path.join(ctx['data_dir'], 'fivecore.sqlite3')
    lock = threading.Lock()

    def ingest_token():
        t = (os.environ.get('FIVECORE_INGEST_TOKEN') or os.environ.get('INGEST_TOKEN') or '').strip()
        f = os.path.join(ctx['data_dir'], 'fivecore_ingest_token.txt')
        if not t and os.path.exists(f):
            t = open(f).read().strip()
        return t

    def db():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        c.execute('CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, kind TEXT, payload TEXT, created_at TEXT)')
        return c

    def latest_ingest():
        with lock:
            c = db()
            try:
                row = c.execute("SELECT payload, created_at FROM snapshots WHERE kind='market' ORDER BY id DESC LIMIT 1").fetchone()
            finally:
                c.close()
        if not row:
            return None
        d = json.loads(row['payload']); d['_received_at'] = row['created_at']
        return d

    def public_payload():
        return build_public_payload(ctx['get'], ctx['ref_day'], ctx['em_label'], ctx.get('sentiment'))

    def select_feed():
        """licensed ingest > in-process public feed > unverified ingest > nothing."""
        ing = latest_ingest()
        if ing and str(ing.get('data_quality', '')).startswith(LICENSED_PREFIX):
            return 'licensed', ing
        pub = public_payload()
        if pub:
            return PUBLIC_QUALITY, pub
        if ing:
            return 'ingest_unverified', ing
        return 'not_configured', None

    def market_view():
        feed, raw = select_feed()
        if not raw:
            return {'available': False, 'feed': feed,
                    'message': '尚未取到行情（后台首次拉取中或数据源失败）。当前没有真实行情，不输出交易结论。'}
        res = analyze_market(raw)
        res.update({
            'available': True, 'feed': feed, 'source': raw.get('source'),
            'trade_date': raw.get('trade_date'), 'data_time': raw.get('data_time') or {},
            'indices': raw.get('indices'), 'sectors': raw.get('sectors'),
            'field_sources': raw.get('field_sources') or {}, 'field_fetched_at': raw.get('field_fetched_at') or {},
            'field_notes': raw.get('field_notes') or {},
            'missing_fields': raw.get('missing_fields') or [k for k in ('advancers', 'decliners', 'unchanged', 'limit_up', 'limit_down',
                              'broken_limit', 'yesterday_limit_premium_pct', 'turnover_yi', 'turnover_vs_prev_pct') if raw.get(k) is None],
            'reference': raw.get('reference'),
            'banner': BANNER_PUBLIC if feed == PUBLIC_QUALITY else (None if feed == 'licensed' else '数据源未核验，仅供研究参考'),
        })
        return res

    # ---------------- pages ----------------
    @r.get('/fivecore', include_in_schema=False)
    def no_slash(request: Request):
        q = ('?' + request.url.query) if request.url.query else ''
        return RedirectResponse('/fivecore/' + q, status_code=307)

    @r.get('/fivecore/', response_class=HTMLResponse)
    def home():
        html = (STATIC / 'index.html').read_text(encoding='utf-8')
        # inline CSS/JS so the page needs only the one token-checked request
        html = html.replace('<link rel="stylesheet" href="static/style.css">', '<style>' + (STATIC / 'style.css').read_text(encoding='utf-8') + '</style>')
        html = html.replace('<script src="static/app.js"></script>', '<script>' + (STATIC / 'app.js').read_text(encoding='utf-8') + '</script>')
        return HTMLResponse(html)

    @r.get('/fivecore/sw.js', include_in_schema=False)
    def sw():
        return Response((STATIC / 'sw.js').read_text(encoding='utf-8'), media_type='application/javascript')

    @r.get('/fivecore/manifest.webmanifest', include_in_schema=False)
    def manifest():
        return JSONResponse({'name': '五核智选', 'short_name': '五核智选', 'start_url': './', 'scope': './', 'display': 'standalone',
                             'background_color': '#080e19', 'theme_color': '#0b1220', 'lang': 'zh-CN',
                             'icons': [{'src': '/icon.png', 'sizes': '512x512', 'type': 'image/png'}]}, media_type='application/manifest+json')

    # ---------------- API ----------------
    @r.get('/fivecore/api/health')
    def health():
        feed, raw = select_feed()
        return {'status': 'ok', 'app': 'FiveCore', 'version': VERSION, 'host': 'a-share-watch', 'market_feed': feed,
                'market_feed_cn': {'not_configured': '未接入', PUBLIC_QUALITY: BANNER_PUBLIC, 'licensed': '授权行情（外部写入）',
                                   'ingest_unverified': '外部写入（未标注授权）'}[feed],
                'last_fetch': (raw or {}).get('timestamp') if feed == PUBLIC_QUALITY else (raw or {}).get('_received_at'),
                'trade_date': (raw or {}).get('trade_date'),
                'ingest_enabled': bool(ingest_token()),
                'server_time': dt.datetime.now(BJ).isoformat()}

    @r.get('/fivecore/api/config')
    def config():
        return {'mode': 'embedded-in-a-share-watch', 'version': VERSION, 'auth': 'ACCESS_TOKEN（与盯盘相同）',
                'live_feed_configured': True, 'ingest_enabled': bool(ingest_token()),
                'decision_policy': 'rules-first; no auto-trading; missing = 未知',
                'market_feed': '内置：a-share-watch 公开网页行情（public_web_unlicensed）；外部授权源可经 ingest 写入并优先使用'}

    @r.get('/fivecore/api/market/latest')
    def latest_market():
        return market_view()

    @r.post('/fivecore/api/ingest/market')
    def ingest_market(payload: MarketPayload, x_ingest_token: str = Header(default='')):
        tok = ingest_token()
        if not tok or not hmac.compare_digest(x_ingest_token.encode(), tok.encode()):
            raise HTTPException(status_code=401, detail='行情写入接口未授权（需正确的 X-Ingest-Token；服务器须配置 FIVECORE_INGEST_TOKEN）。')
        data = payload.model_dump()
        with lock:
            c = db()
            try:
                c.execute('INSERT INTO snapshots(kind,payload,created_at) VALUES(?,?,?)',
                          ('market', json.dumps(data, ensure_ascii=False), dt.datetime.now(BJ).isoformat()))
                c.execute("DELETE FROM snapshots WHERE id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT 500)")
                c.commit()
            finally:
                c.close()
        return {'accepted': True, 'source': payload.source, 'timestamp': payload.timestamp, 'data_quality': payload.data_quality,
                'used_as_primary': payload.data_quality.startswith(LICENSED_PREFIX)}

    @r.post('/fivecore/api/analyze/stock')
    def stock_analysis(stock: StockIn):
        code = (stock.code or '').strip()
        if not ctx['valid_code'](code):
            raise HTTPException(status_code=400, detail='股票代码须为6位数字。')
        m = market_view()
        if not m.get('available'):
            raise HTTPException(status_code=503, detail='没有可用的真实市场快照，暂不能进行五核判断。')
        try:
            q = ctx['quote'](code)
        except Exception:
            q = None
        if not q:
            raise HTTPException(status_code=404, detail=f'未查到 {code} 的行情，请核对代码。')
        s = stock.model_dump()
        autofill = {'code': '代码有效（腾讯行情可查到）',
                    'name': '腾讯 qt.gtimg.cn', 'price': '腾讯 qt.gtimg.cn', 'change_pct': '腾讯 qt.gtimg.cn'}
        ignored = {k: s[k] for k in ('name', 'price', 'change_pct') if s.get(k) not in (None, '')}
        s.update({'code': code, 'name': q.get('name'), 'price': q.get('price'), 'change_pct': q.get('pct')})
        provided = {k: ('用户提供' if s.get(k) is not None else '未提供') for k in
                    ('is_mainline', 'is_leader_or_capacity_core', 'absorption_improving', 'sector_confirmation', 'entry_trigger', 'unacceptable_t1_risk')}
        # 系统判定（a-share-watch 竞价规则）：仅当当日实时竞价结论存在、且用户未填写时，作为买点证据；复盘/非当日只作参考
        hint = None
        au = (ctx['get']('auction').get('data') or {})
        row = next((x for x in (au.get('rows') or []) if x.get('code') == code), None)
        if row:
            live_today = (not au.get('replay')) and au.get('day') == ctx['today']()
            hint = {'label': '系统判定（a-share-watch规则）', 'day': au.get('day'), 'buy_point': row.get('buy_point'),
                    'conclusion': row.get('conclusion'), 'reason': row.get('reason'), 'tag': row.get('tag'),
                    'applied': False, 'note': ''}
            if s.get('entry_trigger') is None and live_today and row.get('conclusion') == '可建仓' and row.get('buy_point') not in (None, '—'):
                s['entry_trigger'] = True; provided['entry_trigger'] = '系统判定（a-share-watch规则）'; hint['applied'] = True
            elif not live_today:
                hint['note'] = f"该结论为 {au.get('day')} 复盘数据，非今日实时竞价，仅作参考，不计分。"
            elif s.get('entry_trigger') is not None:
                hint['note'] = '已采用用户填写的买点判断。'
        res = analyze_stock(s, m)
        res.update({'quote_time': q.get('time'), 'board': ctx['board_of'](code, q.get('name'))[0], 'autofill': autofill,
                    'judgment_source': provided, 'system_hint': hint,
                    'ignored_user_values': ignored or None,
                    'market': {k: m.get(k) for k in ('environment', 'sentiment', 'feed', 'data_quality', 'data_quality_cn', 'timestamp', 'trade_date', 'banner')}})
        return res

    return r
