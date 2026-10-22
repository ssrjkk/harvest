/* harvest demo page — конструкторы, кошельки, симулятор, визард. Только если есть #proView. */
(function(){
"use strict";
if(!document.getElementById("proView"))return;
const hv=window.hv;
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const toast=hv.toast, esc=hv.esc, fmt=hv.fmt;
let lang=hv.lang();
hv.onLang(()=>{lang=hv.lang();paintCfgEmpty();paintEngine();try{if(wst.step===3)renderWiz();}catch(e){}});
const TcfgEmpty=()=>lang==="en"?"# press “Generate YAML”":"# нажмите «Сгенерировать YAML»";
function paintCfgEmpty(){const o=$("#cfgOut");if(o&&o.dataset.empty==="1")o.textContent=TcfgEmpty();}

/* ---------- tabs ---------- */
const TABS=["t-config","t-cmd","t-wallet","t-sim","t-check","t-ref"];
function showTab(id,focus){
  $$(".play-tabs button").forEach(x=>{
    const on=x.dataset.tab===id;
    x.classList.toggle("on",on);
    x.setAttribute("aria-selected",on?"true":"false");
    x.tabIndex=on?0:-1;
  });
  TABS.forEach(t=>{const p=$("#"+t);if(p){p.hidden=t!==id;}});
  if(id==="t-sim")drawChart();
  if(focus){const b=$(`.play-tabs button[data-tab="${id}"]`);if(b)b.focus();}
}
$$(".play-tabs button").forEach(b=>b.addEventListener("click",()=>showTab(b.dataset.tab)));
const _pt=$(".play-tabs");
if(_pt)_pt.addEventListener("keydown",e=>{
  const cur=e.target.closest?e.target.closest("button[data-tab]"):null;
  if(!cur)return;
  const i=TABS.indexOf(cur.dataset.tab);
  let n=null;
  if(e.key==="ArrowRight")n=(i+1)%TABS.length;
  else if(e.key==="ArrowLeft")n=(i-1+TABS.length)%TABS.length;
  else if(e.key==="Home")n=0;
  else if(e.key==="End")n=TABS.length-1;
  if(n!==null){e.preventDefault();showTab(TABS[n],true);}
});

/* ---------- copy (специфичные кнопки демо-зоны) ---------- */
const Tcopied=()=>lang==="en"?"copied":"скопировано";
const TcopyFail=()=>lang==="en"?"select + Ctrl+C":"выделите + Ctrl+C";
function copyText(t){
  // Как в common.js: на file:// и без прав буфера — через textarea.
  return (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).catch(()=>{
    var ta=document.createElement("textarea");ta.value=t;document.body.appendChild(ta);ta.select();
    try{document.execCommand("copy");ta.remove();return true;}
    catch(_){ta.remove();throw 0;}
  });
}
const TcfgNeed=()=>lang==="en"?"generate first":"сначала сгенерируйте";
const _cc=$("#cfgCopy");
if(_cc)_cc.addEventListener("click",()=>{const o=$("#cfgOut");if(o&&o.dataset.empty==="1")return toast(TcfgNeed());const t=o.textContent;copyText(t).then(()=>toast(Tcopied())).catch(()=>toast(TcopyFail()));});
const _cm=$("#cmdCopy");
if(_cm)_cm.addEventListener("click",()=>{const t=$("#cmdOut").textContent;copyText(t).then(()=>toast(Tcopied())).catch(()=>toast(TcopyFail()));});

/* ---------- config builder ---------- */
const NETS={
robinhood:{file:"config_robinhood.yaml",db:"farming_robinhood.db",chain:46630,rpc:"https://rpc.testnet.chain.robinhood.com",faucet:"0.005 → 0.05",fmin:"0.005",fmax:"0.05",faucetUrl:"https://faucet.testnet.chain.robinhood.com/claim",glim:"300000",mgas:"0.003",lo:3,hi:10,delays:"  delay_between_actions: [5, 20]\n  delay_between_cycles: [3600, 7200]",gas:"300000 / 0.003",workers:32,actions:'  - type: transfer\n    target: random_wallet\n    weight: 0.4\n    min_amount: 0.0001\n    max_amount: 0.001\n  - type: vibevibe_swap\n    contract: "0x0000000000000000000000000000000000000000"\n    method: swap\n    weight: 0.35\n    min_amount: 0.0001\n    max_amount: 0.001\n  - type: vibevibe_mint\n    contract: "0x0000000000000000000000000000000000000000"\n    method: mint\n    weight: 0.25'},
vibevibe:{file:"config_vibevibe.yaml",db:"farming_vibevibe.db",chain:46630,rpc:"https://rpc.testnet.chain.robinhood.com",faucet:"0.005 → 0.05",fmin:"0.005",fmax:"0.05",faucetUrl:"https://faucet.testnet.chain.robinhood.com/claim",glim:"100000",mgas:"0.003",lo:3,hi:10,delays:"  delay_between_actions: [5, 20]\n  delay_between_cycles: [3600, 7200]",gas:"100000 / 0.003",workers:32,actions:'  - type: transfer\n    target: random_wallet\n    weight: 1.0\n    min_amount: 0.0001\n    max_amount: 0.001'},
flop:{file:"config_flop.yaml",db:"farming_flop.db",chain:99999,rpc:"https://rpc.testnet.floplabs.io",faucet:"0.01 → 0.1",fmin:"0.01",fmax:"0.1",faucetUrl:"https://faucet.testnet.floplabs.io/claim",glim:"300000",mgas:"0.01",lo:5,hi:15,delays:"  delay_between_actions: [3, 15]\n  delay_between_cycles: [1800, 3600]",gas:"300000 / 0.01",workers:30,actions:'  - type: flop_compute\n    contract: "0x0000000000000000000000000000000000000000"\n    method: submitTask\n    weight: 0.4\n    min_amount: 0.001\n    max_amount: 0.01\n  - type: flop_validate\n    contract: "0x0000000000000000000000000000000000000000"\n    method: validateTask\n    weight: 0.25\n  - type: transfer\n    target: random_wallet\n    weight: 0.2\n    min_amount: 0.0001\n    max_amount: 0.005\n  - type: flop_stake\n    contract: "0x0000000000000000000000000000000000000000"\n    method: stake\n    weight: 0.15\n    min_amount: 0.001\n    max_amount: 0.01'},
arc:{file:"config_arc.yaml",db:"farming_arc.db",chain:5042002,rpc:"https://rpc.testnet.arc.network",faucet:"0.01 → 0.1",fmin:"0.01",fmax:"0.1",faucetUrl:"https://faucet.testnet.arc.network/claim",glim:"200000",mgas:"0.005",lo:5,hi:12,delays:"  delay_between_actions: [2, 10]\n  delay_between_cycles: [1200, 2400]",gas:"200000 / 0.005",workers:30,actions:'  - type: arc_launch\n    contract: "0x0000000000000000000000000000000000000000"\n    method: launchToken\n    weight: 0.3\n    min_amount: 0.001\n    max_amount: 0.01\n  - type: arc_trade\n    contract: "0x0000000000000000000000000000000000000000"\n    method: swap\n    weight: 0.35\n    min_amount: 0.0001\n    max_amount: 0.005\n  - type: transfer\n    target: random_wallet\n    weight: 0.2\n    min_amount: 0.0001\n    max_amount: 0.003\n  - type: arc_add_liquidity\n    contract: "0x0000000000000000000000000000000000000000"\n    method: addLiquidity\n    weight: 0.15\n    min_amount: 0.001\n    max_amount: 0.01'},
simple:{file:"config.simple.yaml",db:"farming_simple.db",chain:46630,rpc:"https://rpc.testnet.chain.robinhood.com",faucet:"0.003 → 0.015",fmin:"0.003",fmax:"0.015",faucetUrl:"https://faucet.testnet.chain.robinhood.com/claim",glim:"21000",mgas:"0.0006",lo:2,hi:4,delays:"  delay_between_actions: [8, 25]\n  delay_between_cycles: [2700, 6300]",gas:"21000 / 0.0006",workers:24,actions:'  - type: transfer\n    target: random_wallet\n    weight: 1.0\n    min_amount: 0.00005\n    max_amount: 0.0004'}
};
function buildYaml(){
  const key=$("#cfgNet").value, n=NETS[key];
  const rpc=$("#cfgRpc").value.trim()||n.rpc, db=$("#cfgDb").value.trim()||n.db;
  const wRaw=parseInt($("#cfgWallets").value||"50",10), wkRaw=parseInt($("#cfgWorkers").value||String(n.workers),10);
  const w=(Number.isInteger(wRaw)&&wRaw>0)?Math.min(wRaw,5000):50;
  const wk=(Number.isInteger(wkRaw)&&wkRaw>0)?Math.min(wkRaw,1000):n.workers;
  const f=$("#cfgFaucet").value.split("→").map(s=>s.trim());const fmin=f[0]||"0.005", fmax=f[1]||"0.05";
  const am=$("#cfgActions").value.replace(/[\[\]]/g,"").split(",").map(s=>s.trim());
  let alo=parseInt(am[0],10), ahi=parseInt(am[1],10);
  if(!Number.isInteger(alo)||alo<0){alo=n.lo;}
  if(!Number.isInteger(ahi)||ahi<alo){ahi=n.hi;}
  const g=$("#cfgGas").value.split("/").map(s=>s.trim());const gas=g[0]||"100000", mgas=g[1]||"0.003";
  const faucetUrl=n.faucetUrl||"https://faucet.example/claim";
  return `# harvest — ${n.file} (generated in browser, preset ${cfgPreset})\nnetwork:\n  rpc_url: ${rpc}\n  chain_id: ${n.chain}\nwallets:\n  count: ${w}\n  seed: ""\nbehavior:\n${PRESETS[cfgPreset]||PRESETS.stealth}\nfaucet:\n  enabled: true\n  min_balance: ${fmin}\n  target_balance: ${fmax}\n  retries: 2\n  strategies:\n    - type: direct\n      url: ${faucetUrl}\nactions:\n${n.actions}\nfarming:\n  actions_per_cycle: [${alo}, ${ahi}]\n${n.delays}\n  skip_cycle_probability: 0.05\nadvanced:\n  gas_limit: ${gas}\n  min_gas_for_action: ${mgas}\nthreading:\n  max_workers: ${wk}\n  timeout_per_wallet: 0\ndatabase:\n  path: ${db}\n  master_key: master.key\n`;
}
const _cfgNet=$("#cfgNet");
if(_cfgNet)_cfgNet.addEventListener("change",e=>{const n=NETS[e.target.value];$("#cfgRpc").value=n.rpc;$("#cfgDb").value=n.db;$("#cfgWorkers").value=n.workers;$("#cfgFaucet").value=n.faucet;$("#cfgActions").value=`[${n.lo}, ${n.hi}]`;$("#cfgGas").value=n.gas;});
const PRESETS={
stealth:"  enabled: true\n  activity: [0.3, 2.0]\n  rest_probability: [0.02, 0.12]\n  rest_cycles: [1, 4]\n  action_delay_scale: [0.5, 2.0]\n  cycle_delay_scale: [0.6, 2.2]\n  action_mix: [0.3, 0.8]\n  amount_jitter: [0.15, 0.4]\n  gas_deviation: [0.02, 0.12]\n  odd_amount_prob: [0.05, 0.35]\n  faucet_skip_prob: [0.05, 0.25]\n  daily_window: [0.1, 0.6]\n  burst_prob: [0.02, 0.12]\n  burst_multiplier: [1.3, 2.5]\n  start_delay: [0, 8]\n  weekend_strength: [0.0, 0.5]\n  day_start: [6.0, 12.0]\n  day_width: [6.0, 14.0]",
balanced:"  enabled: true\n  activity: [0.5, 1.5]\n  rest_probability: 0.05\n  rest_cycles: [1, 3]\n  action_delay_scale: [0.7, 1.5]\n  cycle_delay_scale: [0.8, 1.8]\n  action_mix: 0.4\n  amount_jitter: 0.25\n  gas_deviation: 0.06\n  odd_amount_prob: 0.15\n  faucet_skip_prob: 0.1\n  daily_window: 0.3\n  burst_prob: 0.05\n  burst_multiplier: 1.6\n  start_delay: [0, 3]\n  weekend_strength: 0.0\n  day_start: 9.0\n  day_width: 8.0",
degen:"  enabled: true\n  activity: [1.2, 2.0]\n  rest_probability: [0.0, 0.03]\n  rest_cycles: [1, 2]\n  action_delay_scale: [0.5, 0.9]\n  cycle_delay_scale: [0.6, 1.0]\n  action_mix: [0.5, 1.0]\n  amount_jitter: [0.2, 0.45]\n  gas_deviation: [0.05, 0.15]\n  odd_amount_prob: [0.2, 0.5]\n  faucet_skip_prob: 0.0\n  daily_window: 0.1\n  burst_prob: [0.08, 0.2]\n  burst_multiplier: [1.5, 2.5]\n  start_delay: [0, 2]\n  weekend_strength: 0.0\n  day_start: 9.0\n  day_width: 12.0"};
let cfgPreset="stealth";
function segSync(segSel){
  $$(segSel+" button").forEach(x=>x.setAttribute("aria-pressed",x.classList.contains("on")?"true":"false"));
}
$$("#cfgPreset button").forEach(b=>b.addEventListener("click",()=>{
  $$("#cfgPreset button").forEach(x=>x.classList.remove("on"));b.classList.add("on");
  cfgPreset=b.dataset.p||"stealth";segSync("#cfgPreset");
}));
segSync("#cfgPreset");
const _cfgGen=$("#cfgGen");
if(_cfgGen)_cfgGen.addEventListener("click",()=>{const o=$("#cfgOut");o.dataset.empty="0";o.textContent=buildYaml();toast(lang==="en"?"yaml ready":"yaml готов");});
const _cfgDl=$("#cfgDl");
if(_cfgDl)_cfgDl.addEventListener("click",()=>{const o=$("#cfgOut");o.dataset.empty="0";const y=buildYaml();o.textContent=y;const b=new Blob([y],{type:"text/yaml"});const a=document.createElement("a");a.href=URL.createObjectURL(b);a.download=NETS[$("#cfgNet").value].file;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),2000);});
paintCfgEmpty();

/* ---------- cmd builder ---------- */
function buildCmd(){
  let c="python auto.py";
  const wRaw=parseInt($("#cmdW").value,10), cyRaw=parseInt($("#cmdC").value,10);
  if(Number.isInteger(wRaw)&&wRaw>=0)c+=` --wallets ${Math.min(wRaw,5000)}`;
  if(Number.isInteger(cyRaw)&&cyRaw>=0)c+=` --cycles ${Math.min(cyRaw,100000)}`;
  const cfg=$("#cmdCfg").value.trim();if(cfg&&cfg!=="config.yaml")c+=` --config ${cfg}`;
  if($("#cmdSkipF").checked)c+=" --skip-faucet";
  if($("#cmdSkipFarm").checked)c+=" --skip-farm";
  if($("#cmdFast").checked)c+=" --fast";
  if($("#cmdDry").checked)c+=" --dry-run";
  if($("#cmdEnc").checked)c+=" --encrypt";
  const s=$("#cmdSched").value.trim();
  if(s&&/^\d+(\.\d+)?$/.test(s)&&parseFloat(s)>=0.05)c+=` --schedule ${s}`;
  return c;
}
const _cmdBuild=$("#cmdBuild");
if(_cmdBuild)_cmdBuild.addEventListener("click",()=>{$("#cmdOut").textContent=buildCmd();});

/* ---------- wallets ---------- */
let wallets=[], ethersMod=null, ethersLoading=null, ethersFailed=false;
function paintEngine(){
  const e=$("#wEngine"), m=$("#wMode");
  if(!e||!m)return;
  if(ethersMod){e.value="ethers@6 (real EVM)";m.textContent="ethers@6 (real EVM)";m.className="pill ok";return;}
  if(ethersLoading){e.value=lang==="en"?"loading ethers…":"загружаю ethers…";m.textContent=lang==="en"?"loading":"загрузка";m.className="pill warn";return;}
  if(ethersFailed){e.value="demo (offline)";m.textContent="demo (offline)";m.className="pill warn";return;}
  e.value=lang==="en"?"on demand":"по требованию";
  m.textContent=lang==="en"?"local":"локально";
  m.className="pill";
}
function ensureEthers(){
  if(ethersMod)return Promise.resolve(true);
  if(ethersLoading)return ethersLoading;
  paintEngine();
  // Единственный внешний запрос демо-зоны. Запускается только по кнопке,
  // а не при открытии страницы: иначе обещание «ничего не уходит» было бы ложью.
  ethersLoading=import("https://esm.sh/ethers@6.13.2")
    .then(mod=>{ethersMod=mod;ethersFailed=false;paintEngine();return true;})
    .catch(()=>{ethersMod=null;ethersFailed=true;paintEngine();return false;})
    .finally(()=>{ethersLoading=null;paintEngine();});
  return ethersLoading;
}
paintEngine();
const rndHex=n=>{try{
  return [...crypto.getRandomValues(new Uint8Array(n))].map(b=>b.toString(16).padStart(2,"0")).join("");
}catch(e){
  let s="";for(let i=0;i<n;i++)s+=Math.floor(Math.random()*256).toString(16).padStart(2,"0");return s;
}};
const _wGen=$("#wGen");
if(_wGen)_wGen.addEventListener("click",async()=>{
  const n=Math.min(200,Math.max(1,parseInt($("#wCount").value||"5",10)));
  _wGen.disabled=true;_wGen.setAttribute("aria-busy","true");
  try{
    await ensureEthers();
    for(let i=0;i<n;i++){
      let addr,priv,lbl="";
      if(ethersMod){try{const w=ethersMod.Wallet.createRandom();addr=w.address;priv=w.privateKey;lbl="ethers";}catch(e){ethersMod=null;paintEngine();}}
      if(!addr){priv="0x"+rndHex(32);addr="0x"+rndHex(20);lbl="demo";}
      wallets.push({address:addr,private_key:priv,actions:0,mode:lbl});
    }
    renderWallets();toast(wallets.length+(lang==="en"?" wallets":" кошельков"));
  }finally{
    _wGen.disabled=false;_wGen.removeAttribute("aria-busy");
  }
});
function renderWallets(){
  const tb=$("#wRows");if(!tb)return;
  if(!wallets.length){tb.innerHTML='<tr><td colspan="4" class="note">—</td></tr>';return;}
  tb.innerHTML=wallets.map((w,i)=>`<tr><td class="num">${i+1}</td><td class="addr">${esc(w.address)}${w.mode==="demo"?' <span class="pill warn">demo</span>':""}</td><td class="addr">${esc(w.private_key.slice(0,10))}…${esc(w.private_key.slice(-6))}</td><td class="num">${w.actions}</td></tr>`).join("");
}
function dl(name,text,type){const b=new Blob([text],{type});const a=document.createElement("a");a.href=URL.createObjectURL(b);a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),2000);}
function csvSafe(v){const s=String(v??"");return /^[=+\-@\t\r]/.test(s)?"'"+s:s;}
const Tempty=()=>lang==="en"?"empty":"пусто";
const Tdemo=()=>lang==="en"?"demo keys — not for mainnet":"демо-ключи — не для мейннета";
const _wCsv=$("#wCsv");
if(_wCsv)_wCsv.addEventListener("click",()=>{if(!wallets.length)return toast(Tempty());if(wallets.some(w=>w.mode==="demo"))toast(Tdemo());dl("wallets_export.csv","address,private_key,actions\n"+wallets.map(w=>`${csvSafe(w.address)},${csvSafe(w.private_key)},${w.actions}`).join("\n"),"text/csv");});
const _wJson=$("#wJson");
if(_wJson)_wJson.addEventListener("click",()=>{if(!wallets.length)return toast(Tempty());if(wallets.some(w=>w.mode==="demo"))toast(Tdemo());dl("wallets_export.json",JSON.stringify(wallets.map(w=>({address:w.address,private_key:w.private_key,actions:w.actions})),null,2),"application/json");});
const _wClear=$("#wClear");
if(_wClear)_wClear.addEventListener("click",()=>{wallets=[];renderWallets();});

/* ---------- simulator ---------- */
let simTimer=null, hist=[];
function hash32(s){let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return h>>>0;}
function rnd(seed){let a=seed>>>0;return()=>{a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296;};}
const _simRun=$("#simRun");
if(_simRun)_simRun.addEventListener("click",()=>{
  if(simTimer)return;
  const W=Math.min(2000,Math.max(1,parseInt($("#simW").value,10)||50));
  const C=Math.min(200,Math.max(1,parseInt($("#simC").value,10)||5));
  const skip=parseFloat($("#simSkip").value||"0.05");
  const ab=($("#simA").value||"3,8").split(",").map(x=>parseInt(x.trim(),10));const alo=ab[0]||3,ahi=ab[1]||8;
  const fail=parseFloat($("#simFail").value||"0.08");
  const speed=parseInt($("#simSpeed").value,10)||8;
  const addrs=Array.from({length:W},(_,i)=>"0xSIM"+String(i).padStart(5,"0"));
  const profs=addrs.map(a=>{const r=rnd(hash32(a));return{act:0.5+r()*1.0,burst:r()<0.12?1.6:1.0,rest:r()<0.06?2:0};});
  hist=[];let cyc=0;$("#simLog").textContent=(lang==="en"?`harvest-sim: ${W} wallets, ${C} cycles, skip=${skip}\n`:`harvest-sim: ${W} кошельков, ${C} циклов, skip=${skip}\n`);
  $("#simStat").textContent=lang==="en"?"running":"работает";$("#simStat").className="pill warn";
  const perCycle=[];
  simTimer=setInterval(()=>{
    if(cyc>=C){clearInterval(simTimer);simTimer=null;$("#simStat").textContent=lang==="en"?"done":"готово";$("#simStat").className="pill ok";return;}
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
    slog(lang==="en"
      ?`cycle ${cyc}: wallets_ok=${ok}/${W} actions_ok=${acts} errors=${errs} (${dur}s)`
      :`цикл ${cyc}: кошельков_ok=${ok}/${W} действий_ok=${acts} ошибок=${errs} (${dur}с)`);
    const tw=hist.reduce((s,h)=>s+h.wallets_ok,0),ta=hist.reduce((s,h)=>s+h.actions_ok,0),te=hist.reduce((s,h)=>s+h.errors,0);
    $("#kW").textContent=tw;$("#kA").textContent=ta;$("#kE").textContent=te;$("#kAvg").textContent=(ta/hist.length).toFixed(1);
    renderHist();drawChart(perCycle);
  },Math.max(120,900/speed));
});
function slog(m){const el=$("#simLog");if(!el)return;el.textContent+=m+"\n";el.scrollTop=el.scrollHeight;}
const _simStop=$("#simStop");
if(_simStop)_simStop.addEventListener("click",()=>{if(simTimer){clearInterval(simTimer);simTimer=null;$("#simStat").textContent=lang==="en"?"stopped":"остановлен";$("#simStat").className="pill warn";}});
const _simExp=$("#simExp");
if(_simExp)_simExp.addEventListener("click",()=>{if(!hist.length)return toast(Tempty());dl("cycle_history.csv","started_at,mode,wallets,wallets_ok,actions_ok,errors,duration_s\n"+hist.map(h=>`cycle-${h.cycle},${h.mode},${h.wallets},${h.wallets_ok},${h.actions_ok},${h.errors},${h.duration_s}`).join("\n"),"text/csv");});
function renderHist(){const sh=$("#simHist");if(!sh)return;sh.innerHTML=hist.slice(-12).reverse().map(h=>`<tr><td class="num">${h.cycle}</td><td>${h.mode}</td><td class="num">${h.wallets}</td><td class="num">${h.wallets_ok}</td><td class="num">${h.actions_ok}</td><td class="num">${h.errors}</td><td class="num">${h.duration_s}</td></tr>`).join("");}
function drawChart(data){
  const cv=$("#simChart");if(!cv)return;const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth||600,H=220;cv.width=W*dpr;cv.height=H*dpr;
  const x=cv.getContext("2d");x.scale(dpr,dpr);
  const cs=getComputedStyle(document.documentElement);
  const cLine=cs.getPropertyValue("--line").trim()||"#DDD6C2";
  const cMut=cs.getPropertyValue("--muted").trim()||"#6F6A5C";
  const cGold=cs.getPropertyValue("--gold").trim()||"#D4A72C";
  x.clearRect(0,0,W,H);
  const d=data||hist.map(h=>h.actions_ok);
  x.strokeStyle=cLine;x.beginPath();x.moveTo(34,8);x.lineTo(34,H-24);x.lineTo(W-8,H-24);x.stroke();
  if(!d.length){x.fillStyle=cMut;x.font="12px monospace";x.fillText(lang==="en"?"no data — run simulation":"нет данных — запустите симуляцию",44,H/2);return;}
  const mx=Math.max(...d,1),bw=Math.max(3,(W-60)/d.length-4);
  d.forEach((v,i)=>{const h=(H-48)*v/mx,bx=40+i*(bw+4);
    x.fillStyle=cGold;x.fillRect(bx,H-24-h,bw,h);});
  x.fillStyle=cMut;x.font="11px monospace";x.fillText((lang==="en"?"max ":"макс ")+mx,40,20);
}
window.addEventListener("resize",()=>drawChart());
window.hvRedraw=()=>{try{drawChart();}catch(e){}};

/* ---------- validator ---------- */
const _vRun=$("#vRun");
if(_vRun)_vRun.addEventListener("click",()=>{
  const t=$("#vText").value, out=[];
  const RU=lang!=="en";
  const has=s=>new RegExp("^"+s+":","m").test(t);
  const req=["network","wallets","faucet","actions","farming","threading","database"];
  const missing=req.filter(s=>!has(s));
  out.push(missing.length?["bad",(RU?"нет секций: ":"missing sections: ")+missing.join(", ")]:["ok",RU?"все обязательные секции на месте":"all required sections present"]);
  const rpc=(t.match(/rpc_url:\s*(\S+)/)||[])[1]||"";
  const rpcHost=(rpc.match(/^https?:\/\/([^/:]+)/)||[])[1]||"";
  const rpcOk=/^https:\/\//.test(rpc)||["127.0.0.1","localhost","::1"].indexOf(rpcHost.toLowerCase())!==-1;
  out.push(rpcOk?["ok","rpc TLS ok"]:["bad",RU?`rpc_url должен быть https (http — только 127.0.0.1/localhost), получили: ${rpc||"—"}`:`rpc_url must be https (http only for 127.0.0.1/localhost) — got: ${rpc||"—"}`]);
  const chain=parseInt((t.match(/chain_id:\s*(\d+)/)||[])[1]||"0",10);
  out.push(chain>=1?["ok","chain_id = "+chain]:["bad",RU?"chain_id отсутствует или < 1":"chain_id missing or < 1"]);
  if(chain===99999)out.push(["warn",RU?"chain_id 99999 — маркер-заглушка (config_flop)":"chain_id 99999 is a stub marker (config_flop)"]);
  const mm=(t.match(/min_balance:\s*([\d.]+)/)||[])[1], tg=(t.match(/target_balance:\s*([\d.]+)/)||[])[1];
  if(mm!==undefined&&tg!==undefined)out.push(parseFloat(mm)<=parseFloat(tg)?["ok",`faucet ${mm} ≤ ${tg}`]:["bad",RU?`faucet min_balance ${mm} > target ${tg}`:`faucet min_balance ${mm} > target ${tg}`]);
  if(/contract:\s*"?0x0{40}/.test(t))out.push(["warn",RU?"контракт 0x0 = заглушка → runtime откатится на transfer; для Tier-2 впишите реальный адрес":"0x0 contract = stub → runtime falls back to transfer; fill a real address for Tier-2"]);
  const gas=parseInt((t.match(/gas_limit:\s*(\d+)/)||[])[1]||"0",10);
  out.push(gas>=1?["ok","gas_limit = "+gas]:["bad",RU?"gas_limit должен быть ≥ 1":"gas_limit must be ≥ 1"]);
  if(gas<50000&&/contract:\s*"?(?!0x0{40})0x[0-9a-fA-F]{40}/.test(t))out.push(["bad",RU?"реальное контрактное действие требует gas_limit ≥ 50000":"real contract action needs gas_limit ≥ 50000"]);
  const mw=parseInt((t.match(/max_workers:\s*(\d+)/)||[])[1]||"0",10);
  out.push(mw>=1&&mw<=1000?["ok","max_workers = "+mw]:["bad",RU?"threading.max_workers должен быть 1..1000":"threading.max_workers must be 1..1000"]);
  const dbp=(t.match(/path:\s*(\S+)/)||[])[1]||"";
  out.push(dbp&&!dbp.includes("..")?["ok","database.path = "+dbp]:["bad",RU?"database.path отсутствует или содержит ..":"database.path missing or contains .."]);
  $("#vOut").innerHTML=out.map(([k,m])=>`<p><span class="pill ${k}">${k}</span> <span class="mono" style="font-size:12.5px">${esc(m)}</span></p>`).join("");
});

/* ---------- perf calc ---------- */
const _pRun=$("#pRun");
if(_pRun)_pRun.addEventListener("click",()=>{
  const cpu=Math.max(1,parseInt($("#pCpu").value,10)||8);
  const wIn=parseInt($("#pW").value,10);
  const w=isNaN(wIn)?Math.min(Math.max(20,cpu*2),128):wIn;
  const rpc=Math.min(Math.max(8,cpu*4),128);
  const gen=Math.min(8,Math.max(2,cpu));
  $("#p1").textContent=Math.min(Math.max(20,cpu*2),128)+(isNaN(wIn)?" (auto)":"");
  $("#p2").textContent=rpc;$("#p3").textContent=gen;$("#p4").textContent=Math.max(w*4,100);
});

/* ---------- mode: beginner / pro ---------- */
let mode="new";
try{const m=localStorage.getItem("hv-mode");if(m==="new"||m==="pro")mode=m;}catch(e){}
function applyMode(){
  const isNew=mode==="new";
  const mn=$("#modeNew"),mp=$("#modePro"),nv=$("#newView"),pv=$("#proView");
  if(mn){mn.classList.toggle("on",isNew);mn.setAttribute("aria-pressed",isNew?"true":"false");}
  if(mp){mp.classList.toggle("on",!isNew);mp.setAttribute("aria-pressed",!isNew?"true":"false");}
  if(nv)nv.hidden=!isNew;
  if(pv)pv.hidden=isNew;
}
const _modeNew=$("#modeNew"),_modePro=$("#modePro");
if(_modeNew)_modeNew.addEventListener("click",()=>{mode="new";try{localStorage.setItem("hv-mode",mode);}catch(e){}applyMode();});
if(_modePro)_modePro.addEventListener("click",()=>{mode="pro";try{localStorage.setItem("hv-mode",mode);}catch(e){}applyMode();});
applyMode();

/* ---------- beginner wizard ---------- */
const WNETS={vibevibe:"vibe/vibe",robinhood:"Robinhood Chain",arc:"Arc (Minara)",flop:"Flop Labs"};
let wst={step:1,net:"vibevibe",cycles:2};
$$(".opt").forEach(o=>o.addEventListener("click",()=>{
  $$(".opt").forEach(x=>x.classList.remove("sel"));o.classList.add("sel");wst.net=o.dataset.net;
}));
const _wW=$("#wW");
if(_wW)_wW.addEventListener("input",e=>{$("#wWVal").textContent=e.target.value;e.target.setAttribute("aria-valuetext",e.target.value+" wallets");});
$$("#wC button").forEach(b=>b.addEventListener("click",()=>{
  $$("#wC button").forEach(x=>x.classList.remove("on"));b.classList.add("on");wst.cycles=parseInt(b.dataset.c,10);
}));
function wStep(n){
  wst.step=Math.min(3,Math.max(1,n));
  [1,2,3].forEach(i=>{
    const w=$("#w"+i);if(w)w.hidden=i!==wst.step;
    const st=$("#st"+i);if(st){st.classList.toggle("on",i===wst.step);st.classList.toggle("done",i<wst.step);}
  });
  const wb=$("#wBack"),wn=$("#wNext");
  if(wb)wb.style.visibility=wst.step===1?"hidden":"visible";
  if(wn)wn.style.visibility=wst.step===3?"hidden":"visible";
  if(wst.step===3)renderWiz();
}
const _wBack=$("#wBack"),_wNext=$("#wNext");
if(_wBack)_wBack.addEventListener("click",()=>wStep(wst.step-1));
if(_wNext)_wNext.addEventListener("click",()=>wStep(wst.step+1));
function wizCmd(){
  let c=`python auto.py --config ${NETS[wst.net].file} --wallets ${$("#wW").value} --cycles ${wst.cycles}`;
  if(!$("#wF").checked)c+=" --skip-faucet";
  return c;
}
function renderWiz(){
  const sn=$("#wSumNet");if(sn)sn.textContent=`${WNETS[wst.net]} · ${NETS[wst.net].chain}`;
  const sw=$("#wSumW");if(sw)sw.textContent=$("#wW").value;
  const sc=$("#wSumC");if(sc)sc.textContent=wst.cycles===0?"∞":String(wst.cycles);
  const sf=$("#wSumF");if(sf)sf.textContent=$("#wF").checked?(lang==="en"?"on":"вкл"):(lang==="en"?"off":"выкл");
  const wc=$("#wCmd");if(wc)wc.textContent=wizCmd();
}
const _wCopyCmd=$("#wCopyCmd");
if(_wCopyCmd)_wCopyCmd.addEventListener("click",()=>{
  copyText(wizCmd()).then(()=>toast(Tcopied())).catch(()=>toast(TcopyFail()));
});
const _wDlCfg=$("#wDlCfg");
if(_wDlCfg)_wDlCfg.addEventListener("click",()=>{
  const n=NETS[wst.net];
  const faucetUrl=n.faucetUrl||"https://faucet.example/claim";
  const y=`# harvest — ${n.file} (beginner preset, generated in browser)\nnetwork:\n  rpc_url: ${n.rpc}\n  chain_id: ${n.chain}\nwallets:\n  count: ${$("#wW").value}\n  seed: ""\nfaucet:\n  enabled: ${$("#wF").checked}\n  min_balance: ${n.fmin}\n  target_balance: ${n.fmax}\n  retries: 2\n  strategies:\n    - type: direct\n      url: ${faucetUrl}\nactions:\n${n.actions}\nfarming:\n  actions_per_cycle: [${n.lo}, ${n.hi}]\n${n.delays}\n  skip_cycle_probability: 0.05\nadvanced:\n  gas_limit: ${n.glim}\n  min_gas_for_action: ${n.mgas}\nthreading:\n  max_workers: ${n.workers}\n  timeout_per_wallet: 0\ndatabase:\n  path: ${n.db}\n  master_key: master.key\n`;
  dl(n.file,y,"text/yaml");
});
wStep(1);
})();
