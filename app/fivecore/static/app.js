const $=id=>document.getElementById(id);
const EMBED=location.pathname.startsWith('/fivecore');
const TKEY=EMBED?'asw_t':'fivecore_t';
const U=new URL(location.href);let TK=U.searchParams.get('t');if(TK)localStorage.setItem(TKEY,TK);else TK=localStorage.getItem(TKEY)||'';
async function api(path,opt={}){opt.headers=Object.assign({'X-Token':TK,'Content-Type':'application/json'},opt.headers||{});const r=await fetch(path,opt);let d;try{d=await r.json();}catch(e){d={detail:r.status===401?'需要访问令牌（请使用带 ?t= 的完整链接打开）':'响应解析失败'};}if(!r.ok){const e=new Error(d.detail||('HTTP '+r.status));e.status=r.status;throw e;}return d;}
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const NA='<span class="na">未取到</span>';
const v=(x,suf='')=>x===null||x===undefined?NA:esc(x)+suf;
const t2=s=>s?esc(String(s).replace('T',' ').replace(/\+08:00$/,'').slice(0,19)):NA;
function tick(){$('clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'});}tick();setInterval(tick,1000);
const FIELDS=[['advancers','上涨家数',''],['decliners','下跌家数',''],['unchanged','平盘家数',''],['limit_up','涨停家数',''],['limit_down','跌停家数',''],['broken_limit','炸板家数',''],['yesterday_limit_premium_pct','昨涨停今平均涨幅','%'],['turnover_yi','两市成交额（沪A+深A）',' 亿'],['turnover_vs_prev_pct','成交额较前日同时刻','%'],['indices','主要指数',''],['sectors','涨停行业分布（前10）','']];
function fmtField(k,m){const x=m[k];if(x===null||x===undefined)return NA;if(k==='indices')return Object.entries(x).map(([n,o])=>`${esc(n)} ${esc(o.price)}（${o.pct>0?'+':''}${esc(o.pct)}%）`).join('<br>');if(k==='sectors')return x.map(s=>`${esc(s.name)} ${esc(s.limit_up_count)}`).join('、');if(k==='turnover_vs_prev_pct')return (x>0?'+':'')+esc(x)+'%';const f=FIELDS.find(f=>f[0]===k);return esc(x)+(f?f[2]:'');}
function renderMarket(m){
  const fs=m.field_sources||{},fa=m.field_fetched_at||{},fn=m.field_notes||{};
  $('fieldRows').innerHTML=FIELDS.map(([k,l])=>`<tr><td>${l}</td><td class="v">${fmtField(k,m)}</td><td class="src">${fs[k]?esc(fs[k]):NA}${fn[k]?'<br><span class="na">'+esc(fn[k])+'</span>':''}</td><td class="src">${fa[k]?t2(fa[k]):NA}</td></tr>`).join('');
  $('environment').innerHTML=v(m.environment);$('sentiment').innerHTML=v(m.sentiment);
  $('breadthLine').innerHTML='上涨占比 '+(m.breadth_pct==null?NA:esc(m.breadth_pct)+'%');
  $('premiumLine').innerHTML='昨涨停今表现 '+(m.yesterday_limit_premium_pct==null?NA:esc(m.yesterday_limit_premium_pct)+'%');
  $('limitCounts').innerHTML=`${v(m.limit_up)} / ${v(m.limit_down)}`;$('breakRate').innerHTML=m.broken_limit_rate_pct==null?NA:esc(m.broken_limit_rate_pct)+'%';
  const r=m.reference;$('refBox').innerHTML=r&&r.tier?`<div><b>${esc(r.tier)}</b> · ${esc(r.stage_mode)}</div><div>${esc(r.one_line||'')}</div><div>仓位：${esc(r.position||'未取到')}</div><div>启用买点：${esc((r.buy_points_active||[]).join('、')||'—')}</div><div>暂停买点：${esc((r.buy_points_paused||[]).join('、')||'—')}</div><div class="na" style="font-size:11px">${esc(r.label)}</div>`:NA;
}
async function refresh(){$('refresh').disabled=true;try{const [h,m,c]=await Promise.all([api('api/health'),api('api/market/latest'),api('api/config')]);
  $('apiStatus').textContent=h.status==='ok'?'正常':'异常';$('apiStatus').className=h.status==='ok'?'ok':'';
  $('providerStatus').textContent=h.market_feed_cn||h.market_feed;$('lastFetch').innerHTML=t2(h.last_fetch);
  $('ingestStatus').textContent=h.ingest_enabled?'已启用（需写入密钥）':'未启用';
  const b=$('banner');if(m.available&&m.banner){b.innerHTML=esc(m.banner)+`<small>${esc(m.data_quality_cn||'')} · 交易日 ${esc(m.trade_date||'未取到')}</small>`;b.classList.add('show');}else b.classList.remove('show');
  if(m.available){$('feedStatus').textContent=m.feed==='licensed'?'授权行情':'公开行情（非授权）';$('feedStatus').className='pill'+(m.feed==='licensed'?'':' muted');
    const miss=(m.missing_fields||[]).length;$('notice').innerHTML=`<b>行情快照 · ${esc(m.source||'')}</b><span>抓取时间 ${t2(m.timestamp)}（北京时间） · 交易日 ${esc(m.trade_date||'未取到')} · ${miss?('未取到字段 '+miss+' 项'):'全部字段已取到'}</span>`;
    $('asof').textContent='数据质量：'+(m.data_quality_cn||m.data_quality);renderMarket(m);}
  else{$('feedStatus').textContent='行情未接入';$('feedStatus').className='pill muted';$('notice').innerHTML=`<b>暂无行情快照</b><span>${esc(m.message||'')}</span>`;}
}catch(e){$('apiStatus').textContent=e.status===401?'需要访问令牌':'连接失败';$('notice').innerHTML=`<b>${e.status===401?'需要访问令牌':'连接失败'}</b><span>${esc(e.message)}</span>`;}finally{$('refresh').disabled=false;}}
$('refresh').addEventListener('click',refresh);refresh();setInterval(refresh,30000);
const TRI=[['is_mainline','属于当前主线'],['is_leader_or_capacity_core','龙头 / 容量核心'],['absorption_improving','承接改善'],['sector_confirmation','板块交叉验证通过'],['entry_trigger','预设买点触发'],['unacceptable_t1_risk','存在不可接受的 T+1 风险']];
$('tri').innerHTML=TRI.map(([k,l])=>`<label>${l}<select id="j_${k}"><option value="">未提供</option><option value="1">是</option><option value="0">否</option></select></label>`).join('');
const mark=x=>x===true?'<span class="y">✓ 通过</span>':x===false?'<span class="n">✗ 未通过</span>':'<span class="u">未知 / 未提供</span>';
$('analyze').addEventListener('click',async()=>{const code=$('code').value.trim();if(!/^\d{6}$/.test(code)){alert('请输入6位股票代码');return;}
  const body={code,name:$('name').value||null,price:$('price').value?Number($('price').value):null,change_pct:$('change').value?Number($('change').value):null};
  TRI.forEach(([k])=>{const x=$('j_'+k).value;body[k]=x===''?null:x==='1';});
  const out=$('stockResult');out.classList.remove('hidden');out.textContent='分析中…';
  try{const d=await api('api/analyze/stock',{method:'POST',body:JSON.stringify(body)});
    if(d.name)$('name').value=d.name;if(d.price!=null)$('price').value=d.price;if(d.change_pct!=null)$('change').value=d.change_pct;
    const L=d.check_labels||{},js=d.judgment_source||{};const jmap={mainline:'is_mainline',leader:'is_leader_or_capacity_core',absorption:'absorption_improving',sector_validation:'sector_confirmation',entry:'entry_trigger',t1_risk:'unacceptable_t1_risk'};
    const rows=Object.entries(d.checks).map(([k,x])=>`<div>${esc(L[k]||k)}${js[jmap[k]]&&!['用户提供','未提供'].includes(js[jmap[k]])?' <span class="na">（'+esc(js[jmap[k]])+'）</span>':''}</div><div>${mark(x)}</div>`).join('');
    const h=d.system_hint;const hint=h?`<p>${esc(h.label)}：${esc(h.day)} ${esc(h.buy_point||'—')} · ${esc(h.conclusion||'—')} · ${esc(h.reason||'')}${h.note?'（'+esc(h.note)+'）':''}</p>`:'';
    const q=d.autofill?`<p>名称/价格/涨跌幅：腾讯 qt.gtimg.cn，行情时间 ${esc(d.quote_time||'未取到')} · ${esc(d.board||'')}</p>`:'';
    out.innerHTML=`<h4>${esc(d.name||d.code)}（${esc(d.code)}） · ${esc(d.status)} · ${d.score}/${d.max_score}</h4><p>现价 ${v(d.price)} · 涨跌幅 ${v(d.change_pct,'%')}</p><div class="ck">${rows}</div>${(d.notes||[]).map(n=>'<p>'+esc(n)+'</p>').join('')}${hint}${q}<p>市场快照：${esc(d.market?.environment)} / ${esc(d.market?.sentiment)} · ${esc(d.market?.data_quality_cn||'')}</p><p>${esc(d.reason)}</p>`;}
  catch(e){out.innerHTML=`<h4>暂不可分析</h4><p>${esc(e.message)}</p>`;}});
if('serviceWorker'in navigator)navigator.serviceWorker.register('sw.js').catch(()=>{});
