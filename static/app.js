// Vouch keeps everything in this browser: today's usage, the trust score,
// the running timer, the pending check-in, the record, the 7-day chart and
// the user's own rules. The native apps (Android / Windows) hook in through
// window.VouchNative when it exists.
const KEY = "ii-usage";
const ACTIVE = "ii-active";
const CHECKIN = "ii-checkin";
const TRUST = "ii-trust";
const HISTORY = "ii-history";
const DAYS = "ii-days";
const RULES = "ii-rules";
const COOLDOWN = "ii-cooldown";
const DEMO = new URLSearchParams(location.search).has("demo");
const NATIVE = window.VouchNative || null;

const SCORE_MIN = 300, SCORE_MAX = 850, SCORE_START = 700;
const TIERS = [
  [800, "Excellent", 1.0],
  [740, "Very good", 0.9],
  [670, "Good", 0.75],
  [580, "Fair", 0.5],
  [300, "Poor", 0.25],
];
const KEPT_POINTS = 20, BROKE_POINTS = -50, COOLDOWN_MIN = 10;

const $ = (id) => document.getElementById(id);
let target = "";
let proofImage = "";
let tick = null;
let coolTick = null;

function today() { return new Date().toLocaleDateString("en-CA"); }

function load(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}
function save(key, value) {
  try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, JSON.stringify(value)); } catch {}
}
function native(fn, ...args) {
  try { if (NATIVE && typeof NATIVE[fn] === "function") NATIVE[fn](...args); } catch {}
}

// ---- state ---------------------------------------------------------------

function usage() {
  const u = load(KEY, null);
  return u && u.date === today() ? u : { date: today(), minutes: 0, sessions: 0 };
}
function setUsage(u) { save(KEY, u); render(); }

function score() {
  let t = Number(load(TRUST, SCORE_START));
  if (!Number.isFinite(t)) t = SCORE_START;
  if (t <= 100) t = Math.round(300 + t * 5.5); // older 0-100 scores
  return Math.max(SCORE_MIN, Math.min(SCORE_MAX, Math.round(t)));
}
function setScore(t) { save(TRUST, Math.max(SCORE_MIN, Math.min(SCORE_MAX, Math.round(t)))); render(); }
function tier(s) { return TIERS.find(([floor]) => s >= floor) || TIERS[TIERS.length - 1]; }

function history() {
  const h = load(HISTORY, []);
  return Array.isArray(h) ? h : [];
}
function streak() {
  let n = 0;
  for (const e of history()) { if (!e.kept) break; n++; }
  return n;
}

function rules() {
  return { limit: 0, focusOn: false, focusFrom: "16:00", focusTo: "20:00", note: "", ...load(RULES, {}) };
}
function inFocus(r = rules()) {
  if (!r.focusOn) return false;
  const now = new Date();
  const mins = now.getHours() * 60 + now.getMinutes();
  const [fh, fm] = r.focusFrom.split(":").map(Number);
  const [th, tm] = r.focusTo.split(":").map(Number);
  const from = fh * 60 + fm, to = th * 60 + tm;
  return from <= to ? mins >= from && mins < to : mins >= from || mins < to;
}

function addDayMinutes(kind, minutes) {
  const d = load(DAYS, {});
  const day = d[today()] || { purpose: 0, drift: 0 };
  day[kind] += minutes;
  d[today()] = day;
  const keep = Object.keys(d).sort().slice(-30);
  save(DAYS, Object.fromEntries(keep.map((k) => [k, d[k]])));
}

function ago(ts) {
  const m = Math.round((Date.now() - ts) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

// ---- rendering -------------------------------------------------------------

function renderScore() {
  const s = score();
  const [, name, share] = tier(s);
  const frac = (s - SCORE_MIN) / (SCORE_MAX - SCORE_MIN);
  const fill = $("gaugeFill");
  fill.style.strokeDashoffset = String(270 * (1 - frac));
  fill.style.stroke = s >= 670 ? "var(--good)" : s >= 580 ? "var(--warn)" : "var(--bad)";
  $("scoreNum").textContent = s;
  $("scoreTier").textContent = name;
  $("shareNum").textContent = Math.round(share * 100) + "%";
  const st = streak();
  $("streakText").textContent = st ? `${st} kept in a row` : "";
  const next = [...TIERS].reverse().find(([floor]) => floor > s);
  $("scoreNext").textContent = next ? `${next[0] - s} points to ${next[1]}` : "Top tier";
  const low = s < 580;
  $("honest").classList.toggle("warn", low);
  $("honest").textContent = low
    ? `Score ${s}. Your check-ins keep breaking your own word. If you keep lying to yourself, uninstall the app.`
    : "If you keep lying to yourself, uninstall the app.";
}

function renderRecord() {
  const h = history();
  const list = $("recordList");
  list.textContent = "";
  $("recordEmpty").classList.toggle("hidden", h.length > 0);
  const recent = h.slice(0, 10);
  const kept = recent.filter((e) => e.kept).length;
  $("recordSum").textContent = h.length ? `Kept ${kept} of ${recent.length}` : "";
  for (const e of h.slice(0, 6)) {
    const li = document.createElement("li");
    li.className = e.kept ? "kept" : "broke";
    const top = document.createElement("div");
    top.className = "rec-top";
    for (const [cls, text] of [["tag", e.kept ? "KEPT" : "BROKE"], ["rec-app", `${e.target} · ${e.used}/${e.minutes} min`], ["rec-when", ago(e.at)]]) {
      top.append(Object.assign(document.createElement("span"), { className: cls, textContent: text }));
    }
    const said = document.createElement("div");
    said.className = "said";
    said.append("Said ", Object.assign(document.createElement("q"), { textContent: e.reason }));
    const did = document.createElement("div");
    did.className = "did";
    did.append("Did ", Object.assign(document.createElement("q"), { textContent: e.report || "skipped the check-in" }));
    li.append(top, said, did);
    list.append(li);
  }
}

function renderWeek() {
  const d = load(DAYS, {});
  const days = [];
  for (let i = 6; i >= 0; i--) {
    const dt = new Date();
    dt.setDate(dt.getDate() - i);
    const key = dt.toLocaleDateString("en-CA");
    days.push({ key, label: dt.toLocaleDateString("en", { weekday: "narrow" }), ...(d[key] || { purpose: 0, drift: 0 }) });
  }
  const max = Math.max(30, ...days.map((x) => x.purpose + x.drift));
  const box = $("weekBars");
  box.textContent = "";
  let purpose = 0, drift = 0;
  for (const x of days) {
    purpose += x.purpose; drift += x.drift;
    const col = document.createElement("div");
    col.className = "bar-col";
    col.title = `${x.key}: ${x.purpose} min on purpose, ${x.drift} min drifting`;
    const stack = document.createElement("div");
    stack.className = "bar-stack";
    const dr = Object.assign(document.createElement("div"), { className: "bar drift" });
    dr.style.height = (x.drift / max) * 100 + "%";
    const pu = Object.assign(document.createElement("div"), { className: "bar purpose" });
    pu.style.height = (x.purpose / max) * 100 + "%";
    stack.append(dr, pu);
    col.append(stack, Object.assign(document.createElement("span"), { textContent: x.label }));
    box.append(col);
  }
  $("weekSum").textContent = `${purpose} min on purpose · ${drift} min drifting`;
}

function renderRules() {
  const r = rules();
  $("limitSel").value = String(r.limit);
  $("focusOn").checked = r.focusOn;
  $("focusFrom").value = r.focusFrom;
  $("focusTo").value = r.focusTo;
  $("noteText").value = r.note;
  const bits = [];
  if (r.limit) bits.push(`${r.limit} min/day`);
  if (r.focusOn) bits.push(`focus ${r.focusFrom}–${r.focusTo}` + (inFocus(r) ? " (now)" : ""));
  $("rulesSum").textContent = bits.join(" · ") || "none set";
}

function render() {
  const u = usage();
  $("usageNum").textContent = Math.round(u.minutes);
  $("sessNum").textContent = u.sessions;
  const r = rules();
  $("limitNote").textContent = r.limit ? ` of ${r.limit}` : "";
  renderScore();
  renderRecord();
  renderWeek();
  $("usageSlider").value = Math.min(300, Math.round(u.minutes));
  $("sliderVal").textContent = Math.round(u.minutes) + " min";
  $("trustSlider").value = score();
  $("trustVal").textContent = score();
  renderCooldown();
}

function show(view) {
  ["askView", "verdictView", "checkinView"].forEach((id) => $(id).classList.toggle("hidden", id !== view));
}

// ---- cooldown after a broken promise ------------------------------------

function renderCooldown() {
  const until = Number(load(COOLDOWN, 0));
  const left = until - Date.now();
  clearTimeout(coolTick);
  if (left <= 0) {
    $("cooldown").classList.add("hidden");
    $("goBtn").disabled = false;
    return;
  }
  const s = Math.ceil(left / 1000);
  $("cooldownLeft").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  $("cooldown").classList.remove("hidden");
  $("goBtn").disabled = true;
  coolTick = setTimeout(renderCooldown, 1000);
}

// ---- asking ----------------------------------------------------------------

$("chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c === chip));
  $("customApp").value = "";
  $("customApp").classList.remove("on");
  target = chip.dataset.app;
});
$("customApp").addEventListener("input", (e) => {
  document.querySelectorAll(".chip").forEach((c) => c.classList.remove("on"));
  target = e.target.value.trim();
  e.target.classList.toggle("on", !!target);
});

function shrink(file) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, 1280 / Math.max(img.width, img.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(img.src);
      resolve(canvas.toDataURL("image/jpeg", 0.8));
    };
    img.onerror = () => reject(new Error("That file isn't an image."));
    img.src = URL.createObjectURL(file);
  });
}
$("proofFile").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  try {
    proofImage = await shrink(file);
    $("thumb").src = proofImage;
    $("thumbWrap").classList.remove("hidden");
  } catch (err) {
    $("error").textContent = err.message;
  }
});
$("removeImg").addEventListener("click", () => {
  proofImage = "";
  $("thumbWrap").classList.add("hidden");
});

async function post(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Something went wrong.");
  return data;
}

async function ask() {
  const reason = $("reason").value.trim();
  $("error").textContent = "";
  if (load(CHECKIN, null)) return showCheckin();
  if (Number(load(COOLDOWN, 0)) > Date.now()) return renderCooldown();
  if (!target) return ($("error").textContent = "Pick an app first.");
  if (!reason) return ($("error").textContent = "Type your reason.");
  const u = usage();
  const r = rules();
  $("goBtn").disabled = true;
  $("goBtn").textContent = proofImage || $("proofUrl").value.trim() ? "Checking your proof…" : "Thinking…";
  try {
    const data = await post("/api/check", {
      target, reason,
      usage_minutes: Math.round(u.minutes), sessions: u.sessions, trust: score(),
      daily_limit: r.limit, focus: inFocus(r),
      proof_url: $("proofUrl").value.trim(), image: proofImage,
      history: history().slice(0, 5).map((e) => ({ reason: e.reason, report: e.report, kept: e.kept })),
    });
    showVerdict(data, reason);
  } catch (err) {
    $("error").textContent = err.message || "Couldn't reach the gatekeeper. Try again.";
  } finally {
    $("goBtn").disabled = false;
    $("goBtn").textContent = "Ask";
    renderCooldown();
  }
}

function showVerdict(data, reason) {
  show("verdictView");
  const word = $("verdictWord");
  word.className = "verdict " + (data.approved ? "approve" : "deny");
  word.textContent = data.approved ? `APPROVED · ${data.minutes} MIN` : "DENIED";
  $("verdictMsg").textContent = data.message;
  const cut = data.approved && data.granted > data.minutes;
  $("cutNote").classList.toggle("hidden", !cut);
  if (cut) $("cutNote").textContent = `Granted ${data.granted} min. Your ${data.tier} score keeps ${Math.round(data.share * 100)}%, so you get ${data.minutes}.`;

  const broke = data.approved ? null : history().slice(0, 5).find((e) => !e.kept);
  $("receipt").classList.toggle("hidden", !broke);
  if (broke) {
    $("receiptWhen").textContent = `· ${broke.target}, ${ago(broke.at)}`;
    $("receiptSaid").textContent = broke.reason;
    $("receiptDid").textContent = broke.report || "skipped the check-in";
  }
  const note = rules().note.trim();
  $("selfNote").classList.toggle("hidden", data.approved || !note);
  $("selfNoteText").textContent = note;

  if (data.approved) {
    const u = usage();
    setUsage({ ...u, sessions: u.sessions + 1 });
    save(ACTIVE, { target, reason, start: Date.now(), minutes: data.minutes });
    native("unlock", target, data.minutes);
    startTimer();
  } else {
    ["timer", "timerSub", "doneBtn"].forEach((id) => $(id).classList.add("hidden"));
    $("backBtn").classList.remove("hidden");
  }
}

function startTimer() {
  const a = load(ACTIVE, null);
  if (!a) return;
  show("verdictView");
  ["timer", "timerSub", "doneBtn"].forEach((id) => $(id).classList.remove("hidden"));
  $("backBtn").classList.add("hidden");
  $("timerSub").textContent = `${a.target} is unlocked. At zero it locks again and you check in.`;
  if (!$("verdictWord").textContent) {
    $("verdictWord").className = "verdict approve";
    $("verdictWord").textContent = `APPROVED · ${a.minutes} MIN`;
    $("verdictMsg").textContent = `You said: "${a.reason}"`;
  }
  clearInterval(tick);
  const step = () => {
    const left = a.start + a.minutes * 60000 - Date.now();
    if (left <= 0) return endSession(a.minutes);
    const s = Math.ceil(left / 1000);
    $("timer").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  };
  step();
  tick = setInterval(step, 1000);
}

function endSession(usedMinutes) {
  clearInterval(tick);
  const a = load(ACTIVE, null);
  save(ACTIVE, null);
  native("lock");
  if (!a) return;
  const used = Math.max(1, Math.round(Math.min(usedMinutes, a.minutes)));
  const u = usage();
  setUsage({ ...u, minutes: u.minutes + used });
  save(CHECKIN, { target: a.target, reason: a.reason, minutes: a.minutes, used });
  showCheckin();
}

// ---- check-in ----------------------------------------------------------------

function showCheckin() {
  const c = load(CHECKIN, null);
  if (!c) return show("askView");
  show("checkinView");
  $("checkinAsk").classList.remove("hidden");
  $("checkinResult").classList.add("hidden");
  $("checkinError").textContent = "";
  $("checkinIntro").textContent = `${c.minutes} min of ${c.target}, used ${c.used}. You said: "${c.reason}"`;
  $("report").focus();
}

function checkinResult(kept, message, report) {
  const c = load(CHECKIN, null);
  if (c) {
    save(HISTORY, [{ ...c, report, kept, message, at: Date.now() }, ...history()].slice(0, 30));
    addDayMinutes(kept ? "purpose" : "drift", c.used);
  }
  if (!kept) save(COOLDOWN, Date.now() + COOLDOWN_MIN * 60000);
  const before = score();
  setScore(before + (kept ? KEPT_POINTS : BROKE_POINTS));
  const after = score();
  save(CHECKIN, null);
  $("checkinAsk").classList.add("hidden");
  $("checkinResult").classList.remove("hidden");
  $("checkinWord").className = "verdict " + (kept ? "approve" : "deny");
  $("checkinWord").textContent = kept ? "KEPT YOUR WORD" : "BROKE YOUR WORD";
  $("checkinMsg").textContent = message;
  $("trustChange").textContent = `Score ${before} → ${after} (${tier(after)[1]}, ${Math.round(tier(after)[2] * 100)}% of time)` +
    (kept ? "" : ` · ${COOLDOWN_MIN}-minute cooldown`);
  $("uninstallMsg").classList.toggle("hidden", kept);
}

$("reportBtn").addEventListener("click", async () => {
  const c = load(CHECKIN, null);
  const report = $("report").value.trim();
  if (!c) return show("askView");
  if (!report) return ($("checkinError").textContent = "Say what you actually did.");
  $("reportBtn").disabled = true;
  $("reportBtn").textContent = "Checking…";
  try {
    const data = await post("/api/checkin", { ...c, report });
    checkinResult(data.kept, data.message, report);
  } catch (err) {
    $("checkinError").textContent = err.message || "Couldn't reach the gatekeeper. Try again.";
  } finally {
    $("reportBtn").disabled = false;
    $("reportBtn").textContent = "Submit";
  }
});
$("skipBtn").addEventListener("click", () => {
  if (load(CHECKIN, null)) checkinResult(false, "Skipping the check-in is a way of not looking. That counts as broken.", "");
});

function resetAsk() {
  $("reason").value = "";
  $("proofUrl").value = "";
  $("report").value = "";
  proofImage = "";
  $("thumbWrap").classList.add("hidden");
  $("verdictWord").textContent = "";
  $("receipt").classList.add("hidden");
  $("selfNote").classList.add("hidden");
  $("cutNote").classList.add("hidden");
  show("askView");
}

$("goBtn").addEventListener("click", ask);
$("reason").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) ask(); });
$("doneBtn").addEventListener("click", () => {
  const a = load(ACTIVE, null);
  if (a) endSession((Date.now() - a.start) / 60000);
});
$("backBtn").addEventListener("click", resetAsk);
$("newBtn").addEventListener("click", resetAsk);

// ---- your rules ------------------------------------------------------------

function saveRules() {
  save(RULES, {
    limit: Number($("limitSel").value) || 0,
    focusOn: $("focusOn").checked,
    focusFrom: $("focusFrom").value || "16:00",
    focusTo: $("focusTo").value || "20:00",
    note: $("noteText").value.slice(0, 300),
  });
  renderRules();
  render();
}
["limitSel", "focusOn", "focusFrom", "focusTo"].forEach((id) => $(id).addEventListener("change", saveRules));
$("noteText").addEventListener("input", saveRules);

// ---- demo ------------------------------------------------------------------

if (DEMO) $("demoBox").classList.remove("hidden");
$("usageSlider").addEventListener("input", (e) => setUsage({ ...usage(), minutes: Number(e.target.value) }));
$("trustSlider").addEventListener("input", (e) => setScore(Number(e.target.value)));
$("resetBtn").addEventListener("click", () => {
  clearInterval(tick);
  [ACTIVE, CHECKIN, HISTORY, DAYS, COOLDOWN].forEach((k) => save(k, null));
  save(TRUST, SCORE_START);
  native("lock");
  setUsage({ date: today(), minutes: 0, sessions: 0 });
  resetAsk();
});

if (NATIVE) $("getApps").classList.add("hidden");
renderRules();
render();
if (load(ACTIVE, null)) startTimer();
else if (load(CHECKIN, null)) showCheckin();
