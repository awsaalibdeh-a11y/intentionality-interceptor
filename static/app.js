// Today's usage and the running timer live in this browser only.
const KEY = "ii-usage";
const ACTIVE = "ii-active";
const $ = (id) => document.getElementById(id);
let target = "";
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

function render() {
  const u = usage();
  $("usageNum").textContent = Math.round(u.minutes);
  $("sessNum").textContent = u.sessions;
  $("usageSlider").value = Math.min(300, Math.round(u.minutes));
  $("sliderVal").textContent = Math.round(u.minutes) + " min";
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

async function ask() {
  const reason = $("reason").value.trim();
  $("error").textContent = "";
  if (!target) return ($("error").textContent = "Pick an app first.");
  if (!reason) return ($("error").textContent = "Type your reason.");
  const u = usage();
  $("goBtn").disabled = true;
  $("goBtn").textContent = "Thinking…";
  try {
    const res = await fetch("/api/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, reason, usage_minutes: Math.round(u.minutes), sessions: u.sessions }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Something went wrong.");
    showVerdict(data);
  } catch (err) {
    $("error").textContent = err.message || "Couldn't reach the gatekeeper. Try again.";
  } finally {
    $("goBtn").disabled = false;
    $("goBtn").textContent = "Ask the gatekeeper";
  }
}

function showVerdict(data) {
  $("askView").classList.add("hidden");
  $("verdictView").classList.remove("hidden");
  const word = $("verdictWord");
  word.className = "verdict " + (data.approved ? "approve" : "deny");
  word.textContent = data.approved ? `APPROVED · ${data.minutes} MIN` : "DENIED";
  $("verdictMsg").textContent = data.message;
  if (data.approved) {
    const u = usage();
    setUsage({ ...u, sessions: u.sessions + 1 });
    save(ACTIVE, { target, start: Date.now(), minutes: data.minutes });
    startTimer();
  } else {
    ["timer", "timerSub", "doneBtn"].forEach((id) => $(id).classList.add("hidden"));
  }
}

function startTimer() {
  const a = load(ACTIVE, null);
  if (!a) return;
  $("askView").classList.add("hidden");
  $("verdictView").classList.remove("hidden");
  ["timer", "timerSub", "doneBtn"].forEach((id) => $(id).classList.remove("hidden"));
  $("timerSub").textContent = `${a.target} is unlocked. When this hits zero, it locks again.`;
  if (!$("verdictWord").textContent) {
    $("verdictWord").className = "verdict approve";
    $("verdictWord").textContent = `APPROVED · ${a.minutes} MIN`;
  }
  clearInterval(tick);
  const step = () => {
    const left = a.start + a.minutes * 60000 - Date.now();
    if (left <= 0) return finish(a.minutes, true);
    const s = Math.ceil(left / 1000);
    $("timer").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  };
  step();
  tick = setInterval(step, 1000);
}

function finish(usedMinutes, timeUp) {
  clearInterval(tick);
  save(ACTIVE, null);
  const u = usage();
  setUsage({ ...u, minutes: u.minutes + usedMinutes });
  $("timer").textContent = "00:00";
  $("doneBtn").classList.add("hidden");
  $("verdictWord").className = "verdict " + (timeUp ? "deny" : "approve");
  $("verdictWord").textContent = timeUp ? "TIME'S UP" : "NICE, DONE EARLY";
  $("verdictMsg").textContent = timeUp ? "Locked again. Close the app and get back to what matters." : "That's what intentional looks like.";
  $("timerSub").textContent = `${Math.round(usedMinutes)} min added to today's usage.`;
}

$("goBtn").addEventListener("click", ask);
$("reason").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) ask(); });
$("doneBtn").addEventListener("click", () => {
  const a = load(ACTIVE, null);
  if (a) finish(Math.max(1, (Date.now() - a.start) / 60000), false);
});
$("backBtn").addEventListener("click", () => {
  const a = load(ACTIVE, null);
  if (a) finish(Math.max(1, (Date.now() - a.start) / 60000), false);
  $("verdictView").classList.add("hidden");
  $("askView").classList.remove("hidden");
  $("verdictWord").textContent = "";
  $("reason").value = "";
});
$("usageSlider").addEventListener("input", (e) => {
  setUsage({ ...usage(), minutes: Number(e.target.value) });
});
$("resetBtn").addEventListener("click", () => {
  save(ACTIVE, null);
  clearInterval(tick);
  setUsage({ date: today(), minutes: 0, sessions: 0 });
});

render();
if (load(ACTIVE, null)) startTimer();
