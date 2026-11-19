const $ = (s) => document.querySelector(s);
const API = "/_admin/api";
let picked = [];
let watching = null;   // id do pedido que estamos a acompanhar
let lastDone = null;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function when(ts) {
  return new Date(ts * 1000).toLocaleString("pt-PT", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function call(path, opts = {}) {
  const r = await fetch(API + path, { credentials: "same-origin", ...opts });
  if (r.status === 401) { location.href = "/_admin/login"; throw new Error("sessão"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || "Ocorreu um erro.");
  return data;
}
function postJSON(path, body) {
  return call(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}

function confirmBox(text, yes = "Confirmar") {
  const d = $("#confirm");
  $("#confirmtext").textContent = text;
  $("#confirmyes").textContent = yes;
  d.showModal();
  return new Promise((res) => d.addEventListener("close", () => res(d.returnValue === "yes"), { once: true }));
}

/* ---- ficheiros ---- */
function renderFiles() {
  $("#filelist").innerHTML = picked.map((f, i) => {
    const thumb = f.type.startsWith("image/") ? `<img src="${URL.createObjectURL(f)}" alt="">` : "";
    return `<li>${thumb}<span>${esc(f.name)}</span><button type="button" data-i="${i}" aria-label="Retirar ${esc(f.name)}">×</button></li>`;
  }).join("");
}
function addFiles(list) {
  for (const f of list) if (picked.length < 10) picked.push(f);
  renderFiles();
}
$("#files").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
$("#filelist").addEventListener("click", (e) => {
  if (e.target.dataset.i) { picked.splice(+e.target.dataset.i, 1); renderFiles(); }
});
const drop = $("#drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => { $("#prompt").value = c.textContent; $("#prompt").focus(); }));

/* ---- enviar pedido ---- */
$("#ask").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#formerr");
  err.hidden = true;
  const fd = new FormData();
  fd.append("prompt", $("#prompt").value);
  picked.forEach((f) => fd.append("files", f));
  $("#send").disabled = true;
  $("#send").textContent = "A enviar…";
  try {
    const r = await call("/job", { method: "POST", body: fd });
    watching = r.id;
    $("#prompt").value = "";
    picked = [];
    renderFiles();
    poll();
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
  } finally {
    $("#send").textContent = "Enviar pedido";
  }
});
$("#cancel").addEventListener("click", () => postJSON("/cancel"));

/* ---- estado ---- */
function renderJob(job) {
  const card = $("#jobcard");
  if (!job) { card.hidden = true; return; }
  card.hidden = false;
  const running = !["pronto", "erro", "sem alterações"].includes(job.status);
  const titles = { pronto: "Pronto — veja à direita", erro: "Não foi possível", "sem alterações": "Sem alterações", "a testar": "A testar…" };
  $("#jobtitle").textContent = titles[job.status] || "A trabalhar…";
  $("#jobprompt").textContent = job.prompt;
  $("#cancel").hidden = !running;
  const log = job.log || [];
  $("#log").innerHTML = log.map((l, i) =>
    `<li class="${l.k}${running && i === log.length - 1 ? " live" : ""}">${esc(l.t)}</li>`).join("")
    || (running ? `<li class="live">A preparar…</li>` : "");
  $("#log").scrollTop = 1e6;
  $("#jobsummary").hidden = !job.summary || running;
  $("#jobsummary").textContent = job.summary || "";
  $("#joberror").hidden = !job.error;
  $("#joberror").textContent = job.error || "";
}

function renderDraft(draft) {
  $("#draftcount").textContent = draft.length;
  $("#draftempty").hidden = draft.length > 0;
  $("#draftactions").hidden = draft.length === 0;
  $("#draft").innerHTML = draft.map((c) => `<li>
      <span class="p">${esc(c.prompt)}</span>
      <span class="meta">${esc(c.user)} · ${when(c.ts)}${c.files.length ? " · mudou " + esc(c.files.join(", ")) : ""}</span>
    </li>`).join("");
}

function renderPublished(list) {
  $("#published").innerHTML = list.map((p, i) => `<li>
      <span class="p">${esc(p.title || "Publicação")}</span>
      <span class="meta">${p.user ? esc(p.user) + " · " : ""}${when(p.ts)}${i === 0 ? " · <strong>versão atual</strong>" : ""}</span>
      ${i === 0 ? "" : `<span><button class="link" data-restore="${p.id}">Repor esta versão</button></span>`}
    </li>`).join("");
}

function reloadFrame() {
  const f = $("#frame");
  try { f.contentWindow.location.reload(); } catch { f.src = "/"; }
}

let timer = null;
async function poll() {
  clearTimeout(timer);
  let st;
  try {
    st = await call("/state" + (watching ? "?job=" + watching : ""));
  } catch { timer = setTimeout(poll, 4000); return; }
  const job = st.running || st.job;
  if (st.running && !watching) watching = st.running.id;
  renderJob(job);
  renderDraft(st.draft);
  renderPublished(st.published);
  const busy = !!st.running;
  $("#send").disabled = busy;
  $("#send").title = busy ? `Pedido em curso (${st.running.user})` : "";
  ["#publish", "#undo", "#discard"].forEach((s) => ($(s).disabled = busy));
  if (job && !st.running && job.id !== lastDone) {
    lastDone = job.id;
    if (job.status === "pronto") reloadFrame();
  }
  timer = setTimeout(poll, busy ? 1500 : 6000);
}

async function action(path, body, question, yes, okMsg) {
  if (question && !(await confirmBox(question, yes))) return;
  try {
    await postJSON(path, body);
    watching = null;
    $("#jobcard").hidden = true;
    reloadFrame();
    if (okMsg) alert(okMsg);
  } catch (ex) { alert(ex.message); }
  poll();
}
$("#publish").addEventListener("click", () => action("/publish", null,
  "Publicar todas as alterações por publicar na loja real? Os clientes passam a vê-las de imediato.", "Publicar",
  "Publicado! A loja já mostra as alterações."));
$("#undo").addEventListener("click", () => action("/undo", null, "Desfazer a última alteração por publicar?", "Desfazer"));
$("#discard").addEventListener("click", () => action("/discard", null,
  "Descartar TODAS as alterações por publicar? A pré-visualização volta a ficar igual à loja.", "Descartar"));
$("#published").addEventListener("click", (e) => {
  const id = e.target.dataset.restore;
  if (id) action("/restore", { id }, "Repor esta versão na pré-visualização? Depois confirme e carregue em Publicar.", "Repor");
});

/* ---- visualizador ---- */
document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".seg button").forEach((x) => x.classList.toggle("on", x === b));
  $("#frame").style.width = b.dataset.w ? b.dataset.w + "px" : "100%";
}));
$("#reload").addEventListener("click", reloadFrame);

poll();
