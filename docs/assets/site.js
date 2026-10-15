/* HARVEST site app — vanilla JS, no deps. Live pulpit reads state.json; tools run locally. */
(function(){
"use strict";
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const toast=(m)=>{const t=$("#toast");t.textContent=m;t.classList.add("show");clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("show"),1800);};
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const fmt=n=>{n=Number(n);if(!isFinite(n))return "—";if(Math.abs(n)>=1e6)return(n/1e6).toFixed(2)+"M";if(Math.abs(n)>=1e3)return(n/1e3).toFixed(1)+"K";return String(Math.round(n*100)/100);};

/* ---------- theme ---------- */
const themes=["paper","ink","field"];
let theme=localStorage.getItem("hv-theme")||"paper";
function applyTheme(){document.documentElement.dataset.theme=theme;$("#themeBtn").textContent=theme==="paper"?"◐":theme==="ink"?"◑":"◒";drawLive();}
$("#themeBtn").addEventListener("click",()=>{theme=themes[(themes.indexOf(theme)+1)%themes.length];localStorage.setItem("hv-theme",theme);applyTheme();drawChart();});
applyTheme();

/* ---------- i18n ---------- */
const EN={
skip:"Skip to content",nav_live:"Live",nav_overview:"Overview",nav_networks:"Networks",nav_start:"Start",nav_app:"Demo",nav_arch:"Inside",nav_portal:"Portal",nav_sec:"Security",nav_license:"License",
kick:"EVM testnet farmer · Python 3.12 · MIT · © 2026 ssrjkk",
h1:'Testnet farmer,<br><em>not a lottery.</em>',
lede:"Mass wallet generation, faucets, async transaction farming with human-like behavior profiles, multi-RPC with EMA scoring, web portal and Telegram bot. Below: the live farm pulpit, full docs, and a working in-browser zone — config, commands, wallets, simulator.",
th_fact:"Fact",th_val:"Value",f_net:"Networks bundled",f_tests:"Tests",f_wallet:"Generation",f_rpc:"RPC layer",f_portal:"Control",f_dist:"Distribution",
cta_live:"Live pulpit",cta_demo:"Demo zone",
hero_note:"The pulpit reads state.json published by the farm. The rest is fully static: demo keys never leave your browser. Testnets only.",
contacts:"Author:",
sl_h:"Pulpit — live farm state",sl_sub:"Metrics are published by deploy/publish_dash.py from the farm machine. Refreshes every minute. If nothing was published yet, the site still fully works — data appears on its own.",
t_net:"network",k_act:"Actions",k_proc:"Processed",k_err:"Errors",k_cyc:"Cycles",k_wal:"Wallets",
ch1:"Actions / errors over history",ch2:"Wallets processed",tw:"Top wallets",ch:"Cycle history",nets_h:"Networks",
s1_h:"What it is and what it does",s1_sub:"One auto.py pipeline pass = four phases: generate/reuse wallets → faucet → farm → export. Each wallet's behavior is deterministic from its address, so restarts don't leak the farm by changing the pattern.",
c1_l:"Wallets",c1_h:"Generation & storage",c1_p:"BIP-39 via eth_account, CSV/JSON import/export, AES-256-GCM seed encryption with master key, dedup, CSV-injection guard, atomic file writes.",
c2_l:"Faucet",c2_h:"Balance top-ups",c2_p:"chainstack / direct / web strategies, live-strategy pre-flight, retries, rate-limit, proxy rotation, ensure_balance up to target. No live strategies → force_farm kicks in.",
c3_l:"Farming",c3_h:"Async worker pool",c3_p:"One NetworkManager per session, chunked by chunk_size, crash recovery via state.json, worker count follows RPC health, adaptive weights for failing actions.",
c4_l:"Anti-sybil",c4_h:"Behavior profiles",c4_p:"Activity, pauses, jittered amounts, gas, time-of-day (day −25%, night +150% skip), multi-cycle rests, staggered start, ×1.6 bursts.",
c5_l:"RPC",c5_h:"Resilient network layer",c5_p:"Multi-RPC list, EMA node scoring, best-node rotation, zombie revival on first success, circuit breaker, adaptive token bucket, receipt budget with failover.",
c6_l:"Portal",c6_h:"Web + Telegram",c6_p:"aiohttp dashboard: metrics, start/stop/pause, cycle history, top wallets, export with keys. Long-polling bot: same commands + /doctor, /history, /export. HMAC sessions, default-deny.",
s2_h:"Networks & configs",s2_sub:"Pick a network in the main.py start menu or via --config. The transfers-only preset (config.simple.yaml) is for dense farming with no contract addresses: separate DB farming_simple.db, gas_limit 21000.",
n_net:"Network",n_cfg:"Config",n_cur:"Currency",n_act:"Actions",
vibe_note:"* VibePassMarket buy/sell need an EIP-712 TradeIntent signature with the tradeSigner key (vibevibe.signer_key / FARMER_VIBE_SIGNER_KEY). Without it they are skipped by adaptive weights; transfer / claimRewards / stake always work. ABI lives in abi/vibevibe.json, contracts verified on Blockscout.",
s3_h:"Up and running in five minutes",s3_sub:"Primary target: Windows/PowerShell and an exe build. Python 3.12.",
flags_h:"auto.py flags",flags_d:"Effect",
fl1:"N wallets (0 = from config; default 50)",fl2:"N cycles (0 = forever)",fl3:"Config file ($FARMER_CONFIG → config.yaml)",fl4:"Skip faucet / farm",fl5:"Pipeline every H hours (min 0.05)",fl6:"Faucet batch · workers · minimal delays",fl7:"Dry run · CSV+JSON export · encrypt",fl8:"Cycle history · diagnostics · version",
exe_h:"exe build · portal · tests · pulpit publish",menu_h:"main.py menu",
menu_p:"1 — make wallets · 2 — single cycle · 3 — live console (p pause, q stop) · 4 — balance monitor · 5 — faucet all · 6 — stats · 7/8 — JSON export/import · C — history · 9 — RPC diag · D — doctor · B — backups · L — license · S — switch net · H — help. With args main.py delegates to auto (the exe behaves the same).",
s4_h:"Use it here — demo zone",s4_sub:"Everything runs locally in your browser, no server. On-chain execution stays with the Python farmer — here you prepare configs and commands, test logic in the simulator, and manage testnet wallets.",
tab1:"1 · Config",tab2:"2 · Command",tab3:"3 · Wallets",tab4:"4 · Simulator",tab5:"5 · Check",
f_rpc:"RPC URL",f_db:"DB / master key file",f_wallets:"Wallets",f_workers:"Workers",f_target:"Faucet: min → target",f_actions:"Actions per cycle [min, max]",f_gas:"gas_limit / min_gas_for_action",
gen_cfg:"Generate YAML",dl:"Download file",
f_cfgfile:"Config file",o_sf:"no faucet (force_farm)",o_sfarm:"faucet+export only",o_fast:"minimal delays",o_dry:"dry run",o_enc:"encrypt export",
cmd_out:"Ready command",build:"Build",cmd_sim:"Phase notes",cmd_sim_p:"The command runs 4 run_once phases: top-up wallets → faucet pre-flight + request_batch → FarmerPool (chunks, workers, crash recovery) → export_csv/json. Re-running resumes state.json where it stopped.",
w_warn:"Testnets only. Generation uses WebCrypto; the address is derived locally (ethers via CDN, honest demo-mode label when offline). Nothing is sent to any network.",
w_n:"How many to generate",w_gen:"Generate",w_clear:"Clear",w_act:"actions",
s_w:"Wallets",s_c:"Cycles",s_skip:"skip_cycle_probability",s_a:"Actions [min,max]",s_fail:"Action failure chance",s_speed:"Log speed",
sim_run:"Run simulation",sim_stop:"Stop",sim_exp:"Export history CSV",sim_log:"Live log (like the operator terminal)",sim_chart:"Actions per cycle",
v_h:"Paste a config YAML — checks like config_validate.py",v_run:"Validate",
p_h:"Parallelism calculator (core/performance.py)",p_w:"max_workers (empty = auto)",p_run:"Calculate",
p_n:"Formulas: workers=min(max(20,cpu·2),128) · rpc=min(max(8,cpu·4),128) · gen=min(8,max(2,cpu)) · chunk=max(workers·4,100). RPC cap 128, gen cap 8.",
s5_h:"How it works",s5_sub:"main.py (Rich menu) and auto.py (CLI machine) share one core/. A single NetworkManager per session — shared latency, caches, monitor. DB writes go through BatchWriter in batches with cooldown after repeated failures.",
a_mod:"Module",a_do:"Owns",
a1:"30s ping, 2500ms probe, ×3 retries, failover, 30s receipt budget",
a2:"Priority queue, retire-without-cancel, respawn with backoff",
a3:"2s/500-row flush, dropped_count, 30s cooldown",
a4:"ABI, EIP-712 TradeIntent, default gas 300000",
a5:"wallets/actions_log/nonces/cycle_history, VACUUM, backups",
a6:"Config → key → DB → RPC → faucet → license, exit code",
a_n:"Per-layer detail: §3 of DOCUMENTATION.md in the repo (entry points, portal, configs, Tier-2 for real RPCs/contracts).",
s6_h:"Portal & Telegram",s6_sub:"python -m portal starts aiohttp (web + API) and, with a token, the bot. Secrets via env only. Fail-closed: non-loopback bind with no login method refuses to start.",
po_d:"Purpose",po1:"Session signing, min 16 chars",po2:"BotFather token",po3:"Default-deny user_id list",po4:"DB master key / web-login PIN",po6:"Farmer DB path / farmer config",
tg_p:"Start/stop/pause, stats with wallet count, links, CSV/JSON export, /doctor, /history, /export. Values escaped, generic error replies.",
dp_p:"Ready kit: Dockerfile + compose, long-polling with no public URL, Caddy for HTTPS later, .env template, healthcheck, data volume.",
s7_h:"Security — closed by default",
b1_h:"Encryption",b1_p:"Seeds: AES-256-GCM (12B nonce, AAD wallet-seed). Export files: AES-256-CBC v2 — PBKDF2-SHA256 100k, separated encrypt/MAC keys, HMAC verified before writing.",
b2_p:"RPC URLs are redacted everywhere: credentials before @ and query tokens stripped. Logs, cycle history, CLI output.",
b3_p:"No master key with encrypted seeds → refuse. Portal default-deny, single-instance DB lock, restricted file permissions, login limits 5/10min per IP.",
sec_rep:"Found a vulnerability — don't open a public issue. Use GitHub private vulnerability reporting or contact the maintainer. Only the latest release is supported; reply within 48 hours.",
s8_h:"Tests & questions",t1:"wait_for_receipt: EMA poll budget, failover on error streaks, degraded poll rate, None on budget without nonce rollback, secondary-node cross-check.",t2:"Lint: ruff · flake8 · black (120) · mypy · bandit. CI badge in README.",
q1:"Is this for mainnet?",a_q1:"No. Testnets and practice activity farming only. Don't use real funds or real keys in the site demo.",
q2:"Where are my demo keys?",a_q2:"Only in this tab's memory. The site is static, nothing is uploaded — close the tab or press Clear and they're gone. For real storage use the Python-side export + encryption.",
q3:"What about 0x0 contracts in config?",a_q3:"Stubs by design: the validator passes them with a remark, runtime falls back to transfer. Fill in real addresses from the Tier-2 docs when ready.",
q4:"Why is the pulpit empty?",a_q4:"It comes alive after the first publish: run python deploy/publish_dash.py on the farm machine (scheduled every 15–60 min). Everything else already works.",
s9_h:"MIT License",s9_sub:"Use, copy, modify, publish and sell freely — keep the copyright notice. No warranty.",
ft:"EVM testnet farmer · © 2026 ssrjkk · MIT"
};
let lang=localStorage.getItem("hv-lang")||"ru";
function applyLang(){
  document.documentElement.lang=lang==="ru"?"ru":"en";
  $("#langRu").classList.toggle("on",lang==="ru");
  $("#langEn").classList.toggle("on",lang==="en");
  if(lang==="en"){$$("[data-i18n]").forEach(el=>{const k=el.dataset.i18n;if(EN[k]!==undefined){if(k==="h1")el.innerHTML=EN[k];else el.textContent=EN[k];}});}
}
function setLang(l){lang=l;localStorage.setItem("hv-lang",l);if(l==="en"){applyLang();}else{localStorage.setItem("hv-lang","ru");location.reload();}}
$("#langRu").addEventListener("click",()=>setLang("ru"));
$("#langEn").addEventListener("click",()=>setLang("en"));
if(lang==="en")applyLang();

/* ---------- tabs ---------- */
$$(".play-tabs button").forEach(b=>b.addEventListener("click",()=>{
  $$(".play-tabs button").forEach(x=>x.classList.remove("on"));b.classList.add("on");
  ["t-config","t-cmd","t-wallet","t-sim","t-check"].forEach(id=>$("#"+id).hidden=id!==b.dataset.tab);
}));

/* ---------- copy ---------- */
document.addEventListener("click",e=>{
  const b=e.target.closest("[data-copy]");if(!b)return;
  const pre=$("#"+b.dataset.copy);const txt=pre.textContent.replace(/\n?copy\s*$/,"");
  (navigator.clipboard?navigator.clipboard.writeText(txt):Promise.reject()).then(()=>toast("copied")).catch(()=>{const r=document.createRange();r.selectNode(pre);getSelection().removeAllRanges();getSelection().addRange(r);try{document.execCommand("copy");toast("copied");}catch(_){toast("select + Ctrl+C");}});
});
$("#cfgCopy")?.addEventListener("click",()=>{const t=$("#cfgOut").textContent;(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(()=>toast("copied")).catch(()=>toast("select + Ctrl+C"));});
$("#cmdCopy")?.addEventListener("click",()=>{const t=$("#cmdOut").textContent;(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(()=>toast("copied")).catch(()=>toast("select + Ctrl+C"));});

/* ---------- live pulpit (state.json) ---------- */
let live={actions:[],errors:[],proc:[]};
const FALLBACK_NETS=[
 {slug:"robinhood",name:"Robinhood Chain",chain_id:46630,currency:"ETH",tagline:"Основной тестнет",description:"Переводы и свопы, плотный фарм.",rewards:"—",activities:["transfer","vibevibe_swap","vibevibe_mint"]},
 {slug:"vibevibe",name:"vibe/vibe",chain_id:46630,currency:"ETH",tagline:"Launchpad, 5% сапплая тестнету",description:"claimRewards и stake всегда; buy/sell — с ключом tradeSigner.",rewards:"5% supply",activities:["transfer","claimRewards","stake","buy/sell*"]},
 {slug:"flop",name:"Flop Labs",chain_id:99999,currency:"FLOP",tagline:"chain_id — заглушка",description:"compute / validate / stake + transfer.",rewards:"—",activities:["flop_compute","flop_validate","flop_stake"]},
 {slug:"arc",name:"Arc (Minara.Fun)",chain_id:5042002,currency:"ARC",tagline:"Minara",description:"launch / trade / add_liquidity + transfer.",rewards:"—",activities:["arc_launch","arc_trade","arc_add_liquidity"]}
];
async function loadState(){
  try{const r=await fetch("state.json?t="+Date.now(),{cache:"no-store"});if(!r.ok)throw 0;return await r.json();}
  catch(e){return null;}
}
function renderLive(s){
  if(!s){
    $("#liveAlert").innerHTML='<div class="alert">'+(lang==="en"?"state.json not published yet — the farm will upload metrics. The rest of the site already works.":"state.json ещё не опубликован — ферма скоро загрузит метрики. Остальной сайт уже работает.")+'</div>';
    $("#nets").innerHTML=FALLBACK_NETS.map(netCard).join("");
    setPill("off","—");return;
  }
  $("#liveAlert").innerHTML="";
  const pool=s.pool||{},db=s.db||{};
  const total=(pool.actions??0)+(pool.errors??0);
  const rate=total>0?(pool.actions/total*100):null;
  $("#cActions").textContent=fmt(pool.actions);
  $("#sActions").textContent=(lang==="en"?"per cycle ":"за цикл ")+fmt((pool.actions??0)/Math.max(pool.cycles||1,1));
  $("#cProcessed").textContent=fmt(pool.processed);
  $("#sProcessed").textContent=(lang==="en"?"of ":"из ")+fmt(s.wallet_count);
  $("#cErrors").textContent=fmt(pool.errors);
  $("#sErrors").textContent=(lang==="en"?"dropped ":"дроп ")+fmt(pool.dropped??0);
  $("#cCycles").textContent=fmt(pool.cycles);
  $("#sCycles").textContent=(lang==="en"?"history ":"история ")+fmt(db.cycles);
  $("#cWallets").textContent=fmt(s.wallet_count);
  $("#sRate").textContent=rate===null?(lang==="en"?"success —":"успех —"):(lang==="en"?"success ":"успех ")+rate.toFixed(1)+"%";
  const dot=$("#dot"),st=$("#status");
  if(!s.running){dot.className="dot off";st.textContent="stopped";setPill("off","stopped");}
  else if(pool.paused){dot.className="dot pau";st.textContent="paused";setPill("pau","paused");}
  else{dot.className="dot on";st.textContent="running";setPill("on","running");}
  $("#net").textContent=s.network||"—";
  $("#health").textContent=Number(s.health_factor??1).toFixed(2);
  $("#updated").textContent=(lang==="en"?"updated: ":"обновлено: ")+(s.updated_at||"—");
  const nets=(s.networks&&s.networks.length)?s.networks:FALLBACK_NETS;
  $("#nets").innerHTML=nets.map(n=>netCard(n,n.slug===s.network)).join("");
  live.actions=(s.chart?.actions||[]).slice(-60);
  live.errors=(s.chart?.errors||[]).slice(-60);
  live.proc=(s.chart?.processed||[]).slice(-60);
  drawLive();
  $("#tWallets").innerHTML=(s.top_wallets||[]).map((w,i)=>
    `<tr><td class="num" style="color:var(--muted)">${i+1}</td><td class="addr">${esc(w.address)}</td><td class="num">${fmt(w.actions)}</td></tr>`).join("")
    ||'<tr><td colspan="3" class="note">—</td></tr>';
  $("#tHistory").innerHTML=(s.history||[]).map(c=>{const e=c.errors||0;
    const b=e===0?'<span class="pill ok">ok</span>':`<span class="pill warn">${esc(e)}</span>`;
    return `<tr><td class="num" style="color:var(--muted)">${esc(c.id)}</td><td>${b}</td><td class="num">${fmt(c.actions_ok)}</td><td class="num">${esc(e)}</td><td class="num">${fmt(c.duration_s)}</td><td class="num">${esc(c.wallets)}</td></tr>`;}).join("")
    ||'<tr><td colspan="6" class="note">—</td></tr>';
}
function netCard(n,cur){
  const acts=(n.activities||[]).map(a=>"<li>"+esc(a)+"</li>").join("");
  return `<div class="net"><div><span class="name">${esc(n.name)}</span>${cur?' <span class="cur">'+(lang==="en"?"current":"текущая")+"</span>":""}</div>
    <div class="meta">chain ${esc(n.chain_id)} · ${esc(n.currency)}</div>
    <div class="tag">${esc(n.tagline||"")}</div>
    <div class="desc">${esc(n.description||"")}</div>
    <div class="rewards">${esc(n.rewards||"—")}</div><ul>${acts}</ul></div>`;
}
function setPill(cls,txt){$("#pillDot").className="dot "+cls;$("#pillTxt").textContent=txt;}
function drawLive(){
  lineChart($("#chartMain"),[live.actions,live.errors]);
  lineChart($("#chartProc"),[live.proc]);
}
function lineChart(cv,series){
  if(!cv)return;const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth||600,H=150;cv.width=W*dpr;cv.height=H*dpr;
  const x=cv.getContext("2d");x.scale(dpr,dpr);
  const cs=getComputedStyle(document.documentElement);
  const acc=cs.getPropertyValue("--accent").trim()||"#2E4A22";
  const amb=cs.getPropertyValue("--amber").trim()||"#9A6B0F";
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
  if(!series.some(d=>d&&d.length)){x.fillStyle=mut;x.font="12px monospace";x.fillText("no data",14,H/2);}
}
(async()=>{renderLive(await loadState());})();
setInterval(async()=>{const s=await loadState();if(s)renderLive(s);},60000);
window.addEventListener("resize",drawLive);

/* ---------- config builder ---------- */
const NETS={
robinhood:{file:"config_robinhood.yaml",db:"farming_state.db",chain:46630,rpc:"https://rpc.testnet.chain.robinhood.com",actions:'  - type: vibevibe_swap\n    contract: 0x0000000000000000000000000000000000000000\n    method: swap\n    weight: 1.0\n    min_amount: 0.0001\n    max_amount: 0.001\n  - type: transfer\n    target: random_wallet\n    weight: 2.0\n    min_amount: 0.0001\n    max_amount: 0.001',farm:"  actions_per_cycle: [3, 8]\n  delay_between_actions: [5, 20]\n  delay_between_cycles: [3600, 7200]",workers:20},
vibevibe:{file:"config_vibevibe.yaml",db:"farming_vibevibe.db",chain:46630,rpc:"https://rpc.testnet.chain.robinhood.com",actions:'  - type: transfer\n    target: random_wallet\n    weight: 1.0\n    min_amount: 0.0001\n    max_amount: 0.001',farm:"  actions_per_cycle: [3, 8]\n  delay_between_actions: [5, 20]\n  delay_between_cycles: [3600, 7200]",workers:20},
flop:{file:"config_flop.yaml",db:"farming_flop.db",chain:99999,rpc:"https://rpc.flop-labs.testnet.example",actions:'  - type: flop_compute\n    contract: 0x0000000000000000000000000000000000000000\n    method: compute\n    weight: 1.0\n    min_amount: 0.0001\n    max_amount: 0.001\n  - type: transfer\n    target: random_wallet\n    weight: 1.0\n    min_amount: 0.0001\n    max_amount: 0.001',farm:"  actions_per_cycle: [5, 15]\n  delay_between_actions: [3, 15]\n  delay_between_cycles: [1800, 3600]",workers:30},
arc:{file:"config_arc.yaml",db:"farming_arc.db",chain:5042002,rpc:"https://rpc.arc-testnet.example",actions:'  - type: arc_launch\n    contract: 0x0000000000000000000000000000000000000000\n    method: launch\n    weight: 1.0\n    min_amount: 0.0001\n    max_amount: 0.001\n  - type: transfer\n    target: random_wallet\n    weight: 1.0\n    min_amount: 0.0001\n    max_amount: 0.001',farm:"  actions_per_cycle: [5, 12]\n  delay_between_actions: [2, 10]\n  delay_between_cycles: [1200, 2400]",workers:30},
simple:{file:"config.simple.yaml",db:"farming_simple.db",chain:46630,rpc:"https://rpc.testnet.chain.robinhood.com",actions:'  - type: transfer\n    target: random_wallet\n    weight: 1.0\n    min_amount: 0.0001\n    max_amount: 0.001',farm:"  actions_per_cycle: [3, 8]\n  delay_between_actions: [5, 20]\n  delay_between_cycles: [3600, 7200]",workers:20}
};
function buildYaml(){
  const key=$("#cfgNet").value, n=NETS[key];
  const rpc=$("#cfgRpc").value.trim()||n.rpc, db=$("#cfgDb").value.trim()||n.db;
  const w=Math.max(1,parseInt($("#cfgWallets").value||"50",10)), wk=Math.max(1,parseInt($("#cfgWorkers").value||String(n.workers),10));
  const f=$("#cfgFaucet").value.split("→").map(s=>s.trim());const fmin=f[0]||"0.005", fmax=f[1]||"0.05";
  const g=$("#cfgGas").value.split("/").map(s=>s.trim());const gas=g[0]||"100000", mgas=g[1]||"0.003";
  return `# HARVEST — ${n.file} (generated in browser)\nnetwork:\n  rpc_url: ${rpc}\n  chain_id: ${n.chain}\nwallets:\n  count: ${w}\n  seed: ""\nfaucet:\n  enabled: true\n  min_balance: ${fmin}\n  target_balance: ${fmax}\n  retries: 2\n  strategies:\n    - type: direct\n      url: https://faucet.example/claim\nactions:\n${n.actions}\nfarming:\n${n.farm}\n  skip_cycle_probability: 0.05\nadvanced:\n  gas_limit: ${gas}\n  min_gas_for_action: ${mgas}\nthreading:\n  max_workers: ${wk}\n  timeout_per_wallet: 0\ndatabase:\n  path: ${db}\n  master_key: master.key\n`;
}
$("#cfgNet").addEventListener("change",e=>{const n=NETS[e.target.value];$("#cfgRpc").value=n.rpc;$("#cfgDb").value=n.db;$("#cfgWorkers").value=n.workers;});
$("#cfgGen").addEventListener("click",()=>{$("#cfgOut").textContent=buildYaml();toast("yaml ready");});
$("#cfgDl").addEventListener("click",()=>{const y=$("#cfgOut").textContent.startsWith("#")?$("#cfgOut").textContent:buildYaml();$("#cfgOut").textContent=y;const b=new Blob([y],{type:"text/yaml"});const a=document.createElement("a");a.href=URL.createObjectURL(b);a.download=NETS[$("#cfgNet").value].file;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),2000);});

/* ---------- cmd builder ---------- */
function buildCmd(){
  let c="python auto.py";
  const w=parseInt($("#cmdW").value,10), cy=parseInt($("#cmdC").value,10);
  if(!isNaN(w))c+=` --wallets ${w}`;
  if(!isNaN(cy))c+=` --cycles ${cy}`;
  const cfg=$("#cmdCfg").value.trim();if(cfg&&cfg!=="config.yaml")c+=` --config ${cfg}`;
  if($("#cmdSkipF").checked)c+=" --skip-faucet";
  if($("#cmdSkipFarm").checked)c+=" --skip-farm";
  if($("#cmdFast").checked)c+=" --fast";
  if($("#cmdDry").checked)c+=" --dry-run";
  if($("#cmdEnc").checked)c+=" --encrypt";
  const s=$("#cmdSched").value.trim();if(s)c+=` --schedule ${s}`;
  return c;
}
$("#cmdBuild").addEventListener("click",()=>{$("#cmdOut").textContent=buildCmd();});

/* ---------- wallets ---------- */
let wallets=[], ethersMod=null;
async function detectEthers(){
  try{ethersMod=await import("https://esm.sh/ethers@6.13.2");$("#wEngine").value="ethers@6 (real EVM)";$("#wMode").textContent="ethers@6 (real EVM)";$("#wMode").className="pill ok";}
  catch(e){$("#wEngine").value="demo (offline)";$("#wMode").textContent="demo (offline)";$("#wMode").className="pill warn";}
}
detectEthers();
const rndHex=n=>[...crypto.getRandomValues(new Uint8Array(n))].map(b=>b.toString(16).padStart(2,"0")).join("");
$("#wGen").addEventListener("click",async()=>{
  const n=Math.min(200,Math.max(1,parseInt($("#wCount").value||"5",10)));
  for(let i=0;i<n;i++){
    let addr,priv,lbl="";
    if(ethersMod){try{const w=ethersMod.Wallet.createRandom();addr=w.address;priv=w.privateKey;lbl="ethers";}catch(e){ethersMod=null;}}
    if(!addr){priv="0x"+rndHex(32);addr="0x"+rndHex(20);lbl="demo";$("#wEngine").value="demo (offline)";$("#wMode").textContent="demo (offline)";$("#wMode").className="pill warn";}
    wallets.push({address:addr,private_key:priv,actions:0,mode:lbl});
  }
  renderWallets();toast(wallets.length+" wallets");
});
function renderWallets(){
  const tb=$("#wRows");
  if(!wallets.length){tb.innerHTML='<tr><td colspan="4" class="note">—</td></tr>';return;}
  tb.innerHTML=wallets.map((w,i)=>`<tr><td class="num">${i+1}</td><td class="addr">${esc(w.address)}${w.mode==="demo"?' <span class="pill warn">demo</span>':""}</td><td class="addr">${esc(w.private_key.slice(0,10))}…${esc(w.private_key.slice(-6))}</td><td class="num">${w.actions}</td></tr>`).join("");
}
function dl(name,text,type){const b=new Blob([text],{type});const a=document.createElement("a");a.href=URL.createObjectURL(b);a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),2000);}
$("#wCsv").addEventListener("click",()=>{if(!wallets.length)return toast("empty");dl("wallets_export.csv","address,private_key,actions\n"+wallets.map(w=>`${w.address},${w.private_key},${w.actions}`).join("\n"),"text/csv");});
$("#wJson").addEventListener("click",()=>{if(!wallets.length)return toast("empty");dl("wallets_export.json",JSON.stringify(wallets.map(w=>({address:w.address,private_key:w.private_key,actions:w.actions})),null,2),"application/json");});
$("#wClear").addEventListener("click",()=>{wallets=[];renderWallets();});

/* ---------- simulator ---------- */
let simTimer=null, hist=[];
function hash32(s){let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return h>>>0;}
function rnd(seed){let a=seed>>>0;return()=>{a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296;};}
$("#simRun").addEventListener("click",()=>{
  if(simTimer)return;
  const W=Math.min(2000,Math.max(1,parseInt($("#simW").value,10)||50));
  const C=Math.min(200,Math.max(1,parseInt($("#simC").value,10)||5));
  const skip=parseFloat($("#simSkip").value||"0.05");
  const ab=($("#simA").value||"3,8").split(",").map(x=>parseInt(x.trim(),10));const alo=ab[0]||3,ahi=ab[1]||8;
  const fail=parseFloat($("#simFail").value||"0.08");
  const speed=parseInt($("#simSpeed").value,10)||8;
  const addrs=Array.from({length:W},(_,i)=>"0xSIM"+String(i).padStart(5,"0"));
  const profs=addrs.map(a=>{const r=rnd(hash32(a));return{act:0.5+r()*1.0,burst:r()<0.12?1.6:1.0,rest:r()<0.06?2:0};});
  hist=[];let cyc=0;$("#simLog").textContent=`harvest-sim: ${W} wallets, ${C} cycles, skip=${skip}\n`;
  $("#simStat").textContent="running";$("#simStat").className="pill warn";
  const perCycle=[];
  simTimer=setInterval(()=>{
    if(cyc>=C){clearInterval(simTimer);simTimer=null;$("#simStat").textContent="done";$("#simStat").className="pill ok";return;}
    cyc++;
    let ok=0,acts=0,errs=0;const t0=performance.now();
    for(let i=0;i<W;i++){
      const p=profs[i];
      if(p.rest>0){p.rest--;continue;}
      const r=rnd(hash32(addrs[i]+":"+cyc));
      const night=r()<0.2;const tod=night?1.6:0.85;
      if(r()<skip*p.act*tod){if(r()<0.05)p.rest=1;continue;}
      ok++;
      let na=alo+Math.floor(r()*(ahi-alo+1));na=Math.round(na*p.burst);
      for(let k=0;k<na;k++){if(rnd(hash32(addrs[i]+":"+cyc+":"+k))()<fail)errs++;else acts++;}
    }
    const dur=((performance.now()-t0)/1000*speed+0.4).toFixed(1);
    const row={cycle:cyc,mode:"auto",wallets:W,wallets_ok:ok,actions_ok:acts,errors:errs,duration_s:dur};
    hist.push(row);perCycle.push(acts);
    slog(`cycle ${cyc}: wallets_ok=${ok}/${W} actions_ok=${acts} errors=${errs} (${dur}s)`);
    const tw=hist.reduce((s,h)=>s+h.wallets_ok,0),ta=hist.reduce((s,h)=>s+h.actions_ok,0),te=hist.reduce((s,h)=>s+h.errors,0);
    $("#kW").textContent=tw;$("#kA").textContent=ta;$("#kE").textContent=te;$("#kAvg").textContent=(ta/hist.length).toFixed(1);
    renderHist();drawChart(perCycle);
  },Math.max(120,900/speed));
});
function slog(m){const el=$("#simLog");el.textContent+=m+"\n";el.scrollTop=el.scrollHeight;}
$("#simStop").addEventListener("click",()=>{if(simTimer){clearInterval(simTimer);simTimer=null;$("#simStat").textContent="stopped";$("#simStat").className="pill warn";}});
$("#simExp").addEventListener("click",()=>{if(!hist.length)return toast("empty");dl("cycle_history.csv","started_at,mode,wallets,wallets_ok,actions_ok,errors,duration_s\n"+hist.map(h=>`cycle-${h.cycle},${h.mode},${h.wallets},${h.wallets_ok},${h.actions_ok},${h.errors},${h.duration_s}`).join("\n"),"text/csv");});
function renderHist(){$("#simHist").innerHTML=hist.slice(-12).reverse().map(h=>`<tr><td class="num">${h.cycle}</td><td>${h.mode}</td><td class="num">${h.wallets}</td><td class="num">${h.wallets_ok}</td><td class="num">${h.actions_ok}</td><td class="num">${h.errors}</td><td class="num">${h.duration_s}</td></tr>`).join("");}
function drawChart(data){
  const cv=$("#simChart");if(!cv)return;const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth||600,H=220;cv.width=W*dpr;cv.height=H*dpr;
  const x=cv.getContext("2d");x.scale(dpr,dpr);
  const cs=getComputedStyle(document.documentElement);
  x.clearRect(0,0,W,H);
  const d=data||hist.map(h=>h.actions_ok);
  x.strokeStyle=cs.getPropertyValue("--line");x.beginPath();x.moveTo(34,8);x.lineTo(34,H-24);x.lineTo(W-8,H-24);x.stroke();
  if(!d.length){x.fillStyle=cs.getPropertyValue("--muted");x.font="12px monospace";x.fillText("no data — run simulation",44,H/2);return;}
  const mx=Math.max(...d,1),bw=Math.max(3,(W-60)/d.length-4);
  d.forEach((v,i)=>{const h=(H-48)*v/mx,bx=40+i*(bw+4);
    x.fillStyle=cs.getPropertyValue("--accent");x.fillRect(bx,H-24-h,bw,h);});
  x.fillStyle=cs.getPropertyValue("--muted");x.font="11px monospace";x.fillText("max "+mx,40,20);
}
window.addEventListener("resize",()=>drawChart());

/* ---------- validator ---------- */
$("#vRun").addEventListener("click",()=>{
  const t=$("#vText").value, out=[];
  const has=s=>new RegExp("^"+s+":","m").test(t);
  const req=["network","wallets","faucet","actions","farming","threading","database"];
  const missing=req.filter(s=>!has(s));
  out.push(missing.length?["bad","missing sections: "+missing.join(", ")]:["ok","all required sections present"]);
  const rpc=(t.match(/rpc_url:\s*(\S+)/)||[])[1]||"";
  out.push(/^https:\/\//.test(rpc)?["ok","rpc TLS ok"]:["bad","rpc_url must be https (http only for 127.0.0.1/localhost) — got: "+(rpc||"—")]);
  const chain=parseInt((t.match(/chain_id:\s*(\d+)/)||[])[1]||"0",10);
  out.push(chain>=1?["ok","chain_id = "+chain]:["bad","chain_id missing or < 1"]);
  if(chain===99999)out.push(["warn","chain_id 99999 is a stub marker (config_flop)"]);
  const mm=(t.match(/min_balance:\s*([\d.]+)/)||[])[1], tg=(t.match(/target_balance:\s*([\d.]+)/)||[])[1];
  if(mm!==undefined&&tg!==undefined)out.push(parseFloat(mm)<=parseFloat(tg)?["ok",`faucet ${mm} ≤ ${tg}`]:["bad",`faucet min_balance ${mm} > target ${tg}`]);
  if(/contract:\s*0x0{40}/.test(t))out.push(["warn","0x0 contract = stub → runtime falls back to transfer; fill a real address for Tier-2"]);
  const gas=parseInt((t.match(/gas_limit:\s*(\d+)/)||[])[1]||"0",10);
  out.push(gas>=1?["ok","gas_limit = "+gas]:["bad","gas_limit must be ≥ 1"]);
  if(gas<50000&&/contract:\s*0x(?!0{40})[0-9a-fA-F]{40}/.test(t))out.push(["bad","real contract action needs gas_limit ≥ 50000"]);
  const mw=parseInt((t.match(/max_workers:\s*(\d+)/)||[])[1]||"0",10);
  out.push(mw>=1&&mw<=1000?["ok","max_workers = "+mw]:["bad","threading.max_workers must be 1..1000"]);
  const dbp=(t.match(/path:\s*(\S+)/)||[])[1]||"";
  out.push(dbp&&!dbp.includes("..")?["ok","database.path = "+dbp]:["bad","database.path missing or contains .."]);
  $("#vOut").innerHTML=out.map(([k,m])=>`<p><span class="pill ${k}">${k}</span> <span class="mono" style="font-size:12.5px">${esc(m)}</span></p>`).join("");
});

/* ---------- perf calc ---------- */
$("#pRun").addEventListener("click",()=>{
  const cpu=Math.max(1,parseInt($("#pCpu").value,10)||8);
  const wIn=parseInt($("#pW").value,10);
  const w=isNaN(wIn)?Math.min(Math.max(20,cpu*2),128):wIn;
  const rpc=Math.min(Math.max(8,cpu*4),128);
  const gen=Math.min(8,Math.max(2,cpu));
  $("#p1").textContent=Math.min(Math.max(20,cpu*2),128)+(isNaN(wIn)?" (auto)":"");
  $("#p2").textContent=rpc;$("#p3").textContent=gen;$("#p4").textContent=Math.max(w*4,100);
});
})();
