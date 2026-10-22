/* harvest common — шапка, тема, язык, навигация, пилюля. Один на всех страницах. */
(function(){
"use strict";
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const toast=(m)=>{const t=$("#toast");if(!t)return;t.textContent=m;t.classList.add("show");clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("show"),1800);};
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const fmt=n=>{n=Number(n);if(!isFinite(n))return "—";if(Math.abs(n)>=1e6)return(n/1e6).toFixed(2)+"M";if(Math.abs(n)>=1e3)return(n/1e3).toFixed(1)+"K";return String(Math.round(n*100)/100);};
window.hv={toast,esc,fmt};

/* ---------- theme ---------- */
const themes=["paper","ink","moss"];
const _store=(()=>{
  try{
    const t={get:(k,f)=>{try{const v=localStorage.getItem(k);return v===null?f:v;}catch(e){return f;}},set:(k,v)=>{try{localStorage.setItem(k,v);}catch(e){}}};
    t.get("hv-theme");return t;
  }catch(e){return{get:(k,f)=>f,set:()=>{}};}
})();
let theme=_store.get("hv-theme","ink");
if(!themes.includes(theme)){theme="ink";_store.set("hv-theme",theme);}
function applyTheme(){document.documentElement.dataset.theme=theme;const b=$("#themeBtn");if(!b)return;b.textContent=theme==="paper"?"◐":theme==="ink"?"◑":"◒";b.setAttribute("aria-label","Theme: "+theme);b.setAttribute("aria-pressed",theme==="paper"?"false":"true");}
const _themeBtn=$("#themeBtn");
if(_themeBtn)_themeBtn.addEventListener("click",()=>{theme=themes[(themes.indexOf(theme)+1)%themes.length];_store.set("hv-theme",theme);applyTheme();if(window.hvRedraw)try{window.hvRedraw();}catch(e){}});
applyTheme();

/* ---------- i18n ---------- */
const EN={
skip:"Skip to content",nav_home:"Home",nav_field:"Field",nav_demo:"Demo",nav_docs:"Docs",nav_live:"Field",nav_faucet:"Faucet",nav_bot:"Bot",nav_specs:"Facts",nav_overview:"Overview",nav_start:"Start",nav_app:"Demo",nav_arch:"Inside",nav_portal:"Portal",nav_sec:"Security",nav_license:"License",
h1:'Testnet farmer,<br><em>not a lottery.</em>',
lede:"Mass wallet generation, faucets, async transaction farming with human-like behavior profiles, multi-RPC with EMA scoring, web portal and Telegram bot. The live field lives on the Field page, builders and simulator — on Demo, the reference — on Docs.",
th_fact:"Fact",th_val:"Value",f_net:"Networks bundled",f_net_cfg:"Network",f_tests:"Tests",f_tests_v:"1467 · 57 files · green run",f_wallet:"Generation",f_rpc:"RPC layer",f_rpc_cfg:"RPC URL",f_portal:"Control",f_dist:"Distribution",
cta_live:"Live field",cta_demo:"Demo zone",
hero_note:"The field reads state.json published by the farm. The rest is fully static: demo keys never leave your browser. Testnets only.",
contacts:"Author:",
hg_t:"Farm in numbers",hg_nets:"Networks",hg_tests:"Tests",hg_themes:"Themes / langs",hg_faucet:"Faucet",hg_term:"Up in 3 commands",
sl_h:"Field — live farm state",sl_sub:"Metrics are published by publish_dash.py from the farm machine into the gh-pages branch. The site re-reads state.json every minute (60 s); the publisher runs on its own schedule — every 30 min by default (HarvestDashPublish task, 15–60 min allowed). If nothing was published yet, the site still fully works — data appears on its own.",
t_net:"network",k_act:"Actions",k_proc:"Processed",k_err:"Errors",k_cyc:"Cycles",k_wal:"Wallets",
fe_t:"Farm is stopped. Run",lg_i:"idle",lg_w:"working",lg_o:"ok",lg_e:"error",lg_r:"rest",
ch1:"Actions / errors over history",ch2:"Wallets processed",tw:"Top wallets",ch:"Cycle history",nets_h:"Networks",
pnl_h:"PnL of the last run",pnl_ok:"Successful actions",pnl_farm:"Farm Δ",pnl_faucet:"Faucet Δ",pnl_fail:"Failures",
pnl_types:"By action type",pnl_type:"Action",pnl_fail_short:"failed",pnl_reasons:"Failure reasons",pnl_reason:"Reason",pnl_cnt:"cases",
pnl_note:"Aggregates only: wallet addresses never reach the public branch. The full report with a per-wallet breakdown and explorer links is written locally by the farm (pnl_report.html).",
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
exe_h:"exe build · portal · tests · field publish",menu_h:"main.py menu",
menu_p:"1 — make wallets · 2 — single cycle · 3 — live console (p pause, q stop) · 4 — balance monitor · 5 — faucet all · 6 — stats · 7/8 — JSON export/import · C — history · 9 — RPC diag · D — doctor · B — backups · G — local web panel (FastAPI :8080) · L — license · S — switch net · H — help. With args main.py delegates to auto (the exe behaves the same).",
s4_h:"Use it here — demo zone",s4_sub:"Two modes: 'Beginner' walks you through in 3 steps, 'Pro' gives every knob plus a parameter reference. Everything runs locally in your browser, no server. On-chain execution stays with the Python farmer.",
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
a7:"Local FastAPI pulpit: buttons, WebSocket log, top and history (main.py → G, 127.0.0.1:8080)",
a_n:"Per-layer detail: §3 of DOCUMENTATION.md in the repo (entry points, portal, configs, Tier-2 for real RPCs/contracts).",
s6_h:"Portal & Telegram",s6_sub:"python -m portal starts aiohttp (web + API) and, with a token, the bot. Secrets via env only. Fail-closed: non-loopback bind with no login method refuses to start.",
po_d:"Purpose",po1:"Session signing, min 16 chars",po2:"BotFather token",po3:"Default-deny user_id list",po4:"DB master key / web-login PIN",po6:"Farmer DB path / farmer config",
tg_p:"Start/stop/pause, field and stats, links, CSV/JSON export, /doctor, /history, /pnl, /export — plus /cmd (ready command), /cfg (ready YAML as file), /check (YAML validation), /ref (reference). Same style as this site: harvest // X sections, [RUN]/[STOP]/[ok] marks, status emoji on the home screen. Values escaped, generic error replies.",
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
q4:"Why is the field empty?",a_q4:"It comes alive after the first publish: run python publish_dash.py on the farm machine (scheduled every 30 min by default, 15–60 allowed). Everything else already works.",
copy:"Copy",
cmdk_ph:"Command or section…",cmdk_hint:"↑↓ select · Enter run · Esc close",
s9_h:"MIT License",s9_sub:"Use, copy, modify, publish and sell freely — keep the copyright notice. No warranty.",
preset_h:"Humanity profile (behavior section in YAML)",
f_all:"ALL",fx_h:"Faucet & diagnostics",fx_p1:"Faucet probe is a quota-free handshake; top-up fills to the network maximum. Without a key the strategy is silently excluded at pre-flight.",fx_p2:"Key: console.chainstack.com → user/settings/api-keys (env CHAINSTACK_API_KEY, never in configs).",bot_h:"Telegram bot",bot_p:"Farm bot: Field, [Profiles], /profiles, /cmd /cfg builders, /check validation. Author contact is separate.",specs_h:"Facts",
ft:"EVM testnet farmer · © 2026 ssrjkk · MIT",
mode_new:"Beginner",mode_pro:"Pro",mode_hint:"Beginner — 3 steps in plain words. Pro — builders, simulator, reference.",
w_s1:"Network",w_s2:"Scale",w_s3:"Launch",
w_q1:"What are we farming?",w_q1s:"Farming means automatic practice transactions on a test network. The money isn't real — free and safe.",
wd_v:"Tokens and launches. Best pick to start.",w_rec:"recommended",wd_r:"The main test network: transfers and swaps.",wd_a:"Token launches and trading. Livelier and faster.",wd_f:"Compute and staking. A stub network for experiments.",
w_q2:"What scale?",w_wallets:"Wallets",w_wallets_s:"Wallets are created automatically. 5–10 is enough for a first try.",
w_cycles:"How many rounds?",w_c2:"2 — trial",w_c0:"∞ — nonstop",w_cycles_s:"One round walks all wallets: faucet first, then transactions.",
w_faucet:"Top up balance from the faucet",w_faucet_s:"A faucet is a free giveaway of test coins. Turn off only if you already have coins.",
w_q3:"All set. One command left.",w_sum_net:"Network",w_sum_w:"Wallets",w_sum_c:"Rounds",w_sum_f:"Faucet",
w_copy:"Copy command",w_dl:"Download config",w_back:"← Back",w_next:"Next →",
w_n1:"Install Python 3.12 and run: pip install -r requirements.txt",w_n2:"Put the downloaded config file next to auto.py",w_n3:"Paste the command into PowerShell and press Enter",w_n4:"The site field comes alive after: python publish_dash.py",
tab6:"6 · Reference",
r_cfg_h:"Config YAML — every field with bounds",r_cfg_s:"Out-of-bounds = validator refusal and SystemExit(1). 0x0 stubs and chain 99999 are remarks, not errors: transfers work, contract calls are skipped.",
r_f:"Field",r_b:"Bounds",r_d:"Default / what it does",
rd_rpc:"Multi-RPC: the monitor picks the best live node every 30s",
rd_w:"How many to top up when generate_if_missing; 0 in --wallets = take from config",
rd_aw:"transfer, vibevibe_*, flop_*, arc_*. Zero weight = action disabled",
rd_tt:"Required for transfer. Random = from your own wallet pool",
rd_cm:"Stub = silent fallback to transfer; buy/sell without signer_key are skipped by adaptive weights",
rd_amt:"Amount jitter + odd_amount_prob 'uneven' values vs sybil filters",
rd_del:"Pauses are multiplied by the wallet's delay_*_scale profile",
rd_rc:"30 · on expiry wait_for_receipt = None without nonce rollback; 3+ poll errors = node failover",
rd_th:"Auto: max(4,min(workers·3,128)) · min(8,max(2,cpu)) · max(workers·4,100)",
rd_ca:"Token bucket over RPC; rate floats with node health, remainder carried to the new node",
rd_db:"SQLite WAL by default; postgres:// DSN enables PostgreSQL. No key with encrypted seeds = refusal",
rd_log:"File + console, rotation with restricted permissions, optional JSON format",
r_env_h:"Farmer ENV overrides (FARMER_*)",r_over:"Overrides",
re_rpc:"network.rpc_url; list via ';' or comma",
re_keys:"EIP-712 buy/sell signing · MCP-faucet Bearer key (without it the chainstack strategy drops at pre-flight)",
re_misc:"license password (else getpass, 3 tries) · config path",
r_tim_h:"Hard kernel timings",r_what:"Mechanism",r_num:"Numbers",
rt1:"RPC retries with round-robin failover",rt2:"Node EMA / zombie / circuit breaker",rt3:"RPC monitor / probe / supervisor",rt4:"BatchWriter",rt5:"License",rt6:"Portal: sessions / login / TG",rt7:"Scheduler / single-instance",
r_src:"Source of truth — DOCUMENTATION.md (§2–§6) and core/ code. Portal env vars are in 'Portal & Telegram' above."
};
let lang=_store.get("hv-lang","ru");
window.hv.lang=()=>lang;
window.hv.onLang=f=>{try{_langSubs.push(f);}catch(e){}};
const _langSubs=[];
const RU={};
function snapRU(){$$("[data-i18n]").forEach(el=>{const k=el.dataset.i18n;if(!(k in RU))RU[k]=el.innerHTML;});$$("[data-i18n-ph]").forEach(el=>{const k=el.dataset.i18nPh;if(!(k in RU))RU[k]=el.placeholder;});}
function applyLangStrings(){const D=lang==="en"?EN:RU;$$("[data-i18n]").forEach(el=>{const k=el.dataset.i18n;if(D[k]!==undefined)el.innerHTML=D[k];});$$("[data-i18n-ph]").forEach(el=>{const k=el.dataset.i18nPh;if(D[k]!==undefined)el.placeholder=D[k];});}
function applyLang(){
  document.documentElement.lang=lang==="ru"?"ru":"en";
  const lr=$("#langRu"),le=$("#langEn");
  if(lr){lr.classList.toggle("on",lang==="ru");lr.setAttribute("aria-pressed",lang==="ru"?"true":"false");}
  if(le){le.classList.toggle("on",lang==="en");le.setAttribute("aria-pressed",lang==="en"?"true":"false");}
  applyLangStrings();
  _langSubs.forEach(f=>{try{f();}catch(e){}});
  window.dispatchEvent(new Event("hv:lang"));
}
function setLang(l){if(l===lang)return;lang=l;_store.set("hv-lang",l);applyLang();}
const _lr=$("#langRu"),_le=$("#langEn");
if(_lr)_lr.addEventListener("click",()=>setLang("ru"));
if(_le)_le.addEventListener("click",()=>setLang("en"));
snapRU();
if(lang==="en")applyLang();

/* ---------- nav (burger ≤900px) ---------- */
const navToggle=$("#navToggle"), mainNav=$("#mainNav"), navScrim=$("#navScrim");
function syncNav(){
  if(!mainNav||!navToggle)return;
  const open=mainNav.classList.contains("open");
  navToggle.setAttribute("aria-expanded",String(open));
  navToggle.textContent=open?"✕":"≡";
  if(navScrim)navScrim.hidden=!open;
}
function closeNav(refocus){
  if(!mainNav||!navToggle)return;
  if(!mainNav.classList.contains("open"))return;
  mainNav.classList.remove("open");
  syncNav();
  if(refocus)navToggle.focus();
}
if(navToggle)navToggle.addEventListener("click",()=>{
  if(!mainNav)return;
  mainNav.classList.toggle("open");
  syncNav();
});
if(navScrim)navScrim.addEventListener("click",()=>closeNav());
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeNav(true);});
document.addEventListener("keydown",e=>{
  if(e.key!=="Tab"||!mainNav||!mainNav.classList.contains("open"))return;
  const f=Array.from(mainNav.querySelectorAll("a[href],button:not([disabled])")).filter(el=>el.offsetParent!==null);
  if(!f.length)return;
  const first=f[0],last=f[f.length-1];
  if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}
  else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
});
if(mainNav)mainNav.addEventListener("click",e=>{if(e.target.closest("a"))closeNav();});
syncNav();

/* ---------- copy ---------- */
const Tcopied=()=>lang==="en"?"copied":"скопировано";
const TcopyFail=()=>lang==="en"?"select + Ctrl+C":"выделите + Ctrl+C";
document.addEventListener("click",e=>{
  const b=e.target.closest("[data-copy]");if(!b)return;
  const pre=document.getElementById(b.dataset.copy);if(!pre)return;
  const txt=pre.textContent.replace(/\n?copy\s*$/,"");
  (navigator.clipboard?navigator.clipboard.writeText(txt):Promise.reject()).then(()=>toast(Tcopied())).catch(()=>{var ta=document.createElement("textarea");ta.value=txt;document.body.appendChild(ta);ta.select();try{document.execCommand("copy");toast(Tcopied());}catch(_){toast(TcopyFail());}ta.remove();});
});

/* ---------- live pill (лёгкий опрос для шапки всех страниц) ---------- */
async function pillState(){
  try{const r=await fetch("state.json?t="+Date.now(),{cache:"no-store"});if(!r.ok)throw 0;return await r.json();}
  catch(e){return null;}
}
function paintPill(a,b){
  const dot=$("#pillDot"),txt=$("#pillTxt");
  if(!dot||!txt)return;
  if(typeof a==="string"){dot.className="dot "+a;txt.textContent=b||"—";return;}
  const s=a;
  const L=lang==="en"?{off:"stopped",pause:"paused",on:"running"}:{off:"стоп",pause:"пауза",on:"работает"};
  if(!s){dot.className="dot mut";txt.textContent="—";return;}
  const pool=s.pool||{};
  if(!s.running){dot.className="dot mut";txt.textContent=L.off;}
  else if(pool.paused){dot.className="dot pau";txt.textContent=L.pause;}
  else{dot.className="dot on";txt.textContent=L.on;}
}
window.hv.pill=paintPill;
window.hvLast=null;
async function pollOnce(){
  const s=await pillState();
  window.hvLast=s;
  paintPill(s);
  window.dispatchEvent(new CustomEvent("hv:state",{detail:s}));
  return s;
}
setInterval(async()=>{if(document.hidden)return;try{await pollOnce();}catch(e){}},60000);
window.addEventListener("online",()=>{pollOnce().catch(()=>{});});
pollOnce().catch(()=>{});

/* ---------- появление при скролле ----------
   Прячет только то, что помечено data-reveal, и только когда <html> несёт
   класс rv (его ставит инлайн-скрипт в <head> и только при разрешённом
   motion). Сетки раскрываются каскадом через --d, остальное целиком. */
(function reveal(){
  const secs=$$("[data-reveal]");
  if(!secs.length)return;
  const targets=[];
  secs.forEach(sec=>{
    const kids=sec.querySelectorAll(".grid2 > *,.grid3 > *,.grid4 > *,.grid5 > *,.optgrid > *");
    if(kids.length){
      Array.prototype.forEach.call(kids,(k,i)=>{k.setAttribute("data-reveal","");k.style.setProperty("--d",(i*55)+"ms");targets.push(k);});
    } else targets.push(sec);
  });
  const all=()=>targets.forEach(el=>el.classList.add("in"));
  try{
    if(matchMedia("(prefers-reduced-motion: reduce)").matches||!("IntersectionObserver" in window)){all();return;}
    const io=new IntersectionObserver(ents=>{
      ents.forEach(en=>{if(!en.isIntersecting)return;en.target.classList.add("in");io.unobserve(en.target);});
    },{rootMargin:"0px 0px -6% 0px",threshold:.06});
    targets.forEach(el=>io.observe(el));
    setTimeout(all,1500); // страховка: контент не должен остаться невидимым
  }catch(e){all();}
})();

/* ---------- палитра команд (Ctrl+K) ----------
   Разделы текущей страницы, переходы между страницами, тема и язык.
   Список собирается при открытии — тогда подписи всегда на текущем языке. */
(function palette(){
  const box=$("#cmdk"),inp=$("#cmdkIn"),list=$("#cmdkList"),scrim=$("#cmdkScrim");
  if(!box||!inp||!list)return;
  const T=()=>lang==="en"
    ?{theme:"Theme",lang:"Language",ru:"Russian",en:"English",none:"Nothing found",page:"page",section:"section"}
    :{theme:"Тема",lang:"Язык",ru:"Русский",en:"English",none:"Ничего не найдено",page:"страница",section:"раздел"};
  let items=[],cur=0;
  function build(){
    items=[];
    $$("nav.main a").forEach(a=>items.push({t:(a.textContent||"").trim(),href:a.getAttribute("href"),k:T().page}));
    $$("[data-reveal]").forEach(sec=>{
      if(!sec.id)return;
      const h=sec.querySelector("h1,h2");
      if(h)items.push({t:(h.textContent||"").replace(/\s+/g," ").trim(),href:"#"+sec.id,k:T().section});
    });
    items.push({t:T().theme,run:()=>{const b=$("#themeBtn");if(b)b.click();},k:T().theme});
    items.push({t:T().lang+": "+T().ru,run:()=>{const b=$("#langRu");if(b)b.click();},k:T().lang});
    items.push({t:T().lang+": "+T().en,run:()=>{const b=$("#langEn");if(b)b.click();},k:T().lang});
  }
  function render(){
    const s=(inp.value||"").trim().toLowerCase();
    const out=s?items.filter(i=>i.t.toLowerCase().includes(s)):items;
    if(cur>=out.length)cur=0;
    list.textContent="";
    if(!out.length){
      const e=document.createElement("div");e.className="cmdk-empty";e.textContent=T().none;list.appendChild(e);
      return;
    }
    out.forEach((it,i)=>{
      const b=document.createElement("button");
      b.type="button";b.className="cmdk-item";b.setAttribute("role","option");
      b.setAttribute("aria-selected",i===cur?"true":"false");
      const t=document.createElement("span");t.className="t";t.textContent=it.t;
      const k=document.createElement("span");k.className="k";k.textContent=it.k;
      b.appendChild(t);b.appendChild(k);
      b.addEventListener("click",()=>run(it));
      list.appendChild(b);
    });
  }
  function move(d){
    const its=list.querySelectorAll(".cmdk-item");
    if(!its.length)return;
    its[cur].setAttribute("aria-selected","false");
    cur=(cur+d+its.length)%its.length;
    its[cur].setAttribute("aria-selected","true");
    its[cur].scrollIntoView({block:"nearest"});
  }
  function run(it){hide();if(it.run){it.run();return;}if(it.href)location.href=it.href;}
  function show(){build();cur=0;inp.value="";render();box.classList.add("open");document.body.style.overflow="hidden";setTimeout(()=>inp.focus(),0);}
  function hide(){box.classList.remove("open");document.body.style.overflow="";}
  inp.addEventListener("input",()=>{cur=0;render();});
  inp.addEventListener("keydown",e=>{
    if(e.key==="ArrowDown"){e.preventDefault();move(1);}
    else if(e.key==="ArrowUp"){e.preventDefault();move(-1);}
    else if(e.key==="Enter"){
      e.preventDefault();
      const it=list.querySelectorAll(".cmdk-item")[cur];
      if(it)it.click();
    }
  });
  if(scrim)scrim.addEventListener("click",hide);
  document.addEventListener("keydown",e=>{
    const open=box.classList.contains("open");
    if((e.ctrlKey||e.metaKey)&&(e.key==="k"||e.key==="K")){e.preventDefault();open?hide():show();return;}
    if(e.key==="Escape"&&open){hide();return;}
    if(e.key==="/"&&!open){
      const a=document.activeElement;
      if(a&&(a.tagName==="INPUT"||a.tagName==="TEXTAREA"||a.isContentEditable))return;
      e.preventDefault();show();
    }
  });
})();
})();
