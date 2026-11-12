"use strict";
const $ = (id) => document.getElementById(id);
let api, report = null, sel = { g: 0, i: 0 }, cmpShot = null;

// ---------- small helpers ----------
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) e.append(k);
  return e;
}
function show(id) { for (const s of document.querySelectorAll(".screen")) s.hidden = s.id !== id; window.scrollTo(0, 0); }
function toast(msg, ms = 3500) { const t = $("toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), ms); }
function mb(b) { return b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : Math.round(b / 1e6) + " MB"; }
function plural(n, w) { return `${n} ${w}${n === 1 ? "" : "s"}`; }
function rel(p) { return report && p.toLowerCase().startsWith(report.root.toLowerCase()) ? p.slice(report.root.length).replace(/^[\\/]/, "") : p; }
async function call(name, ...args) {
  try { return await api[name](...args); }
  catch (e) { toast("Something went wrong: " + (e.message || e)); throw e; }
}

// tooltips for [data-tip]; native title= covers the rest
document.addEventListener("mouseover", (e) => { const t = e.target.closest("[data-tip]"); if (t) tip(t); });
document.addEventListener("focusin", (e) => { const t = e.target.closest("[data-tip]"); if (t) tip(t); });
document.addEventListener("mouseout", (e) => { if (e.target.closest("[data-tip]")) $("tip").classList.remove("on"); });
document.addEventListener("focusout", () => $("tip").classList.remove("on"));
function tip(t) {
  const b = $("tip"), r = t.getBoundingClientRect();
  b.textContent = t.dataset.tip; b.classList.add("on");
  b.style.left = Math.min(r.left, innerWidth - 340) + "px"; b.style.top = (r.bottom + 6) + "px";
}

// ---------- start screen ----------
// the bridge may be ready before or after this script runs
if (window.pywebview && window.pywebview.api && window.pywebview.api.init) start();
else window.addEventListener("pywebviewready", start, { once: true });
async function start() {
  if (api) return;
  api = window.pywebview.api;
  const st = await call("init");
  const s = st.settings || {};
  if (s.folder) $("folder").value = s.folder;
  if (s.recursive === false) $("recursive").checked = false;
  if (s.merge) $("merge").checked = true;
  if (s.level) document.querySelector(`input[name=level][value=${s.level}]`).checked = true;
  if (s.mode === "dupes") document.querySelector("input[name=mode][value=dupes]").checked = true;
  modeChanged();
  if (s.ai_url && s.ai_url !== "") $("ai-url").value = s.ai_url;
  if (s.ai) { $("ai").checked = true; aiToggle(s.ai_model); }
  checkFolder();
  showOpenRuns(st.open_runs);
  if (st.problems.length) {
    const b = $("problems"); b.hidden = false;
    b.append(el("b", {}, "The app was interrupted last time while moving photos."),
      el("span", {}, `${plural(st.problems.length, "file")} could not be settled automatically. Check these places:`),
      el("ul", {}, st.problems.map((p) => el("li", {}, p.src, el("br"), p.dst))));
  }
}

function showOpenRuns(runs) {
  const b = $("open-runs"); b.replaceChildren(); b.hidden = !runs.length;
  if (!runs.length) return;
  b.append(el("b", {}, "Photos still waiting in a holding folder from earlier"));
  for (const r of runs) {
    b.append(el("div", { class: "row" },
      el("span", {}, `${r.root} - ${plural(r.count, "photo")} (${mb(r.bytes)}), from ${r.run.replace(/_(\d\d)(\d\d)\d\d$/, " at $1:$2")}`),
      el("span", { class: "row" },
        el("button", { class: "btn small", onclick: async () => {
          if (await call("resume", r.run)) openReview(await call("get_report"));
          else toast("The review for that run is missing. Use Put back instead.");
        } }, "Review"),
        el("button", { class: "btn small", title: "Move them all back where they came from", onclick: async () => {
          const res = await call("restore_run", r.run);
          toast(`Put back ${plural(res.restored, "photo")}` + (res.skipped.length ? `, ${res.skipped.length} could not be moved` : ""));
          showOpenRuns((await call("init")).open_runs);
        } }, "Put back"))));
  }
}

// called from Python when a folder is dropped on the window
window.droppedFolder = (path) => {
  if ($("start").hidden) return toast("Finish or leave this review first, then drop the folder on the start screen.");
  $("folder").value = path; checkFolder();
};

let folderTimer;
$("folder").addEventListener("input", () => { clearTimeout(folderTimer); folderTimer = setTimeout(checkFolder, 300); });
$("browse").addEventListener("click", async () => { const f = await call("choose_folder"); if (f) { $("folder").value = f; checkFolder(); } });
async function checkFolder() {
  const f = $("folder").value.trim(), note = $("folder-note");
  if (!f) { note.textContent = ""; $("scan").disabled = true; return; }
  const r = await call("folder_info", f);
  $("scan").disabled = !r.ok;
  note.textContent = !r.ok ? r.why : r.recycle ? "" : r.recycle_why + " Set-aside photos will stay in the holding folder; nothing is deleted.";
}

$("ai").addEventListener("change", () => aiToggle());
$("ai-retry").addEventListener("click", () => aiToggle());
$("ai-check").addEventListener("click", () => aiToggle(null, $("ai-url").value.trim()));
let aiInfo = null;
async function aiToggle(want, url) {
  $("ai-box").hidden = !$("ai").checked;
  if (!$("ai").checked) return;
  const status = $("ai-status"), sel = $("ai-model");
  status.textContent = "Looking for a local AI server..."; sel.replaceChildren(); sel.disabled = true;
  aiInfo = await call("ai_status", url || null);
  if (!aiInfo.ok) {
    status.textContent = aiInfo.why + " Start LM Studio (or Ollama) with a vision model loaded, then press Look again. Scanning works fine without it.";
    return;
  }
  const vision = aiInfo.models.filter((m) => m.vision !== false);
  status.textContent = `${aiInfo.name} found at ${aiInfo.url}.` +
    (aiInfo.models.some((m) => m.vision === true) ? " Vision models are listed first." : " Pick a model that can see images.");
  for (const m of aiInfo.models) sel.append(el("option", { value: m.id, selected: m.id === want }, m.id + (m.vision === false ? " (text only)" : "")));
  if (!want && vision.length) sel.value = vision[0].id;
  sel.disabled = false;
}

function mode() { return document.querySelector("input[name=mode]:checked").value; }
function modeChanged() {
  const dupes = mode() === "dupes";
  for (const e of document.querySelectorAll(".burst-only")) e.hidden = dupes;
  $("scan").textContent = dupes ? "Find duplicates" : "Find bursts";
}
for (const r of document.querySelectorAll("input[name=mode]")) r.addEventListener("change", modeChanged);

$("scan").addEventListener("click", async () => {
  const dupes = mode() === "dupes";
  const opts = {
    folder: $("folder").value.trim(), recursive: $("recursive").checked, mode: mode(), merge: $("merge").checked,
    level: document.querySelector("input[name=level]:checked").value,
    ai: !dupes && $("ai").checked && !!(aiInfo && aiInfo.ok), ai_url: aiInfo && aiInfo.ok ? aiInfo.url : "",
    ai_model: $("ai-model").value || "",
  };
  if (!(await call("start_scan", opts))) return toast("A scan is already running.");
  show("progress"); poll();
});
$("clear-cache").addEventListener("click", async () => { await call("clear_cache"); toast("Saved measurements cleared."); });

// ---------- progress ----------
async function poll() {
  const p = await call("progress");
  $("p-stage").textContent = p.stage || "Working";
  const pct = p.total ? Math.round(100 * p.done / p.total) : 0;
  $("p-bar").style.width = (p.total ? pct : 3) + "%";
  let eta = "";
  if (p.total && p.done > 2 && p.done < p.total) {
    const secs = (Date.now() / 1000 - p.started) / p.done * (p.total - p.done);
    eta = secs > 90 ? ` - about ${Math.round(secs / 60)} min left` : ` - about ${Math.max(5, Math.round(secs / 5) * 5)} s left`;
  }
  $("p-count").textContent = p.total ? `${p.done} of ${p.total} photos${eta}` : "";
  if (p.running) return setTimeout(poll, 400);
  if (p.error) { show("start"); toast("The scan stopped: " + p.error, 8000); return; }
  if (p.stage !== "done") { show("start"); toast(p.stage); return; }
  openReview(await call("get_report"));
}
$("cancel").addEventListener("click", () => { call("cancel"); $("p-stage").textContent = "Cancelling..."; });

// ---------- review ----------
function openReview(rep) {
  report = rep; sel = { g: 0, i: 0 };
  show("review"); renderSummary(); renderNotes(); renderGroups();
}

function renderSummary() {
  const shots = report.groups.flatMap((g) => g.shots);
  const held = shots.filter((s) => s.held), recycled = shots.filter((s) => s.recycled);
  const bytes = held.reduce((a, s) => a + s.size, 0);
  const bursts = report.groups.filter((g) => g.type === "burst").length, dupes = report.groups.length - bursts;
  $("sum-main").textContent = report.groups.length
    ? `${plural(held.length, "photo")} set aside (${mb(bytes)})` + (recycled.length ? `, ${recycled.length} already in the Recycle Bin` : "")
    : report.mode === "dupes" ? "No duplicates found" : "No bursts or duplicates found";
  $("sum-sub").textContent = `${plural(report.photos, "photo")} checked in ${report.seconds} s. ` + (report.mode === "dupes"
    ? `${plural(dupes, "set")} of copies.`
    : `${plural(bursts, "burst")}` + (dupes ? `, ${plural(dupes, "set")} of copies` : "") + ". Close calls are shown first.");
  $("accept").hidden = !report.recycle;
  $("accept").disabled = !held.length;
  $("restore-all").disabled = !held.length;
}

function renderNotes() {
  const n = $("notes"); n.replaceChildren();
  const add = (cls, ...kids) => n.append(el("div", { class: "banner " + cls }, ...kids));
  if (!report.recycle)
    add("warn", el("span", {}, report.recycle_why + " Set-aside photos stay in the holding folder: ", el("code", {}, report.holding),
      ". Delete that folder yourself when you're happy, or press Restore all."));
  const blocked = report.groups.flatMap((g) => g.shots).filter((s) => s.blocked);
  if (blocked.length) add("warn", el("span", {}, `${plural(blocked.length, "photo")} were left in place because: ${blocked[0].blocked}`));
  if (report.errors.length)
    add("", el("details", {}, el("summary", {}, `${plural(report.errors.length, "file")} could not be read and were left alone.`),
      el("ul", {}, report.errors.map((e) => el("li", {}, rel(e.path) + " - " + e.error)))));
  if (report.skipped_cloud.length) add("", el("span", {}, `${plural(report.skipped_cloud.length, "cloud-only file")} skipped (not downloaded to this PC).`));
}

function shotsOf(g) { return [...g.shots.filter((s) => s.keep), ...g.shots.filter((s) => !s.keep).sort((a, b) => a.moment - b.moment)]; }

function renderGroups() {
  const y = window.scrollY;  // re-rendering empties the page for a moment; keep the user's place
  const box = $("groups"); box.replaceChildren();
  report.groups.forEach((g, gi) => {
    const keepers = g.shots.filter((s) => s.keep), aside = g.shots.filter((s) => !s.keep).sort((a, b) => a.moment - b.moment);
    const title = g.type === "duplicate" ? "Copies" : `Burst of ${g.shots.length}`;
    const when = g.shots[0].time ? new Date(g.shots[0].time * 1000).toLocaleString() : "";
    const card = el("article", { class: "group" + (gi === sel.g ? " focus" : ""), "data-g": gi },
      el("div", { class: "g-head" },
        el("div", {}, el("h3", { style: "display:inline;margin-right:8px" }, title),
          el("span", { class: "tag " + g.confidence, title: g.confidence === "close"
            ? "The photos were very similar. Worth a quick look." : "The pick was clearly better." },
            g.confidence === "close" ? "Close call" : "Confident"),
          ...g.flags.map((f) => el("span", { class: "tag close", style: "margin-left:6px" }, f))),
        el("div", { class: "row" }, el("span", { class: "hint" }, when),
          ...focusControls(g),
          aside.some((s) => !s.recycled) ? el("button", { class: "btn small", title: "Keep every photo in this set",
            onclick: () => keepWhole(g) }, g.type === "duplicate" ? "Keep all copies" : "Keep whole burst") : null)),
      el("div", { class: "g-body" },
        el("div", { class: "keepers" }, el("h4", {}, "Keeping"), keepers.map((s) => shotEl(gi, s))),
        aside.length ? el("div", { class: "aside" }, el("h4", {}, "Set aside"), aside.map((s) => shotEl(gi, s))) : null));
    box.append(card);
  });
  window.scrollTo(0, y);
  markSel(false);
}

// ---------- focus on one person ----------
let focusG = null;  // id of the burst waiting for a person to be clicked
function focusControls(g) {
  if (g.type !== "burst" || !g.shots.some((s) => s.people && s.people.length)) return [];
  if (focusG === g.id) return [el("span", { class: "hint" }, "Click the person you care about in any photo"),
    el("button", { class: "btn small", onclick: () => { focusG = null; renderGroups(); } }, "Cancel")];
  const out = [];
  if (g.focus) out.push(el("span", { class: "tag who", "data-tip": g.focus === "chosen"
    ? "Every photo is judged on the person you clicked (outlined)."
    : "Several people are in these photos. Each photo is judged on the person who appears in most of them (outlined)." },
    g.focus === "chosen" ? "Your person" : "Following one person"));
  if (g.focus === "chosen") out.push(el("button", { class: "btn small", title: "Let the app choose who matters again",
    onclick: () => refocus(g, null, null) }, "Automatic"));
  out.push(el("button", { class: "btn small", title: "Pick who this burst is about; the best photo of that person is kept",
    onclick: () => { focusG = g.id; renderGroups(); } }, "Focus on a person"));
  return out;
}

async function refocus(g, sid, idx) {
  focusG = null;
  const r = await call("focus", g.id, sid, idx);
  if (!r.ok) return toast(r.why, 6000);
  toast(r.skipped ? `Picked again; ${plural(r.skipped, "photo")} could not be moved.` : "Picked again around that person.");
  report = await call("get_report");
  renderSummary(); renderGroups();
}

// outlines over the thumbnail; the image is letterboxed into a 3:4 box, so map image pixels to that box
function peopleLayer(g, s) {
  const choosing = focusG === g.id;
  if (!s.people || !(choosing || (g.focus && s.subject != null))) return null;
  const a = s.img_w / s.img_h, c = 3 / 4;
  const w = a > c ? 100 : 100 * a / c, h = a > c ? 100 * c / a : 100;
  const layer = el("div", { class: "people" + (choosing ? " choosing" : "") });
  const area = (b) => (b[2] - b[0]) * (b[3] - b[1]);
  const order = s.people.map((_, i) => i).sort((i, j) => area(s.people[j]) - area(s.people[i]));  // small ones on top
  order.forEach((i) => {
    const b = s.people[i];
    if (!choosing && i !== s.subject) return;
    layer.append(el("div", { class: "person" + (i === s.subject ? " subj" : ""),
      title: choosing ? "Focus on this person" : "The person this photo was judged on",
      style: `left:${(100 - w) / 2 + w * b[0] / s.img_w}%;top:${(100 - h) / 2 + h * b[1] / s.img_h}%;` +
        `width:${w * (b[2] - b[0]) / s.img_w}%;height:${h * (b[3] - b[1]) / s.img_h}%`,
      onclick: choosing ? (e) => { e.stopPropagation(); refocus(g, s.id, i); } : null }));
  });
  return layer;
}

function shotEl(gi, s) {
  const img = el("img", { alt: rel(s.id), "data-id": s.id, loading: "lazy" });
  lazy(img);
  const pic = el("div", { class: "pic" }, img, peopleLayer(report.groups[gi], s));
  const reasons = s.reasons.map((r) => el("li", { class: r.startsWith("AI") ? "ai" : "" }, r));
  const g = report.groups[gi];
  if (report.merge && g.carry && s.id === g.top && s.keep && !s.recycled && g.shots.some((x) => x.held))
    reasons.push(...g.carry.map((c) => el("li", { title: "Done when you send the copies to the Recycle Bin" }, c)));
  if (s.user) reasons.unshift(el("li", { class: "user" }, s.keep ? "You kept this" : "You set this aside"));
  const m = s.metrics, facts = [];
  if (s.dims) facts.push(`${s.dims[0]}x${s.dims[1]}`);
  facts.push(mb(s.size).replace(/^0 MB$/, "<1 MB"));
  if (m && m.faces) facts.push(m.eyes_open ? "eyes open" : "eyes closed", `smile ${Math.round(m.smile * 100)}%`);
  if (m) facts.push(`sharpness ${m.sharp}`, `${m.clip_hi}% blown out`);
  const btns = s.recycled ? el("div", { class: "hint" }, "In the Recycle Bin")
    : el("div", { class: "btns" }, s.keep
      ? el("button", { class: "btn small", title: "Move this photo to the holding folder (X)", onclick: (e) => { e.stopPropagation(); setKeep(s, false); } }, "Set aside")
      : el("button", { class: "btn small", title: "Move this photo back and keep it (K)", onclick: (e) => { e.stopPropagation(); setKeep(s, true); } }, "Keep"));
  return el("div", { class: "shot" + (s.keep ? " keep" : "") + (s.recycled ? " gone" : ""), tabindex: 0, "data-id": s.id,
    title: `${rel(s.id)}\n${facts.join(", ")}\nClick to compare (Space)`,
    onclick: () => { selectShot(gi, s); openCompare(s); } },
    pic, el("div", { class: "name" }, rel(s.id).split(/[\\/]/).pop()), el("ul", { class: "chips" }, reasons), btns);
}

const thumbObs = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { thumbObs.unobserve(e.target); loadThumb(e.target); }
}, { rootMargin: "400px" });
function lazy(img) { thumbObs.observe(img); }
async function loadThumb(img) {
  const url = await call("image", img.dataset.id, 360);
  if (url) img.src = url; else img.alt = "Not available";
}

async function setKeep(s, keep, force = false) {
  const r = await call("set_keep", s.id, keep, force);
  if (!r.ok && r.confirm) { if (confirm(r.confirm)) return setKeep(s, keep, true); return; }
  if (!r.ok) return toast(r.why, 6000);
  toast(keep ? "Moved back. Keeping it." : "Moved to the holding folder.");
  report = await call("get_report");
  renderSummary(); renderGroups();
}

async function keepWhole(g) {
  for (const s of g.shots) if (!s.keep && !s.recycled) {
    const r = await call("set_keep", s.id, true, false);
    if (!r.ok) { toast(r.why, 6000); break; }
  }
  report = await call("get_report"); renderSummary(); renderGroups();
  toast("Kept every photo in that set.");
}

// ---------- selection & keys ----------
function selectShot(gi, s) { sel = { g: gi, i: shotsOf(report.groups[gi]).indexOf(s) }; markSel(false); }
function curShot() { const g = report.groups[sel.g]; return g && shotsOf(g)[sel.i]; }
function markSel(scroll) {
  document.querySelectorAll(".shot.sel").forEach((e) => e.classList.remove("sel"));
  document.querySelectorAll(".group").forEach((e) => e.classList.toggle("focus", +e.dataset.g === sel.g));
  const s = curShot(); if (!s) return;
  const node = document.querySelector(`.group[data-g="${sel.g}"] .shot[data-id="${CSS.escape(s.id)}"]`);
  if (node) { node.classList.add("sel"); if (scroll) node.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
}

document.addEventListener("keydown", (e) => {
  if (e.target.matches?.("input, select, textarea")) return;
  if (e.key === "?") { $("help").hidden = false; return; }
  if (e.key === "Escape") {
    $("help").hidden = true; $("compare").hidden = true; cmpShot = null;
    if (focusG !== null) { focusG = null; renderGroups(); }
    return;
  }
  if ($("review").hidden || !report || !report.groups.length) return;
  const n = shotsOf(report.groups[sel.g]).length;
  const k = e.key.toLowerCase();
  if (e.key === "ArrowRight") sel.i = Math.min(sel.i + 1, n - 1);
  else if (e.key === "ArrowLeft") sel.i = Math.max(sel.i - 1, 0);
  else if (e.key === "ArrowDown") sel = { g: Math.min(sel.g + 1, report.groups.length - 1), i: 0 };
  else if (e.key === "ArrowUp") sel = { g: Math.max(sel.g - 1, 0), i: 0 };
  else if (k === "k" || k === "x") {
    const s = cmpShot || curShot();
    if (s && !s.recycled && s.keep !== (k === "k")) { setKeep(s, k === "k"); if (cmpShot) closeCompare(); }
    return;
  } else if (e.key === " ") { if ($("compare").hidden) openCompare(curShot()); else closeCompare(); }
  else return;
  e.preventDefault();
  if (cmpShot && e.key.startsWith("Arrow")) openCompare(curShot());
  markSel(true);
});
$("help").addEventListener("click", (e) => { if (e.target === $("help") || e.target.dataset.close !== undefined) $("help").hidden = true; });

// ---------- compare ----------
function pickFor(s) {
  const g = report.groups.find((g) => g.shots.includes(s));
  return g.shots.find((x) => x.keep && x.moment === s.moment && x.role === "pick" && x !== s)
    || g.shots.find((x) => x.id === g.top && x !== s)
    || g.shots.find((x) => x.keep && x !== s) || null;
}
async function openCompare(s) {
  if (!s) return;
  cmpShot = s;
  const p = s.keep ? null : pickFor(s);
  const a = p || s, b = p ? s : null;
  $("compare").hidden = false;
  $("c-bh").parentElement.hidden = !b;
  $("c-a").previousElementSibling.textContent = p ? "Pick: " + rel(a.id) : rel(a.id);
  fill("a", a); if (b) { $("c-bh").textContent = "Set aside: " + rel(b.id); fill("b", b); }
  $("c-keep").hidden = s.keep || s.recycled; $("c-aside").hidden = !s.keep || s.recycled;
}
async function fill(side, s) {
  const img = $("c-" + side), face = $("c-" + side + "f");
  img.removeAttribute("src"); face.removeAttribute("src"); face.hidden = true;
  $("c-" + side + "r").replaceChildren(...s.reasons.map((r) => el("li", {}, r)));
  $("c-" + side + "r").className = "chips" + (s.keep ? " good" : "");
  const [u, f] = await Promise.all([call("image", s.id, 1100), call("face", s.id)]);
  if (cmpShot === null) return;
  if (u) img.src = u;
  if (f) { face.src = f; face.hidden = false; face.title = "Face close-up, where sharpness and eyes were measured"; }
}
function closeCompare() { $("compare").hidden = true; cmpShot = null; }
$("c-close").addEventListener("click", closeCompare);
$("compare").addEventListener("click", (e) => { if (e.target === $("compare")) closeCompare(); });
$("c-keep").addEventListener("click", () => { const s = cmpShot; closeCompare(); setKeep(s, true); });
$("c-aside").addEventListener("click", () => { const s = cmpShot; closeCompare(); setKeep(s, false); });

// ---------- finish ----------
$("accept").addEventListener("click", async () => {
  const held = report.groups.flatMap((g) => g.shots).filter((s) => s.held);
  if (!confirm(`Send ${plural(held.length, "set-aside photo")} (with their Live Photo and sidecar files) to the Recycle Bin?` +
    (report.merge ? "\n\nPhoto details from removed copies are saved next to the copy you keep first." : "") +
    "\n\nYou can restore them from the Recycle Bin later.")) return;
  $("accept").disabled = true;
  const r = await call("accept_all");
  if (r.error) toast(r.error, 8000);
  $("done-title").textContent = r.recycled ? "Sent to the Recycle Bin" : "Nothing was recycled";
  $("done-text").textContent = `${plural(r.recycled, "file")} moved to the Recycle Bin.` +
    (r.left.length ? ` ${plural(r.left.length, "file")} stayed in the holding folder.` : "") +
    (r.kept_back ? ` ${r.kept_back} ${r.kept_back === 1 ? "copy was" : "copies were"} put back because their photo details could not be saved.` : "") +
    " Changed your mind? Open the Recycle Bin and choose Restore.";
  show("done");
});
$("restore-all").addEventListener("click", async () => {
  if (!confirm("Put every set-aside photo back where it was?")) return;
  const r = await call("restore_all");
  $("done-title").textContent = "Everything is back where it was";
  $("done-text").textContent = `${plural(r.restored, "photo")} put back.` + (r.skipped.length
    ? ` ${r.skipped.length} could not be moved (a file with the same name is already there); they are still in the holding folder.` : "");
  show("done");
});
$("open-hold").addEventListener("click", () => call("open_holding"));
$("again").addEventListener("click", async () => { report = null; show("start"); showOpenRuns((await call("init")).open_runs); });
