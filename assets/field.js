/* harvest field page — живое поле. Только если есть #fieldGrid. */
(function(){
"use strict";
if(!document.getElementById("fieldGrid"))return;
const hv=window.hv;
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const toast=hv.toast, esc=hv.esc, fmt=hv.fmt;
const setPill=hv.pill;
let lang=hv.lang();
hv.onLang(()=>{lang=hv.lang();renderLive(window.hvLast);try{drawLive();}catch(e){}});

/* ---------- live pulpit (state.json) ---------- */
let live={actions:[],errors:[],proc:[]};
const FALLBACK_NETS=[
 {slug:"vibevibe",name:"vibe/vibe",chain_id:46630,currency:"ETH",faucet:"https://faucet.testnet.chain.robinhood.com/claim",explorer:"https://explorer.testnet.chain.robinhood.com",tagline:{ru:"Permissionless launchpad на Robinhood Chain",en:"Permissionless launchpad on Robinhood Chain"},description:{ru:"Любой может запустить токен и торговать им на bonding curve. Условия платформы меняются, гарантий нет — только практика.",en:"Anyone can launch a token and trade it on the bonding curve. Platform rules change, no guarantees — practice only."},rewards:{ru:"Правила платформы · без гарантий",en:"Platform rules · no guarantees"},activities:{ru:["Покупка/продажа на bonding curve (buy/sell)","Стейкинг и клейм наград (stake/claimRewards)","Трансферы между кошельками"],en:["Bonding-curve buy/sell","Staking and reward claims (stake/claimRewards)","Wallet-to-wallet transfers"]}},
 {slug:"robinhood",name:"Robinhood Chain",chain_id:46630,currency:"ETH",faucet:"https://faucet.testnet.chain.robinhood.com/claim",explorer:"https://explorer.testnet.chain.robinhood.com",tagline:{ru:"Базовый L2-тестнет Robinhood",en:"Base Robinhood L2 testnet"},description:{ru:"Arbitrum Orbit L2 от Robinhood. Базовые активности: трансферы, взаимодействие с контрактами (swap/mint), стейкинг.",en:"Robinhood Arbitrum Orbit L2. Basics: transfers, contract calls (swap/mint), staking."},rewards:{ru:"Публичных гарантий нет · сеть для практики",en:"No public guarantees · practice network"},activities:{ru:["Трансферы между кошельками","Торговля на bonding curve (swap/mint)","Стейкинг / клейм наград"],en:["Wallet transfers","Bonding-curve trading (swap/mint)","Staking / reward claims"]}},
 {slug:"flop",name:"Flop Labs",chain_id:99999,currency:"FLOP",faucet:"",explorer:"",tagline:{ru:"Launchpad Flop Labs",en:"Flop Labs launchpad"},description:{ru:"Платформа запуска токенов. Активности: compute, validate, stake — отрабатываются контрактными вызовами.",en:"Token launch platform. Activities: compute, validate, stake via contract calls."},rewards:{ru:"Заглушка · наград не обещаем",en:"Stub · no rewards promised"},activities:{ru:["Compute / validate / stake","Трансферы"],en:["Compute / validate / stake","Transfers"]}},
 {slug:"arc",name:"Arc (Minara.Fun)",chain_id:5042002,currency:"ARC",faucet:"",explorer:"https://explorer.testnet.arc.network",tagline:{ru:"Launchpad Minara.Fun",en:"Minara.Fun launchpad"},description:{ru:"Платформа с запуском токенов, торговлей и ликвидностью. Активности: launch, trade, add_liquidity.",en:"Token launches, trading and liquidity. Activities: launch, trade, add_liquidity."},rewards:{ru:"Правила платформы · без гарантий",en:"Platform rules · no guarantees"},activities:{ru:["Запуск / торговля / добавление ликвидности","Трансферы"],en:["Launch / trade / add liquidity","Transfers"]}}
];
async function loadState(){
  try{const r=await fetch("state.json?t="+Date.now(),{cache:"no-store"});if(!r.ok)throw 0;return await r.json();}
  catch(e){return null;}
}
let gridPainted=false;
const _noMotion=()=>typeof matchMedia!=="undefined"&&matchMedia("(prefers-reduced-motion: reduce)").matches;
const _hasRAF=()=>typeof requestAnimationFrame==="function";
function setNum(id,raw){
  const el=$("#"+id);if(!el)return;
  const v=Number(raw);
  if(!isFinite(v)){el.textContent="—";el.dataset.raw="0";return;}
  const from=parseFloat(el.dataset.raw||"0")||0;el.dataset.raw=String(v);
  if(_noMotion()||!_hasRAF()||!isFinite(from)){el.textContent=fmt(v);return;}
  const t0=performance.now();
  const step=t=>{const k=Math.min(1,(t-t0)/600),e=1-Math.pow(1-k,3);
    el.textContent=fmt(from+(v-from)*e);if(k<1)requestAnimationFrame(step);};
  requestAnimationFrame(step);
}
function cellName(c){
  const names={i:{ru:"спит",en:"idle"},w:{ru:"работает",en:"working"},o:{ru:"успех",en:"ok"},e:{ru:"ошибка",en:"error"},r:{ru:"отдыхает",en:"rest"}};
  // Символ ячейки приходит с сервера: без проверки собственного свойства names["__proto__"]
  // или names["constructor"] вернул бы чужой объект, а не подпись состояния.
  const n=Object.prototype.hasOwnProperty.call(names,c)?names[c]:names.i;
  return lang==="en"?n.en:n.ru;
}
function renderGrid(cells,n,running){
  const g=$("#fieldGrid");
  const show=String(cells||"").slice(0,400);
  g.innerHTML=show.split("").map((c,i)=>
    `<span class="cell c-${"iwoer".includes(c)?c:"i"}" data-i="${i}" tabindex="-1" role="img" aria-label="#${i+1} · ${cellName(c)}"></span>`).join("");
  g.classList.toggle("flash",!gridPainted&&show.length>0);
  gridPainted=gridPainted||show.length>0;
  const counts={i:0,w:0,o:0,e:0,r:0};
  for (const ch of show) { if (counts[ch] !== undefined) counts[ch]++; }
  g.setAttribute("aria-label",
    (lang==="en"?"wallets grid":"сетка кошельков") +
    `: ${show.length}` +
    (lang==="en"?` (ok ${counts.o}, work ${counts.w}, err ${counts.e})`:` (ok ${counts.o}, work ${counts.w}, err ${counts.e})`));
  const fm=$("#fieldMore");if(fm)fm.textContent=n>400?`+${n-400}`:"";
}
let gridDetails=[];
function cellDetail(i){
  const d=gridDetails[i];
  if(!d||typeof d.a!=="number")return "";
  const cyc=(d.c===null||d.c===undefined||d.c<0)?"—":String(d.c);
  const base=lang==="en"
    ?` · ${d.a} actions / ${d.t} tries · last cycle ${cyc}`
    :` · действий ${d.a} / попыток ${d.t} · цикл ${cyc}`;
  if(!d.e)return base;
  return base+(lang==="en"?` · last error: ${d.e}`:` · ошибка: ${d.e}`);
}
window.cellDetail=cellDetail;
function lastCycleText(hist){
  if(!hist.length)return "—";
  const c=hist[0];
  const dur=(Math.round(Number(c.duration_s||0)*100)/100);
  return lang==="en"
    ?`cycle ${c.id} · ${dur}s · ${c.wallets||0} wallets · ${c.errors||0} errors`
    :`цикл ${c.id} · ${dur} с · ${c.wallets||0} кошельков · ${c.errors||0} ошибок`;
}
function renderLive(s){
  document.body.classList.remove("is-loading");
  if(!s){
    const retryBtn='<button class="btn small" id="liveRetry" type="button">'+(lang==="en"?"retry":"повторить")+"</button>";
    const la=$("#liveAlert");
    if(la)la.innerHTML='<div class="alert" role="alert">'+(lang==="en"?"No snapshot yet.":"Снимка пока нет.")+" "+retryBtn+"</div>";
    const rb=$("#liveRetry");
    if(rb)rb.addEventListener("click",()=>{refetch();});
    const fe=$("#fieldEmpty");if(fe)fe.hidden=false;
    const fg=$("#fieldGrid");if(fg)fg.style.display="none";
    const fl=$("#fieldLegend");if(fl)fl.style.display="none";
    const nets=$("#nets");if(nets)nets.innerHTML=FALLBACK_NETS.map(netCard).join("");
    const lc=$("#lastCycle");if(lc)lc.hidden=true;
    paintTicker(null);
    setPill("mut",lang==="en"?"offline":"офлайн");return;
  }
  const la=$("#liveAlert");if(la)la.innerHTML="";
  // Протухший снимок — не «всё ок»: стенд молчит дольше обычной паузы
  // публикации (30 мин + запас). Непарсируемая дата — без баннера.
  const sa=$("#staleAlert");
  if(sa){
    let age=-1;
    try{
      const t=Date.parse(String(s.updated_at||"").replace(" ","T").replace(" UTC","Z").replace("UTC","Z"));
      age=isFinite(t)?Date.now()-t:-1;
    }catch(e){age=-1;}
    if(age>15*60*1000){
      sa.hidden=false;
      sa.innerHTML='<div class="alert" role="alert">'+esc(lang==="en"
        ?"Stale data: snapshot "+(s.updated_at||"—")+" — the rig is silent."
        :"Данные устарели: снимок "+(s.updated_at||"—")+" — стенд молчит.")+"</div>";
    }else{sa.hidden=true;sa.innerHTML="";}
  }
  const pool=s.pool||{},db=s.db||{};
  const total=(pool.actions??0)+(pool.errors??0);
  const rate=total>0?(pool.actions/total*100):null;
  setNum("cActions",pool.actions);setNum("cProcessed",pool.processed);
  setNum("cErrors",pool.errors);setNum("cCycles",pool.cycles);setNum("cWallets",s.wallet_count);
  const el=(id,t)=>{const n=$("#"+id);if(n)n.textContent=t;};
  el("sActions",(lang==="en"?"per cycle ":"за цикл ")+fmt((pool.actions??0)/Math.max(pool.cycles||1,1)));
  el("sProcessed",(lang==="en"?"of ":"из ")+fmt(s.wallet_count));
  el("sErrors",(lang==="en"?"dropped ":"дроп ")+fmt(pool.dropped??0));
  el("sCycles",(lang==="en"?"history ":"история ")+fmt(db.cycles));
  el("sRate",rate===null?(lang==="en"?"success —":"успех —"):(lang==="en"?"success ":"успех ")+rate.toFixed(1)+"%");
  const dot=$("#dot"),st=$("#status");
  const Lst=lang==="en"?{off:"stopped",pause:"paused",on:"running"}:{off:"стоп",pause:"пауза",on:"работает"};
  if(dot&&st){
    if(!s.running){dot.className="dot mut";st.textContent=Lst.off;setPill("mut",Lst.off);}
    else if(pool.paused){dot.className="dot pau";st.textContent=Lst.pause;setPill("pau",Lst.pause);}
    else{dot.className="dot on";st.textContent=Lst.on;setPill("on",Lst.on);}
  }
  const term=$("#term");if(term)term.classList.toggle("live",!!s.running&&!pool.paused);
  el("net",s.network||"—");
  const hf=$("#health");if(hf){const h=Number(s.health_factor);hf.textContent=isFinite(h)?h.toFixed(2):"—";}
  el("updated",(lang==="en"?"updated: ":"обновлено: ")+(s.updated_at||"—"));
  const nets=(s.networks&&s.networks.length)?s.networks:FALLBACK_NETS;
  const netsBox=$("#nets");if(netsBox)netsBox.innerHTML=nets.map(n=>netCard(n,n.slug===s.network)).join("");
  paintTicker(nets);
  const hist0=s.history||[];
  const lc=$("#lastCycle");if(lc)lc.hidden=false;
  el("lastCycle",lastCycleText(hist0));
  const wc=s.wallet_cells||{};
  const cells=typeof wc.cells==="string"?wc.cells:"";
  const nWallets=wc.n||s.wallet_count||0;
  gridDetails=Array.isArray(wc.details)?wc.details:[];
  const hasData=cells.length>0||hist0.length>0;
  const fe=$("#fieldEmpty");if(fe)fe.hidden=hasData;
  const fg=$("#fieldGrid");if(fg)fg.style.display=cells?"":"none";
  const fl=$("#fieldLegend");if(fl)fl.style.display=cells?"":"none";
  renderGrid(cells,nWallets,!!s.running);
  // Графики — только из history (state.json ключа chart не несёт).
  live.actions=hist0.map(c=>c.actions_ok||0).slice(-60);
  live.errors=hist0.map(c=>c.errors||0).slice(-60);
  live.proc=hist0.map(c=>c.wallets||0).slice(-60);
  drawLive();
  const tw=$("#tWallets");
  if(tw)tw.innerHTML=(s.top_wallets||[]).map((w,i)=>
    `<tr><td class="num" style="color:var(--muted)">#${w.n??i+1}</td><td class="num">${fmt(w.actions)}</td></tr>`).join("")
    ||'<tr><td colspan="2" class="note">—</td></tr>';
  const th=$("#tHistory");
  if(th)th.innerHTML=(s.history||[]).map(c=>{const e=c.errors||0;
    const b=e===0?'<span class="pill ok">ok</span>':`<span class="pill warn">${esc(e)}</span>`;
    const dur=Number(c.duration_s);const durS=isFinite(dur)?dur.toFixed(1):"—";
    return `<tr><td class="num" style="color:var(--muted)">${esc(c.id)}</td><td>${b}</td><td class="num">${fmt(c.actions_ok)}</td><td class="num">${esc(e)}</td><td class="num">${durS}</td><td class="num">${esc(c.wallets)}</td></tr>`;}).join("")
    ||'<tr><td colspan="6" class="note">—</td></tr>';
  renderPnl(s.pnl);
}
function renderPnl(p){
  const box=$("#pnlBox");
  if(!box)return;
  if(!p){box.hidden=true;return;}
  box.hidden=false;
  const ok=Number(p.actions_ok||0),fail=Number(p.actions_fail||0);
  const rate=(ok+fail)>0?(ok/(ok+fail)*100):null;
  setNum("pnlOk",ok);
  setNum("pnlFail",fail);
  const el=(id,t)=>{const n=$("#"+id);if(n)n.textContent=t;};
  el("pnlRate",rate===null?"—":(lang==="en"?"success ":"успех ")+rate.toFixed(1)+"%");
  const farm=Number(p.farm_delta),faucet=Number(p.faucet_delta);
  const setDelta=(id,v)=>{
    const n=$("#"+id);if(!n)return;
    if(!isFinite(v)){n.textContent="—";n.className="v";return;}
    n.textContent=(v>0?"+":"")+v.toFixed(6);
    n.className="v "+(v>0?"ok":(v<0?"neg":""));
  };
  setDelta("pnlFarm",farm);
  setDelta("pnlFaucet",faucet);
  el("pnlSample",(lang==="en"?"sample ":"выборка ")+(Number(p.measured||0)+"/"+Number(p.total_wallets||0)));
  el("pnlWindow",(lang==="en"?"window ":"окно ")+fmt(p.window_actions));
  el("pnlWhen",(lang==="en"?"at ":"от ")+(p.created_at||"—"));
  const tt=$("#tPnlTypes");
  if(tt)tt.innerHTML=(p.by_type||[]).map(t=>
    `<tr><td>${esc(t.type||"?")}</td><td class="num">${fmt(t.ok)}</td><td class="num">${fmt(t.fail)}</td></tr>`).join("")
    ||'<tr><td colspan="3" class="note">—</td></tr>';
  const tr=$("#tPnlReasons");
  if(tr)tr.innerHTML=(p.by_reason||[]).map(r=>
    `<tr><td>${esc(r.reason||"?")}</td><td class="num">${fmt(r.count)}</td></tr>`).join("")
    ||'<tr><td colspan="2" class="note">—</td></tr>';
}
function netCard(n,cur){
  const L=lang==="en"?"en":"ru";
  const pick=(v,enV)=>{
    if(lang==="en"&&(typeof enV==="string"||Array.isArray(enV)))return enV;
    if(typeof v==="object"&&v!==null)return v[L]||v.ru||"";
    return v||"";
  };
  const acts=(pick(n.activities,n.activities_en)||[]).map(a=>"<li>"+esc(a)+"</li>").join("");
  const links=[n.faucet?'<a href="'+esc(n.faucet)+'" target="_blank" rel="noopener">faucet</a>':"",n.explorer?'<a href="'+esc(n.explorer)+'" target="_blank" rel="noopener">explorer</a>':""].filter(Boolean).join(" · ");
  return `<div class="net"><div><span class="name">${esc(n.name)}</span>${cur?' <span class="cur">'+(lang==="en"?"current":"текущая")+"</span>":""}</div>
    <div class="meta">chain ${esc(n.chain_id)} · ${esc(n.currency)}</div>
    <div class="tag">${esc(pick(n.tagline,n.tagline_en))}</div>
    <div class="desc">${esc(pick(n.description,n.description_en))}</div>
    <div class="rewards">${esc(pick(n.rewards,n.rewards_en)||"—")}</div><ul>${acts}</ul>${links?'<div class="toolbar">'+links+"</div>":""}</div>`;
}
function paintTicker(nets){
  try{
    const list=(nets&&nets.length?nets:FALLBACK_NETS).map(n=>`${n.name} ${n.chain_id}`.toUpperCase());
    const ver=(window.hv&&window.hv.version)||"v2.4.1";
    const ti=$("#tickerIn");if(ti)ti.textContent=("harvest "+ver+" · "+list.join(" · ")+" · MIT · ").repeat(4);
  }catch(e){}
}
function drawLive(){
  lineChart($("#chartMain"),[live.actions,live.errors]);
  lineChart($("#chartProc"),[live.proc]);
}
(function(){
  const tip=$("#cellTip"), grid=$("#fieldGrid");
  if(!tip||!grid)return;
  function show(el,x,y){tip.textContent=el.getAttribute("aria-label")||"";tip.hidden=false;
    try{
      tip.style.left=Math.min(x+12,window.innerWidth-200)+"px";tip.style.top=(y+16)+"px";
    }catch(e){}
  }
  function hide(){tip.hidden=true;}
  const cellOf=e=>(e.target&&e.target.closest)?e.target.closest(".cell"):null;
  grid.addEventListener("mousemove",e=>{const c=cellOf(e);if(!c){hide();return;}show(c,e.clientX,e.clientY);});
  grid.addEventListener("mouseleave",hide);
  grid.addEventListener("focusin",e=>{const c=cellOf(e);if(!c)return;const r=c.getBoundingClientRect();show(c,r.left,r.bottom);});
  grid.addEventListener("focusout",hide);
})();
function lineChart(cv,series){
  if(!cv)return;const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth||600,H=150;cv.width=W*dpr;cv.height=H*dpr;
  const x=cv.getContext("2d");x.scale(dpr,dpr);
  const cs=getComputedStyle(document.documentElement);
  const acc=cs.getPropertyValue("--gold").trim()||"#D4A72C";
  const amb=cs.getPropertyValue("--warn").trim()||"#E07B39";
  const grid=cs.getPropertyValue("--line").trim()||"#DDD6C2";
  const mut=cs.getPropertyValue("--muted").trim()||"#6F6A5C";
  x.clearRect(0,0,W,H);x.strokeStyle=grid;
  for(let i=1;i<4;i++){const y=6+(i/4)*(H-12);x.beginPath();x.moveTo(6,y);x.lineTo(W-6,y);x.stroke();}
  const cols=[acc,amb];
  series.forEach((data,si)=>{
    if(!data||data.length<2){return;}
    const max=Math.max(...data,1),min=Math.min(...data,0);
    const px=i=>6+(i/(data.length-1))*(W-12);
    const py=v=>H-6-((v-min)/Math.max(max-min,1e-9))*(H-12);
    x.beginPath();data.forEach((v,i)=>i?x.lineTo(px(i),py(v)):x.moveTo(px(i),py(v)));
    x.strokeStyle=cols[si%cols.length];x.lineWidth=si===0?2:1.4;x.stroke();
    if(si===0){x.lineTo(px(data.length-1),H);x.lineTo(px(0),H);x.closePath();x.globalAlpha=.12;x.fillStyle=cols[0];x.fill();x.globalAlpha=1;}
  });
  x.fillStyle=mut;x.font="11px monospace";x.textAlign="right";
  series.forEach((data,si)=>{
    if(!data||!data.length)return;
    x.fillStyle=cols[si%cols.length];
    x.fillText(fmt(data[data.length-1]),W-8,16+si*14);
  });
  x.textAlign="left";
  if(!series.some(d=>d&&d.length)){x.fillStyle=mut;x.font="12px monospace";x.fillText(lang==="en"?"no data":"нет данных",14,H/2);}
}
async function refetch(){
  const s=await loadState();window.hvLast=s;renderLive(s);
}
refetch();
/* Дальше обновления едут из common.js: он раз в минуту опрашивает
   state.json для пилюли в шапке и рассылает hv:state. Свой второй
   поллинг тут не нужен — иначе два запроса в минуту и гонка за hvLast. */
window.addEventListener("hv:state",e=>{if(e.detail!==undefined){window.hvLast=e.detail;renderLive(e.detail);}});
window.addEventListener("online",()=>{refetch();});
window.addEventListener("offline",()=>{
  const el=$("#liveAlert");
  if(el)el.innerHTML='<div class="alert" role="alert">'+(lang==="en"?"offline":"офлайн")+'</div>';
});
let _rzT=0;
window.addEventListener("resize",()=>{clearTimeout(_rzT);_rzT=setTimeout(()=>{drawLive();},150);});
window.hvRedraw=()=>{try{drawLive();}catch(e){}};

/* ---------- фильтры/зум/поиск/инспектор поля (только чтение DOM) ---------- */
(function(){
"use strict";
var filter="all",detail=true,query="",sel=-1;
function L(){return lang==="en"?"en":"ru";}
function cells(){return Array.prototype.slice.call(document.querySelectorAll("#fieldGrid .cell"));}
function apply(){
  var list=cells();
  list.forEach(function(el,i){
    var ok=true;
    if(filter!=="all")ok=el.classList.contains("c-"+filter);
    if(ok&&query)ok=String(i+1).indexOf(query)!==-1;
    el.style.display=ok?"":"none";
    el.classList.toggle("sel",i===sel);
  });
  var box=document.getElementById("fxInspector");
  if(box){
    if(sel>=0&&list[sel]){
      var cls=list[sel].className;
      var st=(cls.match(/c-([iwoer])/)||[])[1]||"i";
      var names={i:{ru:"спит",en:"idle"},w:{ru:"работает",en:"working"},o:{ru:"успех",en:"ok"},e:{ru:"ошибка",en:"error"},r:{ru:"отдыхает",en:"rest"}};
      var det="";
      try{if(window.cellDetail)det=window.cellDetail(sel)||"";}catch(_){}
      box.textContent="";
      var b=document.createElement("b");b.textContent="#"+(sel+1);box.appendChild(b);
      box.appendChild(document.createTextNode(" · "+names[st][L()]+det));
    }else{box.innerHTML='<span class="note">—</span>';}
  }
  var z=document.getElementById("fxZoom");
  if(z){
    z.textContent=detail?(L()==="en"?"PANORAMA":"ПАНОРАМА"):(L()==="en"?"250 · DETAIL":"250 · ДЕТАЛЬНО");
    z.setAttribute("aria-pressed",detail?"true":"false");
    z.setAttribute("aria-label",detail
      ?(L()==="en"?"Detailed view. Switch to panorama":"Детальный вид. Переключить на панораму")
      :(L()==="en"?"Panorama. Switch to detailed view":"Панорама. Переключить на детальный вид"));
  }
  var g=document.getElementById("fieldGrid");
  if(g){
    if(detail){g.classList.add("zoom");list.forEach(function(el,i){el.textContent=String(i+1);});}
    else{g.classList.remove("zoom");list.forEach(function(el){el.textContent="";});}
  }
  var q=document.getElementById("fxQ");
  if(q)q.placeholder=L()==="en"?"search #…" :"поиск #…";
}
document.addEventListener("click",function(e){
  var b=e.target.closest?e.target.closest("#fxFilters [data-f]"):null;
  if(b){filter=b.dataset.f;sel=-1;
    Array.prototype.forEach.call(document.querySelectorAll("#fxFilters [data-f]"),function(x){x.classList.remove("on");x.setAttribute("aria-pressed","false");});
    b.classList.add("on");b.setAttribute("aria-pressed","true");apply();writeUrl();return;}
  if(e.target.closest&&e.target.closest("#fxZoom")){detail=!detail;apply();writeUrl();return;}
  var c=e.target.closest?e.target.closest("#fieldGrid .cell"):null;
  if(c){var list=cells();sel=list.indexOf(c);apply();}
});
document.addEventListener("input",function(e){
  if(e.target&&e.target.id==="fxQ"){query=e.target.value.replace(/\D/g,"");apply();writeUrl();}
});
var gridEl=document.getElementById("fieldGrid");
if(gridEl)gridEl.addEventListener("keydown",function(e){
  var list=cells().filter(function(el){return el.style.display!=="none";});
  if(!list.length)return;
  var cols=1;
  try{
    var top=list[0].offsetTop;
    for(var i=1;i<list.length;i++){if(list[i].offsetTop!==top)break;cols=i;}
  }catch(_){cols=1;}
  var cur=list.indexOf(cells()[sel]);
  if(cur<0)cur=0;
  var n=null;
  if(e.key==="ArrowRight")n=Math.min(list.length-1,cur+1);
  else if(e.key==="ArrowLeft")n=Math.max(0,cur-1);
  else if(e.key==="ArrowDown")n=Math.min(list.length-1,cur+cols);
  else if(e.key==="ArrowUp")n=Math.max(0,cur-cols);
  else if(e.key==="Home")n=0;
  else if(e.key==="End")n=list.length-1;
  if(n!==null){e.preventDefault();sel=cells().indexOf(list[n]);apply();}
});
/* ---------- состояние в URL: фильтр/зум/поиск переживают перезагрузку и шарятся ссылкой ---------- */
var _FILTERS=["all","i","w","o","e","r"];
function readUrl(){
  var s="";try{s=location.search||"";}catch(e){}
  if(!s)return;
  // Ключи строки запроса читаем через URLSearchParams, а не складываем их в
  // объект: `?__proto__=…` или `?constructor=…` в прошлом варианте писал
  // свойство в прототип, и проверка ниже читала уже не то, что прислал адрес.
  var q=new URLSearchParams(s);
  var f=q.get("f"),z=q.get("z"),qq=q.get("q");
  if(f&&_FILTERS.indexOf(f)!==-1)filter=f;
  if(z==="1")detail=true;
  else if(z==="0")detail=false;
  if(qq&&/^\d+$/.test(qq))query=qq;
}
function writeUrl(){
  var parts=[];
  if(filter!=="all")parts.push("f="+filter);
  if(!detail)parts.push("z=0");
  if(query)parts.push("q="+encodeURIComponent(query));
  try{history.replaceState(null,"",location.pathname+(parts.length?"?"+parts.join("&"):"")+location.hash);}catch(e){}
}
function syncControls(){
  Array.prototype.forEach.call(document.querySelectorAll("#fxFilters [data-f]"),function(x){
    var on=x.dataset.f===filter;
    x.classList.toggle("on",on);x.setAttribute("aria-pressed",on?"true":"false");
  });
  var q=document.getElementById("fxQ");if(q)q.value=query;
}
new MutationObserver(apply).observe(gridEl,{childList:true});
readUrl();
syncControls();
apply();
/* Смена языка перерисовывает подписи кнопок через common.js,
   а текст зума/инспектора/плейсхолдера — только здесь. */
if(hv.onLang)hv.onLang(()=>{try{apply();}catch(e){}});
})();
})();
