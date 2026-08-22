/* leftside_shared.js — 两个筛选器共用的看板功能 (信号回测 / 错杀候选 / 优质公司 / 排序 / 交易日志)
   由 stock-core 维护, 各仓库发布时复制到 dashboard/ 与 docs/。页面在主脚本末尾调用
   LS.init({$, t, isNum, escH, tagClass, tagText, dash, getData, openDetail, getCur, market})。 */
window.LS = window.LS || {};
LS.init = function(ctx){
  const {$, t, isNum, escH, tagClass, tagText, dash, getData, openDetail, getCur, market} = ctx;
  // ---------- 📊 信号回测 ----------
  function btOpen(code){
    const c=(getData().candidates||[]).find(x=>x.code===code);
    if(c) openDetail(c);
  }
  function renderBacktest(){
    const B = window.__BT__;
    const card = $("#btCard");
    if(!card) return;
    if(!B || !B.agg || !B.agg.pool || !B.agg.pool.n_resolved){ card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    const P=B.agg.pool, M=B.meta||{};
    const pct=(x,d)=>isNum(x)?(x*100).toFixed(d==null?0:d)+"%":dash;
    $("#btMeta").textContent = `${M.first_day} → ${M.last_day} · ${M.n_days}${t("bt_days")} · ${t("bt_hz")}${M.horizon}${t("bt_hz2")}`;
    const stat=(lab,val,sub,col)=>`<div class="rounded-lg p-2.5" style="background:rgba(148,163,184,.07)"><div class="text-[11px] text-slate-400">${lab}</div><div class="text-xl font-bold ${col||"text-white"}">${val}</div>${sub?`<div class="text-[10px] text-slate-500">${sub}</div>`:""}</div>`;
    $("#btStats").innerHTML =
      stat(t("bt_n"), P.n_resolved, `${t("bt_n_open")} ${P.n_open||0}`) +
      stat(t("bt_fill"), pct(P.fill_rate)) +
      stat(t("bt_win"), pct(P.win10), t("bt_win_sub"), P.win10>=0.6?"text-emerald-300":"text-amber-300") +
      stat(t("bt_ret"), (P.avg_ret>0?"+":"")+pct(P.avg_ret,1), t("bt_ret_sub"), P.avg_ret>0?"text-emerald-300":"text-rose-300") +
      stat(t("bt_days_med"), isNum(P.med_days)?P.med_days:dash, t("bt_days_sub"));
    const rows = Object.entries(B.agg.by_tag||{}).sort((a,b)=>(b[1].n_resolved||0)-(a[1].n_resolved||0));
    const th=`<tr class="text-slate-400 text-[11px]"><th class="text-left py-1">${t("bt_col_tag")}</th><th class="text-right">${t("bt_col_n")}</th><th class="text-right">${t("bt_col_win")}</th><th class="text-right">${t("bt_col_r5")}</th><th class="text-right">${t("bt_col_ret")}</th><th class="text-right">${t("bt_col_d")}</th></tr>`;
    $("#btTagTbl").innerHTML = th + rows.map(([k,s])=>{
      const low=(s.n_resolved||0)<12;
      const wc=!isNum(s.win10)?"":(s.win10>=(B.agg.p0||0)?"text-emerald-300":"text-rose-300");
      const rc=!isNum(s.avg_ret)?"":(s.avg_ret>0?"text-emerald-300":"text-rose-300");
      return `<tr class="border-t border-slate-700/40 ${low?"opacity-60":""}"><td class="py-1"><span class="badge ${tagClass(k)}">${escH(tagText(k))}</span>${low?` <span class="text-[10px] text-slate-500">${t("bt_low_n")}</span>`:""}</td><td class="text-right">${s.n_resolved||0}</td><td class="text-right ${wc}">${pct(s.win10)}</td><td class="text-right">${pct(s.reach5)}</td><td class="text-right ${rc}">${isNum(s.avg_ret)?((s.avg_ret>0?"+":"")+pct(s.avg_ret,1)):dash}</td><td class="text-right">${isNum(s.med_days)?s.med_days:dash}</td></tr>`;
    }).join("");
    const dim=(lab,s)=> (s&&s.n_resolved)?`<span class="badge tag-watch" title="n=${s.n_resolved}">${lab} ${pct(s.win10)} <span class="text-[10px] opacity-70">n${s.n_resolved}</span></span>`:"";
    const O=B.agg.by_opp||{}, G=B.agg.by_growth||{}, CS=B.agg.by_cuosha||{};
    $("#btDims").innerHTML =
      [dim(t("bt_opp_hot"),O.hot), dim(t("bt_opp_mid"),O.mid), dim(t("bt_opp_cold"),O.cold),
       dim(t("bt_g_G"),G.G), dim(t("bt_g_M"),G.M), dim(t("bt_g_W"),G.W),
       dim(t("bt_cs"),CS.cs), dim(t("bt_cselig"),CS.elig), dim(t("bt_noncs"),CS.other),
       dim(t("bt_reg_bull"),(B.agg.by_regime||{}).bull), dim(t("bt_reg_bear"),(B.agg.by_regime||{}).bear)].filter(Boolean).join("");
    const rec=(B.recos||[]).slice(0,20);
    // 胜率是"信号类型"的历史频率而非个股预测: 按类型分组, 类型只标一次, 个股显示各自综合分
    const groups={};
    rec.forEach(r=>{ const key=(r.seg_kind==="combo")? `${r.tag}|${r.growth}` : (r.tag||"?"); (groups[key]=groups[key]||{r, items:[]}).items.push(r); });
    $("#btRecos").innerHTML = rec.length? Object.values(groups).map(g=>{
      const r=g.r;
      const lab=(r.seg_kind==="combo")? `${escH(tagText(r.tag))} × ${t("bt_g_"+r.growth)}` : escH(tagText(r.tag));
      return `<div class="w-full"><div class="text-xs text-slate-300 mb-1">${t("bt_reco_seg")} <span class="badge ${tagClass(r.tag)}">${lab}</span> ${t("bt_reco_hist")} <b class="text-emerald-300">${pct(r.seg_win_post)}</b> <span class="text-slate-500">(n=${r.seg_n} · ${t("bt_reco_vs")} ${pct(B.agg.p0)})</span></div><div class="flex flex-wrap gap-2">`+
        g.items.map(x=>`<span class="badge tag-strong cursor-pointer" onclick="btOpen('${escH(x.code)}')" title="${t("bt_reco_tip2")}">${escH(x.code)} ${escH(String(x.name||"").slice(0,10))} <span class="text-[10px] opacity-70">${t("composite")} ${isNum(x.fs)?x.fs.toFixed(1):dash}</span></span>`).join("")+`</div></div>`;
    }).join("") : `<span class="text-xs text-slate-500">${t("bt_reco_none")}</span>`;
    const rc2=(B.recent||[]).slice(0,10);
    $("#btRecent").innerHTML = rc2.length? `<div class="text-xs text-slate-400 mb-1">${t("bt_recent")}</div><div class="flex flex-wrap gap-2">`+rc2.map(e=>{
      const ic=e.status==="won"?"✅":(e.status==="stopped"?"⛔":"⏳");
      const rr=isNum(e.ret)?((e.ret>0?"+":"")+(e.ret*100).toFixed(1)+"%"):dash;
      return `<span class="badge ${(e.ret>0)?"tag-strong":"tag-warn"}" title="${e.sig_date||""} → ${e.exit_date||""}">${ic} ${escH(e.code)} ${rr}</span>`;
    }).join("")+`</div>` : "";
    $("#btNote").textContent = t("bt_note");
  }
  // ---------- 表格排序 (点击列头; Shift+点击 追加次级键) ----------
  const SORTS = { cs: [{k:"score",dir:"desc"}], ql: [{k:"n_pass",dir:"desc"},{k:"score",dir:"desc"}] };
  const RERENDER = { cs: ()=>renderCuosha(), ql: ()=>renderQuality() };
  function sortRows(rows, keys, get){
    return rows.slice().sort((a,b)=>{
      for(const {k,dir} of keys){
        const va=get(a,k), vb=get(b,k);
        const na=(va==null||va!==va), nb=(vb==null||vb!==vb);
        if(na&&nb) continue; if(na) return 1; if(nb) return -1;
        if(va<vb) return dir==="asc"?-1:1; if(va>vb) return dir==="asc"?1:-1;
      }
      return 0;
    });
  }
  function sortClick(which, k, ev){
    const arr=SORTS[which]; const i=arr.findIndex(x=>x.k===k);
    if(ev&&ev.shiftKey){ if(i>=0) arr[i].dir=(arr[i].dir==="desc"?"asc":"desc"); else arr.push({k,dir:"desc"}); }
    else if(i===0){ arr[0].dir=(arr[0].dir==="desc"?"asc":"desc"); }
    else { SORTS[which]=[{k,dir:"desc"}]; }
    RERENDER[which]();
  }
  window.sortClick = sortClick;
  function sortTh(which, k, label, align){
    const arr=SORTS[which]; const i=arr.findIndex(x=>x.k===k);
    const mark = i<0? "" : ` <span class="text-sky-300">${arr[i].dir==="desc"?"▼":"▲"}${arr.length>1?(i+1):""}</span>`;
    return `<th class="${align||"text-left"} py-1 cursor-pointer select-none hover:text-sky-300" title="${t("sort_hint")}" onclick="sortClick('${which}','${k}',event)">${label}${mark}</th>`;
  }
  const p20cell=(v,n,tip)=> isNum(v)? `<span class="${v>=50?"text-emerald-300":(v>=30?"text-amber-300":"text-slate-300")}" title="${tip} · n=${n||dash}">${v.toFixed(0)}%</span>` : dash;
  // ---------- 💎 错杀候选 ----------
  const csGet=(c,k)=>({score:c.cuosha_score, dd:c.cuosha_dd, expl:c.cuosha_expl, g:c._g, up:c.cuosha_upside, p20:c.cuosha_p20, name:c.code, ind:c.industry, tag:c.tag})[k];
  function renderCuosha(){
    const card=$("#csCard"); if(!card) return;
    let hits=(getData().candidates||[]).filter(c=>isNum(c.cuosha_score));
    if(!hits.length){ card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    hits.forEach(c=>{ const qoq=(c.ni_qoq||[]).filter(isNum); c._g = qoq.length? qoq.slice(-2).reduce((a,b)=>a+b,0)/Math.min(2,qoq.length) : null; });
    hits = sortRows(hits, SORTS.cs, csGet);
    $("#csMeta").textContent = `${t("cs_meta_a")}${hits.length}${t("cs_meta_b")}`;
    const th=`<tr class="text-slate-400 text-[11px]"><th class="text-left py-1">#</th>${sortTh("cs","name",t("cs_col_stock"))}${sortTh("cs","ind",t("cs_col_ind"))}${sortTh("cs","score",t("cs_col_score"),"text-right")}${sortTh("cs","dd",t("cs_col_dd"),"text-right")}${sortTh("cs","expl",t("cs_col_expl"),"text-right")}${sortTh("cs","g",t("cs_col_g"),"text-right")}${sortTh("cs","up",t("cs_col_up"),"text-right")}${sortTh("cs","p20",t("cs_col_p20"),"text-right")}${sortTh("cs","tag",t("cs_col_tag"),"text-left pl-3")}</tr>`;
    $("#csTbl").innerHTML = th + hits.slice(0,20).map((c,i)=>{
      const g=c._g;
      return `<tr class="border-t border-slate-700/40 hover:bg-slate-700/20 cursor-pointer" onclick="btOpen('${escH(c.code)}')">`+
        `<td class="py-1.5 text-slate-500">${i+1}</td>`+
        `<td>${(isNum(c.earn_days)&&c.earn_days<=7)?`<span title="${t("earn_tip")} · ${escH(c.earn_date||"")}">📅</span> `:""}${(c.news_flags&&c.news_flags.length)?`<span class="text-rose-300" title="${t("news_flag_tip")}: ${escH(c.news_flags.join(" / "))}">🚩</span> `:""}<b>${escH(String(c.name||"").slice(0,26))}</b> <span class="font-mono text-xs text-slate-400">${escH(c.code)}</span></td>`+
        `<td class="text-slate-400 text-xs">${escH(c.industry||dash)}</td>`+
        `<td class="text-right font-bold text-amber-300" title="${escH(c.cuosha_note||"")}">${c.cuosha_score}</td>`+
        `<td class="text-right text-rose-300">${isNum(c.cuosha_dd)?c.cuosha_dd.toFixed(0)+"%":dash}</td>`+
        `<td class="text-right text-sky-300">${isNum(c.cuosha_expl)?c.cuosha_expl+"%":dash}</td>`+
        `<td class="text-right ${g>0?"text-emerald-300":"text-slate-400"}">${isNum(g)?(g>0?"+":"")+g.toFixed(0)+"%":dash}</td>`+
        `<td class="text-right ${c.cuosha_upside>0?"text-emerald-300 font-semibold":"text-slate-500"}">${(c.cuosha_upside>0)?"+"+c.cuosha_upside+"%":dash}</td>`+
        `<td class="text-right">${p20cell(c.cuosha_p20, c.cuosha_p20_n, t("p20_tip_cs"))}</td>`+
        `<td class="pl-3"><span class="badge ${tagClass(c.tag)}">${escH(tagText(c.tag))}</span></td></tr>`;
    }).join("");
    $("#csNote").innerHTML = escH(t("cs_note")+" "+t("p20_note")) + pickBtLine("cuosha");
  }
  // ---------- 👑 优质公司推荐 ----------
  function qlLink(code){ return market.qlLink(code); }
  const qlGet=(p,k)=>({score:p.score, pe:p.pe, roe:p.roe, q4:(p.ni_q4||[]).slice(-1)[0], y4:(p.ni_y4||[]).slice(-1)[0], dom:(isNum(p.dom_rank)? -p.dom_rank : null), rd:p.rd, p20:p.p20, n_pass:Object.values(p.gates||{}).filter(Boolean).length, name:p.code, ind:p.industry})[k];
  function renderQuality(){
    const card=$("#qlCard"); if(!card) return;
    const Q=window.__QL__;
    if(!Q||!Q.picks||!Q.picks.length){ card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    const M=Q.meta||{};
    $("#qlMeta").textContent = `${t("ql_meta_a")}${M.n_screened??dash}${t("ql_meta_b")}${M.n_pool??dash}${t("ql_meta_c")}${M.n_crown??0}${t("ql_meta_d")} · ${M.date||""}`;
    const GK=["q4","y4","beat","roe","pe","dom"].filter(k=> Q.picks.some(p=>p.gates&&(k in p.gates)));
    const picks = sortRows(Q.picks, SORTS.ql, qlGet);
    const th=`<tr class="text-slate-400 text-[11px]"><th class="text-left py-1">#</th>${sortTh("ql","name",t("ql_col_stock"))}${sortTh("ql","ind",t("ql_col_ind"))}${sortTh("ql","score",t("ql_col_score"),"text-right")}${sortTh("ql","n_pass",t("ql_col_gates"),"text-left pl-3")}${sortTh("ql","pe","PE","text-right")}${sortTh("ql","roe","ROE%","text-right")}${sortTh("ql","q4",t("ql_col_q4"),"text-right")}${sortTh("ql","y4",t("ql_col_y4"),"text-right")}${sortTh("ql","dom",t("ql_col_dom"),"text-left pl-2")}${sortTh("ql","rd",t("ql_col_rd"),"text-right")}${sortTh("ql","p20",t("ql_col_p20"),"text-right")}</tr>`;
    $("#qlTbl").innerHTML = th + picks.map((p,i)=>{
      const crown = GK.every(k=>p.gates&&p.gates[k]) ? "👑 " : "";
      const gates = GK.map(k=>{ const ok=p.gates&&p.gates[k]; return `<span class="badge ${ok?"tag-strong":"tag-watch"} !text-[10px] !px-1.5 ${ok?"":"opacity-50"}" title="${t("ql_g_"+k)}">${ok?"✓":"✗"}${t("ql_g_"+k)}</span>`; }).join(" ");
      const q4=(p.ni_q4||[]).map(v=>(v>0?"+":"")+v.toFixed(0)).join("›");
      const y4=(p.ni_y4||[]).map(v=>(v>0?"+":"")+v.toFixed(0)).join("›");
      const domt = isNum(p.dom_rank)? `${market.domFmt(p.dom_rank)}${isNum(p.dom_share)?` · ${p.dom_share}%`:""}` : dash;
      return `<tr class="border-t border-slate-700/40 hover:bg-slate-700/20">`+
        `<td class="py-1.5 text-slate-500">${i+1}</td>`+
        `<td><a href="${qlLink(p.code)}" target="_blank" rel="noopener" class="hover:underline">${crown}<b>${escH(String(p.name||"").slice(0,26))}</b> <span class="font-mono text-xs text-slate-400">${escH(p.code)} ↗</span></a>${p.accel?` <span title="${t("ql_accel")}">⚡</span>`:""}</td>`+
        `<td class="text-slate-400 text-xs">${escH(p.industry||dash)}</td>`+
        `<td class="text-right font-bold text-emerald-300">${p.score??dash}</td>`+
        `<td class="pl-3">${gates}</td>`+
        `<td class="text-right ${isNum(p.pe)&&p.pe<31?"text-emerald-300":"text-slate-400"}">${isNum(p.pe)?p.pe:dash}</td>`+
        `<td class="text-right ${isNum(p.roe)&&p.roe>=15?"text-emerald-300":"text-slate-400"}">${isNum(p.roe)?p.roe:dash}</td>`+
        `<td class="text-right text-xs">${q4||dash}</td>`+
        `<td class="text-right text-xs">${y4||dash}</td>`+
        `<td class="pl-2 text-xs">${domt}</td>`+
        `<td class="text-right">${isNum(p.rd)?p.rd+"%":(p.rd_exempt?t("ql_rd_na"):dash)}</td>`+
        `<td class="text-right">${p20cell(p.p20, p.p20_n, t("p20_tip_ql"))}</td></tr>`;
    }).join("");
    $("#qlNote").innerHTML = escH(t("ql_note")+" "+t("p20_note")) + pickBtLine("quality");
  }
  function pickBtLine(which){
    const P=(window.__BT__&&window.__BT__.picks_bt)? window.__BT__.picks_bt[which] : null;
    if(!P||!P.n) return "";
    const pc=(v,d)=>isNum(v)?((v>0?"+":"")+v.toFixed(d==null?1:d)+"%"):dash;
    const col=v=>!isNum(v)?"text-slate-400":(v>0?"text-emerald-300":"text-rose-300");
    const seg=(lab,v,n)=>`<span class="mr-3">${lab}<b class="${col(v)}">${pc(v)}</b>${n?` <span class="text-slate-500">n${n}</span>`:""}</span>`;
    const done = P.n30>0;
    return `<div class="mt-2 text-[12px] text-slate-300 border-t border-slate-700/40 pt-2"><span class="text-slate-400">${t("pick_bt_h")}</span> <b>${P.n}</b>${t("pick_bt_n")} · `+
      seg(t("pick_bt_r10"),P.r10,P.n10)+seg(t("pick_bt_r30"),P.r30,P.n30)+seg(t("pick_bt_r60"),P.r60,P.n60)+
      (done? `<span class="mr-3">${t("pick_bt_hit")}<b class="text-sky-300">${isNum(P.hit20)?P.hit20.toFixed(0)+"%":dash}</b></span><span>${t("pick_bt_beat")}<b class="text-sky-300">${isNum(P.beat30)?P.beat30.toFixed(0)+"%":dash}</b></span>` : `<span class="text-slate-500">${t("pick_bt_wait")}</span>`)+
      `</div>`;
  }

  // ---------- 📒 交易日志 / 模拟组合 (localStorage, 本机浏览器) ----------
  const JN_KEY="leftside_journal_v1";
  function jnLoad(){ try{ return JSON.parse(localStorage.getItem(JN_KEY)||"[]"); }catch(e){ return []; } }
  function jnSave(arr){ try{ localStorage.setItem(JN_KEY, JSON.stringify(arr)); }catch(e){} }
  function jnPrice(code){ const c=(getData().candidates||[]).find(x=>x.code===code); return c&&isNum(c.price)? c.price : null; }
  function jnAdd(c){
    const p=c.plan||{}; const tgt=(p.targets&&p.targets.base)||{};
    const entry = isNum(p.entry_ref)? p.entry_ref : c.price;
    const rec={ id:Date.now().toString(36), code:c.code, name:c.name||"", tag:c.tag||"",
      open_date:(getData().meta&&getData().meta.data_date)||new Date().toISOString().slice(0,10),
      entry:entry, stop:isNum(p.stop_price)?p.stop_price:null, target:isNum(tgt.price)?tgt.price:null,
      size_pct:null, status:"open", note:"" };
    const s=prompt(t("jn_prompt_entry"), entry!=null?String(entry):""); if(s===null) return;
    const v=parseFloat(s); if(isNum(v)) rec.entry=v;
    const sz=prompt(t("jn_prompt_size"), ""); if(sz!==null&&sz.trim()!==""){ const z=parseFloat(sz); if(isNum(z)) rec.size_pct=z; }
    const arr=jnLoad(); arr.push(rec); jnSave(arr); renderJournal();
    $("#jnCard").scrollIntoView({behavior:"smooth",block:"start"});
  }
  function jnClose(id){
    const arr=jnLoad(); const r=arr.find(x=>x.id===id); if(!r) return;
    const live=jnPrice(r.code);
    const s=prompt(t("jn_prompt_exit"), live!=null?String(live):""); if(s===null) return;
    const v=parseFloat(s); if(!isNum(v)) return;
    const reason=prompt(t("jn_prompt_reason"), "");
    r.status="closed"; r.exit=v; r.close_date=(getData().meta&&getData().meta.data_date)||new Date().toISOString().slice(0,10); r.reason=(reason||"").slice(0,60);
    jnSave(arr); renderJournal();
  }
  function jnDel(id){ if(!confirm(t("jn_confirm_del"))) return; jnSave(jnLoad().filter(x=>x.id!==id)); renderJournal(); }
  function jnDays(a,b){ try{ return Math.round((new Date(b)-new Date(a))/86400000); }catch(e){ return null; } }
  window.jnClose=jnClose; window.jnDel=jnDel;
  function renderJournal(){
    const card=$("#jnCard"); if(!card) return;
    const arr=jnLoad(); const open=arr.filter(x=>x.status==="open"), closed=arr.filter(x=>x.status==="closed");
    $("#jnMeta").textContent = `${t("jn_meta_a")}${open.length}${t("jn_meta_b")}${closed.length}${t("jn_meta_c")}`;
    const pc=(v,d)=>isNum(v)?((v>0?"+":"")+v.toFixed(d==null?1:d)+"%"):dash;
    const col=v=>!isNum(v)?"":(v>0?"text-emerald-300":"text-rose-300");
    // 统计 (已平仓)
    const rets=closed.map(r=>isNum(r.entry)&&isNum(r.exit)&&r.entry>0? (r.exit/r.entry-1)*100 : null).filter(isNum);
    const wins=rets.filter(v=>v>0), losses=rets.filter(v=>v<=0);
    const avg=a=>a.length? a.reduce((x,y)=>x+y,0)/a.length : null;
    const holds=closed.map(r=>jnDays(r.open_date,r.close_date)).filter(isNum);
    const stopsHonored=closed.filter(r=>isNum(r.stop)&&isNum(r.exit)&&r.exit<=r.entry).filter(r=>r.exit>=r.stop*0.97).length;
    const stopsTotal=closed.filter(r=>isNum(r.stop)&&isNum(r.exit)&&r.exit<=r.entry).length;
    const chip=(lab,val,cls)=>`<span class="badge tag-watch">${lab} <b class="${cls||""}">${val}</b></span>`;
    $("#jnStats").innerHTML = closed.length? [
      chip(t("jn_s_win"), rets.length? (wins.length/rets.length*100).toFixed(0)+"%":dash, "text-sky-300"),
      chip(t("jn_s_avg"), pc(avg(rets)), col(avg(rets))),
      chip(t("jn_s_avgw"), pc(avg(wins)), "text-emerald-300"), chip(t("jn_s_avgl"), pc(avg(losses)), "text-rose-300"),
      chip(t("jn_s_hold"), holds.length? Math.round(avg(holds))+t("jn_days"):dash),
      chip(t("jn_s_stop"), stopsTotal? (stopsHonored/stopsTotal*100).toFixed(0)+"%":dash, "text-amber-300")
    ].join("") : `<span class="text-xs text-slate-500">${t("jn_empty_stats")}</span>`;
    // 持仓表
    const th1=`<tr class="text-slate-400 text-[11px]"><th class="text-left py-1">${t("jn_col_stock")}</th><th class="text-left">${t("jn_col_open")}</th><th class="text-right">${t("jn_col_entry")}</th><th class="text-right">${t("jn_col_now")}</th><th class="text-right">${t("jn_col_pnl")}</th><th class="text-right">${t("jn_col_stop")}</th><th class="text-right">${t("jn_col_target")}</th><th class="text-right">${t("jn_col_size")}</th><th class="text-right">${t("jn_col_days")}</th><th></th></tr>`;
    $("#jnOpen").innerHTML = open.length? th1+open.map(r=>{
      const live=jnPrice(r.code); const pnl=(isNum(live)&&isNum(r.entry)&&r.entry>0)? (live/r.entry-1)*100 : null;
      const nearStop=(isNum(live)&&isNum(r.stop)&&live<=r.stop); const hitTgt=(isNum(live)&&isNum(r.target)&&live>=r.target);
      return `<tr class="border-t border-slate-700/40 ${nearStop?"bg-rose-500/10":(hitTgt?"bg-emerald-500/10":"")}">`+
        `<td class="py-1.5"><b>${escH(r.name)}</b> <span class="font-mono text-xs text-slate-400">${escH(r.code)}</span> <span class="badge ${tagClass(r.tag)} !text-[10px]">${escH(tagText(r.tag))}</span></td>`+
        `<td class="text-xs text-slate-400">${escH(r.open_date||"")}</td><td class="text-right">${isNum(r.entry)?r.entry:dash}</td>`+
        `<td class="text-right">${isNum(live)?live:`<span class="text-slate-500" title="${t("jn_no_live")}">${dash}</span>`}</td>`+
        `<td class="text-right font-semibold ${col(pnl)}">${pc(pnl)}</td>`+
        `<td class="text-right ${nearStop?"text-rose-300 font-bold":""}">${isNum(r.stop)?r.stop:dash}${nearStop?" ⛔":""}</td>`+
        `<td class="text-right ${hitTgt?"text-emerald-300 font-bold":""}">${isNum(r.target)?r.target:dash}${hitTgt?" 🎯":""}</td>`+
        `<td class="text-right">${isNum(r.size_pct)?r.size_pct+"%":dash}</td><td class="text-right">${jnDays(r.open_date,(getData().meta&&getData().meta.data_date)||new Date().toISOString().slice(0,10))??dash}</td>`+
        `<td class="text-right whitespace-nowrap"><button class="segbtn" onclick="jnClose('${r.id}')">${t("jn_btn_close")}</button> <button class="segbtn" onclick="jnDel('${r.id}')">✕</button></td></tr>`;
    }).join("") : `<tr><td class="text-xs text-slate-500 py-2">${t("jn_empty_open")}</td></tr>`;
    // 已平仓表
    const th2=`<tr class="text-slate-400 text-[11px]"><th class="text-left py-1">${t("jn_col_stock")}</th><th class="text-left">${t("jn_col_open")}</th><th class="text-left">${t("jn_col_closed")}</th><th class="text-right">${t("jn_col_entry")}</th><th class="text-right">${t("jn_col_exit")}</th><th class="text-right">${t("jn_col_pnl")}</th><th class="text-right">${t("jn_col_days")}</th><th class="text-left pl-2">${t("jn_col_reason")}</th><th></th></tr>`;
    $("#jnClosed").innerHTML = closed.length? th2+closed.slice().reverse().map(r=>{
      const pnl=(isNum(r.entry)&&isNum(r.exit)&&r.entry>0)? (r.exit/r.entry-1)*100 : null;
      return `<tr class="border-t border-slate-700/40"><td class="py-1.5"><b>${escH(r.name)}</b> <span class="font-mono text-xs text-slate-400">${escH(r.code)}</span></td>`+
        `<td class="text-xs text-slate-400">${escH(r.open_date||"")}</td><td class="text-xs text-slate-400">${escH(r.close_date||"")}</td>`+
        `<td class="text-right">${isNum(r.entry)?r.entry:dash}</td><td class="text-right">${isNum(r.exit)?r.exit:dash}</td>`+
        `<td class="text-right font-semibold ${col(pnl)}">${pc(pnl)}</td><td class="text-right">${jnDays(r.open_date,r.close_date)??dash}</td>`+
        `<td class="pl-2 text-xs text-slate-400">${escH(r.reason||"")}</td><td class="text-right"><button class="segbtn" onclick="jnDel('${r.id}')">✕</button></td></tr>`;
    }).join("") : "";
    $("#jnNote").textContent = t("jn_note");
  }
  $("#jnExport").onclick = ()=>{ const blob=new Blob([JSON.stringify(jnLoad(),null,2)],{type:"application/json"}); const a=document.createElement("a"); a.href=URL.createObjectURL(blob); a.download="journal.json"; a.click(); };
  $("#jnImport").onchange = (e)=>{ const f=e.target.files&&e.target.files[0]; if(!f) return; const rd=new FileReader(); rd.onload=()=>{ try{ const arr=JSON.parse(rd.result); if(Array.isArray(arr)){ const cur=jnLoad(); const ids=new Set(cur.map(x=>x.id)); arr.forEach(x=>{ if(x&&x.id&&!ids.has(x.id)) cur.push(x); }); jnSave(cur); renderJournal(); } }catch(err){ alert("bad json"); } }; rd.readAsText(f); e.target.value=""; };
  $("#jnAddBtn").onclick = ()=>{ if(getCur()) jnAdd(getCur()); };
  window.btOpen = btOpen;
  LS.renderBacktest = renderBacktest; LS.renderCuosha = renderCuosha; LS.renderQuality = renderQuality;
  LS.renderJournal = renderJournal; LS.btOpen = btOpen;
  LS.ready = true;
};
