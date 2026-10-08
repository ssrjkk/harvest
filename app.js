/* HARVEST — статический дашборд (GitHub Pages). Читает state.json. */
const $=id=>document.getElementById(id);
let actions=[],errors=[],proc=[];
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);}
function fmt(n){n=Number(n);if(!isFinite(n))return "—";if(Math.abs(n)>=1e6)return(n/1e6).toFixed(2)+"M";if(Math.abs(n)>=1e3)return(n/1e3).toFixed(1)+"K";return String(Math.round(n*100)/100);}
const c1="#58a6ff",c2="#bc8cff",grid="rgba(110,118,129,.18)";

async function load(){
  try{const r=await fetch("state.json?t="+Date.now());if(!r.ok)throw 0;return await r.json();}
  catch(e){
    document.getElementById("term").insertAdjacentHTML("afterend",
      '<div class="alert">state.json ещё не опубликован — ферма скоро загрузит метрики. Постоянный сайт жив, данные появятся автоматически.</div>');
    return null;}
}
function render(s){
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
  $("sRate").textContent=rate===null?"успех —":("успех "+rate.toFixed(1)+"%");
  const dot=$("dot"),st=$("status");
  if(!s.running){dot.className="dot off";st.textContent="stopped";}
  else if(pool.paused){dot.className="dot pau";st.textContent="paused";}
  else{dot.className="dot on";st.textContent="running";}
  $("net").textContent=s.network||"—";
  $("health").textContent=(s.health_factor??1).toFixed(2);
  $("updated").textContent="обновлено: "+(s.updated_at||"—");

  // сети
  const nets=s.networks||[];
  $("nets").innerHTML=nets.map(n=>{
    const acts=(n.activities||[]).map(a=>'<li>'+esc(a)+'</li>').join("");
    const cur=n.slug===s.network?' <span class="cur">текущая</span>':'';
    return `<div class="net"><div><span class="name">${esc(n.name)}</span>${cur}</div>
      <div class="meta">chain ${n.chain_id} · ${esc(n.currency)}</div>
      <div class="tag">${esc(n.tagline||"")}</div>
      <div class="desc">${esc(n.description||"")}</div>
      <div class="rewards">🎁 ${esc(n.rewards||"—")}</div>
      <ul>${acts}</ul></div>`;}).join("");

  // графики
  actions=(s.chart?.actions||[]).slice(-60);
  errors=(s.chart?.errors||[]).slice(-60);
  proc=(s.chart?.processed||[]).slice(-60);
  draw();

  $("tWallets").innerHTML=(s.top_wallets||[]).map((w,i)=>
    `<tr><td class="mono" style="color:var(--faint)">${i+1}</td><td class="mono">${esc(w.address)}</td><td class="mono">${fmt(w.actions)}</td></tr>`).join("")
    ||'<tr><td colspan="3" style="color:var(--muted)">кошельков пока нет — запусти фарм в боте</td></tr>';
  $("tHistory").innerHTML=(s.history||[]).map(c=>{const e=c.errors||0;
    const b=e===0?'<span class="badge ok">ok</span>':`<span class="badge warn">${esc(e)}</span>`;
    return `<tr><td class="mono" style="color:var(--faint)">${esc(c.id)}</td><td>${b}</td><td class="mono">${fmt(c.actions_ok)}</td><td class="mono">${esc(e)}</td><td class="mono">${fmt(c.duration_s)}</td><td class="mono">${esc(c.wallets)}</td></tr>`;}).join("")
    ||'<tr><td colspan="6" style="color:var(--muted)">циклов пока нет</td></tr>';
}
function draw(){
  drawChart($("chartMain"),[actions,errors],[c1,c2]);
  drawChart($("chartProc"),[proc],[c1]);
}
function drawChart(canvas,series,colors){
  const dpr=window.devicePixelRatio||1,w=canvas.clientWidth*dpr,h=canvas.clientHeight*dpr;
  if(canvas.width!==w)canvas.width=w;if(canvas.height!==h)canvas.height=h;
  const ctx=canvas.getContext("2d");ctx.clearRect(0,0,w,h);const pad=6;
  ctx.strokeStyle=grid;
  for(let i=1;i<4;i++){const y=pad+(i/4)*(h-pad*2);ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(w-pad,y);ctx.stroke();}
  series.forEach((data,si)=>{if(data.length<2)return;
    const max=Math.max(...data,1),min=Math.min(...data,0);
    const x=i=>pad+(i/(data.length-1))*(w-pad*2);
    const y=v=>h-pad-((v-min)/Math.max(max-min,1e-9))*(h-pad*2);
    ctx.beginPath();data.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));
    ctx.strokeStyle=colors[si%colors.length];ctx.lineWidth=(si===0?2:1.4)*dpr;ctx.stroke();
    if(si===0){const g=ctx.createLinearGradient(0,0,0,h);g.addColorStop(0,"rgba(88,166,255,.15)");g.addColorStop(1,"rgba(88,166,255,0)");ctx.lineTo(x(data.length-1),h);ctx.lineTo(x(0),h);ctx.closePath();ctx.fillStyle=g;ctx.fill();}
  });
}
(async()=>{const s=await load();if(s)render(s);})();
setInterval(async()=>{const s=await load();if(s)render(s);},60000);