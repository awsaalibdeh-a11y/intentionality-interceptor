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
const SOS = "ii-sos";
const INTRO = "ii-intro";
const SOS_MINUTES = 3, SOS_COST = 30;
const BLOCKS = "ii-blocks";
const IGNORE_WHY = "ii-ignore-why";   // an ignore session that ended and still needs its reason
const BADDAY = "ii-badday";           // {date, until, last}
const IGNORE_MINUTES = 15, IGNORE_COST = 40, BADDAY_EVERY_DAYS = 7;
const DEMO = new URLSearchParams(location.search).has("demo");
const NATIVE = window.VouchNative || null;

const SCORE_MIN = 300, SCORE_MAX = 850, SCORE_START = 700;
const TIERS = [
  [800, "Excellent", 1.0],
  [740, "Very good", 0.95],
  [670, "Good", 0.85],
  [580, "Fair", 0.7],
  [300, "Poor", 0.45],
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
  for (const e of history()) {
    if (e.emergency || e.badday) continue;
    if (!e.kept) break;
    n++;
  }
  return n;
}
// What the AI sees: real check-ins only, not emergency passes.
function promises() { return history().filter((e) => !e.emergency && !e.badday); }

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
    li.className = e.badday ? "bad" : e.ignored ? "broke" : e.emergency ? "sos" : e.kept ? "kept" : "broke";
    const top = document.createElement("div");
    top.className = "rec-top";
    for (const [cls, text] of [["tag", e.badday ? "BAD DAY" : e.ignored ? "IGNORED" : e.emergency ? "EMERGENCY" : e.kept ? "KEPT" : "BROKE"], ["rec-app", `${e.target} · ${e.used}/${e.minutes} min`], ["rec-when", ago(e.at)]]) {
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
  renderBadges();
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
      daily_limit: r.limit, focus: inFocus(r), block: nowBlock(),
      proof_url: $("proofUrl").value.trim(), image: proofImage,
      history: promises().slice(0, 5).map((e) => ({ reason: e.reason, report: e.report, kept: e.kept })),
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

  const broke = data.approved ? null : promises().slice(0, 5).find((e) => !e.kept);
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
    save(ACTIVE, { target, reason, start: Date.now(), minutes: data.minutes, proven: (data.judgment || {}).proof === "VERIFIED" });
    native("unlock", target, data.minutes);
    startTimer();
  } else {
    ["timer", "timerSub", "doneBtn"].forEach((id) => $(id).classList.add("hidden"));
    $("backBtn").classList.remove("hidden");
    if (data.lock_minutes) {
      lockFor(data.lock_minutes);
      $("cutNote").classList.remove("hidden");
      $("cutNote").textContent = `Trying to game it locks everything for ${data.lock_minutes} min (the AI's call).`;
    }
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
  if (a.ignored) {
    save(IGNORE_WHY, { target: a.target, minutes: a.minutes, used });
    return askWhy();
  }
  if (a.emergency) {
    // Emergency passes skip the check-in; they show in the record so they can't hide.
    save(HISTORY, [{ target: a.target, reason: a.reason, minutes: a.minutes, used, report: "", kept: true, emergency: true, at: Date.now() }, ...history()].slice(0, 30));
    render();
    return resetAsk();
  }
  save(CHECKIN, { target: a.target, reason: a.reason, minutes: a.minutes, used, proven: !!a.proven });
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

function lockFor(minutes) {
  save(COOLDOWN, Date.now() + minutes * 60000);
  native("lock");
  renderCooldown();
}

// Points depend on how it went, not just kept/broke: stopping early, proof and streaks earn more;
// an honest slip costs less than drifting, and evasion costs the most (the AI's lockout length says how bad it was).
function pointsFor(kept, c, lock, skipped) {
  const parts = [];
  if (kept) {
    parts.push(["Kept your word", 15]);
    const unused = (c.minutes || 0) - (c.used || 0);
    if (c.minutes && c.used < c.minutes * 0.8) parts.push(["Stopped early", Math.min(10, Math.max(2, Math.round(unused / 2)))]);
    if (c.proven) parts.push(["Proven task", 5]);
    const st = streak();
    if (st > 0) parts.push([`Streak of ${st}`, Math.min(10, st * 2)]);
  } else if (skipped) {
    parts.push(["Skipped the check-in", -60]);
  } else if (lock <= 15) {
    parts.push(["Honest small slip", -25]);
  } else if (lock <= 30) {
    parts.push(["Drifted", -40]);
  } else if (lock <= 60) {
    parts.push(["Vague or evasive", -55]);
  } else {
    parts.push(["Tried to game it", -70]);
  }
  return parts;
}

function checkinResult(kept, message, report, lockMinutes, skipped) {
  const c = load(CHECKIN, null) || {};
  const lock = kept ? 0 : lockMinutes || COOLDOWN_MIN;
  const parts = pointsFor(kept, c, lock, skipped);
  const delta = parts.reduce((t, [, n]) => t + n, 0);
  if (c.target) {
    save(HISTORY, [{ ...c, report, kept, message, points: delta, at: Date.now() }, ...history()].slice(0, 30));
    addDayMinutes(kept ? "purpose" : "drift", c.used);
  }
  if (lock) lockFor(lock);
  const before = score();
  setScore(before + delta);
  const after = score();
  $("pointsList").textContent = "";
  for (const [label, n] of parts) {
    const li = document.createElement("li");
    li.append(Object.assign(document.createElement("span"), { textContent: label }),
      Object.assign(document.createElement("b"), { textContent: (n > 0 ? "+" : "") + n, className: n < 0 ? "neg" : "" }));
    $("pointsList").append(li);
  }
  save(CHECKIN, null);
  $("checkinAsk").classList.add("hidden");
  $("checkinResult").classList.remove("hidden");
  $("checkinWord").className = "verdict " + (kept ? "approve" : "deny");
  $("checkinWord").textContent = kept ? "KEPT YOUR WORD" : "BROKE YOUR WORD";
  $("checkinMsg").textContent = message;
  $("trustChange").textContent = `Score ${before} → ${after} (${tier(after)[1]}, ${Math.round(tier(after)[2] * 100)}% of time)` +
    (kept ? "" : ` · everything locked for ${lock} min (the AI's call)`);
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
    const broken = promises().slice(0, 10).filter((e) => !e.kept).length;
    const data = await post("/api/checkin", { ...c, report, broken_recently: broken });
    checkinResult(data.kept, data.message, report, data.lock_minutes);
  } catch (err) {
    $("checkinError").textContent = err.message || "Couldn't reach the gatekeeper. Try again.";
  } finally {
    $("reportBtn").disabled = false;
    $("reportBtn").textContent = "Submit";
  }
});
$("skipBtn").addEventListener("click", () => {
  if (load(CHECKIN, null)) checkinResult(false, "Skipping the check-in is a way of not looking. That counts as broken.", "", 45, true);
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
  [ACTIVE, CHECKIN, HISTORY, DAYS, COOLDOWN, SOS, IGNORE_WHY, BADDAY].forEach((k) => save(k, null));
  save(TRUST, SCORE_START);
  native("lock");
  setUsage({ date: today(), minutes: 0, sessions: 0 });
  resetAsk();
});

// ---- native setup (Android / Windows apps) -------------------------------

function nativeStatus() {
  try { return JSON.parse(NATIVE.status()); } catch { return null; }
}
function renderSetup() {
  const st = NATIVE && typeof NATIVE.status === "function" ? nativeStatus() : null;
  if (!st) return;
  const done = (id, ok, label) => {
    $(id).textContent = ok ? "On" : label;
    $(id).disabled = ok;
  };
  done("setUsage", st.usage !== false, "Allow");
  done("setOverlay", st.overlay !== false, "Allow");
  done("setGuard", !!st.guard, "Turn on");
  $("setupView").classList.toggle("hidden", st.usage !== false && st.overlay !== false && !!st.guard);
}
window.vouchNativeChanged = renderSetup;
$("setUsage").addEventListener("click", () => native("askUsage"));
$("setOverlay").addEventListener("click", () => native("askOverlay"));
$("setGuard").addEventListener("click", () => { native("startGuard"); setTimeout(renderSetup, 500); });

// The lock screen sends people here as /?app=TikTok: pick that app for them.
const asked = new URLSearchParams(location.search).get("app");
if (asked) {
  const chip = [...document.querySelectorAll(".chip")].find((c) => c.dataset.app.toLowerCase() === asked.toLowerCase());
  if (chip) chip.click();
  else { $("customApp").value = asked; $("customApp").dispatchEvent(new Event("input")); }
}

// ---- tabs ----------------------------------------------------------------

function goTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.dataset.tab !== name));
  document.querySelectorAll(".tabbar button").forEach((b) => b.classList.toggle("on", b.dataset.go === name));
  window.scrollTo(0, 0);
}
document.querySelectorAll(".tabbar button").forEach((b) => b.addEventListener("click", () => goTab(b.dataset.go)));

// ---- emergency pass: 3 minutes, once a day, costs points, no AI ------------

$("sosBtn").addEventListener("click", () => {
  $("error").textContent = "";
  if (load(SOS, "") === today()) return ($("error").textContent = "You've used today's emergency pass.");
  if (!target) return ($("error").textContent = "Pick the app first.");
  const why = $("reason").value.trim();
  if (!why) return ($("error").textContent = "Say what the emergency is.");
  if (!confirm(`Open ${target} for ${SOS_MINUTES} minutes without the AI? It costs ${SOS_COST} points, works once a day, and shows in your record.`)) return;
  save(SOS, today());
  setScore(score() - SOS_COST);
  const u = usage();
  setUsage({ ...u, sessions: u.sessions + 1 });
  save(ACTIVE, { target, reason: why, start: Date.now(), minutes: SOS_MINUTES, emergency: true });
  native("unlock", target, SOS_MINUTES);
  show("verdictView");
  $("verdictWord").className = "verdict approve";
  $("verdictWord").textContent = `EMERGENCY · ${SOS_MINUTES} MIN`;
  $("verdictMsg").textContent = `−${SOS_COST} points. Handle it and close it.`;
  startTimer();
});

// ---- badges ------------------------------------------------------------------

const BADGES = [
  ["First promise kept", (h) => h.some((e) => e.kept && !e.emergency)],
  ["3 in a row", () => streak() >= 3],
  ["7 in a row", () => streak() >= 7],
  ["Proof pro: 5 proven tasks", (h) => h.filter((e) => e.kept && e.proven).length >= 5],
  ["Honest even when it hurt", (h) => h.some((e) => !e.kept && /honest|admit|sorry|i drifted|got distracted/i.test(e.report || ""))],
  ["Very good score", () => score() >= 740],
  ["Excellent score", () => score() >= 800],
];
function renderBadges() {
  const h = history();
  const box = $("badgeList");
  box.textContent = "";
  for (const [name, test] of BADGES) {
    const got = !!test(h);
    box.append(Object.assign(document.createElement("span"), { className: "badge" + (got ? " got" : ""), textContent: name }));
  }
}

// ---- AI review of your record ------------------------------------------------

$("reviewBtn").addEventListener("click", async () => {
  const h = promises().slice(0, 20);
  if (h.length < 3) {
    $("reviewEmpty").textContent = "Vouch needs at least 3 check-ins to see a pattern.";
    return;
  }
  $("reviewBtn").disabled = true;
  $("reviewBtn").textContent = "Thinking…";
  try {
    const data = await post("/api/review", {
      history: h.map((e) => ({ target: e.target, reason: e.reason, report: e.report, kept: e.kept, used: e.used, hour: new Date(e.at).getHours(), day: new Date(e.at).toLocaleDateString("en", { weekday: "short" }) })),
      score: score(),
    });
    $("revPattern").textContent = data.pattern;
    $("revWorking").textContent = data.working;
    $("revTry").textContent = data.try_this;
    $("reviewOut").classList.remove("hidden");
    $("reviewEmpty").classList.add("hidden");
  } catch (err) {
    $("reviewEmpty").textContent = err.message || "Couldn't reach Vouch. Try again.";
  } finally {
    $("reviewBtn").disabled = false;
    $("reviewBtn").textContent = "Review";
  }
});

// ---- first-run intro -----------------------------------------------------------

const INTRO_STEPS = [
  ["Name it.", "Before a distracting app opens, tell Vouch what you want it for. A real task with a finish line, or an honest break."],
  ["Prove it.", "Tasks need proof: the exact link or a screenshot. The AI checks that it matches. No proof, five minutes at most."],
  ["Keep your word.", "When time's up, you check in. Keep your word and your score climbs, and a higher score means more time. Lie, and it drops. If you keep lying to yourself, uninstall the app."],
];
let introAt = 0;
function showIntro(i) {
  introAt = i;
  $("intro").classList.remove("hidden");
  $("introStep").textContent = `${i + 1} / ${INTRO_STEPS.length}`;
  $("introTitle").textContent = INTRO_STEPS[i][0];
  $("introText").textContent = INTRO_STEPS[i][1];
  $("introNext").textContent = i === INTRO_STEPS.length - 1 ? "Start" : "Next";
}
function closeIntro() { $("intro").classList.add("hidden"); save(INTRO, true); }
$("introNext").addEventListener("click", () => (introAt < INTRO_STEPS.length - 1 ? showIntro(introAt + 1) : closeIntro()));
$("introSkip").addEventListener("click", closeIntro);
$("introAgain").addEventListener("click", () => showIntro(0));
if (!load(INTRO, false) && !DEMO) showIntro(0);

// ---- your day: schedule blocks ------------------------------------------------

function blocks() {
  const b = load(BLOCKS, []);
  return Array.isArray(b) ? b : [];
}
function mins(t) { const [h, m] = String(t).split(":").map(Number); return h * 60 + (m || 0); }
function blockAt(date = new Date()) {
  const now = date.getHours() * 60 + date.getMinutes();
  return blocks().find((b) => {
    const f = mins(b.from), t = mins(b.to);
    return f <= t ? now >= f && now < t : now >= f || now < t;
  }) || null;
}
function nowBlock() {
  const b = blockAt();
  return b ? { label: b.label, kind: b.kind } : null;
}
function nextBlock() {
  const now = new Date().getHours() * 60 + new Date().getMinutes();
  return blocks().filter((b) => mins(b.from) > now).sort((a, b) => mins(a.from) - mins(b.from))[0] || null;
}
const KIND_TEXT = { focus: "focus", free: "free time", other: "" };
function renderDay() {
  const list = $("blockList");
  list.textContent = "";
  const all = blocks().slice().sort((a, b) => mins(a.from) - mins(b.from));
  $("blocksEmpty").classList.toggle("hidden", all.length > 0);
  const cur = blockAt();
  all.forEach((b) => {
    const li = document.createElement("li");
    li.className = "block " + b.kind + (b === cur || (cur && b.from === cur.from && b.label === cur.label) ? " current" : "");
    li.append(
      Object.assign(document.createElement("span"), { className: "b-time", textContent: `${b.from}–${b.to}` }),
      Object.assign(document.createElement("span"), { className: "b-label", textContent: b.label }),
      Object.assign(document.createElement("span"), { className: "b-kind", textContent: b.kind }));
    const del = Object.assign(document.createElement("button"), { className: "b-del", type: "button", textContent: "×", title: "Remove" });
    del.addEventListener("click", () => { save(BLOCKS, blocks().filter((x) => !(x.from === b.from && x.to === b.to && x.label === b.label))); renderDay(); });
    li.append(del);
    list.append(li);
  });
  const focusMin = all.filter((b) => b.kind === "focus" && !/sleep/i.test(b.label)).reduce((t, b) => t + ((mins(b.to) - mins(b.from) + 1440) % 1440), 0);
  $("daySum").textContent = all.length ? `${Math.round(focusMin / 6) / 10} h of focus planned` : "";
  // the "now" strip on Home
  const nb = nextBlock();
  $("nowCard").classList.toggle("hidden", !cur && !nb);
  $("nowDot").className = "now-dot " + (cur ? cur.kind : "idle");
  $("nowLabel").textContent = cur ? cur.label : "Nothing planned now";
  $("nowWhen").textContent = cur ? `· ${KIND_TEXT[cur.kind] ? KIND_TEXT[cur.kind] + " · " : ""}until ${cur.to}` : "";
  $("nowNext").textContent = nb ? `Next: ${nb.label} at ${nb.from}` : "";
}
$("bAdd").addEventListener("click", () => {
  const b = { from: $("bFrom").value, to: $("bTo").value, label: $("bLabel").value.trim(), kind: $("bKind").value };
  if (!b.from || !b.to || !b.label) return;
  save(BLOCKS, [...blocks(), b].slice(0, 24));
  $("bLabel").value = "";
  renderDay();
});
const TEMPLATES = {
  school: [["07:30", "14:30", "School", "other"], ["15:00", "16:00", "Free time", "free"], ["16:00", "18:00", "Homework", "focus"], ["18:00", "19:30", "Free time", "free"], ["20:00", "21:00", "Study", "focus"], ["22:30", "07:00", "Sleep", "focus"]],
  weekend: [["10:00", "12:00", "Projects", "focus"], ["12:00", "18:00", "Free time", "free"], ["23:30", "08:30", "Sleep", "focus"]],
  exam: [["08:00", "12:00", "Revision", "focus"], ["12:00", "13:00", "Lunch break", "free"], ["13:00", "17:00", "Past papers", "focus"], ["17:00", "18:30", "Free time", "free"], ["19:00", "21:00", "Flashcards", "focus"], ["22:00", "06:30", "Sleep", "focus"]],
};
document.querySelectorAll("[data-template]").forEach((btn) => btn.addEventListener("click", () => {
  save(BLOCKS, TEMPLATES[btn.dataset.template].map(([from, to, label, kind]) => ({ from, to, label, kind })));
  renderDay();
}));
$("bClear").addEventListener("click", () => { save(BLOCKS, []); renderDay(); });
$("nowCard").addEventListener("click", () => goTab("day"));
setInterval(renderDay, 60000);

// ---- ignore: open anyway, but the reason is required afterwards ---------------

function startIgnore(app) {
  if (!app) return ($("error").textContent = "Pick the app first.");
  if (!confirm(`Open ${app} for ${IGNORE_MINUTES} minutes without asking? It costs ${IGNORE_COST} points, shows in your record, and afterwards you must say why. That part can't be skipped.`)) return;
  setScore(score() - IGNORE_COST);
  const u = usage();
  setUsage({ ...u, sessions: u.sessions + 1 });
  save(ACTIVE, { target: app, reason: "Ignored Vouch", start: Date.now(), minutes: IGNORE_MINUTES, ignored: true });
  native("unlock", app, IGNORE_MINUTES);
  show("verdictView");
  $("verdictWord").className = "verdict deny";
  $("verdictWord").textContent = `IGNORED · ${IGNORE_MINUTES} MIN`;
  $("verdictMsg").textContent = `−${IGNORE_COST} points. When it ends you'll have to say why.`;
  startTimer();
}
$("ignoreBtn").addEventListener("click", () => { $("error").textContent = ""; startIgnore(target); });

function askWhy() {
  const w = load(IGNORE_WHY, null);
  if (!w) return;
  $("whyIntro").textContent = `You opened ${w.target} for ${w.used} min without asking. What was really going on?`;
  $("whyModal").classList.remove("hidden");
  $("whyText").focus();
}
$("whySend").addEventListener("click", () => {
  const w = load(IGNORE_WHY, null);
  const why = $("whyText").value.trim();
  if (why.length < 10) return ($("whyError").textContent = "Give a real answer, at least a sentence.");
  if (w) {
    save(HISTORY, [{ target: w.target, reason: "Ignored Vouch", minutes: w.minutes, used: w.used, report: why, kept: false, ignored: true, points: -IGNORE_COST, at: Date.now() }, ...history()].slice(0, 30));
    addDayMinutes("drift", w.used);
  }
  save(IGNORE_WHY, null);
  $("whyText").value = "";
  $("whyError").textContent = "";
  $("whyModal").classList.add("hidden");
  render();
  resetAsk();
});
// No way out of the "why" box: no close button, Escape does nothing, and it comes back after a reload.
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("whyModal").classList.contains("hidden")) e.preventDefault(); }, true);

// ---- bad day: everything open until midnight, no points, once a week ------------

function badDay() { return load(BADDAY, {}) || {}; }
function badDayActive() { const b = badDay(); return b.until && Date.now() < b.until; }
function renderBadDay() {
  const on = badDayActive();
  $("badBanner").classList.toggle("hidden", !on);
  if (on) $("badMsg").textContent = badDay().message || "";
}
$("badBtn").addEventListener("click", () => {
  $("badError").textContent = "";
  const last = badDay().last || 0;
  const wait = Math.ceil((last + BADDAY_EVERY_DAYS * 86400000 - Date.now()) / 86400000);
  if (badDayActive()) return ($("error").textContent = "Bad day mode is already on.");
  if (wait > 0) return ($("error").textContent = `Bad day mode is once a week. Next one in ${wait} day${wait === 1 ? "" : "s"}.`);
  $("badModal").classList.remove("hidden");
});
$("badCancel").addEventListener("click", () => $("badModal").classList.add("hidden"));
$("badGo").addEventListener("click", async () => {
  const midnight = new Date(); midnight.setHours(24, 0, 0, 0);
  const feeling = $("badFeeling").value.trim();
  $("badGo").disabled = true;
  let message = "";
  try { message = (await post("/api/badday", { feeling })).message; } catch { message = "Some days are heavy. Be kind to yourself."; }
  $("badGo").disabled = false;
  save(BADDAY, { last: Date.now(), until: midnight.getTime(), message });
  save(HISTORY, [{ target: "Everything", reason: feeling || "Bad day", minutes: 0, used: 0, report: "", kept: true, badday: true, at: Date.now() }, ...history()].slice(0, 30));
  native("unlock", "*", Math.ceil((midnight - Date.now()) / 60000));
  $("badModal").classList.add("hidden");
  $("badFeeling").value = "";
  render();
  renderBadDay();
});

// Native lock cards send people here as /?ignore=TikTok
const ignoring = new URLSearchParams(location.search).get("ignore");
if (ignoring) setTimeout(() => startIgnore(ignoring), 300);

if (NATIVE) $("getApps").classList.add("hidden");
renderSetup();
renderRules();
render();
renderDay();
renderBadDay();
if (load(IGNORE_WHY, null)) askWhy();
if (load(ACTIVE, null)) startTimer();
else if (load(CHECKIN, null)) showCheckin();
