// Today's usage, the trust score, the running timer and any pending check-in
// live in this browser only.
const KEY = "ii-usage";
const ACTIVE = "ii-active";
const CHECKIN = "ii-checkin";
const TRUST = "ii-trust";
const DEMO = new URLSearchParams(location.search).has("demo");
const $ = (id) => document.getElementById(id);
let target = "";
let proofImage = "";
let tick = null;

function today() { return new Date().toLocaleDateString("en-CA"); }

function load(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}
function save(key, value) {
  try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, JSON.stringify(value)); } catch {}
}

function usage() {
  const u = load(KEY, null);
  return u && u.date === today() ? u : { date: today(), minutes: 0, sessions: 0 };
}
function setUsage(u) { save(KEY, u); render(); }

function trust() {
  const t = Number(load(TRUST, 70));
  return Number.isFinite(t) ? Math.max(0, Math.min(100, t)) : 70;
}
function setTrust(t) { save(TRUST, Math.max(0, Math.min(100, Math.round(t)))); render(); }

function render() {
  const u = usage();
  const t = trust();
  $("usageNum").textContent = Math.round(u.minutes);
  $("sessNum").textContent = u.sessions;
  $("trustNum").textContent = t;
  $("trustPill").className = "trust " + (t >= 70 ? "good" : t >= 50 ? "ok" : "low");
  $("usageSlider").value = Math.min(300, Math.round(u.minutes));
  $("sliderVal").textContent = Math.round(u.minutes) + " min";
  $("trustSlider").value = t;
  $("trustVal").textContent = t;
  const low = t < 50;
  $("honest").classList.toggle("warn", low);
  $("honestText").textContent = low
    ? `Your trust is ${t}. Your check-ins keep breaking your own word. If you keep lying to yourself, uninstall the app.`
    : "If you keep lying to yourself, uninstall the app.";
}

function show(view) {
  ["askView", "verdictView", "checkinView"].forEach((id) => $(id).classList.toggle("hidden", id !== view));
}

// Pick an app
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

// Screenshot proof, shrunk in the browser before upload
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
  if (!target) return ($("error").textContent = "Pick an app first.");
  if (!reason) return ($("error").textContent = "Type your reason.");
  const u = usage();
  $("goBtn").disabled = true;
  $("goBtn").textContent = proofImage || $("proofUrl").value.trim() ? "Checking your proof…" : "Thinking…";
  try {
    const data = await post("/api/check", {
      target, reason,
      usage_minutes: Math.round(u.minutes), sessions: u.sessions, trust: trust(),
      proof_url: $("proofUrl").value.trim(), image: proofImage,
    });
    showVerdict(data, reason);
  } catch (err) {
    $("error").textContent = err.message || "Couldn't reach the gatekeeper. Try again.";
  } finally {
    $("goBtn").disabled = false;
    $("goBtn").textContent = "Ask the gatekeeper";
  }
}

function showVerdict(data, reason) {
  show("verdictView");
  const word = $("verdictWord");
  word.className = "verdict " + (data.approved ? "approve" : "deny");
  word.textContent = data.approved ? `APPROVED · ${data.minutes} MIN` : "DENIED";
  $("verdictMsg").textContent = data.message;
  if (data.approved) {
    const u = usage();
    setUsage({ ...u, sessions: u.sessions + 1 });
    save(ACTIVE, { target, reason, start: Date.now(), minutes: data.minutes });
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
  $("timerSub").textContent = `${a.target} is unlocked. When this hits zero, it locks again and you check in.`;
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

// Time is up (or they stopped early): add the usage and require a check-in.
function endSession(usedMinutes) {
  clearInterval(tick);
  const a = load(ACTIVE, null);
  save(ACTIVE, null);
  if (!a) return;
  const used = Math.max(1, Math.round(Math.min(usedMinutes, a.minutes)));
  const u = usage();
  setUsage({ ...u, minutes: u.minutes + used });
  save(CHECKIN, { target: a.target, reason: a.reason, minutes: a.minutes, used });
  showCheckin();
}

function showCheckin() {
  const c = load(CHECKIN, null);
  if (!c) return show("askView");
  show("checkinView");
  $("checkinAsk").classList.remove("hidden");
  $("checkinResult").classList.add("hidden");
  $("checkinError").textContent = "";
  $("checkinIntro").textContent = `You got ${c.minutes} min of ${c.target} and used ${c.used}. You said: "${c.reason}"`;
  $("report").focus();
}

function checkinResult(kept, message) {
  const before = trust();
  setTrust(before + (kept ? 5 : -20));
  const after = trust();
  save(CHECKIN, null);
  $("checkinAsk").classList.add("hidden");
  $("checkinResult").classList.remove("hidden");
  $("checkinWord").className = "verdict " + (kept ? "approve" : "deny");
  $("checkinWord").textContent = kept ? "KEPT YOUR WORD" : "BROKE YOUR WORD";
  $("checkinMsg").textContent = message;
  $("trustChange").textContent = `Trust ${before} → ${after}` + (after < 50 ? ". Tasks now need proof, and breaks are shorter." : "");
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
    checkinResult(data.kept, data.message);
  } catch (err) {
    $("checkinError").textContent = err.message || "Couldn't reach the gatekeeper. Try again.";
  } finally {
    $("reportBtn").disabled = false;
    $("reportBtn").textContent = "Submit check-in";
  }
});
$("skipBtn").addEventListener("click", () => {
  if (load(CHECKIN, null)) checkinResult(false, "Skipping the check-in is a way of not looking. That counts as broken.");
});

function resetAsk() {
  $("reason").value = "";
  $("proofUrl").value = "";
  $("report").value = "";
  proofImage = "";
  $("thumbWrap").classList.add("hidden");
  $("verdictWord").textContent = "";
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

if (DEMO) $("demoBox").classList.remove("hidden");
$("usageSlider").addEventListener("input", (e) => setUsage({ ...usage(), minutes: Number(e.target.value) }));
$("trustSlider").addEventListener("input", (e) => setTrust(Number(e.target.value)));
$("resetBtn").addEventListener("click", () => {
  clearInterval(tick);
  [ACTIVE, CHECKIN].forEach((k) => save(k, null));
  save(TRUST, 70);
  setUsage({ date: today(), minutes: 0, sessions: 0 });
  resetAsk();
});

render();
if (load(ACTIVE, null)) startTimer();
else if (load(CHECKIN, null)) showCheckin();
