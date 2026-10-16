/* HARVEST terminal — live wiring to state.json. No deps, no fake data. */
(function(){
"use strict";
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const fmt=n=>{n=Number(n);if(!isFinite(n))return "—";if(Math.abs(n)>=1e6)return(n/1e6).toFixed(2)+"M";if(Math.abs(n)>=1e3)return(n/1e3).toFixed(1)+"K";return String(Math.round(n*100)/100);};

const STR={
ru:{field:"ПОЛЕ",cells:"ячейки",all:"ВСЕ",networks:"СЕТИ",history:"ИСТОРИЯ ЦИКЛОВ",top:"ТОП КОШЕЛЬКОВ",
specs:"ФАКТЫ",faucet:"КРАН И ДИАГНОСТИКА",bot:"TELEGRAM-БОТ",search:"поиск #…",zoom_d:"250 ДЕТАЛЬНО",zoom_p:"ПАНОРАМА",
inspector:"ИНСПЕКТОР",num:"№",state:"состояние",actions:"действий",updated:"обновлено",nodata:"—",
st_i:"спит",st_w:"работает",st_o:"успех",st_e:"ошибка",st_r:"отдыхает",
empty:"state.json ещё не опубликован — ферма скоро загрузит метрики."},
en:{field:"FIELD",cells:"cells",all:"ALL",networks:"NETWORKS",history:"CYCLE HISTORY",top:"TOP WALLETS",
specs:"FACTS",faucet:"FAUCET & DIAGNOSTICS",bot:"TELEGRAM BOT",search:"search #…",zoom_d:"250 DETAIL",zoom_p:"PANORAMA",
inspector:"INSPECTOR",num:"#",state:"state",actions:"actions",updated:"updated",nodata:"—",
st_i:"idle",st_w:"working",st_o:"ok",st_e:"error",st_r:"rest",
empty:"state.json not published yet — the farm will upload metrics soon."}
};
let lang=(localStorage.getItem("hv-term-lang")||"ru");
if(!STR[lang])lang="ru";
const t=k=>STR[lang][k]||k;

let S=null, filter="all", zoom="250", sel=0, query="";

async function loadState(){
  try{const r=await fetch("state.json?t="+Date.now(),{cache:"no-store"});if(!r.ok)throw 0;return await r.json();}
  catch(_){return null;}
}
function cellList(){
  if(!S||!S.wallet_cells)return{cells:"",n:0};
  return{cells:String(S.wallet_cells.cells||""),n:Number(S.wallet_cells.n||0)};
}
function counts(){
  const{cells}=cellList();const c={i:0,w:0,o:0,e:0,r:0};
  for(const ch of cells)if(c[ch]!==undefined)c[ch]++;
  return c;
}
function renderTicker(){
  const el=$("#tick");if(!el)return;
  const net=S?(esc(S.network||"—")):"—";
  const upd=S?esc(S.updated_at||"—"):"—";
  el.innerHTML=("HARVEST v2.3.0 · "+net.toUpperCase()+" · <span id='tickTests'>1421 TESTS</span> · MIT · "+t("updated")+": "+upd+" ·&nbsp;").repeat(2);
}
function renderHead(){
  const pill=$("#livePill");
  if(!S){pill.innerHTML='<span class="pill dim">STOP</span>';return;}
  const run=!!S.running;
  pill.innerHTML='<span class="pill '+(run?"ok":"dim")+'">'+(run?"RUN":"STOP")+"</span>";
  $("#netLine").textContent=(S.network||"—")+" · health "+(S.health_factor??"—");
}
function renderCells(){
  const box=$("#cells");if(!box)return;
  const{cells,n}=cellList();
  const idx=[];
  for(let i=0;i<cells.length;i++){
    const ch=cells[i];
    if(filter!=="all"&&ch!==filter)continue;
    if(query){if(!String(i+1).includes(query))continue;}
    idx.push(i);
  }
  const cap=zoom==="250"?250:400;
  const shown=idx.slice(0,cap);
  box.className="cells inset"+(zoom==="250"?" detail":"");
  let h="";
  for(const i of shown){
    const ch=cells[i];
    h+='<button class="cell s'+ch+(i===sel?" sel":"")+'" data-i="'+i+'" title="#'+(i+1)+" "+t("st_"+ch)+'"></button>';
  }
  box.innerHTML=h||('<span class="note">'+t("empty")+"</span>");
  const c=counts();
  $("#statLine").innerHTML=
    t("cells")+": <b>"+n+"</b> · i "+c.i+" · w "+c.w+" · o "+c.o+" · e "+c.e+" · r "+c.r+
    (n>cells.length?" · +"+(n-cells.length):"");
  renderInspector();
}
function renderInspector(){
  const{cells,n}=cellList();
  const el=$("#inspector");if(!el)return;
  if(!cells.length||sel<0||sel>=cells.length){el.innerHTML='<span class="note">'+t("nodata")+"</span>";return;}
  const ch=cells[sel];
  el.innerHTML='<dl class="kv"><dt>'+t("num")+'</dt><dd>#'+(sel+1)+(n>cells.length?" / "+n:"")+"</dd>"+
    "<dt>"+t("state")+'</dt><dd><span class="dot" style="background:'+({i:"#3a4356",w:"#00f0ff",o:"#ccff00",e:"#ff0055",r:"#f59e0b"}[ch]||"#888")+'"></span>'+t("st_"+ch)+"</dd></dl>";
}
function renderStats(){
  const el=$("#stats");if(!el||!S)return;
  const p=S.pool||{},db=S.db||{};
  const rows=[
    [lang==="ru"?"Действий":"Actions",fmt(p.actions??db.actions),lang==="ru"?"Циклов":"Cycles",fmt(p.cycles??db.cycles)],
    [lang==="ru"?"Ошибок":"Errors",fmt(p.errors??db.errors),lang==="ru"?"Кошельков":"Wallets",fmt(S.wallet_count)],
  ];
  el.innerHTML=rows.map(r=>"<tr><td>"+r[0]+'</td><td class="num">'+r[1]+"</td><td>"+r[2]+'</td><td class="num">'+r[3]+"</td></tr>").join("");
}
function renderNets(){
  const el=$("#nets");if(!el||!S||!S.networks)return;
  el.innerHTML=S.networks.map(n=>{
    const cur=(S.network&&n.slug===S.network)?' <span class="pill ok">●</span>':"";
    const links=[n.faucet?'<a href="'+esc(n.faucet)+'" target="_blank" rel="noopener">faucet</a>':"",
                 n.explorer?'<a href="'+esc(n.explorer)+'" target="_blank" rel="noopener">explorer</a>':""].filter(Boolean).join(" · ");
    return '<div class="inset" style="padding:8px 10px"><b>'+esc(n.name)+"</b> · "+esc(n.chain_id)+" · "+esc(n.currency)+cur+
      '<div class="note">'+esc(n.tagline||"")+"</div>"+
      (links?'<div style="font-size:12px">'+links+"</div>":"")+"</div>";
  }).join("");
}
function renderTables(){
  if(!S)return;
  const th=$("#tHistory"),tw=$("#tWallets");
  if(th)th.innerHTML=(S.history||[]).map(h=>"<tr><td class='num'>"+esc(h.id)+"</td><td class='num'>"+esc(h.actions_ok)+"</td><td class='num'>"+esc(h.errors)+"</td><td class='num'>"+esc(h.duration_s!=null?Number(h.duration_s).toFixed(1):"—")+"</td><td class='num'>"+esc(h.wallets)+"</td></tr>").join("")||'<tr><td colspan="5" class="note">'+t("nodata")+"</td></tr>";
  if(tw)tw.innerHTML=(S.top_wallets||[]).map(w=>"<tr><td class='num'>#"+esc(w.n)+"</td><td class='num'>"+esc(w.actions)+"</td></tr>").join("")||'<tr><td colspan="2" class="note">'+t("nodata")+"</td></tr>";
}
function renderSpecs(){
  const el=$("#specTests");if(el)el.textContent="1421 tests · 56 files";
}
function applyLang(){
  document.querySelectorAll("[data-l]").forEach(e=>{e.textContent=t(e.dataset.l);});
  $("#q").placeholder=t("search");
  renderTicker();renderHead();renderCells();renderStats();renderNets();renderTables();renderSpecs();
}
async function refresh(){
  S=await loadState();
  if(!S){$("#liveAlert").innerHTML='<div class="inset" style="padding:8px 12px">'+t("empty")+"</div>";}
  else{$("#liveAlert").innerHTML="";}
  renderTicker();renderHead();renderCells();renderStats();renderNets();renderTables();renderSpecs();
}
document.addEventListener("DOMContentLoaded",()=>{
  $("#langRu").onclick=()=>{lang="ru";localStorage.setItem("hv-term-lang",lang);applyLang();};
  $("#langEn").onclick=()=>{lang="en";localStorage.setItem("hv-term-lang",lang);applyLang();};
  $$("#filters .btn").forEach(b=>b.onclick=()=>{filter=b.dataset.f;$$("#filters .btn").forEach(x=>x.classList.remove("on"));b.classList.add("on");sel=0;renderCells();});
  $("#zoomBtn").onclick=()=>{zoom=zoom==="250"?"1000":"250";$("#zoomBtn").textContent=zoom==="250"?t("zoom_d"):t("zoom_p");renderCells();};
  $("#q").oninput=e=>{query=e.target.value.replace(/\D/g,"");renderCells();};
  $("#cells").addEventListener("click",e=>{const b=e.target.closest("[data-i]");if(!b)return;sel=Number(b.dataset.i);renderCells();});
  applyLang();refresh();
  setInterval(refresh,60000);
});
})();
