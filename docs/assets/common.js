/* harvest common — шапка, тема, язык, навигация, пилюля. Один на всех страницах. */
(function(){
"use strict";
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const toast=(m)=>{const t=$("#toast");if(!t)return;t.textContent=m;t.classList.add("show");clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("show"),1800);};
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const fmt=n=>{n=Number(n);if(!isFinite(n))return "—";if(Math.abs(n)>=1e6)return(n/1e6).toFixed(2)+"M";if(Math.abs(n)>=1e3)return(n/1e3).toFixed(1)+"K";return String(Math.round(n*100)/100);};
const VERSION="v2.4.1";
window.hv={toast,esc,fmt,version:VERSION};
/* Подпись в консоли разработчика: сайт статичный, ключи из демо-генератора
   никуда не уходят. Видно только с открытыми devtools. */
try{console.log("%c harvest "+VERSION+" %c EVM testnet farmer · ключи остаются в браузере ","background:#D4A72C;color:#14110A;font-weight:bold","color:#9A8F73")}catch(e){}

/* ---------- theme ---------- */
const themes=["paper","ink","moss","terminal"];
const _themeGlyph={paper:"◐",ink:"◑",moss:"◒",terminal:"◓"};
const _store=(()=>{
  try{
    const t={get:(k,f)=>{try{const v=localStorage.getItem(k);return v===null?f:v;}catch(e){return f;}},set:(k,v)=>{try{localStorage.setItem(k,v);}catch(e){}}};
    t.get("hv-theme");return t;
  }catch(e){return{get:(k,f)=>f,set:()=>{}};}
})();
let theme=_store.get("hv-theme","terminal");
if(!themes.includes(theme)){theme="terminal";_store.set("hv-theme",theme);}
function applyTheme(){document.documentElement.dataset.theme=theme;const b=$("#themeBtn");if(!b)return;b.textContent=_themeGlyph[theme]||"◓";b.setAttribute("aria-label","Theme: "+theme);b.setAttribute("aria-pressed",theme==="paper"?"false":"true");}
const _themeBtn=$("#themeBtn");
if(_themeBtn)_themeBtn.addEventListener("click",()=>{theme=themes[(themes.indexOf(theme)+1)%themes.length];_store.set("hv-theme",theme);applyTheme();if(window.hvRedraw)try{window.hvRedraw();}catch(e){}});
applyTheme();

/* ---------- i18n ---------- */
const EN={
skip:"Skip to content",copy:"Copy",nav_home:"Home",nav_field:"Field",nav_demo:"Demo",nav_docs:"Docs",ft_map:"Map",
h1:'A testnet farm.<br><em>No promises.</em>',
lede:"harvest is a tool for practising on EVM testnets: it creates wallets, pulls test coins from faucets, sends transactions, and shows you what came out. Written in Python, runs on your machine.",
th_fact:"Fact",th_val:"Value",f_net:"Networks bundled",f_net_cfg:"Network",f_tests:"Tests",f_tests_v:"pytest · contract scenarios · CI",f_wallet:"Generation",f_wallet_v:"BIP-39 · ~60 wallets/sec · AES-256-GCM",f_rpc:"RPC layer",f_rpc_v:"multi-RPC · EMA · circuit breaker · TTL caches",f_rpc_cfg:"RPC URL",f_portal:"Control",f_portal_v:"CLI auto.py · Rich menu main.py · portal + Telegram bot",f_dist:"Distribution",f_dist_v:"PyInstaller exe · Docker + compose",
cta_live:"Open the field",cta_demo:"Try the demo",cta_bot:"Telegram bot",
hero_kicker:"TESTNETS · PYTHON 3.12 · MIT",hero_kicker_r:"NO DROP PROMISES",
hero_note:"Testnets only. Demo keys stay in your browser; the farm runs on your computer. No drop or profit guarantees: it's a tool, not a lottery ticket.",
contacts:"Author:",
hg_t:"What's inside",hg_nets:"Networks",hg_nets_v:"4 testnets · Robinhood, Flop, Arc, vibe/vibe",hg_control:"Control",hg_control_v:"Python CLI · menu · web portal · Telegram",hg_themes:"Themes / languages",hg_themes_v:"Paper · Ink · Moss · Terminal / RU · EN",hg_license:"License",hg_license_v:"MIT · read it, change it",hg_term:"Three commands to the menu",
net_vibe_note:"— launchpad on Robinhood",net_vibe_act:"transfer · buy/sell — with signer_key",
code_menu:"# menu: network → farm",code_dry:"# dry run, no transactions",code_portal:"# aiohttp + API + bot",code_tests:"# local check",code_publish:"# state.json → gh-pages → site field",
sl_h:"The field — what's happening now",sl_sub:"The farm publishes a snapshot here: metrics, history and the wallet grid. The site re-reads state.json once a minute; a publish usually happens every 30 minutes. If there's no snapshot yet, the page will be empty — that's normal, not a broken site.",
t_net:"network",k_act:"Actions",k_proc:"Processed",k_err:"Errors",k_cyc:"Cycles",k_wal:"Wallets",
fe_t:"No data yet. When the farm publishes its first snapshot, the field will come alive. You can start it by hand:",fe_pub:"then <code>python publish_dash.py</code>.",lg_i:"idle",lg_w:"working",lg_o:"ok",lg_e:"error",lg_r:"rest",
ch1:"Actions / errors over history",ch2:"Wallets processed",tw:"Top wallets",ch:"Cycle history",nets_h:"Networks",
pnl_h:"PnL of the last run",pnl_ok:"Successful actions",pnl_farm:"Farm Δ",pnl_faucet:"Faucet Δ",pnl_fail:"Failures",
pnl_types:"By action type",pnl_type:"Action",pnl_fail_short:"failed",pnl_reasons:"Failure reasons",pnl_reason:"Reason",pnl_cnt:"cases",
pnl_note:"Aggregates only: wallet addresses never reach the public branch. The full report with a per-wallet breakdown and explorer links is written locally by the farm (pnl_report.html).",
s1_h:"Why this exists",s1_sub:"The short version: I like tinkering with testnets, and I wanted to take the routine out of it. And to show how it all works, without the fog.",
letter_p1:"I started harvest with a simple problem: I had several test networks and a pile of identical actions I was doing by hand. Create wallets, wait for the faucet, send a transfer, write down what happened. After a couple of evenings it was clear this belonged in a script, while I watched the numbers and stepped in now and then.",
letter_p2:"So there's no \"secret strategy\" here, and no promise that someone will notice you and hand you something. There's a Python farm, configs for specific networks, a portal to start and stop it, and this site. You can read all of it, change it, and run it yourself. If something breaks, the logs will show what.",
letter_p3:"The site is deliberately static. The demo zone computes keys in your browser and never sends them anywhere. The field shows only what the farm chose to publish: no addresses, no private keys.",
letter_sign:"— ssrjkk, author of harvest",
c1_l:"Wallets",c1_h:"Create them, don't lose them",c1_p:"BIP-39 via eth_account, CSV/JSON import and export, AES-256-GCM seed encryption. Running it again doesn't breed duplicates.",
c2_l:"Faucet",c2_h:"Wait for fuel",c2_p:"chainstack, direct and web strategies, retries, limits and proxies. If no live faucet is available, the farm says so honestly instead of pretending.",
c3_l:"Farming",c3_h:"Send the transactions",c3_p:"Async pool, chunks, crash recovery from state.json. The behavior profile is tied to the address, so a restart doesn't change the pattern.",
limits_h:"Limits worth knowing up front",
lim1_h:"Testnets only",lim1_p:"No mainnet, no real money, no real keys. This is a sandbox for practice, not a way to earn.",
lim2_h:"No guarantees",lim2_p:"The project doesn't promise a drop, points or profit. A testnet can close, the rules can change, and your activity may not count.",
lim3_h:"Keys are yours",lim3_p:"The demo generator keeps keys only in tab memory. The farm keeps them in your DB; encryption needs a master key. Backups are your responsibility.",
lim4_h:"Not fully automatic",lim4_p:"Configs, RPCs and contracts sometimes need a manual fix. If a network is a stub, some actions will be skipped — and that's fine.",
s2_h:"Four testnets",s2_sub:"Pick a network in the main.py menu or with --config. Each network has its own config and DB. For dense farming without contracts there's config.simple.yaml: transfers only, gas_limit 21000.",
n_net:"Network",n_cfg:"Config",n_cur:"Currency",n_act:"Actions",
vibe_note:"* The shipped config_vibevibe.yaml includes transfer. buy/sell on VibePassMarket need an EIP-712 TradeIntent signature with the tradeSigner key (vibevibe.signer_key or env FARMER_VIBE_SIGNER_KEY). Without the key they're skipped; transfer, claimRewards and stake work. ABI lives in abi/vibevibe.json.",
net_note:"99999 on Flop is a stub marker: transfers work, contract calls without real addresses fall back to transfer. It's not a config error.",
s3_h:"Try it without rushing",s3_sub:"The first target is Windows/PowerShell and the exe build, but Python 3.12 works anywhere. Start with 3–5 wallets: it's easier to see what's happening.",
flags_h:"auto.py flags",flags_d:"Effect",
fl1:"N wallets (0 = from config; default 50)",fl2:"N cycles (0 = forever)",fl3:"Config file ($FARMER_CONFIG → config.yaml)",fl4:"Skip faucet / farm",fl5:"Pipeline every H hours (min 0.05)",fl6:"Faucet batch · workers · minimal delays",fl7:"Dry run · CSV+JSON export · encrypt",fl8:"Cycle history · diagnostics · version",
exe_h:"exe build · portal · tests · field publish",menu_h:"main.py menu",
menu_p:"1 — create wallets · 2 — one cycle · 3 — live console (p pause, q stop) · 4 — balance monitor · 5 — faucet all · 6 — stats · 7/8 — JSON export/import · C — history · 9 — RPC diagnostics · D — doctor · B — backups · G — local web panel (FastAPI, 127.0.0.1:8080) · L — license · S — switch network · H — help. With arguments main.py delegates to auto (the exe behaves the same).",
docs_more:"Details are in the <a href=\"docs.html\">docs</a> and DOCUMENTATION.md. If something doesn't add up, it's a bug, not a feature — tell the author.",
start_note:"Tip: run --dry-run first, then 3 wallets and one cycle. The farm doesn't ask for trust — every step is visible in the logs.",
s4_h:"Demo — try it without the farm",s4_sub:"Two modes: 'Beginner' walks you through three steps without a terminal, 'Pro' gives you the builders, generator, simulator and reference. The site is static: everything is computed in your browser, there is no server.",
tab1:"1 · Config",tab2:"2 · Command",tab3:"3 · Wallets",tab4:"4 · Simulator",tab5:"5 · Check",
f_db:"DB / master key file",f_wallets:"Wallets",f_workers:"Workers",f_target:"Faucet: min → target",f_actions:"Actions per cycle [min, max]",f_gas:"gas_limit / min_gas_for_action",
gen_cfg:"Generate YAML",dl:"Download file",
f_cfgfile:"Config file",o_sf:"no faucet (force_farm)",o_sfarm:"faucet+export only",o_fast:"minimal delays",o_dry:"dry run",o_enc:"encrypt export",
cmd_out:"Ready command",build:"Build",cmd_sim:"What will happen",cmd_sim_p:"The command goes through four phases: top up wallets → check the faucet and request coins → farming pool with chunks and recovery → CSV/JSON export. Running it again resumes state.json where it stopped.",
w_warn:"Testnets only. Keys are generated in your browser and are never sent anywhere. The first time you press Generate, the page loads ethers from esm.sh — that's the demo zone's only external request. Without a network it switches to an honest demo mode with a label.",
w_n:"How many to generate",w_gen:"Generate",w_clear:"Clear",w_act:"actions",
s_w:"Wallets",s_c:"Cycles",s_skip:"skip_cycle_probability",s_a:"Actions [min,max]",s_fail:"Action failure chance",s_speed:"Log speed",
sim_run:"Run simulation",sim_stop:"Stop",sim_exp:"Export history CSV",sim_log:"Live log (like the operator terminal)",sim_chart:"Actions per cycle",
v_h:"Paste a config YAML — checks like config_validate.py",v_run:"Validate",
p_h:"Parallelism calculator (core/performance.py)",p_w:"max_workers (empty = auto)",p_run:"Calculate",
p_n:"Formulas: workers=min(max(20,cpu·2),128) · rpc=min(max(8,cpu·4),128) · gen=min(8,max(2,cpu)) · chunk=max(workers·4,100). RPC cap 128, gen cap 8.",
s5_h:"How it works",s5_sub:"main.py is the human menu; auto.py is the same pipeline for scripts. Both share one core/. One NetworkManager per session — latency, caches and the monitor are shared. DB writes go through BatchWriter in batches, with a pause after a run of failures.",
a_mod:"Module",a_do:"Owns",
a1:"30s ping, 2500ms probe, ×3 retries, failover, 30s receipt budget",
a2:"Priority queue, retire-without-cancel, respawn with backoff",
a3:"2s/500-row flush, dropped_count, 30s cooldown",
a4:"ABI, EIP-712 TradeIntent, default gas 300000",
a5:"wallets/actions_log/nonces/cycle_history, VACUUM, backups",
a6:"Config → key → DB → RPC → faucet → license, exit code",
a7:"Local FastAPI pulpit: buttons, WebSocket log, top and history (main.py → G, 127.0.0.1:8080)",
a_n:"Per-layer detail: §3 of DOCUMENTATION.md in the repo (entry points, portal, configs, Tier-2 for real RPCs/contracts).",
s6_h:"Portal & Telegram",s6_sub:"python -m portal brings up aiohttp (web + API) and, if a token is set, the Telegram bot. Secrets come only from env. If the portal is exposed beyond loopback and there's no way to log in, it refuses to start.",
po_d:"Purpose",po1:"Session signing, min 16 chars",po2:"BotFather token",po3:"Default-deny user_id list",po4:"DB master key / web-login PIN",po6:"Farmer DB path / farmer config",
tg_p:"Start/stop/pause, field and stats, links, CSV/JSON export, buttons under messages, commands: /status, /monitor, /doctor, /diag, /history, /pnl, /balance, /wallets, /profiles, /export, /network, /faucet, /rpc, /scheduler, /alerts — plus /cmd (ready command), /cfg (ready YAML as file), /check (YAML validation), /ref (reference). Same style as this site: harvest // X sections, [RUN]/[STOP]/[ok] marks, status emoji on the home screen. Values escaped, generic error replies.",
dp_p:"Ready kit: Dockerfile + compose, long-polling with no public URL, Caddy for HTTPS later, .env template, healthcheck, data volume.",
s7_h:"Security — closed by default",
b1_h:"Encryption",b1_p:"Seeds: AES-256-GCM (12B nonce, AAD wallet-seed). Export files: AES-256-CBC v2 — PBKDF2-SHA256 100k, separated encrypt/MAC keys, HMAC verified before writing.",
b2_p:"RPC URLs are redacted everywhere: credentials before @ and query tokens stripped. Logs, cycle history, CLI output.",
b3_p:"No master key with encrypted seeds → refuse. Portal default-deny, single-instance DB lock, restricted file permissions, login limits 5/10min per IP.",
sec_rep:"If you find a vulnerability, don't open a public issue. Use GitHub private vulnerability reporting or contact the maintainer directly. Only the latest release is supported; I try to reply within 48 hours.",
s8_h:"Checks & questions",t1:"wait_for_receipt: EMA poll budget, failover on error streaks, degraded poll rate, None on budget without nonce rollback, secondary-node cross-check.",t2:"Lint: ruff · flake8 · black (120) · mypy · bandit. CI runs the same checks you can run locally; core/portal coverage is a CI gate, not a number to brag about on the site.",t3:"The load test is opt-in: <code class=\"inline\">$env:HARVEST_LOAD=1; python -m pytest tests/test_load_opt.py -q</code>.",
q1:"Is this for mainnet?",a_q1:"No. Testnets and practice activity only. Don't use real funds or real keys in the site demo.",
q2:"Where are my demo keys?",a_q2:"Only in this tab's memory. The site is static, nothing is uploaded — close the tab or press Clear and the keys are gone. For permanent storage use the Python-side export and encryption.",
q3:"What about 0x0 contracts in config?",a_q3:"Stubs by design: the validator passes them with a remark, runtime falls back to transfer. Fill in real addresses from the Tier-2 docs when ready.",
q4:"Why is the field empty?",a_q4:"It comes alive after the first publish: run python publish_dash.py on the farm machine (every 30 minutes by default, 15–60 allowed; main history doesn't grow — the snapshot goes to the gh-pages branch). If there's no data, it's not a site error — the farm just hasn't sent anything yet.",
q5:"Do you promise a drop?",a_q5:"No. harvest doesn't guarantee a drop, points or profit. Testnets change, and activity may not count. The tool helps you practise carefully, but the outcome isn't up to it.",
cmdk_ph:"Command or section…",cmdk_hint:"↑↓ select · Enter run · Esc close",
s9_h:"MIT License",s9_sub:"Use, copy, modify, publish and sell freely — keep the copyright notice. No warranty.",
preset_h:"Wallet character (behavior section in YAML)",preset_stealth:"Quiet",preset_balanced:"Even",preset_degen:"Lively",preset_note:"Only changes behavior: pauses, activity, amount spread. It doesn't affect the run command.",
f_all:"ALL",fx_h:"Faucet & diagnostics",fx_p1:"The faucet checks the key and quota first, and only then asks for coins. Without CHAINSTACK_API_KEY the strategy simply doesn't turn on — that's not an error, it's an honest skip.",fx_p2:"Key: console.chainstack.com → user/settings/api-keys. Keep it in the CHAINSTACK_API_KEY env var, not in the config.",bot_h:"Telegram bot",bot_p:"The farm bot shows the field, computes PnL and understands commands: /status /monitor /doctor /history /pnl /balance /wallets /profiles /export /network /pause /resume /stop /faucet /rpc /scheduler /alerts /cmd /cfg /check /ref, plus step-by-step /cmd and /cfg wizards. For author contact there's a separate @ssrjkk_bot.",specs_h:"Project at a glance",
ft:"EVM testnet farmer · © 2026 ssrjkk · MIT",
mode_new:"Beginner",mode_pro:"Pro",mode_hint:"Beginner — three steps in plain words. Pro — builders, simulator and reference.",
w_s1:"Network",w_s2:"Scale",w_s3:"Launch",
w_q1:"What are we farming?",w_q1s:"Farming here means practice transactions on a test network. The money isn't real, and it costs nothing.",
wd_v:"Tokens and launches. Most people start here.",w_rec:"calm start",wd_r:"The main test network: transfers and swaps.",wd_a:"Token launches and trading. Busier, more log events.",wd_f:"Compute and staking. A stub network for experiments.",
w_q2:"What scale?",w_wallets:"Wallets",w_wallets_s:"Wallets are created automatically. 5–10 is enough for a first try.",
w_cycles:"How many rounds?",w_c2:"2 — trial",w_c0:"∞ — nonstop",w_cycles_s:"One round walks all wallets: faucet first, then transactions.",
w_faucet:"Top up balance from the faucet",w_faucet_s:"A faucet is a free giveaway of test coins. Turn off only if you already have coins.",
w_q3:"All set. One command left.",w_sum_net:"Network",w_sum_w:"Wallets",w_sum_c:"Rounds",w_sum_f:"Faucet",
w_copy:"Copy command",w_dl:"Download config",w_back:"← Back",w_next:"Next →",
w_n1:"Install Python 3.12 and run: pip install -r requirements.txt",w_n2:"Put the downloaded config file next to auto.py",w_n3:"Paste the command into PowerShell and press Enter",w_n4:"The site field comes alive after: python publish_dash.py",
tab6:"6 · Reference",
r_cfg_h:"Config YAML — every field with bounds",r_cfg_s:"Out-of-bounds = validator refusal and SystemExit(1). 0x0 stubs and chain 99999 are remarks, not errors: transfers work, contract calls are skipped.",r_f:"Field",r_b:"Bounds",r_d:"Default / what it does",
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
rt1:"RPC retries with round-robin failover",rt1_n:"3 attempts · retryable classification by error text",rt2:"Node EMA / zombie / circuit breaker",rt2_n:"α 0.7/0.3 · death after 3 errors · breaker 5 errors / 30s",rt3:"RPC monitor / probe / supervisor",rt3_n:"30s · 2500ms ping · restart with backoff 1s→30s",rt4:"BatchWriter",rt4_n:"flush 2s / 500 rows · 3 straight failures = batch drop + 30s cooldown",rt5:"License",rt5_n:"hub timeout 8s · body ≤ 1MB · offline grace 7 days · .license_cache",rt6:"Portal: sessions / login / TG",rt6_n:"7-day session · login 5/10min per-IP + 20/10min global · initData 16KB, 10min window",rt7:"Scheduler / single-instance",rt7_n:"--schedule ≥ 0.05h · db_path.lock, one farm per DB",
rd_rpc_b:"https URL or list; http — 127.0.0.1/localhost/::1 only",
rd_chain_b:"≥ 1; 99999 = stub marker",rd_chain_d:"Robinhood/vibe 46630 · Arc 5042002",
rd_fbal_b:"min ≤ target",rd_fbal_d:"ensure_balance tops up to target · vibe/robinhood 0.005→0.05 · flop/arc 0.01→0.1 · simple 0.003→0.015",
rd_fret_b:"1..20 / 1..64",rd_fret_d:"2 · faucet batch --batch 50, 3s pause",
rd_act_b:"type from set; weight ≥ 0",rd_tt_b:"address or random_wallet",
rd_cm_b:"0x address + method name; 0x0…0 = stub",
rd_amt_b:"≥ 0, min ≤ max",rd_apc_b:"[min, max], min ≤ max",rd_apc_d:"[3,8] vibe/robinhood · [5,15] flop · [5,12] arc · [2,4] simple",
rd_del_b:"[min, max] seconds",rd_skip_d:"0.05 · scaled by activity and time of day (night ×~1.5)",
rd_gas_b:"≥ 1; ≥ 50000 for real contracts",rd_gas_d:"300000 robinhood/flop · 200000 arc · 100000 vibe · 21000 simple",
rd_mgas_d:"0.003 · below — action skipped",rd_rc_b:"seconds",
rd_workers_d:"20 vibe/robinhood · 30 flop/arc · 24 simple; empty = auto min(max(20,cpu·2),128)",
rd_db_b:"no '..'; 64-hex key or file",rd_log_b:"level name",
re_chain:"chain_id / wallets.count",re_threads:"threading.*",re_faucetc:"faucet / cache",
re_faucetbal:"faucet balances",re_logdb:"logs / DB / encryption key",
cmd_sched:"--schedule (hours, empty — one-shot)",
aria_menu:"Menu",aria_filters:"Field filters",aria_f_all:"All wallets",
aria_f_i:"Sleeping",aria_f_w:"Working",aria_f_o:"Successful",aria_f_e:"With errors",aria_f_r:"Resting",
aria_q:"Wallet number",aria_grid:"Wallet grid",aria_mode:"Mode",aria_protabs:"Pro tabs",
aria_vtext:"Config YAML to validate",
r_src:"Source of truth — DOCUMENTATION.md (§2–§6) and core/ code. Portal env vars are in 'Portal & Telegram' above."
};
let lang=_store.get("hv-lang","ru");
window.hv.lang=()=>lang;
const _langSubs=[];
window.hv.onLang=f=>{try{_langSubs.push(f);}catch(e){}};
const RU={};
function snapRU(){$$("[data-i18n]").forEach(el=>{const k=el.dataset.i18n;if(!(k in RU))RU[k]=el.innerHTML;});$$("[data-i18n-ph]").forEach(el=>{const k=el.dataset.i18nPh;if(!(k in RU))RU[k]=el.placeholder;});$$("[data-i18n-aria]").forEach(el=>{const k=el.dataset.i18nAria;if(!(k in RU))RU[k]=el.getAttribute("aria-label")||"";});}
function applyLangStrings(){const D=lang==="en"?EN:RU;$$("[data-i18n]").forEach(el=>{const k=el.dataset.i18n;if(D[k]!==undefined)el.innerHTML=D[k];});$$("[data-i18n-ph]").forEach(el=>{const k=el.dataset.i18nPh;if(D[k]!==undefined)el.placeholder=D[k];});$$("[data-i18n-aria]").forEach(el=>{const k=el.dataset.i18nAria;if(D[k]!==undefined)el.setAttribute("aria-label",D[k]);});}
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
  // Кнопка живёт внутри <pre>, поэтому копируем клон без интерфейсных узлов:
  // иначе в буфер попадает её подпись «копировать».
  const clone=pre.cloneNode(true);
  clone.querySelectorAll(".copy-btn").forEach(el=>el.remove());
  const txt=clone.textContent.replace(/\s+$/,"");
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
  if(typeof a==="string"){dot.className="dot "+a;txt.textContent=b||(lang==="en"?"offline":"офлайн");return;}
  const s=a;
  const L=lang==="en"?{off:"stopped",pause:"paused",on:"running"}:{off:"стоп",pause:"пауза",on:"работает"};
  if(!s){dot.className="dot mut";txt.textContent=lang==="en"?"offline":"офлайн";return;}
  const pool=s.pool||{};
  if(!s.running){dot.className="dot mut";txt.textContent=L.off;}
  else if(pool.paused){dot.className="dot pau";txt.textContent=L.pause;}
  else{dot.className="dot on";txt.textContent=L.on;}
}
window.hv.pill=paintPill;
window.hvLast=null;
window.addEventListener("hv:lang",()=>{try{paintPill(window.hvLast);}catch(e){}});
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
