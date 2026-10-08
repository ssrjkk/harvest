/* HARVEST — фронтенд: сети, кнопки фарма, метрики, графики, кошельки. */

const $ = (id) => document.getElementById(id);
let actionsSeries = [], errorsSeries = [], procSeries = [];
let networks = [];
let stoppingRequested = false;
const refreshMs = 4000;

function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);}
function fmt(n){n=Number(n);if(!isFinite(n))return "—";if(Math.abs(n)>=1e6)return(n/1e6).toFixed(2)+"M";if(Math.abs(n)>=1e3)return(n/1e3).toFixed(1)+"K";return String(Math.round(n*100)/100);}
function isDark(){return document.documentElement.getAttribute("data-bs-theme")!=="light";}
function c1(){return isDark()?"#22d3ee":"#0891b2";}
function c2(){return isDark()?"#c084fc":"#9333ea";}
function gridC(){return isDark()?"rgba(255,255,255,0.06)":"rgba(10,15,30,0.06)";}

async function j(url,opts){
  const r=await fetch(url,opts);
  const body=await r.json().catch(()=>({}));
  if(r.status===401){location.href="/login";throw new Error("unauth");}
  if(!r.ok)throw new Error(body.error||"HTTP "+r.status);
  return body;
}

function applyTheme(t){t=t==="light"?"light":"dark";document.documentElement.setAttribute("data-bs-theme",t);localStorage.setItem("harvest_theme",t);
  if($("thDark"))$("thDark").classList.toggle("active",t==="dark");if($("thLight"))$("thLight").classList.toggle("active",t==="light");drawCharts();}
function initTheme(){applyTheme(localStorage.getItem("harvest_theme")||"dark");$("thDark")?.addEventListener("click",()=>applyTheme("dark"));$("thLight")?.addEventListener("click",()=>applyTheme("light"));}
function nav(name){document.querySelectorAll(".nav-item[data-nav]").forEach(el=>el.classList.toggle("active",el.getAttribute("data-nav")===name));
  document.querySelectorAll(".hx-section").forEach(s=>s.classList.toggle("active",s.id==="sec-"+name));}

function netMeta(n){
  return `chain ${n.chain_id} · ${esc(n.currency)}` + (n.faucet?` · <a href="${esc(n.faucet)}" target="_blank" rel="noopener">кран</a>`:"");
}
function netCard(n){
  const tag = n.running?'<span class="badge bg-success-lt">фармит</span>' : n.current?'<span class="badge bg-secondary-lt">текущая</span>':'';
  const btn = n.running
    ? `<button class="btn btn-danger btn-sm w-100" data-net-stop="${n.slug}"><i class="ti ti-player-stop me-1"></i>Стоп</button>`
    : `<button class="btn btn-primary btn-sm w-100" data-net-start="${n.slug}"><i class="ti ti-player-play me-1"></i>Фармить</button>`;
  const acts = (n.activities||[]).map(a=>`<li><i class="ti ti-check text-success me-1"></i>${esc(a)}</li>`).join("");
  return `<div class="col-md-6 col-xl-3"><div class="card h-100">
    <div class="card-body">
      <div class="d-flex justify-content-between"><div>
        <div class="fw-bold" style="font-size:16px">${esc(n.name)}</div>
        <div class="mono text-secondary" style="font-size:11px">${netMeta(n)}</div>
      </div>${tag}</div>
      <div class="mt-2" style="font-size:13px;color:var(--tblr-body-color)">${esc(n.tagline||"")}</div>
      <div class="mt-1 text-secondary" style="font-size:12px">${esc(n.description||"")}</div>
      <div class="mt-2"><div class="text-secondary mono" style="font-size:10.5px;text-transform:uppercase;letter-spacing:.5px">Награды</div><div class="mono" style="font-size:12px">${esc(n.rewards||"—")}</div></div>
      <div class="mt-2"><div class="text-secondary mono" style="font-size:10.5px;text-transform:uppercase;letter-spacing:.5px">Активности</div><ul class="mb-0 ps-3 mt-1" style="font-size:12px">${acts}</ul></div>
    </div>
    <div class="card-footer">${btn}</div>
  </div></div>`;
}
async function loadNetworks(){
  try{
    const d=await j("/api/networks");
    networks=d.networks||[];
    const sel=$("netSelect");
    sel.innerHTML=networks.map(n=>`<option value="${n.slug}" ${n.current?"selected":""}>${esc(n.name)}</option>`).join("");
    $("netCards").innerHTML=networks.map(netCard).join("");
    document.querySelectorAll("[data-net-start]").forEach(b=>b.onclick=()=>netAction(b.dataset.netStart,"start"));
    document.querySelectorAll("[data-net-stop]").forEach(b=>b.onclick=()=>netAction(b.dataset.netStop,"stop"));
    renderNetMeta();
  }catch(e){if(e.message!=="unauth")toast("Сети: "+e.message);}
}
function selectedNet(){return networks.find(n=>n.slug===$("netSelect").value)||null;}
function renderNetMeta(){const n=selectedNet();if(n){$("netMeta").innerHTML=netMeta(n);
  const d=$("netDesc");if(d)d.innerHTML=`<span class="fw-bold">${esc(n.name)}</span> — ${esc(n.tagline||"")}. ${esc(n.description||"")} <span class="text-secondary">(${esc(n.rewards||"")})</span>`;}}

async function netAction(slug,action){
  try{await j(`/api/farm/network/${slug}/${action}`,{method:"POST"});await loadNetworks();await tick();}
  catch(e){toast("Не удалось: "+e.message);}
}

async function init(){
  initTheme();
  const me=await j("/api/me");if($("who"))$("who").textContent=me.name||"—";
  const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand();}
  document.querySelectorAll(".nav-item[data-nav]").forEach(el=>el.addEventListener("click",()=>nav(el.getAttribute("data-nav"))));
  $("navLogout").onclick=()=>j("/api/logout",{method:"POST"}).then(()=>location.href="/login");
  $("btnNetStart").onclick=()=>{const n=selectedNet();if(n)netAction(n.slug,"start");};
  $("btnNetStop").onclick=()=>{const n=selectedNet();if(n)netAction(n.slug,"stop");};
  $("netSelect").onchange=renderNetMeta;
  $("btnRefresh").onclick=()=>tick();
  $("btnExportCsv").onclick=()=>exportWallets("csv");
  $("btnExportJson").onclick=()=>exportWallets("json");

  const links=await j("/api/links");
  $("linksTitle").textContent=links.title||"Ссылки";
  const list=$("links");list.textContent="";
  const items=links.items||[];
  if(!items.length){list.innerHTML='<span class="text-secondary">Ссылки появятся позже</span>';}
  else{list.innerHTML=items.filter(l=>/^https?:\/\//i.test(String(l.url||""))).map(l=>`<a class="d-block mono py-1" href="${esc(l.url)}" target="_blank" rel="noopener">→ ${esc(l.label||l.url)}</a>`).join("");}

  await loadNetworks();
  setInterval(tick,refreshMs);
  tick();
}

function exportWallets(fmt){window.location.href="/api/wallets/export?format="+fmt;}

function renderState(s){
  const pool=s.pool||{},db=s.db||{};
  const total=(pool.actions??0)+(pool.errors??0);
  const rate=total>0?(pool.actions/total*100):null;
  $("cActions").textContent=fmt(pool.actions);
  $("sActions").textContent="за цикл "+fmt((pool.actions??0)/Math.max(pool.cycles||1,1));
  $("cProcessed").textContent=fmt(pool.processed);
  $("sProcessed").textContent="из "+fmt(s.wallet_count);
  $("cErrors").textContent=fmt(pool.errors);
  $("sErrors").textContent="дроп "+fmt(pool.dropped??0);
  $("cCycles").textContent=fmt(pool.cycles);
  $("sCycles").textContent="история "+fmt(db.cycles);
  $("cWallets").textContent=fmt(s.wallet_count);
  $("cWorkers").textContent=fmt(pool.dyn_workers??0);
  $("sWorkers").textContent="health "+(s.health_factor??1).toFixed(2);
  $("sRate").textContent=rate===null?"—":rate.toFixed(1)+"%";
  $("successBar").style.width=(rate===null?0:rate)+"%";
  const dot=$("dot"),st=$("statusText");
  if(!s.running){stoppingRequested=false;dot.className="hx-dot off";st.textContent="stopped";}
  else if(stoppingRequested){dot.className="hx-dot pau";st.textContent="stopping…";}
  else if(pool.paused){dot.className="hx-dot pau";st.textContent="paused";}
  else{dot.className="hx-dot on";st.textContent="running";}
  $("healthText").textContent=(s.health_factor??1).toFixed(2);
  $("updatedText").textContent=new Date().toLocaleTimeString("ru-RU");
  if(s.network){$("netName").textContent=s.network;if($("pagePretitle"))$("pagePretitle").textContent="сеть "+s.network;if($("netSelect"))$("netSelect").value=s.network;}
}

async function tick(){
  try{
    const s=await j("/api/stats");
    renderState(s);
    const pool=s.pool||{};
    actionsSeries.push(pool.actions??0);errorsSeries.push(pool.errors??0);procSeries.push(pool.processed??0);
    if(actionsSeries.length>60){actionsSeries.shift();errorsSeries.shift();procSeries.shift();}
    drawCharts();
    $("chartLegend1").textContent="окно "+actionsSeries.length;
    $("chartLegend2").textContent="окно "+procSeries.length;
    await loadHistory();await loadWallets();
  }catch(e){if(e.message!=="unauth")toast("Ошибка обновления: "+e.message);}
}

function drawCharts(){drawChart($("chartMain"),[actionsSeries,errorsSeries],[c1(),c2()]);drawChart($("chartProc"),[procSeries],[c1()]);}
function drawChart(canvas,series,colors){
  if(!canvas)return;
  const dpr=window.devicePixelRatio||1,w=canvas.clientWidth*dpr,h=canvas.clientHeight*dpr;
  if(canvas.width!==w)canvas.width=w;if(canvas.height!==h)canvas.height=h;
  const ctx=canvas.getContext("2d");ctx.clearRect(0,0,w,h);const pad=6;
  ctx.strokeStyle=gridC();ctx.lineWidth=1;
  for(let i=1;i<4;i++){const y=pad+(i/4)*(h-pad*2);ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(w-pad,y);ctx.stroke();}
  series.forEach((data,si)=>{
    if(data.length<2)return;
    const max=Math.max(...data,1),min=Math.min(...data,0);
    const x=i=>pad+(i/(data.length-1))*(w-pad*2);
    const y=v=>h-pad-((v-min)/Math.max(max-min,1e-9))*(h-pad*2);
    const col=colors[si%colors.length];
    ctx.beginPath();data.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));
    ctx.strokeStyle=col;ctx.lineWidth=(si===0?2:1.4)*dpr;ctx.stroke();
    if(si===0){const g=ctx.createLinearGradient(0,0,0,h);g.addColorStop(0,hexA(col,.16));g.addColorStop(1,hexA(col,0));ctx.lineTo(x(data.length-1),h);ctx.lineTo(x(0),h);ctx.closePath();ctx.fillStyle=g;ctx.fill();}
  });
}
function hexA(hex,a){try{hex=hex.replace("#","");return`rgba(${parseInt(hex.slice(0,2),16)},${parseInt(hex.slice(2,4),16)},${parseInt(hex.slice(4,6),16)},${a})`;}catch(e){return`rgba(34,211,238,${a})`;}}

async function loadHistory(){
  try{
    const d=await j("/api/cycle-history");
    const rows=(d.history||[]).map(c=>{const e=c.errors||0;
      const tag=e===0?'<span class="badge bg-success-lt">ok</span>':`<span class="badge bg-warning-lt">${esc(e)}</span>`;
      return `<tr><td class="mono text-secondary">${esc(c.id)}</td><td>${tag}</td><td class="mono">${fmt(c.actions_ok)}</td><td class="mono">${esc(e)}</td><td class="mono">${fmt(c.duration_s)}</td><td class="mono">${esc(c.wallets)}</td><td class="mono text-secondary">${esc(String(c.started_at||"").slice(0,16))}</td></tr>`;}).join("");
    $("tHistory").innerHTML=rows||'<tr><td colspan="7" class="text-secondary">циклов пока нет</td></tr>';
  }catch(e){}
}
async function loadWallets(){
  try{
    const d=await j("/api/top-wallets");
    const rows=(d.wallets||[]).map((w,i)=>`<tr><td class="mono text-secondary">${i+1}</td><td><span class="mono copyable" data-addr="${esc(w.address)}">${esc(w.address)}</span></td><td class="mono">${fmt(w.actions)}</td></tr>`).join("");
    $("tWallets").innerHTML=rows||'<tr><td colspan="3" class="text-secondary">кошельков нет — нажми «Фармить»</td></tr>';
    document.querySelectorAll(".copyable").forEach(el=>el.addEventListener("click",()=>navigator.clipboard?.writeText(el.getAttribute("data-addr")).then(()=>toast("Адрес скопирован"))));
  }catch(e){}
}
function toast(msg){const t=document.createElement("div");t.className="toast show position-fixed bottom-0 start-50 translate-middle-x mb-3";t.innerHTML=`<div class="toast-body">${esc(msg)}</div>`;document.body.appendChild(t);setTimeout(()=>t.remove(),3500);}

init().catch(e=>console.error(e));