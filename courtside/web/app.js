"use strict";

let STATE = null;
let selectedId = null;
let activeTab = "profile";

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

// ---------------------------------------------------------------- transport

async function api(path, options) {
  const res = await fetch(path, options);
  let body = null;
  try { body = await res.json(); } catch (e) { /* non-JSON (e.g. an image) */ }
  if (!res.ok) throw new Error((body && body.error) || res.statusText);
  return body;
}

function post(path, payload) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
}

let toastTimer = null;
function toast(message, isError) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("err", !!isError);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), isError ? 6000 : 3200);
}

// ---------------------------------------------------------------- helpers

const playerById = (id) => STATE.players.find((p) => p.id === Number(id));
const teamByIndex = (i) => STATE.teams.find((t) => t.index === Number(i));

function applyState(next) {
  STATE = next;
  $("#romline").textContent =
    `${next.rom.title} [${next.rom.cart}] · CIC ${next.rom.cic} · ` +
    `${next.teams.length} teams · ${next.meta.player_used} players`;
  $("#dirty").classList.toggle("hidden", !next.dirty);
  renderTeamFilter();
  renderList();
  if (selectedId !== null) renderDetail();
}

function photoUrl(id) {
  return `/api/photo/${id}.png?v=${STATE && STATE.dirty ? Date.now() : 0}`;
}

// ---------------------------------------------------------------- sidebar

function renderTeamFilter() {
  const sel = $("#team-filter");
  if (sel.dataset.built) return;
  sel.dataset.built = "1";
  const opts = ['<option value="">All teams</option>'];
  STATE.teams.forEach((t) => {
    opts.push(`<option value="${t.index}">${t.display} (${t.count})</option>`);
  });
  sel.innerHTML = opts.join("");
}

function visiblePlayers() {
  const needle = $("#search").value.trim().toLowerCase();
  const team = $("#team-filter").value;
  const sort = $("#sort").value;
  let list = STATE.players.slice(0, STATE.players.length);
  if (team !== "") list = list.filter((p) => String(p.team) === team);
  if (needle) {
    list = list.filter((p) =>
      (`${p.first_name} ${p.last_name}`).toLowerCase().includes(needle) ||
      String(p.jersey) === needle || String(p.id) === needle);
  }
  const cmp = {
    roster: (a, b) => (a.team - b.team)
      || ((a.starter || 9) - (b.starter || 9))
      || a.last_name.localeCompare(b.last_name),
    name: (a, b) => a.last_name.localeCompare(b.last_name)
      || a.first_name.localeCompare(b.first_name),
    overall: (a, b) => b.overall - a.overall,
    jersey: (a, b) => a.jersey - b.jersey,
  }[sort];
  return list.sort(cmp);
}

function renderList() {
  const list = visiblePlayers();
  const ul = $("#players");
  ul.innerHTML = list.map((p) => `
    <li data-id="${p.id}" class="${p.id === selectedId ? "sel" : ""}">
      <span class="num">${p.starter ? `<span class="star">${p.starter}</span>` : p.jersey_text}</span>
      <span class="nm">${escapeHtml(p.first_name)} ${escapeHtml(p.last_name)}</span>
      <span class="meta">${p.position} ${p.overall}</span>
    </li>`).join("");
  $("#listcount").textContent =
    `${list.length} of ${STATE.players.length} players`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------- detail

function optionList(items, current) {
  return items.map((it) =>
    `<option value="${it.value}"${String(it.value) === String(current) ? " selected" : ""}>` +
    `${escapeHtml(it.label)}</option>`).join("");
}

function renderDetail() {
  const p = playerById(selectedId);
  const host = $("#detail");
  if (!p) { host.innerHTML = '<p class="empty">Pick a player from the list.</p>'; return; }

  const frag = $("#tpl-detail").content.cloneNode(true);
  const root = document.createElement("div");
  root.appendChild(frag);

  // header
  $(".portrait", root).src = photoUrl(p.id);
  $(".f-first", root).value = p.first_name;
  $(".f-last", root).value = p.last_name;
  $(".subtitle", root).textContent =
    `#${p.jersey_text} · ${p.position} · ${p.height_text} · ${p.weight_lbs} lb · ` +
    `${p.years_pro_text} · ${p.team_name}` +
    `${p.starter ? ` · starter ${p.starter}` : ""} · id ${p.id}`;
  $(".ovr-value", root).textContent = p.overall;

  // profile
  const teamOpts = STATE.teams.map((t) => ({ value: t.index, label: `${t.display} (${t.count})` }));
  $(".f-team", root).innerHTML = optionList(teamOpts, p.team);
  $(".f-starter", root).value = String(p.starter);
  $(".f-jersey", root).value = p.jersey;
  $(".f-position", root).innerHTML = optionList(
    STATE.meta.positions.map((x) => ({ value: x.code, label: `${x.code} — ${x.label}` })), p.position);
  const feet = Math.floor(p.height_inches / 12);
  const inch = p.height_inches % 12;
  $(".f-feet", root).innerHTML = optionList(
    [4, 5, 6, 7, 8].map((f) => ({ value: f, label: `${f} ft` })), feet);
  $(".f-inches", root).innerHTML = optionList(
    [...Array(12).keys()].map((i) => ({ value: i, label: `${i} in` })), inch);
  $(".f-weight", root).value = p.weight_lbs;
  $(".f-years", root).value = p.years_pro;
  $(".f-range", root).innerHTML = optionList(
    STATE.meta.range_feet.map((ft, i) => ({ value: i, label: `${ft} feet` })), p.range);

  // ratings
  $(".ratings", root).innerHTML = STATE.meta.attributes.map((a) => `
    <div class="rating">
      <span class="lbl">${escapeHtml(a.label)}</span>
      <input type="range" min="${a.min}" max="${a.max}" value="${p.attributes[a.key]}"
             data-attr="${a.key}">
      <output>${p.attributes[a.key]}</output>
    </div>`).join("");

  // appearance
  $(".portrait-current", root).src = photoUrl(p.id);
  $(".who-current", root).textContent = `${p.first_name} ${p.last_name} (current)`;
  const others = STATE.players
    .filter((o) => o.id !== p.id)
    .sort((a, b) => a.last_name.localeCompare(b.last_name))
    .map((o) => ({ value: o.id, label: `${o.last_name}, ${o.first_name} — ${o.team_name}` }));
  $(".f-source", root).innerHTML = optionList(others, others.length ? others[0].value : "");
  $(".portrait-source", root).src = photoUrl($(".f-source", root).value);
  $(".model-grid", root).innerHTML = ["model_b", "flags_a", "flags_b", "misc_14", "misc_15"]
    .map((key) => miscField(key, p)).join("");

  // moves
  $(".f-trade", root).innerHTML = optionList(others, others.length ? others[0].value : "");
  $(".f-signto", root).innerHTML = optionList(teamOpts, p.team);
  $(".problems", root).innerHTML =
    STATE.problems.map((w) => `<div>${escapeHtml(w)}</div>`).join("");

  // advanced
  $(".misc-grid", root).innerHTML = STATE.meta.misc
    .filter((m) => !["model_b", "flags_a", "flags_b", "misc_14", "misc_15"].includes(m.key))
    .map((m) => miscField(m.key, p)).join("");

  host.innerHTML = "";
  host.appendChild(root);
  showTab(activeTab);
  wireDetail(host, p);
}

function miscField(key, p) {
  const meta = STATE.meta.misc.find((m) => m.key === key);
  if (!meta) return "";
  return `<label>${escapeHtml(meta.label)}
    <input type="number" min="${meta.min}" max="${meta.max}" value="${p.misc[key]}"
           data-misc="${key}"></label>`;
}

function showTab(name) {
  activeTab = name;
  $$("#detail .tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$("#detail .tab").forEach((t) => t.classList.toggle("hidden", t.dataset.tab !== name));
}

async function patch(fields) {
  try {
    const res = await post(`/api/player/${selectedId}`, fields);
    applyState(res.state);
  } catch (e) { toast(e.message, true); }
}

async function doAction(payload) {
  try {
    const res = await post("/api/action", payload);
    applyState(res.state);
    toast(res.message);
  } catch (e) { toast(e.message, true); }
}

function wireDetail(host, p) {
  $$("#detail .tabs button").forEach((b) =>
    b.addEventListener("click", () => showTab(b.dataset.tab)));

  const bind = (sel, field, cast) => {
    const el = $(sel, host);
    if (!el) return;
    el.addEventListener("change", () => patch({ [field]: cast ? cast(el.value) : el.value }));
  };
  bind(".f-first", "first_name");
  bind(".f-last", "last_name");
  bind(".f-jersey", "jersey", Number);
  bind(".f-position", "position");
  bind(".f-weight", "weight_lbs", Number);
  bind(".f-years", "years_pro", Number);
  bind(".f-range", "range", Number);
  bind(".f-team", "team", Number);
  bind(".f-starter", "starter", Number);

  const feet = $(".f-feet", host), inches = $(".f-inches", host);
  const pushHeight = () =>
    patch({ height_feet: Number(feet.value), height_inches_part: Number(inches.value) });
  feet.addEventListener("change", pushHeight);
  inches.addEventListener("change", pushHeight);

  $$(".rating input", host).forEach((slider) => {
    const out = slider.nextElementSibling;
    slider.addEventListener("input", () => { out.textContent = slider.value; });
    slider.addEventListener("change", () =>
      patch({ attributes: { [slider.dataset.attr]: Number(slider.value) } }));
  });

  $$("[data-bulk]", host).forEach((btn) => btn.addEventListener("click", () => {
    const mode = btn.dataset.bulk;
    const attrs = {};
    STATE.meta.attributes.forEach((a) => {
      const cur = p.attributes[a.key];
      attrs[a.key] = mode === "max" ? a.max
        : Math.max(a.min, Math.min(a.max, cur + Number(mode)));
    });
    patch({ attributes: attrs });
  }));

  $$("[data-misc]", host).forEach((input) => input.addEventListener("change", () =>
    patch({ misc: { [input.dataset.misc]: Number(input.value) } })));

  const source = $(".f-source", host);
  source.addEventListener("change", () => {
    $(".portrait-source", host).src = photoUrl(source.value);
  });
  $$("[data-app]", host).forEach((btn) => btn.addEventListener("click", () => {
    const payload = {
      action: btn.dataset.app === "swap" ? "swap_appearance" : "appearance",
      target: String(p.id),
      source: String(source.value),
      photo: $(".c-photo", host).checked,
      face: $(".c-face", host).checked,
      model: $(".c-model", host).checked,
    };
    toast("Repacking TEAMDATA.IFF, this takes a few seconds…");
    doAction(payload);
  }));

  const picker = $("#photo-input");
  picker.onchange = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    toast("Converting the picture\u2026");
    try {
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("could not read that file"));
        reader.readAsDataURL(file);
      });
      const res = await post(`/api/photo/${p.id}`, {
        data, name: file.name,
        fit: $(".f-fit", host).value,
        dither: $(".c-dither", host).checked,
      });
      applyState(res.state);
      toast(res.message);
    } catch (err) { toast(err.message, true); }
  };

  $$("[data-photo]", host).forEach((btn) => btn.addEventListener("click", () => {
    if (btn.dataset.photo === "save") {
      const a = document.createElement("a");
      a.href = `/api/photo/${p.id}.png?scale=6&v=${Date.now()}`;
      a.download = `${p.first_name}-${p.last_name}.png`.replace(/\s+/g, "-").toLowerCase();
      a.click();
      return;
    }
    picker.click();
  }));

  $$("[data-move]", host).forEach((btn) => btn.addEventListener("click", () => {
    const kind = btn.dataset.move;
    if (kind === "trade") {
      doAction({ action: "trade", a: String(p.id), b: String($(".f-trade", host).value) });
    } else if (kind === "sign") {
      doAction({ action: "move", player: String(p.id), team: String($(".f-signto", host).value) });
    } else {
      doAction({ action: "release", player: String(p.id) });
    }
  }));
}

// ---------------------------------------------------------------- modal

function ask(title, text, value) {
  return new Promise((resolve) => {
    const modal = $("#modal");
    $("#modal-title").textContent = title;
    $("#modal-text").textContent = text;
    const input = $("#modal-input");
    input.value = value || "";
    modal.classList.remove("hidden");
    input.focus();
    input.select();
    const done = (result) => {
      modal.classList.add("hidden");
      $("#modal-ok").onclick = null;
      $("#modal-cancel").onclick = null;
      resolve(result);
    };
    $("#modal-ok").onclick = () => done(input.value);
    $("#modal-cancel").onclick = () => done(null);
    input.onkeydown = (e) => { if (e.key === "Enter") done(input.value); };
  });
}

// ---------------------------------------------------------------- boot

async function boot() {
  $("#search").addEventListener("input", renderList);
  $("#team-filter").addEventListener("change", renderList);
  $("#sort").addEventListener("change", renderList);
  $("#players").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-id]");
    if (!li) return;
    selectedId = Number(li.dataset.id);
    renderList();
    renderDetail();
  });

  $("#btn-save").addEventListener("click", async () => {
    const suggested = (STATE.rom.source || "courtside.z64").replace(/\.(z64|n64|v64)$/i, "") +
      "-edited.z64";
    const path = await ask("Save ROM", "Where should the edited ROM be written?", suggested);
    if (!path) return;
    toast("Writing ROM…");
    try {
      const res = await post("/api/save", { path });
      let msg = `Saved ${res.path} · CRC ${res.crc[0]} ${res.crc[1]}`;
      if (res.warnings.length) msg += ` · ${res.warnings.length} warning(s)`;
      toast(msg);
      applyState((await api("/api/state")));
    } catch (e) { toast(e.message, true); }
  });

  $("#btn-export").addEventListener("click", async () => {
    const data = await api("/api/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "courtside-roster.json";
    a.click();
    URL.revokeObjectURL(a.href);
  });

  $("#btn-import").addEventListener("click", () => $("#file-input").click());
  $("#file-input").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const parsed = JSON.parse(await file.text());
      const res = await post("/api/import", { roster: parsed });
      applyState(res.state);
      toast(`Imported roster${res.notes.length ? ` (${res.notes.length} note(s))` : ""}`);
    } catch (err) { toast(err.message, true); }
    e.target.value = "";
  });

  applyState(await api("/api/state"));
  if (STATE.players.length) {
    selectedId = STATE.players[0].id;
    renderList();
    renderDetail();
  }
}

boot().catch((e) => toast(e.message, true));
