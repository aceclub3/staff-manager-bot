/* Пульт керівника — Telegram Mini App (vanilla JS).
   Усі дії йдуть в API, що викликає ТІ САМІ корутини бота, що й кнопки в чаті. */
"use strict";
const tg = window.Telegram && window.Telegram.WebApp;
// initData від Telegram. Кешуємо в sessionStorage: при перезавантаженні webview
// (напр., під час рестарту бота) Telegram інколи не передає launch-параметри знову,
// і tg.initData стає порожнім -> 401. Кеш дозволяє переавторизуватись тим самим
// (досі валідним, <24год) підписом без потреби перевідкривати застосунок.
let INIT = (tg && tg.initData) || "";
try {
  if (INIT) sessionStorage.setItem("pult_init", INIT);
  else INIT = sessionStorage.getItem("pult_init") || "";
} catch (e) {}

function applySafeArea(){
  if (!tg) return;
  const sa = tg.safeAreaInset || {};
  const ca = tg.contentSafeAreaInset || {};
  const top = (Number(sa.top) || 0) + (Number(ca.top) || 0);
  const bottom = (Number(sa.bottom) || 0) + (Number(ca.bottom) || 0);
  document.documentElement.style.setProperty("--safe-top", top + "px");
  document.documentElement.style.setProperty("--safe-bottom", bottom + "px");
}
function setupViewport(){
  if (!tg) return;
  try { tg.ready(); } catch (e) {}
  try { tg.expand(); } catch (e) {}
  // не закривати застосунок випадковим свайпом вниз під час скролу
  try { if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); } catch (e) {}
  // повноекранний режим (Bot API 8.0+); на старіших клієнтах просто лишається expand()
  try {
    if (tg.requestFullscreen && (!tg.isVersionAtLeast || tg.isVersionAtLeast("8.0"))) tg.requestFullscreen();
  } catch (e) {}
  applySafeArea();
  ["safeAreaChanged", "contentSafeAreaChanged", "fullscreenChanged", "viewportChanged"].forEach(ev => {
    try { tg.onEvent && tg.onEvent(ev, applySafeArea); } catch (e) {}
  });
}
setupViewport();

const view = document.getElementById("view");
const modal = document.getElementById("modal");
const modalCard = document.getElementById("modal-card");

let ME = null;
let TAB = "board";
const FILTERS = { status: "open", venue: null, urgency: null, unassigned: false };

/* ---------- helpers ---------- */
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function toast(msg){const t=document.createElement("div");t.className="toast";t.textContent=msg;document.body.appendChild(t);setTimeout(()=>t.remove(),2200);}
function haptic(type){try{tg&&tg.HapticFeedback&&tg.HapticFeedback.notificationOccurred(type);}catch(e){}}

async function api(path, opts={}){
  const headers = Object.assign({ "X-Telegram-Init-Data": INIT }, opts.headers||{});
  if (opts.body && typeof opts.body !== "string"){ headers["Content-Type"]="application/json"; opts.body=JSON.stringify(opts.body); }
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (!res.ok){
    let err = "HTTP "+res.status;
    try { const j = await res.json(); if (j.error) err = j.error; } catch(e){}
    const e = new Error(err); e.status = res.status; throw e;
  }
  return res.json();
}
function errText(e){
  const m = { unauthorized:"Не авторизовано (відкрийте через бота).", forbidden:"Немає доступу.",
    no_perm:"Немає прав на цю дію.", owner_only:"Лише власник.", already_closed:"Задачу вже закрито.",
    bad_target:"Невірний виконавець.", empty:"Порожньо.", not_found:"Не знайдено.", internal:"Помилка сервера." };
  return m[e && e.message] || (e && e.message) || "Помилка";
}

/* ---------- modal ---------- */
function openModal(html){ modalCard.innerHTML = html; modal.classList.remove("hidden"); }
function closeModal(){ modal.classList.add("hidden"); modalCard.innerHTML=""; }
modal.addEventListener("click", e => { if (e.target === modal) closeModal(); });

/* ---------- boot ---------- */
async function boot(){
  try { ME = await api("/api/me"); }
  catch(e){ view.innerHTML = `<div class="empty">🔒 ${esc(errText(e))}<br><br>Відкрийте «Пульт керівника» через кнопку в боті.</div>`; return; }
  document.getElementById("who-name").textContent = ME.name;
  document.getElementById("who-role").textContent = ME.role + (ME.venues.length ? " · "+ME.venues.join(", ") : "");
  if (ME.is_admin) document.getElementById("tab-staff").hidden = false;
  render();
}
document.getElementById("refresh").addEventListener("click", render);
document.querySelectorAll(".tab").forEach(b => b.addEventListener("click", () => {
  TAB = b.dataset.tab;
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x===b));
  render();
}));

function render(){
  if (TAB === "board") renderBoard();
  else if (TAB === "analytics") renderAnalytics();
  else if (TAB === "shifts") renderShifts();
  else if (TAB === "staff") renderStaff();
}

/* ---------- BOARD ---------- */
async function renderBoard(){
  view.innerHTML = `<div class="loading">Завантаження…</div>`;
  const venues = ME.venues;
  const chips = [];
  chips.push(chip("Відкриті", FILTERS.status==="open", ()=>{FILTERS.status="open";renderBoard();}));
  chips.push(chip("Закриті", FILTERS.status==="closed", ()=>{FILTERS.status="closed";renderBoard();}));
  if (venues.length > 1){
    chips.push(chip("Усі заклади", !FILTERS.venue, ()=>{FILTERS.venue=null;renderBoard();}));
    venues.forEach(v => chips.push(chip(v, FILTERS.venue===v, ()=>{FILTERS.venue=v;renderBoard();})));
  }
  chips.push(chip("🔥 Термінові", FILTERS.urgency==="Висока", ()=>{FILTERS.urgency=FILTERS.urgency==="Висока"?null:"Висока";renderBoard();}));
  chips.push(chip("⚠️ Без виконавця", FILTERS.unassigned, ()=>{FILTERS.unassigned=!FILTERS.unassigned;renderBoard();}));

  let data;
  const q = new URLSearchParams({ status: FILTERS.status });
  if (FILTERS.venue) q.set("venue", FILTERS.venue);
  if (FILTERS.urgency) q.set("urgency", FILTERS.urgency);
  if (FILTERS.unassigned) q.set("unassigned","1");
  try { data = await api("/api/tasks?"+q.toString()); }
  catch(e){ view.innerHTML = `<div class="empty">${esc(errText(e))}</div>`; return; }

  const filt = document.createElement("div"); filt.className="filters"; chips.forEach(c=>filt.appendChild(c));
  const list = document.createElement("div");
  if (!data.tasks.length){ list.innerHTML = `<div class="empty">✅ Немає ${FILTERS.status==="open"?"відкритих":"закритих"} задач</div>`; }
  else data.tasks.forEach(t => list.appendChild(taskCard(t)));

  view.innerHTML = `<div class="section-title">Задачі (${data.count})</div>`;
  view.appendChild(filt); view.appendChild(list);
  ensureFab();
}
function chip(label, on, fn){ const b=document.createElement("button"); b.className="chip"+(on?" on":""); b.textContent=label; b.onclick=fn; return b; }
function taskCard(t){
  const c = document.createElement("div"); c.className="card"; c.onclick=()=>openTask(t.fid);
  const dot = t.resolved ? "" : `<span class="dot ${t.acknowledged?"wip":"new"}"></span>`;
  const age = t.age_days>=1 ? `<span class="badge age">⏰ ${t.age_days}д</span>` : "";
  const noass = (!t.resolved && !t.assignee_id) ? `<span class="badge warn">⚠️ без виконавця</span>` : "";
  const ph = t.has_photo ? `<span class="badge">📷</span>` : "";
  const ass = t.assignee_name ? `<span class="badge">👤 ${esc(t.assignee_name)}</span>` : "";
  // превʼю останнього запису історії (коментар/статус) + скільки ще записів
  const entries = (t.log || "").split(" | ").map(s => s.trim()).filter(Boolean);
  const last = entries.length
    ? `<div class="card-last">${esc(entries[entries.length - 1])}${entries.length > 1 ? ` <span class="muted">· ще ${entries.length - 1}</span>` : ""}</div>`
    : "";
  c.innerHTML = `<div class="row1">${dot}<span class="fid">${esc(t.fid)}</span> · ${esc(t.restaurant)}
      · ${t.category_icon} ${esc(t.category)} ${t.urgency_icon} ${age} ${noass} ${ph} ${ass}</div>
    <div class="summary">${esc(t.summary)}</div>${last}`;
  return c;
}
function ensureFab(){
  if (document.getElementById("fab")) return;
  const f=document.createElement("button"); f.id="fab"; f.className="fab"; f.textContent="+"; f.title="Нова задача";
  f.onclick=newTaskDialog; document.body.appendChild(f);
}

/* ---------- TASK DETAIL ---------- */
async function openTask(fid){
  openModal(`<div class="loading">Завантаження…</div>`);
  let d;
  try { d = await api("/api/tasks/"+encodeURIComponent(fid)); }
  catch(e){ openModal(`<button class="close" onclick="closeModalGlobal()">✕</button><div class="empty">${esc(errText(e))}</div>`); return; }
  const t = d.task;
  const act = t.can_act;
  let actions = "";
  if (!t.resolved && act){
    actions = `<div class="btn-row">
      <button class="btn primary" data-a="done">✅ Виконано</button>
      <button class="btn" data-a="wip">🔧 В роботі</button>
      <button class="btn" data-a="assign">👤 Доручити</button></div>
      <div class="btn-row"><button class="btn" data-a="comment">💬 Коментар</button>
      ${t.is_owner?`<button class="btn danger" data-a="del">🗑️ Видалити</button>`:""}</div>`;
  } else if (t.resolved && act){
    actions = `<div class="btn-row"><button class="btn" data-a="cancel">↩️ Відновити</button>
      <button class="btn" data-a="comment">💬 Коментар</button>
      ${t.is_owner?`<button class="btn danger" data-a="del">🗑️ Видалити</button>`:""}</div>`;
  }
  const entries = (t.log || "").split(" | ").map(s => s.trim()).filter(Boolean);
  const history = `<div class="hsec">💬 Історія та коментарі</div>` + (entries.length
    ? `<div class="history">${entries.map(e => `<div class="hitem">${esc(e)}</div>`).join("")}</div>`
    : `<div class="hint" style="padding:2px 2px 4px">— поки немає —</div>`);
  openModal(`<button class="close" onclick="closeModalGlobal()">✕</button>
    <h3>${t.category_icon} ${esc(t.category)} ${t.urgency_icon}</h3>
    <div class="kv"><span class="k">Номер</span><span class="fid">${esc(t.fid)}</span></div>
    <div class="kv"><span class="k">Заклад</span><span>${esc(t.restaurant)}</span></div>
    <div class="kv"><span class="k">Суть</span><span>${esc(t.summary)}</span></div>
    <div class="kv"><span class="k">Статус</span><span>${esc(t.status)}</span></div>
    <div class="kv"><span class="k">Терміновість</span><span>${esc(t.urgency)}</span></div>
    <div class="kv"><span class="k">Хто повідомив</span><span>${esc(t.sender_name)} (${esc(t.sender_role)})</span></div>
    ${t.assignee_name?`<div class="kv"><span class="k">Виконавець</span><span>${esc(t.assignee_name)}</span></div>`:""}
    <div class="kv"><span class="k">Створено</span><span>${esc(t.created_date)} ${esc(t.created_time)} · ⏰${t.age_days}д</span></div>
    ${t.has_photo?`<img class="detail-photo" id="dphoto" alt="фото">`:""}
    ${history}
    <div style="height:10px"></div>
    ${actions}`);
  if (t.has_photo) loadPhoto(t.fid, document.getElementById("dphoto"));
  modalCard.querySelectorAll("[data-a]").forEach(b => b.onclick = () => taskAction(t, b.dataset.a));
}
window.closeModalGlobal = closeModal;

async function loadPhoto(fid, img){
  if (!img) return;
  try {
    const res = await fetch("/api/photo/"+encodeURIComponent(fid), { headers:{ "X-Telegram-Init-Data": INIT } });
    if (!res.ok){ img.remove(); return; }
    img.src = URL.createObjectURL(await res.blob());
  } catch(e){ img.remove(); }
}

async function taskAction(t, a){
  if (a === "assign") return assignDialog(t.fid);
  if (a === "comment") return commentDialog(t.fid);
  if (a === "del"){
    const go = () => doAction(t.fid, "delete", null, "🗑️ Видалено");
    if (tg && tg.showConfirm) tg.showConfirm("Видалити задачу "+t.fid+"?", ok=>{ if(ok) go(); });
    else if (confirm("Видалити "+t.fid+"?")) go();
    return;
  }
  // done / wip / cancel
  doAction(t.fid, "status", { action: a }, a==="done"?"✅ Виконано":a==="wip"?"🔧 В роботі":"↩️ Відновлено");
}
async function doAction(fid, kind, body, okMsg){
  try {
    await api(`/api/tasks/${encodeURIComponent(fid)}/${kind}`, { method:"POST", body: body||{} });
    haptic("success"); toast(okMsg||"Готово"); closeModal(); render();
  } catch(e){ haptic("error"); toast(errText(e)); }
}

function commentDialog(fid){
  openModal(`<button class="close" onclick="closeModalGlobal()">✕</button>
    <h3>💬 Коментар до ${esc(fid)}</h3>
    <textarea id="cm" placeholder="Текст коментаря…"></textarea>
    <button class="btn primary block" id="cmgo">Зберегти</button>`);
  document.getElementById("cmgo").onclick = async () => {
    const text = document.getElementById("cm").value.trim();
    if (!text) return;
    try { await api(`/api/tasks/${encodeURIComponent(fid)}/comment`,{method:"POST",body:{text}});
      haptic("success"); toast("Коментар збережено"); closeModal(); render(); }
    catch(e){ toast(errText(e)); }
  };
}

async function assignDialog(fid){
  openModal(`<div class="loading">Завантаження…</div>`);
  let s;
  try { s = await api("/api/staff"); } catch(e){ toast(errText(e)); closeModal(); return; }
  const rows = s.staff.map(p => `<button class="btn block" data-uid="${p.uid}" style="justify-content:flex-start;margin-bottom:6px">
      👤 ${esc(p.name)} <span class="muted">(${esc(p.role)})</span></button>`).join("");
  openModal(`<button class="close" onclick="closeModalGlobal()">✕</button>
    <h3>👤 Доручити ${esc(fid)}</h3>${rows||'<div class="empty">Немає активних співробітників</div>'}`);
  modalCard.querySelectorAll("[data-uid]").forEach(b => b.onclick = async () => {
    try { await api(`/api/tasks/${encodeURIComponent(fid)}/assign`,{method:"POST",body:{assignee_id:Number(b.dataset.uid)}});
      haptic("success"); toast("Доручено"); closeModal(); render(); }
    catch(e){ toast(errText(e)); }
  });
}

function newTaskDialog(){
  const opts = ME.all_restaurants.map(r=>`<option value="${esc(r)}">${esc(r)}</option>`).join("");
  openModal(`<button class="close" onclick="closeModalGlobal()">✕</button>
    <h3>➕ Нова задача</h3>
    <textarea id="nt" placeholder="Опишіть проблему/задачу — Claude класифікує…"></textarea>
    <label class="hint">Заклад (необовʼязково — інакше визначиться з тексту)</label>
    <select id="ntr"><option value="">— авто —</option>${opts}</select>
    <button class="btn primary block" id="ntgo">Створити</button>`);
  document.getElementById("ntgo").onclick = async () => {
    const text = document.getElementById("nt").value.trim();
    const restaurant = document.getElementById("ntr").value;
    if (!text) return;
    const btn = document.getElementById("ntgo"); btn.textContent="Створюю…"; btn.disabled=true;
    try { const r = await api("/api/tasks",{method:"POST",body:{text, restaurant}});
      haptic("success"); toast("Створено: "+(r.ids||[]).join(", ")); closeModal(); render(); }
    catch(e){ toast(errText(e)); btn.textContent="Створити"; btn.disabled=false; }
  };
}

/* ---------- ANALYTICS ---------- */
async function renderAnalytics(){
  removeFab();
  view.innerHTML = `<div class="loading">Завантаження…</div>`;
  let p = (renderAnalytics._period = renderAnalytics._period || "7d");
  let d;
  try { d = await api("/api/analytics?period="+p); } catch(e){ view.innerHTML=`<div class="empty">${esc(errText(e))}</div>`; return; }
  const chips = ["today","7d","30d"].map(x => `<button class="chip ${x===p?"on":""}" data-p="${x}">${({today:"Сьогодні","7d":"7 днів","30d":"30 днів"})[x]}</button>`).join("");
  const stat = (n,l)=>`<div class="stat"><div class="num">${n}</div><div class="lbl">${l}</div></div>`;
  const bars = (obj, total) => Object.entries(obj).sort((a,b)=>b[1]-a[1]).map(([k,v])=>{
    const pct = total? Math.round(v*100/total):0;
    return `<div class="bar-row"><span class="name">${esc(k)}</span><span class="bar"><i style="width:${pct}%"></i></span><span class="val">${v}</span></div>`;
  }).join("") || `<div class="hint">—</div>`;
  const venueBars = Object.entries(d.by_venue).map(([k,v])=>
    `<div class="bar-row"><span class="name">${esc(k)}</span><span class="bar"><i style="width:${d.open?Math.round(v.open*100/d.open):0}%"></i></span><span class="val">${v.open}/${v.closed}</span></div>`
  ).join("") || `<div class="hint">—</div>`;
  const oldest = d.oldest_open.map(o=>`<div class="card" onclick="openTask('${esc(o.fid)}')"><div class="row1"><span class="fid">${esc(o.fid)}</span> · ${esc(o.restaurant)} <span class="badge age">⏰ ${o.age_days}д</span></div><div class="summary">${esc(o.summary)}</div></div>`).join("") || `<div class="hint">—</div>`;

  view.innerHTML = `<div class="section-title">Аналітика</div>
    <div class="filters">${chips}</div>
    <div class="stat-grid">
      ${stat(d.open,"Відкрито зараз")}
      ${stat(d.closed_in_period,"Закрито за період")}
      ${stat(d.new_in_period,"Нових за період")}
      ${stat(d.avg_days_to_close==null?"—":d.avg_days_to_close,"Сер. днів до закриття")}
    </div>
    <div class="section-title">По закладах (відкрито/закрито)</div>${venueBars}
    <div class="section-title">Відкриті по категоріях</div>${bars(d.by_category, d.open)}
    <div class="section-title">Навантаження (відкриті по виконавцях)</div>${bars(d.workload, d.open)}
    <div class="section-title">Найстаріші відкриті</div>${oldest}`;
  view.querySelectorAll("[data-p]").forEach(b=>b.onclick=()=>{renderAnalytics._period=b.dataset.p;renderAnalytics();});
}

/* ---------- SHIFTS ---------- */
async function renderShifts(){
  removeFab();
  view.innerHTML = `<div class="loading">Завантаження…</div>`;
  let d;
  try { d = await api("/api/shift-reports"); } catch(e){ view.innerHTML=`<div class="empty">${esc(errText(e))}</div>`; return; }
  if (!d.count){ view.innerHTML = `<div class="section-title">Підсумки змін</div><div class="empty">📝 Підсумків поки немає</div>`; return; }
  const reps = d.reports.map(r=>`<div class="card" style="cursor:default">
      <div class="row1">🌙 ${esc(r.restaurant)} · ${esc(r.who)}</div>
      <div class="summary">${esc(r.text)}</div>
      <div style="height:8px"></div>
      <button class="btn block" data-key="${esc(r.key)}">➕ Створити задачу</button></div>`).join("");
  view.innerHTML = `<div class="section-title">Підсумки змін за ${esc(d.day)} (${d.count})</div>
    ${d.summary?`<div class="log">${esc(d.summary)}</div><div style="height:12px"></div>`:""}
    ${reps}`;
  view.querySelectorAll("[data-key]").forEach(b=>b.onclick=async()=>{
    b.textContent="Створюю…"; b.disabled=true;
    try{ const r=await api(`/api/shift-reports/${encodeURIComponent(b.dataset.key)}/to-task`,{method:"POST",body:{}});
      haptic("success"); toast("Створено: "+(r.ids||[]).join(", ")); b.textContent="✅ Створено"; }
    catch(e){ toast(errText(e)); b.textContent="➕ Створити задачу"; b.disabled=false; }
  });
}

/* ---------- STAFF ---------- */
async function renderStaff(){
  removeFab();
  view.innerHTML = `<div class="loading">Завантаження…</div>`;
  let s;
  try { s = await api("/api/staff?all=1"); } catch(e){ view.innerHTML=`<div class="empty">${esc(errText(e))}</div>`; return; }
  const items = s.staff.map(p=>{
    const tag = p.status==="pending"?`<span class="tag pending">очікує</span>`:p.status==="fired"?`<span class="tag fired">звільнений</span>`:`<span class="tag">${esc(p.role)}</span>`;
    const venues = p.status==="active"? `<div class="hint" style="margin-top:4px">🏠 ${esc(p.venues.join(", ")||"—")}</div>`:"";
    return `<div class="staff-item"><div class="nm">${esc(p.name)} ${tag}</div>${venues}
      <div class="btn-row" style="margin-top:8px">${staffButtons(p)}</div></div>`;
  }).join("") || `<div class="empty">Немає співробітників</div>`;
  view.innerHTML = `<div class="section-title">Персонал (${s.staff.length})</div>${items}`;
  view.querySelectorAll("[data-act]").forEach(b=>b.onclick=()=>staffAction(b.dataset.act, Number(b.dataset.uid), b.dataset.name));
}
function staffButtons(p){
  const u=p.uid, n=esc(p.name);
  if (p.status==="pending") return `<button class="btn primary" data-act="approve" data-uid="${u}" data-name="${n}">✅ Підтвердити</button>
    <button class="btn danger" data-act="reject" data-uid="${u}" data-name="${n}">❌ Відхилити</button>`;
  if (p.status==="fired") return `<button class="btn" data-act="restore" data-uid="${u}" data-name="${n}">♻️ Відновити</button>`;
  let b = `<button class="btn" data-act="venues" data-uid="${u}" data-name="${n}">🏠 Заклади</button>
    <button class="btn" data-act="role" data-uid="${u}" data-name="${n}">🔄 Роль</button>`;
  b += `<button class="btn danger" data-act="fire" data-uid="${u}" data-name="${n}">❌ Звільнити</button>`;
  return b;
}
async function staffAction(act, uid, name){
  if (act==="venues") return venuesDialog(uid, name);
  if (act==="role") return roleDialog(uid, name);
  const confirmMap = { approve:["Підтвердити "+name+"?","approve","✅ Підтверджено"],
    reject:["Відхилити "+name+"?","reject","Відхилено"], fire:["Звільнити "+name+"?","fire","Звільнено"],
    restore:["Відновити "+name+"?","restore","♻️ Відновлено"] };
  const [q, ep, ok] = confirmMap[act];
  const go = async () => { try{ await api(`/api/staff/${uid}/${ep}`,{method:"POST",body:{}}); haptic("success"); toast(ok); renderStaff(); }catch(e){ toast(errText(e)); } };
  if (tg && tg.showConfirm) tg.showConfirm(q, v=>{ if(v) go(); }); else if (confirm(q)) go();
}
async function venuesDialog(uid, name){
  let cur=[];
  try { const s=await api("/api/staff?all=1"); const p=s.staff.find(x=>x.uid===uid); cur=p?p.venues:[]; } catch(e){}
  const boxes = ME.all_restaurants.map(r=>`<label style="display:flex;align-items:center;gap:8px;margin:8px 0">
    <input type="checkbox" value="${esc(r)}" ${cur.includes(r)?"checked":""} style="width:auto;margin:0"> ${esc(r)}</label>`).join("");
  openModal(`<button class="close" onclick="closeModalGlobal()">✕</button>
    <h3>🏠 Доступ: ${esc(name)}</h3>${boxes}
    <button class="btn primary block" id="vgo">Зберегти</button>`);
  document.getElementById("vgo").onclick=async()=>{
    const venues=[...modalCard.querySelectorAll("input:checked")].map(i=>i.value);
    if (!venues.length) return toast("Має лишитись хоча б один заклад");
    try{ await api(`/api/staff/${uid}/venues`,{method:"POST",body:{venues}}); haptic("success"); toast("Збережено"); closeModal(); renderStaff(); }
    catch(e){ toast(errText(e)); }
  };
}
function roleDialog(uid, name){
  const roles=["Офіціант","Адміністратор","Повар","Шеф-повар","Управляючий","Виконавчий директор","Технічний спеціаліст","Клінінг"];
  const opts=roles.map(r=>`<option value="${esc(r)}">${esc(r)}</option>`).join("");
  openModal(`<button class="close" onclick="closeModalGlobal()">✕</button>
    <h3>🔄 Роль: ${esc(name)}</h3><select id="rl">${opts}</select>
    <button class="btn primary block" id="rlgo">Зберегти</button>`);
  document.getElementById("rlgo").onclick=async()=>{
    const role=document.getElementById("rl").value;
    try{ await api(`/api/staff/${uid}/role`,{method:"POST",body:{role}}); haptic("success"); toast("Роль змінено"); closeModal(); renderStaff(); }
    catch(e){ toast(errText(e)); }
  };
}

function removeFab(){ const f=document.getElementById("fab"); if(f) f.remove(); }

boot();
