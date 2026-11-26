const state = {
  page: 1,
  perPage: 50,
  contacts: [],
  selectedContactIds: new Set(),
  driveLoaded: false,
  driveStack: [{ id: "", name: "Mon Drive" }],
  driveItems: [],
  driveSelected: new Map(),
  localLoaded: false,
  localPath: "",
  localParent: "",
  localEntries: [],
  localSelected: new Set(),
  uploadFiles: [],
  templates: [],
  charts: {},
};

const $ = (id) => document.getElementById(id);

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function safeUrl(raw) {
  const s = String(raw ?? "").trim();
  if (!s) return "#";
  try {
    const u = new URL(s, window.location.origin);
    if (u.protocol === "http:" || u.protocol === "https:") return u.href;
  } catch {}
  return "#";
}

function sanitizeHtml(dirty) {
  const doc = new DOMParser().parseFromString(String(dirty ?? ""), "text/html");
  const blockedTags = new Set(["script", "iframe", "object", "embed", "svg", "math", "form", "style", "link", "meta", "base"]);
  const walk = (node) => {
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType === Node.ELEMENT_NODE) {
        const tag = child.tagName.toLowerCase();
        if (blockedTags.has(tag)) {
          child.remove();
          continue;
        }
        for (const attr of Array.from(child.attributes)) {
          const name = attr.name.toLowerCase();
          const val = (attr.value || "").trim().toLowerCase();
          if (name.startsWith("on") || name === "srcdoc") {
            child.removeAttribute(attr.name);
          } else if ((name === "href" || name === "src" || name === "action") && /^(javascript|vbscript|data):/i.test(val)) {
            child.removeAttribute(attr.name);
          }
        }
        walk(child);
      }
    }
  };
  walk(doc.body);
  return doc.body.innerHTML;
}

function getCsrfToken() {
  return document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";
}

function fmtSize(bytes) {
  const n = Number(bytes);
  if (!n || Number.isNaN(n)) return "—";
  if (n < 1024) return `${n} o`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} Ko`;
  return `${(n / (1024 * 1024)).toFixed(1)} Mo`;
}

function toast(msg, type = "") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  $("toastContainer").appendChild(el);
  setTimeout(() => el.remove(), 4400);
}

async function api(url, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const headers = new Headers(opts.headers || {});
  if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
    const tok = getCsrfToken();
    if (tok && !headers.has("X-CSRF-Token")) {
      headers.set("X-CSRF-Token", tok);
    }
  }
  const res = await fetch(url, { ...opts, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Erreur HTTP ${res.status}`);
  return data;
}

const STATUS_LABELS = {
  new: ["Nouveau", "badge-st-new"],
  contacted: ["Contacté", "badge-st-contacted"],
  client: ["Client", "badge-st-client"],
  followup: ["À relancer", "badge-st-followup"],
  unsubscribed: ["Ne plus contacter", "badge-st-unsubscribed"],
};

function switchTab(tab) {
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  if (tab === "crm") { loadKanban(); loadTasks(); loadSegments(); }
  if (tab === "conversion") { loadWaTemplates(); loadHotSignals(); loadTrackedLinks(); loadLeadForms(); initWaInbox(); }
  else { stopWaPolling(); }
  if (tab === "training-forms") loadTrainingForms();
  if (tab === "payments") loadPayments();
  if (tab === "booking") loadBookingHub();
  if (tab === "dashboard") loadDashboard();
  if (tab === "quality") loadDuplicates();
  if (tab === "gdrive" && !state.driveLoaded) loadDriveFiles();
  if (tab === "local" && !state.localLoaded) initLocalBrowser();
  if (tab === "connectors") loadWebhookInfo();
  if (tab === "emailing") { loadTemplates(); loadSmtp(); }
  if (tab === "history") loadHistory();
  if (tab === "admin") loadAdmin();
}

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

document.querySelectorAll(".subtab").forEach((btn) => {
  btn.addEventListener("click", () => {
    const parent = btn.closest(".tab-panel");
    parent.querySelectorAll(".subtab").forEach((b) => b.classList.remove("active"));
    parent.querySelectorAll(".subtab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`subtab-${btn.dataset.subtab}`).classList.add("active");
    if (btn.dataset.subtab === "pc-watchers") loadWatchers();
    if (btn.dataset.subtab === "em-sequences") loadSequences();
    if (btn.dataset.subtab === "em-deliverability") loadMultiSmtp();
    if (btn.dataset.subtab === "em-analytics") loadEmailLogs();
    if (btn.dataset.subtab === "crm-kanban") loadKanban();
    if (btn.dataset.subtab === "crm-tasks") loadTasks();
    if (btn.dataset.subtab === "crm-segments") loadSegments();
    if (btn.dataset.subtab === "conv-wa") { loadWaTemplates(); loadHotSignals(); initWaInbox(); }
    if (btn.dataset.subtab === "conv-broadcast") { loadBroadcastHub(); }
    if (btn.dataset.subtab === "conv-social") { loadSocialHub(); }
    if (btn.dataset.subtab === "conv-radar") { loadRadarAndAiSettings(); }
    if (btn.dataset.subtab === "conv-links") { loadTrackedLinks(); loadHotSignals(); }
    if (btn.dataset.subtab === "conv-forms") loadLeadForms();
    if (btn.dataset.subtab === "conv-b2b") loadClosingSettings();
  });
});

// ---------------- STATS & DASHBOARD ----------------

async function loadStats() {
  try {
    const s = await api("/api/stats");
    $("statTotal").textContent = s.total_contacts;
    $("navTotalBadge").textContent = s.total_contacts;
    $("statPro").textContent = s.by_email_type.pro || 0;
    $("statGdrive").textContent = s.by_source.gdrive || 0;
    $("statLocal").textContent = (s.by_source.pc || 0) + (s.by_source.upload || 0);

    const tagOpts =
      '<option value="">Tous les tags</option>' +
      s.top_tags.map((t) => `<option value="${esc(t.tag)}">${esc(t.tag)} (${t.count})</option>`).join("");
    const curTag = $("filterTag").value;
    $("filterTag").innerHTML = tagOpts;
    $("filterTag").value = curTag;
    const curCampTag = $("campTag").value;
    $("campTag").innerHTML = tagOpts;
    $("campTag").value = curCampTag;

    const curDom = $("filterDomain").value;
    $("filterDomain").innerHTML =
      '<option value="">Tous les domaines</option>' +
      s.top_domains.map((d) => `<option value="${esc(d.domain)}">@${esc(d.domain)} (${d.count})</option>`).join("");
    $("filterDomain").value = curDom;

    const curCountry = $("filterCountry").value;
    $("filterCountry").innerHTML =
      '<option value="">Tous les pays</option>' +
      (s.top_countries || []).map((c) => `<option value="${esc(c.country)}">${esc(c.country)} (${c.count})</option>`).join("");
    $("filterCountry").value = curCountry;

    return s;
  } catch (e) {
    console.error(e);
    return null;
  }
}

async function loadDashboard() {
  try {
    const data = await api("/api/dashboard/executive");
    const s = data.stats || {};
    const k = data.kpis || {};
    const cur = data.workspace?.currency || "FCFA";
    const fmtM = (v) => `${Math.round(Number(v || 0)).toLocaleString("fr-FR")} ${cur}`;

    if ($("dashCollected")) $("dashCollected").textContent = fmtM(k.total_collected);
    if ($("dashRemaining")) $("dashRemaining").textContent = `Reste à recouvrer : ${fmtM(k.total_remaining)}`;
    if ($("dashPayCount")) $("dashPayCount").textContent = `${k.payments_count || 0} reçu(s)`;
    if ($("dashPipeline")) $("dashPipeline").textContent = fmtM(k.pipeline_value);
    if ($("dashConvRate")) $("dashConvRate").textContent = `${k.conversion_rate || 0}% closing`;
    if ($("dashClientsSub")) $("dashClientsSub").textContent = `${k.clients_count || 0} client(s) actif(s)`;
    if ($("dashHotLeads")) $("dashHotLeads").textContent = String(k.hot_leads || 0);
    if ($("dashTotalPill")) $("dashTotalPill").textContent = `${k.total_contacts || 0} contacts`;
    if ($("dashUpcomingAppts")) $("dashUpcomingAppts").textContent = String(k.upcoming_appts_count || 0);
    const totalAct = (k.wa_messages_7d || 0) + (k.calls_7d || 0) + (k.emails_7d || 0);
    if ($("dashActivity7d")) $("dashActivity7d").textContent = String(totalAct);
    if ($("dashActivityBreak")) $("dashActivityBreak").textContent = `${k.wa_messages_7d || 0} WA · ${k.calls_7d || 0} appels · ${k.emails_7d || 0} emails`;
    if ($("dashMxValid")) $("dashMxValid").textContent = String((s.by_mx || {}).valid || 0);
    if ($("dashDomainsSub")) $("dashDomainsSub").textContent = `MX valides · ${s.unique_domains || 0} domaines`;
    if ($("dashImportsPill")) $("dashImportsPill").textContent = `${s.imports_count || 0} imports`;

    // Actionable Hot Leads List
    const hotBox = $("dashHotLeadsList");
    const hotItems = data.top_hot_leads || [];
    if (hotBox) {
      hotBox.innerHTML = hotItems.length
        ? hotItems
            .map(
              (c) => `
              <div class="dash-action-row">
                <div style="min-width:0;">
                  <div style="font-weight:600;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(c.full_name || c.email)} ${scoreBadge(c.lead_score)}</div>
                  <div class="contact-role">${esc([c.company, c.phone || c.email, c.deal_value > 0 ? fmtM(c.deal_value) : ""].filter(Boolean).join(" · "))}</div>
                </div>
                <div style="display:flex;gap:4px;flex-shrink:0;">
                  ${c.phone ? `<button class="icon-btn dash-hot-call" data-idx="${c.id}" title="Appeler">📞</button>` : ""}
                  ${c.phone ? `<button class="icon-btn dash-hot-wa" data-idx="${c.id}" title="WhatsApp interne">💬</button>` : ""}
                  <button class="icon-btn dash-hot-prof" data-idx="${c.id}" title="Fiche 360°">👁</button>
                </div>
              </div>
            `
            )
            .join("")
        : '<div class="empty-cell">Aucun prospect chaud en attente.</div>';

      hotBox.querySelectorAll(".dash-hot-call").forEach((b) => {
        b.addEventListener("click", () => {
          const c = hotItems.find((x) => x.id === Number(b.dataset.idx));
          if (c) callContact(c);
        });
      });
      hotBox.querySelectorAll(".dash-hot-wa").forEach((b) => {
        b.addEventListener("click", () => {
          const c = hotItems.find((x) => x.id === Number(b.dataset.idx));
          if (c) openWhatsAppForContact(c);
        });
      });
      hotBox.querySelectorAll(".dash-hot-prof").forEach((b) => {
        b.addEventListener("click", () => openProfileModal(Number(b.dataset.idx)));
      });
    }

    // Actionable Agenda & Reminders List
    const agBox = $("dashAgendaList");
    if (agBox) {
      const rows = [];
      (data.upcoming_appointments || []).forEach((a) => {
        rows.push(`
          <div class="dash-action-row">
            <div>
              <div style="font-weight:600;font-size:13px;">📅 ${esc(a.appointment_date)} à ${esc(a.appointment_time)} — ${esc(a.full_name)}</div>
              <div class="contact-role">${esc(a.topic)} · ${esc(a.mode)} · ${esc(a.phone)}</div>
            </div>
            <button class="btn btn-ghost btn-sm" onclick="switchTab('booking')">Voir</button>
          </div>
        `);
      });
      (data.pending_tasks || []).forEach((t) => {
        rows.push(`
          <div class="dash-action-row">
            <div>
              <div style="font-weight:600;font-size:13px;">⏰ ${esc(t.due_date)} — ${esc(t.title)}</div>
              <div class="contact-role">${esc(t.full_name || t.email || "Rappel CRM")}</div>
            </div>
            <button class="btn btn-ghost btn-sm" onclick="switchTab('crm')">CRM</button>
          </div>
        `);
      });
      agBox.innerHTML = rows.length ? rows.join("") : '<div class="empty-cell">Aucun rendez-vous ni rappel en attente.</div>';
    }

    $("topDomainsList").innerHTML = (s.top_domains || []).length
      ? s.top_domains
          .slice(0, 5)
          .map((d) => `<div class="domain-row"><span>@${esc(d.domain)}</span><strong>${d.count}</strong></div>`)
          .join("")
      : '<div class="empty-cell">Aucun domaine pour le moment.</div>';

    if (typeof Chart === "undefined") return;

    const renderChart = (key, canvasId, config) => {
      if (state.charts[key]) state.charts[key].destroy();
      const el = $(canvasId);
      if (el) state.charts[key] = new Chart(el, config);
    };

    // 1. Conversion Funnel (Horizontal Bar)
    const funnel = data.funnel || [];
    renderChart("funnel", "chartFunnel", {
      type: "bar",
      data: {
        labels: funnel.map((f) => f.label),
        datasets: [{
          label: "Contacts",
          data: funnel.map((f) => f.count),
          backgroundColor: funnel.map((f) => f.color),
          borderRadius: 6,
        }],
      },
      options: {
        indexAxis: "y",
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
      },
    });

    // 2. Acquisition by Channel (All channels)
    const chanLabelsMap = {
      whatsapp: "WhatsApp",
      facebook: "Facebook",
      tiktok: "TikTok",
      maps_radar: "Radar Maps",
      form: "Formulaires",
      booking: "Page RDV",
      gdrive: "Google Drive",
      pc: "PC Local",
      upload: "Upload",
      manual: "Manuel",
    };
    const chEntries = Object.entries(data.by_channel || {}).slice(0, 8);
    renderChart("source", "chartSource", {
      type: "bar",
      data: {
        labels: chEntries.length ? chEntries.map(([k]) => chanLabelsMap[k] || k) : ["Aucune donnée"],
        datasets: [{
          label: "Contacts",
          data: chEntries.length ? chEntries.map(([, v]) => v) : [0],
          backgroundColor: ["#22c55e", "#3b82f6", "#ec4899", "#f59e0b", "#8b5cf6", "#06b6d4", "#14b8a6", "#94a3b8"],
          borderRadius: 6,
        }],
      },
      options: { maintainAspectRatio: false, plugins: { legend: { display: false } } },
    });

    // 3. Payments by Method (Doughnut)
    const pmEntries = Object.entries(k.by_payment_method || {});
    renderChart("payMethods", "chartPayMethods", {
      type: "doughnut",
      data: {
        labels: pmEntries.length ? pmEntries.map(([m]) => m) : ["Aucun paiement"],
        datasets: [{
          data: pmEntries.length ? pmEntries.map(([, v]) => v) : [1],
          backgroundColor: pmEntries.length ? ["#f59e0b", "#2563eb", "#16a34a", "#8b5cf6", "#06b6d4"] : ["#e2e8f0"],
        }],
      },
      options: { maintainAspectRatio: false, plugins: { legend: { position: "bottom" } } },
    });

    // 4. Email Quality Doughnut
    renderChart("type", "chartEmailType", {
      type: "doughnut",
      data: {
        labels: ["Professionnel", "Personnel", "Générique"],
        datasets: [{
          data: [(s.by_email_type || {}).pro || 0, (s.by_email_type || {}).perso || 0, (s.by_email_type || {}).generic || 0],
          backgroundColor: ["#6366f1", "#0ea5e9", "#f59e0b"],
        }],
      },
      options: { maintainAspectRatio: false, plugins: { legend: { position: "bottom" } } },
    });
  } catch (e) {
    toast(e.message, "error");
  }
}

$("dashRefreshBtn")?.addEventListener("click", loadDashboard);

$("backupDriveBtn").addEventListener("click", async () => {
  $("backupDriveBtn").disabled = true;
  $("backupDriveBtn").textContent = "Sauvegarde en cours…";
  try {
    const res = await api("/api/backup/gdrive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account: $("driveAccountInput").value.trim() }),
    });
    toast(`Base sauvegardée sur Google Drive : ${res.filename}`, "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    $("backupDriveBtn").disabled = false;
    $("backupDriveBtn").textContent = "☁️ Sauvegarder sur Google Drive";
  }
});

// ---------------- CONTACTS ----------------

let nlHasPhone = false;

function getFilterParams() {
  const params = new URLSearchParams();
  if ($("searchInput").value.trim()) params.set("q", $("searchInput").value.trim());
  if ($("filterStatus").value) params.set("status", $("filterStatus").value);
  if ($("filterEmailType").value) params.set("email_type", $("filterEmailType").value);
  if ($("filterMx").value) params.set("mx_status", $("filterMx").value);
  if ($("filterSource").value) params.set("source_type", $("filterSource").value);
  if ($("filterTag").value) params.set("tag", $("filterTag").value);
  if ($("filterDomain").value) params.set("domain", $("filterDomain").value);
  if ($("filterCountry").value) params.set("country", $("filterCountry").value);
  if (Number($("filterMinScore").value) > 0) params.set("min_score", $("filterMinScore").value);
  if ($("sortBy").value) params.set("sort_by", $("sortBy").value);
  if (nlHasPhone) params.set("has_phone", "1");
  return params;
}

function scoreBadge(score) {
  const s = Number(score || 0);
  if (s >= 65) return `<span class="score-pill score-hot">🔥 ${s}</span>`;
  if (s >= 40) return `<span class="score-pill score-warm">⚡ ${s}</span>`;
  return `<span class="score-pill score-cold">${s}</span>`;
}

function sourceBadge(type) {
  const map = {
    gdrive: ["Google Drive", "badge-gdrive"],
    pc: ["PC", "badge-pc"],
    upload: ["Upload", "badge-upload"],
    imap: ["Boîte Email", "badge-perso"],
    ocr: ["OCR", "badge-pro"],
    webhook: ["Webhook", "badge-tag"],
    manual: ["Manuel", "badge-manual"],
  };
  const [label, cls] = map[type] || [type, "badge-manual"];
  return `<span class="badge ${cls}">${label}</span>`;
}

function emailTypeBadge(t) {
  if (t === "pro") return '<span class="badge badge-pro">Pro</span>';
  if (t === "perso") return '<span class="badge badge-perso">Perso</span>';
  if (t === "generic") return '<span class="badge badge-generic">Générique</span>';
  return "";
}

function mxBadge(m) {
  if (m === "valid") return '<span class="badge badge-mx-valid">MX ✓</span>';
  if (m === "invalid") return '<span class="badge badge-mx-invalid">MX ✕</span>';
  return "";
}

function statusBadge(st) {
  const [lbl, cls] = STATUS_LABELS[st] || STATUS_LABELS.new;
  return `<span class="badge ${cls}">${lbl}</span>`;
}

async function loadContacts() {
  const params = getFilterParams();
  params.set("page", state.page);
  params.set("per_page", state.perPage);

  try {
    const data = await api(`/api/contacts?${params.toString()}`);
    state.contacts = data.items;
    state.selectedContactIds.clear();
    updateBulkBar();
    $("selectAllContacts").checked = false;

    const tbody = $("contactsTbody");
    if (!data.items.length) {
      tbody.innerHTML =
        '<tr><td colspan="9" class="empty-cell">Aucun contact trouvé avec ces filtres.</td></tr>';
    } else {
      tbody.innerHTML = data.items
        .map((c) => {
          const initials = (c.full_name || c.email || "?").slice(0, 2).toUpperCase();
          const tagsHtml = c.tags
            ? c.tags.split(",").map((t) => `<span class="badge badge-tag">${esc(t.trim())}</span>`).join("")
            : '<span style="color:#cbd5e1">—</span>';
          const webLink = c.website
            ? `<a href="${esc(safeUrl(c.website))}" target="_blank" rel="noopener noreferrer" style="font-size:11.5px;color:#4f46e5;text-decoration:none;">🌐</a>`
            : "";
          return `
            <tr>
              <td class="col-check"><input type="checkbox" class="contact-check" data-id="${c.id}" /></td>
              <td>
                <div class="contact-cell">
                  <div class="avatar">${esc(initials)}</div>
                  <div>
                    <div class="contact-name">${esc(c.full_name || "Sans nom")}</div>
                    ${c.role ? `<div class="contact-role">${esc(c.role)}</div>` : ""}
                  </div>
                </div>
              </td>
              <td>
                <div><a class="email-link" data-copy="${esc(c.email)}" title="Cliquer pour copier">${esc(c.email)}</a></div>
                <div>${emailTypeBadge(c.email_type)}${mxBadge(c.mx_status)}</div>
              </td>
              <td>${scoreBadge(c.lead_score)}</td>
              <td>${statusBadge(c.status)}</td>
              <td>
                <div>${esc(c.company || "—")} ${webLink} ${c.industry ? `<span class="badge badge-manual">${esc(c.industry)}</span>` : ""}</div>
                <div class="contact-role">${[c.country, c.phone].filter(Boolean).map(esc).join(" · ") || ""}</div>
              </td>
              <td>${tagsHtml}</td>
              <td>
                ${sourceBadge(c.source_type)}
                <span class="source-detail" title="${esc(c.source_detail)}">${esc(c.source_detail)}</span>
              </td>
              <td class="col-actions">
                ${c.phone ? `<button class="icon-btn" data-call="${c.id}" title="Appeler ${esc(c.phone)}">📞</button>` : ""}
                ${c.phone ? `<button class="icon-btn" data-wa="${c.id}" title="Contacter sur WhatsApp">💬</button>` : ""}
                <button class="icon-btn" data-prof="${c.id}" title="Fiche 360° & Rappels">👁</button>
                <button class="icon-btn" data-edit="${c.id}" title="Modifier">✎</button>
                <button class="icon-btn danger" data-del="${c.id}" title="Supprimer">🗑</button>
              </td>
            </tr>
          `;
        })
        .join("");
    }

    $("paginationBar").innerHTML = `
      <span>Affichage de ${data.items.length} sur ${data.total} contact(s) — Page ${data.page} / ${data.pages}</span>
      <div class="pagination-btns">
        <button class="btn btn-secondary btn-sm" id="prevPageBtn" ${data.page <= 1 ? "disabled" : ""}>← Précédent</button>
        <button class="btn btn-secondary btn-sm" id="nextPageBtn" ${data.page >= data.pages ? "disabled" : ""}>Suivant →</button>
      </div>
    `;
    $("prevPageBtn").onclick = () => { state.page--; loadContacts(); };
    $("nextPageBtn").onclick = () => { state.page++; loadContacts(); };

    tbody.querySelectorAll(".contact-check").forEach((cb) => {
      cb.addEventListener("change", () => {
        const id = Number(cb.dataset.id);
        if (cb.checked) state.selectedContactIds.add(id);
        else state.selectedContactIds.delete(id);
        updateBulkBar();
      });
    });
    tbody.querySelectorAll(".email-link").forEach((a) => {
      a.addEventListener("click", () => {
        navigator.clipboard.writeText(a.dataset.copy);
        toast(`Email copié : ${a.dataset.copy}`, "success");
      });
    });
    tbody.querySelectorAll("[data-call]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const c = state.contacts.find((x) => x.id === Number(btn.dataset.call));
        if (c) callContact(c);
      });
    });
    tbody.querySelectorAll("[data-wa]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const c = state.contacts.find((x) => x.id === Number(btn.dataset.wa));
        if (c) openWhatsAppForContact(c);
      });
    });
    tbody.querySelectorAll("[data-prof]").forEach((btn) => {
      btn.addEventListener("click", () => openProfileModal(Number(btn.dataset.prof)));
    });
    tbody.querySelectorAll("[data-edit]").forEach((btn) => {
      btn.addEventListener("click", () => openEditModal(Number(btn.dataset.edit)));
    });
    tbody.querySelectorAll("[data-del]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Supprimer ce contact ?")) return;
        await api(`/api/contacts/${btn.dataset.del}`, { method: "DELETE" });
        toast("Contact supprimé", "success");
        refreshAll();
      });
    });
  } catch (e) {
    toast(e.message, "error");
  }
}

function updateBulkBar() {
  const n = state.selectedContactIds.size;
  $("bulkCount").textContent = n;
  $("bulkBar").classList.toggle("hidden", n === 0);
}

$("selectAllContacts").addEventListener("change", (e) => {
  document.querySelectorAll(".contact-check").forEach((cb) => {
    cb.checked = e.target.checked;
    const id = Number(cb.dataset.id);
    if (cb.checked) state.selectedContactIds.add(id);
    else state.selectedContactIds.delete(id);
  });
  updateBulkBar();
});

$("bulkDeleteBtn").addEventListener("click", async () => {
  const ids = [...state.selectedContactIds];
  if (!confirm(`Supprimer ${ids.length} contact(s) ?`)) return;
  await api("/api/contacts/bulk", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "delete", ids }),
  });
  toast(`${ids.length} contact(s) supprimé(s)`, "success");
  refreshAll();
});

$("bulkTagBtn").addEventListener("click", async () => {
  const tag = $("bulkTagInput").value.trim();
  if (!tag) return toast("Saisissez un nom de tag.", "error");
  const ids = [...state.selectedContactIds];
  await api("/api/contacts/bulk", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "add_tag", ids, tag }),
  });
  $("bulkTagInput").value = "";
  toast(`Tag "${tag}" ajouté à ${ids.length} contact(s)`, "success");
  refreshAll();
});

$("bulkStatusBtn").addEventListener("click", async () => {
  const status = $("bulkStatusSelect").value;
  if (!status) return toast("Choisissez un statut.", "error");
  const ids = [...state.selectedContactIds];
  await api("/api/contacts/bulk", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "set_status", ids, status }),
  });
  toast(`Statut mis à jour pour ${ids.length} contact(s)`, "success");
  refreshAll();
});

let searchTimer;
$("searchInput").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.page = 1; loadContacts(); }, 220);
});
["sortBy", "filterMinScore", "filterCountry", "filterStatus", "filterEmailType", "filterMx", "filterSource", "filterTag", "filterDomain"].forEach((id) => {
  $(id).addEventListener("change", () => { state.page = 1; loadContacts(); });
});
$("resetFiltersBtn").addEventListener("click", () => {
  ["searchInput", "nlSearchInput", "filterCountry", "filterStatus", "filterEmailType", "filterMx", "filterSource", "filterTag", "filterDomain"].forEach((id) => ($(id).value = ""));
  $("filterMinScore").value = "0";
  nlHasPhone = false;
  $("nlBadges").classList.add("hidden");
  state.page = 1;
  loadContacts();
});

// Natural Language Smart Search
async function runNaturalSearch(promptText) {
  const prompt = (promptText ?? $("nlSearchInput").value).trim();
  if (!prompt) return;
  $("nlSearchInput").value = prompt;
  try {
    const res = await api("/api/search/natural", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    const f = res.filters || {};
    ["searchInput", "filterCountry", "filterStatus", "filterEmailType", "filterMx", "filterSource", "filterTag", "filterDomain"].forEach((id) => ($(id).value = ""));
    $("filterMinScore").value = "0";
    nlHasPhone = Boolean(f.has_phone);

    if (f.q) $("searchInput").value = f.q;
    if (f.email_type) $("filterEmailType").value = f.email_type;
    if (f.mx_status) $("filterMx").value = f.mx_status;
    if (f.status) $("filterStatus").value = f.status;
    if (f.source_type) $("filterSource").value = f.source_type;
    if (f.domain) $("filterDomain").value = f.domain;
    if (f.country) $("filterCountry").value = f.country;
    if (f.min_score) $("filterMinScore").value = String(f.min_score >= 70 ? 70 : f.min_score >= 50 ? 50 : 30);
    if (f.sort_by) $("sortBy").value = f.sort_by;

    const badges = res.badges || [];
    if (badges.length) {
      $("nlBadges").classList.remove("hidden");
      $("nlBadges").innerHTML =
        '<span style="font-size:12px;color:#64748b;">Filtres détectés :</span>' +
        badges.map((b) => `<span class="badge badge-tag">${esc(b)}</span>`).join("");
    }
    state.page = 1;
    loadContacts();
  } catch (e) {
    toast(e.message, "error");
  }
}

$("nlSearchBtn").addEventListener("click", () => runNaturalSearch());
$("nlSearchInput").addEventListener("keydown", (e) => { if (e.key === "Enter") runNaturalSearch(); });
document.querySelectorAll(".chip-btn").forEach((btn) => {
  btn.addEventListener("click", () => runNaturalSearch(btn.dataset.nl));
});

$("copyEmailsBtn").addEventListener("click", async () => {
  try {
    let emails = [];
    if (state.selectedContactIds.size > 0) {
      emails = state.contacts
        .filter((c) => state.selectedContactIds.has(c.id) && c.status !== "unsubscribed")
        .map((c) => c.email);
    } else {
      const data = await api(`/api/contacts/emails?${getFilterParams().toString()}`);
      emails = data.emails || [];
    }
    if (!emails.length) return toast("Aucun email éligible à copier.", "error");
    await navigator.clipboard.writeText(emails.join(", "));
    toast(`${emails.length} email(s) copié(s) !`, "success");
  } catch (e) {
    toast(e.message, "error");
  }
});

$("exportMenuBtn").addEventListener("click", (e) => {
  e.stopPropagation();
  $("exportMenuBtn").parentElement.classList.toggle("open");
});
document.addEventListener("click", () => $("exportMenuBtn").parentElement.classList.remove("open"));
document.querySelectorAll("[data-export]").forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    const params = getFilterParams();
    params.set("format", a.dataset.export);
    window.location.href = `/api/export?${params.toString()}`;
  });
});

// ---------------- MODAL ADD/EDIT ----------------

function openAddModal() {
  $("modalTitle").textContent = "Nouveau contact";
  $("editContactId").value = "";
  $("contactForm").reset();
  $("fStatus").value = "new";
  $("contactModal").classList.remove("hidden");
}

function openEditModal(id) {
  const c = state.contacts.find((x) => x.id === id);
  if (!c) return;
  $("modalTitle").textContent = "Modifier le contact";
  $("editContactId").value = c.id;
  $("fEmail").value = c.email || "";
  $("fStatus").value = c.status || "new";
  $("fFirstName").value = c.first_name || "";
  $("fLastName").value = c.last_name || "";
  $("fPhone").value = c.phone || "";
  $("fCompany").value = c.company || "";
  $("fRole").value = c.role || "";
  if ($("fDealValue")) $("fDealValue").value = c.deal_value || "";
  $("fTags").value = c.tags || "";
  $("fNotes").value = c.notes || "";
  $("contactModal").classList.remove("hidden");
}

function closeModal() { $("contactModal").classList.add("hidden"); }
$("openAddModalBtn").addEventListener("click", openAddModal);
$("closeModalBtn").addEventListener("click", closeModal);
$("cancelModalBtn").addEventListener("click", closeModal);

$("contactForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("editContactId").value;
  const payload = {
    email: $("fEmail").value.trim(),
    status: $("fStatus").value,
    first_name: $("fFirstName").value.trim(),
    last_name: $("fLastName").value.trim(),
    phone: $("fPhone").value.trim(),
    company: $("fCompany").value.trim(),
    role: $("fRole").value.trim(),
    deal_value: Number($("fDealValue")?.value || 0),
    tags: $("fTags").value.trim(),
    notes: $("fNotes").value.trim(),
  };
  try {
    if (id) {
      await api(`/api/contacts/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toast("Contact mis à jour", "success");
    } else {
      await api("/api/contacts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toast("Contact ajouté", "success");
    }
    closeModal();
    refreshAll();
  } catch (err) {
    toast(err.message, "error");
  }
});

// ---------------- QUALITY, ENRICHMENT & DUPLICATES ----------------

$("runAutoEnrichBtn").addEventListener("click", async () => {
  $("runAutoEnrichBtn").disabled = true;
  $("runAutoEnrichBtn").textContent = "Enrichissement en cours…";
  try {
    const res = await api("/api/enrich/auto", { method: "POST" });
    $("autoEnrichBox").classList.remove("hidden");
    $("autoEnrichBox").innerHTML = `<strong>${res.total_processed}</strong> contacts analysés et scorés : <strong>+${res.enriched_names}</strong> noms déduits, <strong>+${res.enriched_companies}</strong> entreprises, <strong>+${res.enriched_countries}</strong> pays détectés.`;
    toast("Enrichissement & Lead Scoring terminés !", "success");
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    $("runAutoEnrichBtn").disabled = false;
    $("runAutoEnrichBtn").textContent = "⚡ Enrichir & Scorer toute la base";
  }
});

$("showHotLeadsBtn").addEventListener("click", () => {
  $("filterMinScore").value = "50";
  $("sortBy").value = "score_desc";
  state.page = 1;
  switchTab("contacts");
  loadContacts();
});

$("runWebEnrichBtn").addEventListener("click", async () => {
  $("runWebEnrichBtn").disabled = true;
  $("runWebEnrichBtn").textContent = "Analyse des sites web en cours…";
  try {
    const res = await api("/api/enrich/web", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit_domains: 40 }),
    });
    $("webEnrichBox").classList.remove("hidden");
    $("webEnrichBox").innerHTML = `<strong>${res.domains_scanned}</strong> domaines Pro scannés -> <strong>${res.domains_enriched}</strong> sites enrichis (<strong>${res.contacts_updated}</strong> contacts mis à jour avec secteur/description).`;
    toast("Enrichissement Web terminé !", "success");
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    $("runWebEnrichBtn").disabled = false;
    $("runWebEnrichBtn").textContent = "🌐 Enrichir 40 domaines Pro depuis le Web";
  }
});

$("runMxCheckBtn").addEventListener("click", async () => {
  $("runMxCheckBtn").disabled = true;
  $("runMxCheckBtn").textContent = "Vérification MX en cours…";
  try {
    const res = await api("/api/quality/verify-mx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: 500 }),
    });
    $("mxResultBox").classList.remove("hidden");
    $("mxResultBox").innerHTML = `<strong>${res.checked_contacts}</strong> contact(s) vérifié(s) (${res.checked_domains} domaines) : <span style="color:#15803d"><strong>${res.valid} valides</strong></span>, <span style="color:#dc2626"><strong>${res.invalid} invalides</strong></span>.`;
    toast("Vérification MX terminée !", "success");
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    $("runMxCheckBtn").disabled = false;
    $("runMxCheckBtn").textContent = "⚡ Vérifier les domaines MX";
  }
});

$("showInvalidMxBtn").addEventListener("click", () => {
  $("filterMx").value = "invalid";
  state.page = 1;
  switchTab("contacts");
  loadContacts();
});

$("reclassifyBtn").addEventListener("click", async () => {
  const res = await api("/api/quality/reclassify", { method: "POST" });
  toast(`Classification mise à jour (${res.updated} contact(s) actualisé(s)).`, "success");
  refreshAll();
});

$("showGenericBtn").addEventListener("click", () => {
  $("filterEmailType").value = "generic";
  state.page = 1;
  switchTab("contacts");
  loadContacts();
});

async function loadDuplicates() {
  $("duplicatesContainer").innerHTML = '<div class="empty-cell">Recherche des doublons…</div>';
  try {
    const data = await api("/api/quality/duplicates");
    const groups = data.groups || [];
    $("dupCountBadge").textContent = `${groups.length} groupe(s)`;
    if (!groups.length) {
      $("duplicatesContainer").innerHTML = '<div class="empty-cell">Aucun doublon potentiel détecté. Votre base est propre !</div>';
      return;
    }
    $("duplicatesContainer").innerHTML = groups
      .map(
        (g, idx) => `
        <div class="dup-group" data-group="${idx}">
          <div class="dup-header">
            <strong>${esc(g.reason)}</strong>
            <button class="btn btn-primary btn-sm merge-group-btn" data-group="${idx}">Fusionner vers le contact principal</button>
          </div>
          <div class="dup-list">
            ${g.contacts
              .map(
                (c, ci) => `
              <label class="dup-item">
                <input type="radio" name="primary-${idx}" value="${c.id}" ${ci === 0 ? "checked" : ""} />
                <strong>${esc(c.email)}</strong>
                <span>${esc(c.full_name || "")}</span>
                <span class="subtitle">${esc(c.phone || "")} ${esc(c.company || "")}</span>
              </label>
            `
              )
              .join("")}
          </div>
        </div>
      `
      )
      .join("");

    $("duplicatesContainer").querySelectorAll(".merge-group-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const gIdx = Number(btn.dataset.group);
        const group = groups[gIdx];
        const checked = document.querySelector(`input[name="primary-${gIdx}"]:checked`);
        const primaryId = Number(checked.value);
        const secondaryIds = group.contacts.map((c) => c.id).filter((id) => id !== primaryId);
        await api("/api/quality/merge", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ primary_id: primaryId, secondary_ids: secondaryIds }),
        });
        toast("Doublons fusionnés avec succès !", "success");
        loadDuplicates();
        refreshAll();
      });
    });
  } catch (e) {
    toast(e.message, "error");
  }
}
$("refreshDuplicatesBtn").addEventListener("click", loadDuplicates);

// ---------------- PREVIEW MODAL ----------------

function closePreview() { $("previewModal").classList.add("hidden"); }
$("closePreviewBtn").addEventListener("click", closePreview);
$("closePreviewBtn2").addEventListener("click", closePreview);

function showPreviewModal(data) {
  $("previewTitle").textContent = `Aperçu : ${data.name}`;
  $("previewSummary").innerHTML = `<strong>${data.total}</strong> contact(s) détecté(s) dans ce fichier (affichage des ${data.sample.length} premiers).`;
  $("previewTbody").innerHTML = data.sample.length
    ? data.sample
        .map(
          (r) => `
        <tr>
          <td>${esc(r.email)}</td>
          <td>${esc(r.full_name || `${r.first_name || ""} ${r.last_name || ""}`.trim() || "—")}</td>
          <td>${esc(r.phone || "—")}</td>
          <td>${esc(r.company || "—")}</td>
        </tr>
      `
        )
        .join("")
    : '<tr><td colspan="4" class="empty-cell">Aucune adresse email détectée dans ce fichier.</td></tr>';
  $("previewModal").classList.remove("hidden");
}

// ---------------- GOOGLE DRIVE ----------------

async function checkDriveStatus() {
  const pill = $("driveStatusPill");
  try {
    const s = await api("/api/gdrive/status");
    if (s.connected) {
      pill.classList.add("ok");
      pill.innerHTML = '<span class="dot"></span><span>Google Drive connecté</span>';
    } else {
      pill.classList.add("err");
      pill.innerHTML = '<span class="dot"></span><span>Drive non connecté</span>';
    }
  } catch {
    pill.classList.add("err");
    pill.innerHTML = '<span class="dot"></span><span>Drive indisponible</span>';
  }
}

function renderDriveBreadcrumbs() {
  $("driveBreadcrumbs").innerHTML = state.driveStack
    .map((c, idx) => `<button class="crumb-btn" data-idx="${idx}">${esc(c.name)}</button>`)
    .join(" <span>/</span> ");
  $("driveBreadcrumbs").querySelectorAll(".crumb-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.driveStack = state.driveStack.slice(0, Number(btn.dataset.idx) + 1);
      $("driveSearchInput").value = "";
      loadDriveFiles();
    });
  });
}

async function loadDriveFiles() {
  state.driveLoaded = true;
  state.driveSelected.clear();
  updateDriveImportBtn();
  renderDriveBreadcrumbs();

  const curFolder = state.driveStack[state.driveStack.length - 1].id;
  const q = $("driveSearchInput").value.trim();
  const type = $("driveTypeFilter").value;
  const account = $("driveAccountInput").value.trim();

  const params = new URLSearchParams();
  if (curFolder && !q && !type) params.set("folder_id", curFolder);
  if (q) params.set("q", q);
  if (type) params.set("type", type);
  if (account) params.set("account", account);

  $("driveTbody").innerHTML = '<tr><td colspan="5" class="empty-cell">Chargement depuis Google Drive…</td></tr>';
  try {
    const data = await api(`/api/gdrive/files?${params.toString()}`);
    state.driveItems = data.items || [];
    if (!state.driveItems.length) {
      $("driveTbody").innerHTML = '<tr><td colspan="5" class="empty-cell">Aucun fichier trouvé.</td></tr>';
      return;
    }
    $("driveTbody").innerHTML = state.driveItems
      .map((f) => {
        const icon = f.isFolder ? "📁" : f.mimeType.includes("spreadsheet") ? "📊" : "📄";
        return `
          <tr>
            <td class="col-check">${f.isFolder ? "" : `<input type="checkbox" class="drive-check" data-id="${esc(f.id)}" />`}</td>
            <td>
              ${
                f.isFolder
                  ? `<button class="folder-link" data-folder-id="${esc(f.id)}" data-folder-name="${esc(f.name)}">${icon} ${esc(f.name)}</button>`
                  : `<span>${icon} ${esc(f.name)}</span>`
              }
            </td>
            <td><span class="badge badge-manual">${esc(f.mimeType.replace("application/vnd.google-apps.", "Google "))}</span></td>
            <td>${fmtSize(f.size)}</td>
            <td class="col-actions">
              ${f.isFolder ? "" : `<button class="btn btn-ghost btn-sm drive-preview-btn" data-id="${esc(f.id)}">👁 Aperçu</button>`}
            </td>
          </tr>
        `;
      })
      .join("");

    $("driveTbody").querySelectorAll(".folder-link").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.driveStack.push({ id: btn.dataset.folderId, name: btn.dataset.folderName });
        $("driveSearchInput").value = "";
        $("driveTypeFilter").value = "";
        loadDriveFiles();
      });
    });
    $("driveTbody").querySelectorAll(".drive-check").forEach((cb) => {
      cb.addEventListener("change", () => {
        const item = state.driveItems.find((x) => x.id === cb.dataset.id);
        if (cb.checked && item) state.driveSelected.set(item.id, item);
        else state.driveSelected.delete(cb.dataset.id);
        updateDriveImportBtn();
      });
    });
    $("driveTbody").querySelectorAll(".drive-preview-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const item = state.driveItems.find((x) => x.id === btn.dataset.id);
        if (!item) return;
        btn.disabled = true;
        btn.textContent = "…";
        try {
          const data = await api("/api/preview/gdrive", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ...item, account: $("driveAccountInput").value.trim() }),
          });
          showPreviewModal(data);
        } catch (e) {
          toast(e.message, "error");
        } finally {
          btn.disabled = false;
          btn.textContent = "👁 Aperçu";
        }
      });
    });
  } catch (e) {
    $("driveTbody").innerHTML = `<tr><td colspan="5" class="empty-cell">Erreur : ${esc(e.message)}</td></tr>`;
  }
}

function updateDriveImportBtn() {
  const n = state.driveSelected.size;
  $("driveImportBtn").disabled = n === 0;
  $("driveImportBtn").textContent = `Importer la sélection (${n})`;
}

$("driveSelectAll").addEventListener("change", (e) => {
  $("driveTbody").querySelectorAll(".drive-check").forEach((cb) => {
    cb.checked = e.target.checked;
    const item = state.driveItems.find((x) => x.id === cb.dataset.id);
    if (cb.checked && item) state.driveSelected.set(item.id, item);
    else state.driveSelected.delete(cb.dataset.id);
  });
  updateDriveImportBtn();
});

$("driveSearchBtn").addEventListener("click", loadDriveFiles);
$("driveRefreshBtn").addEventListener("click", loadDriveFiles);
$("driveSearchInput").addEventListener("keydown", (e) => { if (e.key === "Enter") loadDriveFiles(); });

$("driveImportBtn").addEventListener("click", async () => {
  const files = [...state.driveSelected.values()];
  if (!files.length) return;
  $("driveImportBtn").disabled = true;
  $("driveImportBtn").textContent = "Importation…";
  try {
    const res = await api("/api/gdrive/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        files,
        tag: $("driveTagInput").value.trim(),
        account: $("driveAccountInput").value.trim(),
      }),
    });
    const inserted = res.results.reduce((s, r) => s + r.inserted, 0);
    const updated = res.results.reduce((s, r) => s + r.updated, 0);
    toast(`Import Drive terminé : +${inserted} nouveaux, ${updated} mis à jour.`, "success");
    state.driveSelected.clear();
    $("driveSelectAll").checked = false;
    $("driveTbody").querySelectorAll(".drive-check").forEach((c) => (c.checked = false));
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    updateDriveImportBtn();
  }
});

// ---------------- LOCAL PC BROWSER & WATCHERS ----------------

async function initLocalBrowser() {
  state.localLoaded = true;
  try {
    const data = await api("/api/local/shortcuts");
    $("localShortcuts").innerHTML = data.shortcuts
      .map((s) => `<button class="shortcut-btn" data-path="${esc(s.path)}">📁 ${esc(s.label)}</button>`)
      .join("");
    $("localShortcuts").querySelectorAll(".shortcut-btn").forEach((btn) => {
      btn.addEventListener("click", () => browseLocal(btn.dataset.path));
    });
    if (data.shortcuts.length) {
      const docs = data.shortcuts.find((s) => s.label === "Documents") || data.shortcuts[0];
      browseLocal(docs.path);
    }
  } catch (e) {
    toast(e.message, "error");
  }
}

async function browseLocal(path) {
  state.localSelected.clear();
  updateLocalImportBtn();
  $("localSelectAll").checked = false;
  $("localTbody").innerHTML = '<tr><td colspan="5" class="empty-cell">Lecture du dossier…</td></tr>';

  try {
    const data = await api(`/api/local/browse?path=${encodeURIComponent(path)}`);
    state.localPath = data.path;
    state.localParent = data.parent;
    $("localPathInput").value = data.path;
    $("localUpBtn").disabled = !data.parent;

    if (!data.entries.length) {
      $("localTbody").innerHTML = '<tr><td colspan="5" class="empty-cell">Dossier vide.</td></tr>';
      return;
    }

    $("localTbody").innerHTML = data.entries
      .map((e) => {
        if (e.isDir) {
          return `
            <tr>
              <td class="col-check"></td>
              <td><button class="folder-link" data-path="${esc(e.path)}">📁 ${esc(e.name)}</button></td>
              <td><span class="badge badge-manual">Dossier</span></td>
              <td>—</td>
              <td></td>
            </tr>
          `;
        }
        return `
          <tr>
            <td class="col-check">${e.supported ? `<input type="checkbox" class="local-check" data-path="${esc(e.path)}" />` : ""}</td>
            <td>📄 ${esc(e.name)}</td>
            <td>${e.supported ? `<span class="badge badge-supported">${esc(e.ext)}</span>` : `<span class="badge badge-manual">${esc(e.ext || "fichier")}</span>`}</td>
            <td>${fmtSize(e.size)}</td>
            <td class="col-actions">${e.supported ? `<button class="btn btn-ghost btn-sm local-preview-btn" data-path="${esc(e.path)}">👁 Aperçu</button>` : ""}</td>
          </tr>
        `;
      })
      .join("");
    bindLocalRows();
  } catch (e) {
    $("localTbody").innerHTML = `<tr><td colspan="5" class="empty-cell">${esc(e.message)}</td></tr>`;
  }
}

async function scanLocalFolder() {
  const root = $("localPathInput").value.trim() || state.localPath;
  const mode = $("localScanMode").value;
  if (!root) return;
  state.localSelected.clear();
  updateLocalImportBtn();
  $("localSelectAll").checked = false;
  $("localTbody").innerHTML = '<tr><td colspan="5" class="empty-cell">Scan récursif en cours…</td></tr>';
  try {
    const data = await api(`/api/local/scan?path=${encodeURIComponent(root)}&mode=${encodeURIComponent(mode)}`);
    renderScannedFiles(data.files || [], "Aucun fichier compatible trouvé dans ce dossier.");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function scanAllKeyZones() {
  const mode = $("localScanMode").value;
  state.localSelected.clear();
  updateLocalImportBtn();
  $("localSelectAll").checked = false;
  $("localTbody").innerHTML = '<tr><td colspan="5" class="empty-cell">Scan global en cours (Bureau, Documents, Téléchargements, Disque D)…</td></tr>';
  try {
    const data = await api(`/api/local/scan-all?mode=${encodeURIComponent(mode)}`);
    renderScannedFiles(data.files || [], "Aucun fichier compatible trouvé dans vos 4 dossiers clés.");
  } catch (e) {
    toast(e.message, "error");
  }
}

function renderScannedFiles(files, emptyMsg) {
  if (!files.length) {
    $("localTbody").innerHTML = `<tr><td colspan="5" class="empty-cell">${esc(emptyMsg)}</td></tr>`;
    return;
  }
  $("localTbody").innerHTML = files
    .map(
      (f) => `
      <tr>
        <td class="col-check"><input type="checkbox" class="local-check" data-path="${esc(f.path)}" /></td>
        <td>
          <div>📄 ${esc(f.name)} ${f.zone ? `<span class="badge badge-pc">${esc(f.zone)}</span>` : ""}</div>
          <div class="source-detail" style="max-width:520px" title="${esc(f.path)}">${esc(f.path)}</div>
        </td>
        <td><span class="badge badge-supported">${esc(f.ext)}</span></td>
        <td>${fmtSize(f.size)}</td>
        <td class="col-actions"><button class="btn btn-ghost btn-sm local-preview-btn" data-path="${esc(f.path)}">👁 Aperçu</button></td>
      </tr>
    `
    )
    .join("");
  bindLocalRows();
  toast(`${files.length} fichier(s) détecté(s) !`, "success");
}

function bindLocalRows() {
  $("localTbody").querySelectorAll(".folder-link").forEach((btn) => {
    btn.addEventListener("click", () => browseLocal(btn.dataset.path));
  });
  $("localTbody").querySelectorAll(".local-check").forEach((cb) => {
    cb.addEventListener("change", () => {
      if (cb.checked) state.localSelected.add(cb.dataset.path);
      else state.localSelected.delete(cb.dataset.path);
      updateLocalImportBtn();
    });
  });
  $("localTbody").querySelectorAll(".local-preview-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "…";
      try {
        const data = await api("/api/preview/local", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: btn.dataset.path }),
        });
        showPreviewModal(data);
      } catch (e) {
        toast(e.message, "error");
      } finally {
        btn.disabled = false;
        btn.textContent = "👁 Aperçu";
      }
    });
  });
}

function updateLocalImportBtn() {
  const n = state.localSelected.size;
  $("localImportBtn").disabled = n === 0;
  $("localImportBtn").textContent = `Importer la sélection (${n})`;
}

$("localSelectAll").addEventListener("change", (e) => {
  $("localTbody").querySelectorAll(".local-check").forEach((cb) => {
    cb.checked = e.target.checked;
    if (cb.checked) state.localSelected.add(cb.dataset.path);
    else state.localSelected.delete(cb.dataset.path);
  });
  updateLocalImportBtn();
});

$("localUpBtn").addEventListener("click", () => { if (state.localParent) browseLocal(state.localParent); });
$("localGoBtn").addEventListener("click", () => browseLocal($("localPathInput").value.trim()));
$("localPathInput").addEventListener("keydown", (e) => { if (e.key === "Enter") browseLocal($("localPathInput").value.trim()); });
$("localScanBtn").addEventListener("click", scanLocalFolder);
$("localScanAllBtn").addEventListener("click", scanAllKeyZones);

$("localImportBtn").addEventListener("click", async () => {
  const paths = [...state.localSelected];
  if (!paths.length) return;
  $("localImportBtn").disabled = true;
  $("localImportBtn").textContent = "Importation…";
  try {
    const res = await api("/api/local/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, tag: $("localTagInput").value.trim() }),
    });
    const inserted = res.results.reduce((s, r) => s + r.inserted, 0);
    const updated = res.results.reduce((s, r) => s + r.updated, 0);
    toast(`Import PC terminé : +${inserted} nouveaux, ${updated} mis à jour.`, "success");
    state.localSelected.clear();
    $("localSelectAll").checked = false;
    $("localTbody").querySelectorAll(".local-check").forEach((c) => (c.checked = false));
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    updateLocalImportBtn();
  }
});

// Watchers
async function loadWatchers() {
  const data = await api("/api/watchers");
  const tbody = $("watchersTbody");
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-cell">Aucun dossier surveillé pour le moment.</td></tr>';
    return;
  }
  tbody.innerHTML = data.items
    .map(
      (w) => `
      <tr>
        <td><code>${esc(w.path)}</code></td>
        <td>${w.tag ? `<span class="badge badge-tag">${esc(w.tag)}</span>` : "—"}</td>
        <td>${w.mode === "structured" ? "CSV/Excel/vCard/JSON" : "Tous (+ PDF/Word/Txt)"}</td>
        <td>${esc(w.last_scan_at ? w.last_scan_at.replace("T", " ") : "En attente")}</td>
        <td class="col-actions"><button class="icon-btn danger del-watcher-btn" data-id="${w.id}">🗑</button></td>
      </tr>
    `
    )
    .join("");
  tbody.querySelectorAll(".del-watcher-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/watchers/${btn.dataset.id}`, { method: "DELETE" });
      toast("Surveillance supprimée", "success");
      loadWatchers();
    });
  });
}

$("addWatcherBtn").addEventListener("click", async () => {
  const path = $("watchPathInput").value.trim();
  if (!path) return toast("Saisissez le chemin d'un dossier.", "error");
  try {
    await api("/api/watchers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path,
        tag: $("watchTagInput").value.trim(),
        mode: $("watchModeSelect").value,
      }),
    });
    $("watchPathInput").value = "";
    toast("Surveillance automatique activée (scan toutes les 60s) !", "success");
    loadWatchers();
  } catch (e) {
    toast(e.message, "error");
  }
});

$("runWatchersNowBtn").addEventListener("click", async () => {
  const res = await api("/api/watchers/run", { method: "POST" });
  toast(`Scan auto terminé : ${res.files_imported} fichier(s), +${res.inserted} nouveaux.`, "success");
  loadWatchers();
  refreshAll();
});

// ---------------- DRAG & DROP UPLOAD ----------------

const dropzone = $("dropzone");
$("browseFilesBtn").addEventListener("click", () => $("fileInput").click());
$("fileInput").addEventListener("change", (e) => setUploadFiles(e.target.files));
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  setUploadFiles(e.dataTransfer.files);
});

function setUploadFiles(fileList) {
  state.uploadFiles = [...fileList];
  $("startUploadBtn").disabled = state.uploadFiles.length === 0;
  $("uploadFileList").innerHTML = state.uploadFiles
    .map((f) => `<li><span>📄 ${esc(f.name)}</span><span>${fmtSize(f.size)}</span></li>`)
    .join("");
}

$("startUploadBtn").addEventListener("click", async () => {
  if (!state.uploadFiles.length) return;
  const fd = new FormData();
  state.uploadFiles.forEach((f) => fd.append("files", f));
  fd.append("tag", $("uploadTagInput").value.trim());
  $("startUploadBtn").disabled = true;
  $("startUploadBtn").textContent = "Importation…";
  try {
    const res = await api("/api/upload", { method: "POST", body: fd });
    const inserted = res.results.reduce((s, r) => s + r.inserted, 0);
    const updated = res.results.reduce((s, r) => s + r.updated, 0);
    toast(`Upload terminé : +${inserted} nouveaux, ${updated} mis à jour.`, "success");
    state.uploadFiles = [];
    $("uploadFileList").innerHTML = "";
    $("fileInput").value = "";
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    $("startUploadBtn").textContent = "Importer les fichiers choisis";
    $("startUploadBtn").disabled = state.uploadFiles.length === 0;
  }
});

// ---------------- CONNECTORS: IMAP, OCR, WEBHOOK ----------------

const IMAP_PRESETS = {
  gmail: { host: "imap.gmail.com", folder: "INBOX" },
  outlook: { host: "outlook.office365.com", folder: "INBOX" },
  yahoo: { host: "imap.mail.yahoo.com", folder: "INBOX" },
  custom: { host: "", folder: "INBOX" },
};

$("imapPreset").addEventListener("change", (e) => {
  const p = IMAP_PRESETS[e.target.value];
  if (p) {
    $("imapHost").value = p.host;
    $("imapFolder").value = p.folder;
  }
});

$("runImapImportBtn").addEventListener("click", async () => {
  $("runImapImportBtn").disabled = true;
  $("runImapImportBtn").textContent = "Connexion & scan IMAP…";
  try {
    const res = await api("/api/connectors/imap/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        host: $("imapHost").value.trim(),
        port: 993,
        username: $("imapUser").value.trim(),
        password: $("imapPass").value,
        folder: $("imapFolder").value.trim() || "INBOX",
        limit: Number($("imapLimit").value || 150),
        parse_signatures: $("imapParseSigs").checked,
        tag: $("imapTag").value.trim(),
      }),
    });
    $("imapResultBox").classList.remove("hidden");
    $("imapResultBox").innerHTML = `<strong>${res.emails_scanned}</strong> emails analysés : <strong>+${res.inserted}</strong> nouveaux contacts, <strong>${res.updated}</strong> mis à jour.`;
    toast("Import IMAP terminé !", "success");
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    $("runImapImportBtn").disabled = false;
    $("runImapImportBtn").textContent = "🚀 Aspirer les contacts IMAP";
  }
});

$("runOcrBtn").addEventListener("click", async () => {
  const files = $("ocrFileInput").files;
  if (!files || !files.length) return toast("Sélectionnez au moins une image.", "error");
  const fd = new FormData();
  [...files].forEach((f) => fd.append("files", f));
  fd.append("tag", $("ocrTagInput").value.trim());
  fd.append("auto_import", $("ocrAutoImport").checked ? "true" : "false");

  $("runOcrBtn").disabled = true;
  $("runOcrBtn").textContent = "Lecture OCR en cours…";
  try {
    const res = await api("/api/connectors/ocr/scan", { method: "POST", body: fd });
    const box = $("ocrResultBox");
    box.classList.remove("hidden");
    const linesHtml = (res.previews || [])
      .map(
        (p) =>
          `<div style="margin-bottom:8px;"><strong>${esc(p.filename)}</strong> — ${p.contacts.length} contact(s) : ${p.contacts.map((c) => esc(c.email)).join(", ") || "aucun email"}<br><small style="color:#64748b;">${esc(p.raw_text.slice(0, 220))}</small></div>`
      )
      .join("");
    box.innerHTML = linesHtml || "Aucun texte détecté.";
    if (res.import_summary) {
      toast(`OCR terminé : +${res.import_summary.inserted} nouveaux contacts !`, "success");
      refreshAll();
    } else {
      toast(`OCR terminé : ${res.total_contacts} contact(s) détecté(s).`, "success");
    }
  } catch (e) {
    toast(e.message, "error");
  } finally {
    $("runOcrBtn").disabled = false;
    $("runOcrBtn").textContent = "🔍 Lancer le scan OCR";
  }
});

async function loadWebhookInfo() {
  try {
    const data = await api("/api/webhooks/info");
    $("webhookUrl").value = data.url;
    $("webhookToken").value = data.token;
    $("webhookExample").textContent =
      `curl -X POST "${data.url}" \\\n` +
      `  -H "Content-Type: application/json" \\\n` +
      `  -H "X-API-Key: ${data.token}" \\\n` +
      `  -d '{"email":"client@entreprise.com","full_name":"Amadou Diallo","company":"Tech Afrique","phone":"+22507000000","tag":"n8n"}'`;
  } catch {}
}

$("regenWebhookBtn").addEventListener("click", async () => {
  if (!confirm("Régénérer la clé API Webhook ? L'ancienne clé ne fonctionnera plus.")) return;
  await api("/api/webhooks/regenerate", { method: "POST" });
  toast("Nouvelle clé API générée", "success");
  loadWebhookInfo();
});

$("testWebhookBtn").addEventListener("click", async () => {
  const url = $("webhookUrl").value;
  const token = $("webhookToken").value;
  try {
    const res = await api(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": token },
      body: JSON.stringify({
        email: "webhook.test@contactshub.local",
        full_name: "Test Webhook",
        company: "ContactsHub",
        tag: "Webhook-Test",
      }),
    });
    toast(`Webhook OK ! (+${res.inserted} nouveau, ${res.updated} mis à jour)`, "success");
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  }
});

// ---------------- EMAILING & TEMPLATES ----------------

$("toggleSmtpBtn").addEventListener("click", () => $("smtpCard").classList.toggle("hidden"));

async function loadSmtp() {
  try {
    const cfg = await api("/api/smtp");
    $("smtpHost").value = cfg.host || "smtp.gmail.com";
    $("smtpPort").value = cfg.port || 587;
    $("smtpUser").value = cfg.username || "";
    $("smtpFromName").value = cfg.from_name || "";
  } catch {}
}

$("saveSmtpBtn").addEventListener("click", async () => {
  try {
    await api("/api/smtp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        host: $("smtpHost").value.trim(),
        port: Number($("smtpPort").value || 587),
        username: $("smtpUser").value.trim(),
        password: $("smtpPass").value,
        from_name: $("smtpFromName").value.trim(),
        use_tls: true,
      }),
    });
    $("smtpPass").value = "";
    toast("Configuration SMTP enregistrée !", "success");
    $("smtpCard").classList.add("hidden");
  } catch (e) {
    toast(e.message, "error");
  }
});

async function loadTemplates() {
  const data = await api("/api/templates");
  state.templates = data.items || [];
  $("templateSelect").innerHTML =
    '<option value="">— Choisir un modèle —</option>' +
    state.templates.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  if (state.templates.length && !$("tplId").value) {
    selectTemplate(state.templates[0].id);
  }
}

function selectTemplate(id) {
  const t = state.templates.find((x) => String(x.id) === String(id));
  if (!t) return;
  $("templateSelect").value = t.id;
  $("tplId").value = t.id;
  $("tplName").value = t.name;
  $("tplSubject").value = t.subject;
  $("tplBody").value = t.body;
  if ($("tplBodyVisual")) {
    $("tplBodyVisual").innerHTML = /<[a-z][\s\S]*>/i.test(t.body || "")
      ? sanitizeHtml(t.body)
      : esc(t.body || "").replace(/\n/g, "<br>");
  }
}

$("templateSelect").addEventListener("change", (e) => selectTemplate(e.target.value));
$("newTemplateBtn").addEventListener("click", () => {
  $("tplId").value = "";
  $("templateSelect").value = "";
  $("tplName").value = "";
  $("tplSubject").value = "";
  $("tplBody").value = "";
  if ($("tplBodyVisual")) $("tplBodyVisual").innerHTML = "";
});

$("saveTemplateBtn").addEventListener("click", async () => {
  const id = $("tplId").value;
  const payload = {
    name: $("tplName").value.trim(),
    subject: $("tplSubject").value.trim(),
    body: $("tplBody").value.trim(),
  };
  try {
    if (id) {
      await api(`/api/templates/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      const res = await api("/api/templates", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      $("tplId").value = res.id;
    }
    toast("Modèle enregistré !", "success");
    loadTemplates();
  } catch (e) {
    toast(e.message, "error");
  }
});

$("deleteTemplateBtn").addEventListener("click", async () => {
  const id = $("tplId").value;
  if (!id || !confirm("Supprimer ce modèle ?")) return;
  await api(`/api/templates/${id}`, { method: "DELETE" });
  $("newTemplateBtn").click();
  toast("Modèle supprimé", "success");
  loadTemplates();
});

$("openMailtoBtn").addEventListener("click", async () => {
  const params = new URLSearchParams();
  if ($("campTag").value) params.set("tag", $("campTag").value);
  if ($("campStatus").value) params.set("status", $("campStatus").value);
  if ($("campType").value) params.set("email_type", $("campType").value);
  const data = await api(`/api/contacts/emails?${params.toString()}`);
  const emails = (data.emails || []).slice(0, Number($("campMax").value || 50));
  if (!emails.length) return toast("Aucun contact éligible trouvé.", "error");
  const subj = encodeURIComponent($("tplSubject").value.trim());
  const body = encodeURIComponent($("tplBody").value.trim());
  window.location.href = `mailto:?bcc=${encodeURIComponent(emails.join(","))}&subject=${subj}&body=${body}`;
});

$("sendCampaignBtn").addEventListener("click", async () => {
  if (!confirm("Envoyer cette campagne email personnalisée via votre serveur SMTP ?")) return;
  $("sendCampaignBtn").disabled = true;
  $("sendCampaignBtn").textContent = "Envoi en cours…";
  try {
    const res = await api("/api/campaigns/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subject: $("tplSubject").value.trim(),
        body: $("tplBody").value.trim(),
        tag: $("campTag").value,
        status: $("campStatus").value,
        email_type: $("campType").value,
        max_recipients: Number($("campMax").value || 50),
      }),
    });
    toast(`Campagne envoyée : ${res.sent} email(s) envoyé(s), ${res.errors.length} erreur(s).`, "success");
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    $("sendCampaignBtn").disabled = false;
    $("sendCampaignBtn").textContent = "🚀 Envoyer la campagne (SMTP)";
  }
});

// ---------------- SEQUENCES, MULTI-SMTP, DELIVERABILITY & ANALYTICS ----------------

async function loadSequences() {
  try {
    const data = await api("/api/sequences");
    const items = data.items || [];
    const box = $("sequencesList");
    if (!items.length) {
      box.innerHTML = '<div class="empty-cell">Aucune séquence créée pour le moment.</div>';
      return;
    }
    box.innerHTML = items
      .map(
        (s) => `
        <div class="dup-group">
          <div class="dup-header">
            <div>
              <strong>${esc(s.name)}</strong>
              <span class="badge ${s.status === "active" ? "badge-st-client" : "badge-st-followup"}" style="margin-left:6px;">${s.status === "active" ? "Active" : "En pause"}</span>
              <span class="subtitle" style="margin-left:8px;">${s.steps.length} étape(s) · Délai ${s.delay_min_sec}-${s.delay_max_sec}s · Actifs: ${s.enrollments.active}, Terminés: ${s.enrollments.completed}, 💬 Réponses: ${s.enrollments.replied || 0}, ⚠️ Bounces: ${s.enrollments.bounced || 0}</span>
            </div>
            <div style="display:flex;gap:6px;flex-wrap:wrap;">
              <button class="btn btn-primary btn-sm seq-enroll-btn" data-id="${s.id}">+ Inscrire contacts</button>
              <button class="btn btn-secondary btn-sm seq-toggle-btn" data-id="${s.id}">${s.status === "active" ? "⏸ Pause" : "▶ Reprendre"}</button>
              <button class="icon-btn danger seq-del-btn" data-id="${s.id}">🗑</button>
            </div>
          </div>
          <div class="dup-list">
            ${s.steps.map((st) => `<div class="dup-item"><strong>Étape ${st.step_order} (J+${st.delay_days})</strong><span>${esc(st.subject)}</span></div>`).join("")}
          </div>
        </div>
      `
      )
      .join("");

    box.querySelectorAll(".seq-toggle-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api(`/api/sequences/${btn.dataset.id}/toggle`, { method: "POST" });
        loadSequences();
      });
    });
    box.querySelectorAll(".seq-del-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Supprimer cette séquence ?")) return;
        await api(`/api/sequences/${btn.dataset.id}`, { method: "DELETE" });
        loadSequences();
      });
    });
    box.querySelectorAll(".seq-enroll-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const tag = prompt("Tag des contacts à inscrire (laisser vide = tous les contacts Pro non désinscrits) :", "") ?? "";
        const maxStr = prompt("Nombre maximum de contacts à inscrire :", "30");
        if (maxStr === null) return;
        const res = await api(`/api/sequences/${btn.dataset.id}/enroll`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tag: tag.trim(),
            email_type: tag.trim() ? "" : "pro",
            max_count: Number(maxStr) || 30,
          }),
        });
        toast(`${res.enrolled} contact(s) inscrit(s) dans la séquence !`, "success");
        loadSequences();
      });
    });
  } catch (e) {
    toast(e.message, "error");
  }
}

$("createSequenceBtn").addEventListener("click", async () => {
  const name = $("seqName").value.trim();
  const s1Subj = $("seqStep1Subj").value.trim();
  const s1Body = $("seqStep1Body").value.trim();
  if (!name || !s1Subj || !s1Body) return toast("Nom, objet et message de l'étape 1 requis.", "error");
  const steps = [{ delay_days: 0, subject: s1Subj, body: s1Body }];
  const s2Subj = $("seqStep2Subj").value.trim();
  const s2Body = $("seqStep2Body").value.trim();
  if (s2Subj && s2Body) {
    steps.push({ delay_days: Number($("seqStep2Days").value || 3), subject: s2Subj, body: s2Body });
  }
  try {
    await api("/api/sequences", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        delay_min_sec: Number($("seqDelayMin").value || 20),
        delay_max_sec: Number($("seqDelayMax").value || 45),
        include_unsubscribe: $("seqIncludeUnsub").checked,
        steps,
      }),
    });
    $("seqName").value = "";
    $("seqStep1Subj").value = "";
    $("seqStep1Body").value = "";
    $("seqStep2Subj").value = "";
    $("seqStep2Body").value = "";
    toast("Séquence automatisée créée !", "success");
    loadSequences();
  } catch (e) {
    toast(e.message, "error");
  }
});

$("refreshSequencesBtn").addEventListener("click", loadSequences);
$("runSequencesNowBtn").addEventListener("click", async () => {
  const res = await api("/api/sequences/run-now", { method: "POST" });
  toast(`Séquences exécutées : ${res.sent} envoyé(s), ${res.errors} erreur(s), ${res.completed} terminé(s).`, "success");
  loadSequences();
});

$("checkDelivBtn").addEventListener("click", async () => {
  const dom = $("delivDomainInput").value.trim();
  if (!dom) return toast("Saisissez un domaine ou email.", "error");
  $("checkDelivBtn").disabled = true;
  try {
    const res = await api("/api/emailing/deliverability", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ domain: dom }),
    });
    const box = $("delivResultBox");
    box.classList.remove("hidden");
    box.innerHTML =
      `<div><strong>Domaine : ${esc(res.domain)} — Score DNS : ${res.score}/100</strong></div>` +
      `<div style="margin-top:6px;">MX : ${res.mx.ok ? "✅ OK" : "❌ Absent"} · SPF : ${res.spf.ok ? "✅ OK" : "❌ Absent"} · DMARC : ${res.dmarc.ok ? "✅ OK" : "❌ Absent"}</div>` +
      `<ul style="margin:6px 0 0 18px;padding:0;">${res.advice.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>`;
  } catch (e) {
    toast(e.message, "error");
  } finally {
    $("checkDelivBtn").disabled = false;
  }
});

async function loadMultiSmtp() {
  const data = await api("/api/smtp-accounts");
  const tbody = $("multiSmtpTbody");
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-cell">Aucune boîte SMTP ajoutée à la rotation.</td></tr>';
    return;
  }
  tbody.innerHTML = data.items
    .map(
      (a) => `
      <tr>
        <td><strong>${esc(a.label)}</strong></td>
        <td>${esc(a.host)}:${a.port}</td>
        <td>${esc(a.username)}</td>
        <td>${a.sent_today}</td>
        <td>${a.daily_limit}</td>
        <td class="col-actions"><button class="icon-btn danger del-msmtp-btn" data-id="${a.id}">🗑</button></td>
      </tr>
    `
    )
    .join("");
  tbody.querySelectorAll(".del-msmtp-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/smtp-accounts/${btn.dataset.id}`, { method: "DELETE" });
      loadMultiSmtp();
    });
  });
}

$("addMultiSmtpBtn").addEventListener("click", async () => {
  try {
    await api("/api/smtp-accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label: $("mSmtpLabel").value.trim(),
        host: $("mSmtpHost").value.trim(),
        port: 587,
        username: $("mSmtpUser").value.trim(),
        password: $("mSmtpPass").value,
        from_name: $("mSmtpFrom").value.trim(),
        daily_limit: Number($("mSmtpLimit").value || 100),
      }),
    });
    $("mSmtpPass").value = "";
    toast("Boîte SMTP ajoutée à la rotation !", "success");
    loadMultiSmtp();
  } catch (e) {
    toast(e.message, "error");
  }
});

async function loadEmailLogs() {
  const data = await api("/api/emailing/logs");
  $("emStatSent").textContent = data.summary.sent;
  $("emStatOpened").textContent = data.summary.opened;
  $("emStatRate").textContent = `${data.summary.open_rate}%`;
  $("emStatFailed").textContent = data.summary.failed;
  const tbody = $("emailLogsTbody");
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-cell">Aucun envoi enregistré pour le moment.</td></tr>';
    return;
  }
  tbody.innerHTML = data.items
    .map(
      (l) => `
      <tr>
        <td>${esc(l.sent_at.replace("T", " "))}</td>
        <td>${esc(l.email)}</td>
        <td>${esc(l.subject)}</td>
        <td><span class="badge ${l.status === "sent" ? "badge-st-client" : "badge-st-unsubscribed"}">${esc(l.status)}</span></td>
        <td>${l.opened_count > 0 ? `<span class="badge badge-st-contacted">👁 ${l.opened_count}</span>` : "—"}</td>
      </tr>
    `
    )
    .join("");
}
$("refreshEmailLogsBtn").addEventListener("click", loadEmailLogs);

// ---------------- CRM: KANBAN, TASKS, SEGMENTS & 360 PROFILE ----------------

$("saveSegmentBtn").addEventListener("click", async () => {
  const name = prompt("Nom du segment dynamique à sauvegarder :");
  if (!name || !name.trim()) return;
  const filters = {};
  if ($("searchInput").value.trim()) filters.q = $("searchInput").value.trim();
  if ($("filterStatus").value) filters.status = $("filterStatus").value;
  if ($("filterEmailType").value) filters.email_type = $("filterEmailType").value;
  if ($("filterMx").value) filters.mx_status = $("filterMx").value;
  if ($("filterSource").value) filters.source_type = $("filterSource").value;
  if ($("filterTag").value) filters.tag = $("filterTag").value;
  if ($("filterDomain").value) filters.domain = $("filterDomain").value;
  if ($("filterCountry").value) filters.country = $("filterCountry").value;
  if (Number($("filterMinScore").value) > 0) filters.min_score = Number($("filterMinScore").value);
  if (nlHasPhone) filters.has_phone = true;

  await api("/api/crm/segments", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim(), filters }),
  });
  toast(`Segment "${name.trim()}" sauvegardé !`, "success");
});

async function loadKanban() {
  const params = new URLSearchParams();
  if ($("kanbanSearch").value.trim()) params.set("q", $("kanbanSearch").value.trim());
  if (Number($("kanbanMinScore").value) > 0) params.set("min_score", $("kanbanMinScore").value);

  try {
    const data = await api(`/api/crm/pipeline?${params.toString()}`);
    const cur = data.currency || "FCFA";
    const fmtMoney = (v) => `${Number(v || 0).toLocaleString("fr-FR")} ${cur}`;
    if ($("kanbanTotalValue")) $("kanbanTotalValue").textContent = fmtMoney(data.pipeline_value_total);
    if ($("kanbanWonValue")) $("kanbanWonValue").textContent = fmtMoney(data.columns?.client?.value_total || 0);
    if ($("kanbanActiveValue")) {
      const act = (data.columns?.contacted?.value_total || 0) + (data.columns?.followup?.value_total || 0);
      $("kanbanActiveValue").textContent = fmtMoney(act);
    }
    ["new", "contacted", "followup", "client"].forEach((stage) => {
      const colData = data.columns[stage] || { items: [], total: 0, value_total: 0 };
      $(`kCount-${stage}`).textContent = colData.total;
      if ($(`kVal-${stage}`)) $(`kVal-${stage}`).textContent = fmtMoney(colData.value_total);
      const box = $(`kCol-${stage}`);
      if (!colData.items.length) {
        box.innerHTML = '<div class="empty-cell" style="padding:18px 8px!important;font-size:12px;">Glissez un contact ici</div>';
        return;
      }
      box.innerHTML = colData.items
        .map(
          (c) => `
          <div class="kanban-card" draggable="true" data-cid="${c.id}">
            <div class="kanban-card-title">${esc(c.full_name || "Sans nom")}</div>
            <div class="kanban-card-email">${esc(c.email)}</div>
            <div class="kanban-card-meta">
              <span>${esc(c.company || c.country || "—")}</span>
              ${scoreBadge(c.lead_score)}
            </div>
            ${Number(c.deal_value) > 0 ? `<div class="kanban-deal-pill">💰 ${fmtMoney(c.deal_value)}</div>` : ""}
          </div>
        `
        )
        .join("");

      box.querySelectorAll(".kanban-card").forEach((card) => {
        card.addEventListener("dragstart", (e) => {
          e.dataTransfer.setData("text/plain", card.dataset.cid);
        });
        card.addEventListener("click", () => openProfileModal(Number(card.dataset.cid)));
      });
    });
  } catch (e) {
    toast(e.message, "error");
  }
}

document.querySelectorAll(".kanban-col").forEach((col) => {
  col.addEventListener("dragover", (e) => { e.preventDefault(); col.classList.add("dragover"); });
  col.addEventListener("dragleave", () => col.classList.remove("dragover"));
  col.addEventListener("drop", async (e) => {
    e.preventDefault();
    col.classList.remove("dragover");
    const cid = Number(e.dataTransfer.getData("text/plain"));
    const stage = col.dataset.stage;
    if (!cid || !stage) return;
    try {
      await api("/api/crm/pipeline/move", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ contact_id: cid, status: stage }),
      });
      toast("Contact déplacé dans le pipeline !", "success");
      loadKanban();
      refreshAll();
    } catch (err) {
      toast(err.message, "error");
    }
  });
});

$("kanbanApplyBtn").addEventListener("click", loadKanban);
$("kanbanSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") loadKanban(); });
$("refreshCrmBtn").addEventListener("click", () => { loadKanban(); loadTasks(); loadSegments(); });

async function loadTasks() {
  try {
    const data = await api("/api/crm/tasks");
    const tbody = $("tasksTbody");
    const today = new Date().toISOString().slice(0, 10);
    if (!data.items.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty-cell">Aucun rappel planifié. Ouvrez une fiche 360° (👁) pour en créer un.</td></tr>';
      return;
    }
    tbody.innerHTML = data.items
      .map((t) => {
        const overdue = !t.done && t.due_date < today;
        const isToday = !t.done && t.due_date === today;
        const dateBadge = overdue
          ? `<span class="badge badge-st-unsubscribed">En retard (${esc(t.due_date)})</span>`
          : isToday
          ? `<span class="badge badge-st-followup">Aujourd'hui</span>`
          : `<span class="badge badge-manual">${esc(t.due_date)}</span>`;
        return `
          <tr style="${t.done ? "opacity:0.55;text-decoration:line-through;" : ""}">
            <td class="col-check"><input type="checkbox" class="task-toggle" data-id="${t.id}" ${t.done ? "checked" : ""} /></td>
            <td><strong>${esc(t.title)}</strong></td>
            <td><a href="#" class="email-link task-open-prof" data-cid="${t.contact_id}">${esc(t.contact_name || t.contact_email || "Contact")}</a></td>
            <td>${dateBadge}</td>
            <td class="col-actions"><button class="icon-btn danger task-del" data-id="${t.id}">🗑</button></td>
          </tr>
        `;
      })
      .join("");

    tbody.querySelectorAll(".task-toggle").forEach((cb) => {
      cb.addEventListener("change", async () => {
        await api(`/api/crm/tasks/${cb.dataset.id}/toggle`, { method: "POST" });
        loadTasks();
      });
    });
    tbody.querySelectorAll(".task-del").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api(`/api/crm/tasks/${btn.dataset.id}`, { method: "DELETE" });
        loadTasks();
      });
    });
    tbody.querySelectorAll(".task-open-prof").forEach((a) => {
      a.addEventListener("click", (e) => { e.preventDefault(); openProfileModal(Number(a.dataset.cid)); });
    });
  } catch (e) {
    toast(e.message, "error");
  }
}

async function loadSegments() {
  try {
    const data = await api("/api/crm/segments");
    const grid = $("segmentsGrid");
    if (!data.items.length) {
      grid.innerHTML = '<div class="empty-cell">Aucun segment dynamique.</div>';
      return;
    }
    grid.innerHTML = data.items
      .map(
        (s) => `
        <div class="segment-card">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
            <strong>${esc(s.name)}</strong>
            <button class="icon-btn danger seg-del-btn" data-id="${s.id}">🗑</button>
          </div>
          <div style="font-size:28px;font-weight:700;color:var(--primary);">${s.count} <span style="font-size:13px;font-weight:500;color:var(--muted);">contact(s)</span></div>
          <div style="display:flex;gap:8px;">
            <button class="btn btn-primary btn-sm seg-open-btn" data-idx="${s.id}">Voir les contacts →</button>
          </div>
        </div>
      `
      )
      .join("");

    grid.querySelectorAll(".seg-del-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api(`/api/crm/segments/${btn.dataset.id}`, { method: "DELETE" });
        loadSegments();
      });
    });
    grid.querySelectorAll(".seg-open-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const seg = data.items.find((x) => String(x.id) === btn.dataset.idx);
        if (!seg) return;
        const f = seg.filters || {};
        ["searchInput", "filterCountry", "filterStatus", "filterEmailType", "filterMx", "filterSource", "filterTag", "filterDomain"].forEach((id) => ($(id).value = ""));
        $("filterMinScore").value = "0";
        nlHasPhone = Boolean(f.has_phone);
        if (f.q) $("searchInput").value = f.q;
        if (f.status) $("filterStatus").value = f.status;
        if (f.email_type) $("filterEmailType").value = f.email_type;
        if (f.mx_status) $("filterMx").value = f.mx_status;
        if (f.source_type) $("filterSource").value = f.source_type;
        if (f.tag) $("filterTag").value = f.tag;
        if (f.domain) $("filterDomain").value = f.domain;
        if (f.country) $("filterCountry").value = f.country;
        if (f.min_score) $("filterMinScore").value = String(f.min_score >= 70 ? 70 : f.min_score >= 50 ? 50 : 30);
        state.page = 1;
        switchTab("contacts");
        loadContacts();
      });
    });
  } catch (e) {
    toast(e.message, "error");
  }
}

// 360 Contact Profile Modal
$("closeProfileBtn").addEventListener("click", () => $("profileModal").classList.add("hidden"));

async function openProfileModal(cid) {
  try {
    const data = await api(`/api/contacts/${cid}/profile`);
    const c = data.contact;
    $("profContactId").value = c.id;
    $("profName").textContent = c.full_name || c.email;
    $("profSub").textContent = [c.email, c.company, c.country, c.phone].filter(Boolean).join(" · ");
    $("profTaskDate").value = new Date(Date.now() + 86400000 * 2).toISOString().slice(0, 10);

    $("profMetaBar").innerHTML =
      `${scoreBadge(c.lead_score)} ${emailTypeBadge(c.email_type)} ${mxBadge(c.mx_status)} ${statusBadge(c.status)}` +
      (c.phone ? ` <button class="btn btn-primary btn-sm" id="profCallBtn" title="Appeler ${esc(c.phone)}">📞 Appeler</button>` : "") +
      (c.phone ? ` <button class="btn btn-secondary btn-sm" id="profWaBtn" title="Ouvrir WhatsApp interne">💬 WhatsApp</button>` : "") +
      (c.website ? ` <a href="${esc(safeUrl(c.website))}" target="_blank" rel="noopener noreferrer" class="badge badge-manual">🌐 ${esc(c.website)}</a>` : "") +
      (c.industry ? ` <span class="badge badge-tag">${esc(c.industry)}</span>` : "");

    const profCallBtn = $("profCallBtn");
    if (profCallBtn) {
      profCallBtn.addEventListener("click", () => callContact(c, { refreshProfile: true }));
    }
    const profWaBtn = $("profWaBtn");
    if (profWaBtn) {
      profWaBtn.addEventListener("click", () => {
        $("profileModal").classList.add("hidden");
        openWhatsAppForContact(c);
      });
    }

    const items = [];
    (data.tasks || []).forEach((t) => {
      items.push({
        date: t.created_at,
        html: `<span>⏰ <strong>Rappel (${esc(t.due_date)}) :</strong> ${esc(t.title)} ${t.done ? "✅" : ""}</span><span class="subtitle">${esc(t.created_at.slice(0, 10))}</span>`,
      });
    });
    (data.activities || []).forEach((a) => {
      const icon = a.kind === "call" ? "📞" : a.kind === "meeting" ? "🤝" : a.kind === "status_change" ? "📌" : "📝";
      items.push({
        date: a.created_at,
        html: `<span>${icon} ${esc(a.content)}</span><span class="subtitle">${esc(a.created_at.replace("T", " "))}</span>`,
      });
    });
    (data.emails || []).forEach((em) => {
      items.push({
        date: em.sent_at,
        html: `<span>✉️ <strong>Email :</strong> ${esc(em.subject)} ${em.opened_count > 0 ? `(👁 ${em.opened_count})` : ""}</span><span class="subtitle">${esc(em.sent_at.replace("T", " "))}</span>`,
      });
    });
    items.sort((a, b) => (b.date || "").localeCompare(a.date || ""));

    $("profTimeline").innerHTML = items.length
      ? items.map((i) => `<div class="timeline-item">${i.html}</div>`).join("")
      : '<div class="empty-cell">Aucune activité enregistrée pour ce contact.</div>';

    $("profileModal").classList.remove("hidden");
  } catch (e) {
    toast(e.message, "error");
  }
}

$("profAddActBtn").addEventListener("click", async () => {
  const cid = Number($("profContactId").value);
  const content = $("profActText").value.trim();
  if (!content) return;
  await api(`/api/contacts/${cid}/activities`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: $("profActKind").value, content }),
  });
  $("profActText").value = "";
  toast("Activité ajoutée", "success");
  openProfileModal(cid);
});

$("profAddTaskBtn").addEventListener("click", async () => {
  const cid = Number($("profContactId").value);
  const title = $("profTaskTitle").value.trim();
  if (!title) return;
  await api("/api/crm/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ contact_id: cid, title, due_date: $("profTaskDate").value }),
  });
  $("profTaskTitle").value = "";
  toast("Rappel de relance créé !", "success");
  openProfileModal(cid);
  loadTasks();
});

// ---------------- HISTORY ----------------

async function loadHistory() {
  try {
    const data = await api("/api/imports");
    const tbody = $("historyTbody");
    if (!data.items.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty-cell">Aucun import enregistré pour le moment.</td></tr>';
      return;
    }
    tbody.innerHTML = data.items
      .map(
        (h) => `
        <tr>
          <td>${esc(h.imported_at.replace("T", " "))}</td>
          <td>${sourceBadge(h.source_type)}</td>
          <td><strong>${esc(h.source_name)}</strong></td>
          <td>${h.total_found}</td>
          <td style="color:#15803d;font-weight:600">+${h.inserted}</td>
          <td style="color:#4f46e5;font-weight:600">${h.updated}</td>
          <td>${h.skipped}</td>
        </tr>
      `
      )
      .join("");
  } catch (e) {
    toast(e.message, "error");
  }
}
$("refreshHistoryBtn").addEventListener("click", loadHistory);

// ---------------- PACK CONVERSION: WHATSAPP, TRACKED LINKS & LEAD FORMS ----------------

let waTemplatesCache = [];

function renderWaMessage(tpl, contact) {
  const first = contact.first_name || (contact.full_name || "").split(" ")[0] || "";
  return (tpl || "")
    .replace(/\{prenom\}|\{prénom\}/gi, first)
    .replace(/\{nom\}/gi, contact.last_name || "")
    .replace(/\{entreprise\}/gi, contact.company || "votre entreprise");
}

function cleanWaPhone(phone) {
  let d = String(phone || "").replace(/\D+/g, "");
  if (d.startsWith("00")) d = d.slice(2);
  return d;
}

function cleanTelPhone(phone) {
  const raw = String(phone || "").trim();
  const hasPlus = raw.startsWith("+");
  const digits = raw.replace(/\D+/g, "");
  if (!digits) return "";
  return hasPlus ? `+${digits}` : digits;
}

async function callContact(contact, opts = {}) {
  const tel = cleanTelPhone(contact.phone);
  if (!tel) return toast("Numéro de téléphone invalide.", "error");
  const link = document.createElement("a");
  link.href = `tel:${tel}`;
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
  try {
    await api("/api/call/log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contact_id: contact.id, phone: tel }),
    });
    toast(`Appel lancé & enregistré pour ${contact.full_name || contact.email} (${tel})`, "success");
    refreshAll();
    if (opts.refreshProfile) openProfileModal(contact.id);
  } catch {}
}

// ---------------- INTERNAL WHATSAPP INBOX (QR BRIDGE) ----------------
let waActiveChat = null;
let waPollTimer = null;
let waPollingActive = false;
let waLastState = "offline";
let waConversationsCache = [];

function stopWaPolling() {
  waPollingActive = false;
  if (waPollTimer) {
    clearTimeout(waPollTimer);
    waPollTimer = null;
  }
}

function initWaInbox() {
  stopWaPolling();
  waPollingActive = true;
  const tick = async () => {
    if (!waPollingActive) return;
    if (!document.hidden) {
      await Promise.allSettled([
        refreshWaStatus(true),
        loadWaConversations(true),
        waActiveChat ? loadWaMessages(waActiveChat, true) : Promise.resolve(),
      ]);
    }
    if (waPollingActive) {
      waPollTimer = setTimeout(tick, 4500);
    }
  };
  tick();
}

async function refreshWaStatus(silent = false) {
  try {
    const st = await api("/api/wa/status");
    waLastState = st.state || "offline";
    const pill = $("waStatusPill");
    const meta = $("waStatusMeta");
    const qrCard = $("waQrCard");
    const qrImg = $("waQrImg");
    const connectBtn = $("waConnectBtn");
    const syncBtn = $("waSyncBtn");
    const logoutBtn = $("waLogoutBtn");
    if (!pill) return;

    const dbCounts = st.db_counts || { contacts: 0, messages: 0 };
    if (st.state === "connected") {
      pill.className = "wa-status-pill connected";
      pill.textContent = `🟢 Connecté ${st.me?.phone || ""}`;
      meta.textContent = `${st.me?.name ? st.me.name + " · " : ""}${dbCounts.contacts} contact(s) WA · ${dbCounts.messages} message(s)`;
      qrCard.classList.add("hidden");
      connectBtn.classList.add("hidden");
      syncBtn.classList.remove("hidden");
      logoutBtn.classList.remove("hidden");
    } else if (st.state === "qr" && st.qr) {
      pill.className = "wa-status-pill qr";
      pill.textContent = "🟡 Scannez le QR Code";
      meta.textContent = "En attente de validation depuis votre téléphone…";
      qrImg.src = st.qr;
      qrCard.classList.remove("hidden");
      connectBtn.classList.add("hidden");
      syncBtn.classList.add("hidden");
      logoutBtn.classList.add("hidden");
    } else if (st.state === "connecting") {
      pill.className = "wa-status-pill qr";
      pill.textContent = "🟡 Connexion en cours…";
      meta.textContent = "Génération du QR Code ou reconnexion…";
      connectBtn.classList.add("hidden");
      syncBtn.classList.add("hidden");
      logoutBtn.classList.add("hidden");
    } else {
      pill.className = "wa-status-pill offline";
      pill.textContent = "⚪ Non connecté";
      meta.textContent = st.lastError || st.error || `${dbCounts.contacts} contact(s) WA · ${dbCounts.messages} message(s) en archive`;
      qrCard.classList.add("hidden");
      connectBtn.classList.remove("hidden");
      syncBtn.classList.add("hidden");
      logoutBtn.classList.add("hidden");
    }
  } catch (e) {
    if (!silent) toast(e.message, "error");
  }
}

async function loadWaConversations(silent = false) {
  try {
    const q = ($("waConvSearch")?.value || "").trim();
    const data = await api(`/api/wa/conversations?q=${encodeURIComponent(q)}`);
    waConversationsCache = data.items || [];
    const box = $("waConvList");
    if (!box) return;
    if (!waConversationsCache.length) {
      box.innerHTML = '<div class="empty-cell" style="padding:24px 12px;">Aucune discussion synchronisée.<br/>Connectez WhatsApp ou cliquez sur 💬 sur un contact.</div>';
      return;
    }
    box.innerHTML = waConversationsCache
      .map((conv) => {
        const isActive = waActiveChat && (waActiveChat.jid === conv.jid || (waActiveChat.phone && waActiveChat.phone === conv.phone));
        const timeStr = (conv.last_sent_at || "").slice(11, 16) || (conv.last_sent_at || "").slice(0, 10);
        const crmBadge = conv.contact_id ? `<span class="badge badge-pro" style="font-size:10px;padding:1px 5px;">CRM</span>` : "";
        return `
          <div class="wa-conv-item ${isActive ? "active" : ""}" data-jid="${esc(conv.jid)}">
            <div class="wa-conv-top">
              <span class="wa-conv-name">${esc(conv.display_name)} ${crmBadge}</span>
              <span class="wa-conv-time">${esc(timeStr)}</span>
            </div>
            <div class="wa-conv-preview">${conv.last_from_me ? "Vous : " : ""}${esc(conv.last_body || conv.phone)}</div>
          </div>
        `;
      })
      .join("");
    box.querySelectorAll(".wa-conv-item").forEach((el) => {
      el.addEventListener("click", () => {
        const conv = waConversationsCache.find((x) => x.jid === el.dataset.jid);
        if (conv) openInternalWaChat(conv);
      });
    });
  } catch (e) {
    if (!silent) toast(e.message, "error");
  }
}

async function loadWaMessages(chat, silent = false) {
  if (!chat) return;
  try {
    const params = new URLSearchParams();
    if (chat.jid) params.set("jid", chat.jid);
    if (chat.phone) params.set("phone", chat.phone);
    if (chat.contact_id) params.set("contact_id", String(chat.contact_id));
    const data = await api(`/api/wa/messages?${params.toString()}`);
    const msgs = data.items || [];
    const box = $("waChatMessages");
    if (!box) return;
    const wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    if (!msgs.length) {
      box.innerHTML = `
        <div class="wa-empty-chat">
          <div style="font-size:26px;margin-bottom:6px;">💬</div>
          <div><strong>${esc(chat.display_name || chat.phone)}</strong></div>
          <div class="subtitle" style="margin-top:4px;">Aucun message synchronisé pour ce contact. Écrivez votre premier message ci-dessous !</div>
        </div>
      `;
      return;
    }
    box.innerHTML = msgs
      .map((m) => {
        const dt = (m.sent_at || "").replace("T", " ").slice(0, 16);
        return `
          <div class="wa-bubble ${m.from_me ? "out" : "in"}">
            <div>${esc(m.body)}</div>
            <div class="wa-bubble-meta">${esc(dt)} ${m.from_me ? "✓" : ""}</div>
          </div>
        `;
      })
      .join("");
    if (!silent || wasAtBottom) {
      box.scrollTop = box.scrollHeight;
    }
  } catch {}
}

function openInternalWaChat(chat, prefillText = "") {
  const digits = cleanWaPhone(chat.phone || "");
  waActiveChat = {
    jid: chat.jid || (digits ? `${digits}@s.whatsapp.net` : ""),
    phone: chat.phone || (digits ? `+${digits}` : ""),
    contact_id: chat.contact_id || chat.id || null,
    display_name: chat.display_name || chat.full_name || chat.crm_name || chat.phone || "Contact",
    first_name: chat.first_name || "",
    last_name: chat.last_name || "",
    full_name: chat.full_name || chat.display_name || "",
    company: chat.company || chat.crm_company || "",
    email: chat.email || chat.crm_email || "",
  };
  $("waChatTitle").textContent = waActiveChat.display_name;
  $("waChatSub").textContent = [waActiveChat.phone, waActiveChat.company, waActiveChat.email].filter(Boolean).join(" · ");

  const actions = $("waChatHeaderActions");
  actions.innerHTML = `
    ${waActiveChat.phone ? `<button class="btn btn-secondary btn-sm" id="waHeadCallBtn">📞 Appeler</button>` : ""}
    ${waActiveChat.contact_id ? `<button class="btn btn-secondary btn-sm" id="waHeadSumBtn" title="Résumer cette discussion dans la Fiche 360°">📋 Résumer</button>` : ""}
    ${waActiveChat.contact_id ? `<button class="btn btn-secondary btn-sm" id="waHeadProfBtn">👁 Fiche 360°</button>` : ""}
  `;
  const sumBtn = $("waHeadSumBtn");
  if (sumBtn) {
    sumBtn.addEventListener("click", async () => {
      try {
        await api("/api/ai/summarize-wa", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            contact_id: waActiveChat.contact_id,
            phone: waActiveChat.phone,
            jid: waActiveChat.jid,
            contact_name: waActiveChat.display_name,
          }),
        });
        toast("Résumé IA enregistré dans la Fiche 360° !", "success");
      } catch (e) {
        toast(e.message, "error");
      }
    });
  }
  const callBtn = $("waHeadCallBtn");
  if (callBtn) {
    callBtn.addEventListener("click", () => {
      if (waActiveChat.contact_id) {
        callContact({ id: waActiveChat.contact_id, phone: waActiveChat.phone, full_name: waActiveChat.display_name, email: waActiveChat.email });
      } else {
        const tel = cleanTelPhone(waActiveChat.phone);
        if (tel) window.location.href = `tel:${tel}`;
      }
    });
  }
  const profBtn = $("waHeadProfBtn");
  if (profBtn) {
    profBtn.addEventListener("click", () => openProfileModal(waActiveChat.contact_id));
  }

  if (prefillText) {
    $("waComposerInput").value = prefillText;
  }
  loadWaConversations(true);
  loadWaMessages(waActiveChat);
  $("waComposerInput").focus();
}

async function openWhatsAppForContact(contact, customTpl = null) {
  const digits = cleanWaPhone(contact.phone);
  if (!digits) return toast("Numéro de téléphone invalide pour WhatsApp.", "error");
  // Switch to Conversion -> WhatsApp Interne without opening any external browser tab
  switchTab("conversion");
  const convTab = $("tab-conversion");
  convTab.querySelectorAll(".subtab").forEach((b) => b.classList.toggle("active", b.dataset.subtab === "conv-wa"));
  convTab.querySelectorAll(".subtab-panel").forEach((p) => p.classList.toggle("active", p.id === "subtab-conv-wa"));
  await loadWaTemplates();
  const tpl = customTpl ?? ($("waTplBody")?.value || "Bonjour {prenom} 👋");
  const msg = renderWaMessage(tpl, contact);
  openInternalWaChat(
    {
      contact_id: contact.id,
      phone: contact.phone,
      display_name: contact.full_name || contact.email || contact.phone,
      first_name: contact.first_name,
      last_name: contact.last_name,
      full_name: contact.full_name,
      company: contact.company,
      email: contact.email,
    },
    msg
  );
  $("waInbox")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function sendInternalWaMessage() {
  if (!waActiveChat) return toast("Sélectionnez d'abord une conversation.", "error");
  const input = $("waComposerInput");
  const text = input.value.trim();
  if (!text) return;
  const btn = $("waSendMsgBtn");
  btn.disabled = true;
  try {
    await api("/api/wa/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        contact_id: waActiveChat.contact_id,
        phone: waActiveChat.phone,
        jid: waActiveChat.jid,
        text,
      }),
    });
    input.value = "";
    await loadWaMessages(waActiveChat);
    await loadWaConversations(true);
    toast("Message WhatsApp envoyé !", "success");
  } catch (e) {
    toast(e.message, "error");
    if (waLastState !== "connected") {
      refreshWaStatus();
    }
  } finally {
    btn.disabled = false;
  }
}

$("waSendMsgBtn")?.addEventListener("click", sendInternalWaMessage);
$("waComposerInput")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendInternalWaMessage();
  }
});
$("waQuickTplSelect")?.addEventListener("change", (e) => {
  const t = waTemplatesCache.find((x) => String(x.id) === e.target.value);
  if (t && waActiveChat) {
    $("waComposerInput").value = renderWaMessage(t.body, waActiveChat);
    $("waComposerInput").focus();
  } else if (t) {
    $("waComposerInput").value = t.body;
  }
  e.target.value = "";
});
$("waConvSearch")?.addEventListener("input", () => loadWaConversations(true));
$("waConnectBtn")?.addEventListener("click", async () => {
  try {
    $("waStatusPill").className = "wa-status-pill qr";
    $("waStatusPill").textContent = "🟡 Démarrage du pont WhatsApp…";
    await api("/api/wa/connect", { method: "POST" });
    setTimeout(() => refreshWaStatus(), 1200);
    setTimeout(() => refreshWaStatus(), 3000);
  } catch (e) {
    toast(e.message, "error");
  }
});
$("waLogoutBtn")?.addEventListener("click", async () => {
  if (!confirm("Déconnecter votre session WhatsApp de ContactsHub ?")) return;
  await api("/api/wa/logout", { method: "POST" });
  toast("Session WhatsApp déconnectée.", "info");
  refreshWaStatus();
});
$("waSyncBtn")?.addEventListener("click", async () => {
  try {
    const res = await api("/api/wa/sync", { method: "POST" });
    toast(`Synchronisé : ${res.contacts || 0} contact(s), ${res.messages || 0} message(s)`, "success");
    loadWaConversations();
    refreshWaStatus(true);
  } catch (e) {
    toast(e.message, "error");
  }
});
$("waImportContactsBtn")?.addEventListener("click", async () => {
  try {
    const res = await api("/api/wa/import-contacts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_id: state.workspaceId || 1 }),
    });
    toast(`Import WhatsApp terminé : ${res.inserted} ajouté(s), ${res.linked} relié(s) au CRM !`, "success");
    refreshAll();
    loadWaConversations(true);
  } catch (e) {
    toast(e.message, "error");
  }
});

async function loadWaTemplates() {
  try {
    const data = await api("/api/whatsapp/templates");
    waTemplatesCache = data.items || [];
    $("waTemplateSelect").innerHTML = waTemplatesCache
      .map((t) => `<option value="${t.id}">${esc(t.name)}</option>`)
      .join("");
    const quickSel = $("waQuickTplSelect");
    if (quickSel) {
      quickSel.innerHTML =
        '<option value="">✨ Modèle…</option>' +
        waTemplatesCache.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
    }
    if (waTemplatesCache.length) {
      $("waTplName").value = waTemplatesCache[0].name;
      $("waTplBody").value = waTemplatesCache[0].body;
    }
  } catch {}
}

$("waTemplateSelect").addEventListener("change", (e) => {
  const t = waTemplatesCache.find((x) => String(x.id) === e.target.value);
  if (t) {
    $("waTplName").value = t.name;
    $("waTplBody").value = t.body;
  }
});

$("saveWaTplBtn").addEventListener("click", async () => {
  try {
    await api("/api/whatsapp/templates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("waTplName").value.trim(), body: $("waTplBody").value.trim() }),
    });
    toast("Modèle WhatsApp enregistré !", "success");
    loadWaTemplates();
  } catch (e) {
    toast(e.message, "error");
  }
});

$("delWaTplBtn").addEventListener("click", async () => {
  const id = $("waTemplateSelect").value;
  if (!id || !confirm("Supprimer ce modèle WhatsApp ?")) return;
  await api(`/api/whatsapp/templates/${id}`, { method: "DELETE" });
  loadWaTemplates();
});

async function loadHotSignals() {
  try {
    const data = await api("/api/conversion/hot-signals");
    const ready = data.whatsapp_ready || [];
    const wTbody = $("waReadyTbody");
    wTbody.innerHTML = ready.length
      ? ready
          .map(
            (c) => `
          <tr>
            <td><strong>${esc(c.full_name || c.email)}</strong><div class="contact-role">${esc(c.company || c.country || "")}</div></td>
            <td>${esc(c.phone)}</td>
            <td>${scoreBadge(c.lead_score)}</td>
            <td class="col-actions"><button class="btn btn-primary btn-sm wa-send-one" data-id="${c.id}">💬 WhatsApp</button></td>
          </tr>
        `
          )
          .join("")
      : '<tr><td colspan="4" class="empty-cell">Aucun contact avec numéro de téléphone.</td></tr>';

    wTbody.querySelectorAll(".wa-send-one").forEach((btn) => {
      btn.addEventListener("click", () => {
        const c = ready.find((x) => x.id === Number(btn.dataset.id));
        if (c) openWhatsAppForContact(c);
      });
    });

    const sigs = data.signals || [];
    $("hotSignalsList").innerHTML = sigs.length
      ? sigs
          .map(
            (s) => `
          <div class="timeline-item">
            <div>
              <strong>${esc(s.full_name || s.email)}</strong> ${scoreBadge(s.lead_score)}
              <div class="subtitle" style="font-size:12px;">${esc(s.signal_text)}</div>
            </div>
            <div style="display:flex;gap:4px;align-items:center;">
              <button class="icon-btn sig-prof-btn" data-id="${s.id}" title="Fiche 360°">👁</button>
            </div>
          </div>
        `
          )
          .join("")
      : '<div class="empty-cell">Partagez une page de capture ou un lien tracké pour voir vos prospects chauds apparaître ici !</div>';

    $("hotSignalsList").querySelectorAll(".sig-prof-btn").forEach((btn) => {
      btn.addEventListener("click", () => openProfileModal(Number(btn.dataset.id)));
    });
  } catch {}
}

async function loadTrackedLinks() {
  const data = await api("/api/links");
  const tbody = $("linksTbody");
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-cell">Aucun lien tracké créé.</td></tr>';
    return;
  }
  tbody.innerHTML = data.items
    .map(
      (l) => `
      <tr>
        <td><strong>${esc(l.title)}</strong></td>
        <td><a href="${esc(l.short_url)}" target="_blank" class="email-link">${esc(l.short_url)}</a></td>
        <td><span class="badge badge-st-client">${l.clicks_count}</span></td>
        <td class="col-actions"><button class="icon-btn danger del-link-btn" data-id="${l.id}">🗑</button></td>
      </tr>
    `
    )
    .join("");
  tbody.querySelectorAll(".del-link-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/links/${btn.dataset.id}`, { method: "DELETE" });
      loadTrackedLinks();
    });
  });
}

$("createLinkBtn").addEventListener("click", async () => {
  try {
    const res = await api("/api/links", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: $("linkTitleInput").value.trim(), target_url: $("linkUrlInput").value.trim() }),
    });
    $("linkTitleInput").value = "";
    $("linkUrlInput").value = "";
    navigator.clipboard.writeText(res.short_url).catch(() => {});
    toast(`Lien tracké créé et copié : ${res.short_url}`, "success");
    loadTrackedLinks();
  } catch (e) {
    toast(e.message, "error");
  }
});

async function loadLeadForms() {
  const data = await api("/api/forms");
  const tbody = $("formsTbody");
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-cell">Aucune page de capture.</td></tr>';
    return;
  }
  tbody.innerHTML = data.items
    .map(
      (f) => `
      <tr>
        <td><strong>${esc(f.title)}</strong></td>
        <td><a href="${esc(f.public_url)}" target="_blank" class="email-link">${esc(f.public_url)}</a></td>
        <td><span class="badge badge-tag">${esc(f.tag)}</span></td>
        <td><strong>${f.submissions_count}</strong></td>
        <td class="col-actions">
          <button class="icon-btn copy-form-btn" data-url="${esc(f.public_url)}" title="Copier le lien">📋</button>
          <button class="icon-btn danger del-form-btn" data-id="${f.id}" title="Supprimer">🗑</button>
        </td>
      </tr>
    `
    )
    .join("");
  tbody.querySelectorAll(".copy-form-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      navigator.clipboard.writeText(btn.dataset.url);
      toast("Lien de la page copié !", "success");
    });
  });
  tbody.querySelectorAll(".del-form-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Supprimer cette page de capture ?")) return;
      await api(`/api/forms/${btn.dataset.id}`, { method: "DELETE" });
      loadLeadForms();
    });
  });
}

$("createFormBtn").addEventListener("click", async () => {
  try {
    const res = await api("/api/forms", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: $("formTitleInput").value.trim(),
        slug: $("formSlugInput").value.trim(),
        subtitle: $("formSubInput").value.trim(),
        cta_text: $("formCtaInput").value.trim(),
        tag: $("formTagInput").value.trim(),
      }),
    });
    $("formTitleInput").value = "";
    $("formSlugInput").value = "";
    $("formSubInput").value = "";
    toast(`Page créée : ${res.public_url}`, "success");
    loadLeadForms();
  } catch (e) {
    toast(e.message, "error");
  }
});

$("refreshConvBtn").addEventListener("click", () => {
  loadWaTemplates();
  loadHotSignals();
  loadTrackedLinks();
  loadLeadForms();
});

// ---------------- B2B PROSPECTING, CLOSING LINKS & COPY GENERATOR ----------------

let lastB2bProspects = [];

$("runB2bCrawlBtn").addEventListener("click", async () => {
  const urls = $("b2bUrlsInput").value.trim();
  if (!urls) return toast("Collez au moins un site web d'entreprise.", "error");
  $("runB2bCrawlBtn").disabled = true;
  $("runB2bCrawlBtn").textContent = "Scan des sites en cours…";
  $("b2bResultsTbody").innerHTML = '<tr><td colspan="4" class="empty-cell">Visite des sites & extraction des contacts…</td></tr>';
  try {
    const res = await api("/api/prospecting/crawl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls }),
    });
    lastB2bProspects = res.prospects || [];
    const impBtn = $("importB2bBtn");
    impBtn.classList.toggle("hidden", lastB2bProspects.length === 0);
    impBtn.textContent = `+ Importer tous (${lastB2bProspects.length})`;

    $("b2bResultsTbody").innerHTML = lastB2bProspects.length
      ? lastB2bProspects
          .map(
            (p) => `
          <tr>
            <td><strong>${esc(p.email)}</strong></td>
            <td>${esc(p.company || "—")}</td>
            <td>${esc(p.phone || "—")}</td>
            <td>${p.industry ? `<span class="badge badge-tag">${esc(p.industry)}</span>` : "—"}</td>
          </tr>
        `
          )
          .join("")
      : `<tr><td colspan="4" class="empty-cell">${res.sites_scanned} site(s) scanné(s), aucun email public détecté.</td></tr>`;
    toast(`${lastB2bProspects.length} prospect(s) extrait(s) sur ${res.sites_scanned} site(s) !`, "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    $("runB2bCrawlBtn").disabled = false;
    $("runB2bCrawlBtn").textContent = "🔍 Extraire les prospects";
  }
});

$("importB2bBtn").addEventListener("click", async () => {
  if (!lastB2bProspects.length) return;
  $("importB2bBtn").disabled = true;
  try {
    const res = await api("/api/prospecting/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prospects: lastB2bProspects, tag: $("b2bTagInput").value.trim() }),
    });
    toast(`Import B2B terminé : +${res.inserted} nouveaux, ${res.updated} mis à jour (enrichis & scorés) !`, "success");
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    $("importB2bBtn").disabled = false;
  }
});

async function loadClosingSettings() {
  try {
    const data = await api("/api/closing/settings");
    $("closeBizName").value = data.business_name || "";
    $("closeBookingUrl").value = data.booking_url || "";
    $("closePaymentUrl").value = data.payment_url || "";
    $("paymentWebhookDisplay").textContent = `POST ${data.payment_webhook_url}  (Header X-API-Key: ${data.webhook_token})`;
  } catch {}
}

$("saveClosingBtn").addEventListener("click", async () => {
  await api("/api/closing/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      business_name: $("closeBizName").value.trim(),
      booking_url: $("closeBookingUrl").value.trim(),
      payment_url: $("closePaymentUrl").value.trim(),
    }),
  });
  toast("Liens de rendez-vous et paiement enregistrés !", "success");
});

$("genCopyBtn").addEventListener("click", async () => {
  const res = await api("/api/closing/generate-copy", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      offer_type: $("copyOfferSelect").value,
      channel: $("copyChannelSelect").value,
    }),
  });
  $("genCopySubj").value = res.subject || "";
  $("genCopyBody").value = res.body || "";
  toast("Script de vente généré !", "success");
});

$("saveGenCopyBtn").addEventListener("click", async () => {
  const subj = $("genCopySubj").value.trim();
  const body = $("genCopyBody").value.trim();
  if (!body) return toast("Générez d'abord un script.", "error");
  const channel = $("copyChannelSelect").value;
  if (channel === "whatsapp") {
    await api("/api/whatsapp/templates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: subj || "Script WhatsApp généré", body }),
    });
    toast("Enregistré dans vos modèles WhatsApp !", "success");
    loadWaTemplates();
  } else {
    await api("/api/templates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: subj || "Script Email généré", subject: subj, body }),
    });
    toast("Enregistré dans vos modèles Email !", "success");
  }
});

// ---------------- TRAINING FORMS BUILDER & SUBMISSIONS ----------------

let trainingFormsCache = [];

function linesToArray(val) {
  return String(val || "")
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

async function loadTrainingForms() {
  try {
    const data = await api("/api/forms");
    trainingFormsCache = data.items || [];
    const tbody = $("tfListTbody");
    if (!trainingFormsCache.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty-cell">Aucun formulaire créé.</td></tr>';
      return;
    }
    tbody.innerHTML = trainingFormsCache
      .map(
        (f) => `
        <tr>
          <td>
            <strong>${esc(f.title)}</strong>
            ${f.config?.org_name ? `<div class="contact-role">${esc(f.config.org_name)} ${f.config.badge ? "· " + esc(f.config.badge) : ""}</div>` : ""}
          </td>
          <td><span class="badge ${f.form_kind === "training" ? "badge-pro" : "badge-perso"}">${f.form_kind === "training" ? "🎓 Formation A4" : "📝 Capture"}</span></td>
          <td><a href="${esc(f.public_url)}" target="_blank" class="email-link">${esc(f.public_url)}</a></td>
          <td><span class="badge badge-tag">${esc(f.tag)}</span></td>
          <td><strong>${f.submissions_count}</strong></td>
          <td class="col-actions">
            <button class="icon-btn tf-subs-btn" data-id="${f.id}" title="Voir les inscrits">👥</button>
            <button class="icon-btn tf-copy-btn" data-url="${esc(f.public_url)}" title="Copier le lien">📋</button>
            ${f.form_kind === "training" ? `<button class="icon-btn tf-edit-btn" data-id="${f.id}" title="Modifier">✎</button>` : ""}
            <button class="icon-btn tf-dup-btn" data-id="${f.id}" title="Dupliquer pour une nouvelle session">📄</button>
            <button class="icon-btn danger tf-del-btn" data-id="${f.id}" title="Supprimer">🗑</button>
          </td>
        </tr>
      `
      )
      .join("");

    tbody.querySelectorAll(".tf-copy-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        navigator.clipboard.writeText(btn.dataset.url);
        toast("Lien du formulaire copié !", "success");
      });
    });
    tbody.querySelectorAll(".tf-subs-btn").forEach((btn) => {
      btn.addEventListener("click", () => loadFormSubmissions(Number(btn.dataset.id)));
    });
    tbody.querySelectorAll(".tf-edit-btn").forEach((btn) => {
      btn.addEventListener("click", () => openEditTrainingForm(Number(btn.dataset.id)));
    });
    tbody.querySelectorAll(".tf-dup-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api(`/api/forms/${btn.dataset.id}/duplicate`, { method: "POST" });
        toast("Formulaire dupliqué ! Modifiez ses dates/tarifs pour votre nouvelle session.", "success");
        loadTrainingForms();
      });
    });
    tbody.querySelectorAll(".tf-del-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Supprimer ce formulaire et ses inscriptions archivées ?")) return;
        await api(`/api/forms/${btn.dataset.id}`, { method: "DELETE" });
        toast("Formulaire supprimé", "success");
        loadTrainingForms();
      });
    });
  } catch (e) {
    toast(e.message, "error");
  }
}

function openNewTrainingForm() {
  $("tfEditId").value = "";
  $("tfBuilderTitle").textContent = "Créer un nouveau formulaire de formation";
  $("tfSlug").disabled = false;
  $("tfTitle").value = "";
  $("tfSlug").value = "";
  $("tfBadge").value = "";
  $("tfSubtitle").value = "Formulaire officiel d'inscription à la formation.";
  $("tfBuilderCard").classList.remove("hidden");
}

function openEditTrainingForm(fid) {
  const f = trainingFormsCache.find((x) => x.id === fid);
  if (!f) return;
  const c = f.config || {};
  $("tfEditId").value = f.id;
  $("tfBuilderTitle").textContent = `Modifier : ${f.title}`;
  $("tfSlug").value = f.slug;
  $("tfSlug").disabled = true;
  $("tfOrgName").value = c.org_name || "MSF'S English Learning Academy";
  $("tfTitle").value = f.title || "";
  $("tfBadge").value = c.badge || "";
  $("tfLocation").value = c.location || "";
  $("tfWhatsapp").value = c.whatsapp_number || "";
  $("tfTuition").value = c.tuition_fee || "";
  $("tfRegFee").value = c.registration_fee || "";
  $("tfTotalFee").value = c.total_fee || "";
  $("tfPrefix").value = c.dossier_prefix || "MSF-2026";
  $("tfColor").value = c.primary_color || "#0077e6";
  $("tfTag").value = f.tag || "";
  $("tfSubtitle").value = f.subtitle || "";
  $("tfSessions").value = (c.sessions || []).join("\n");
  $("tfLevels").value = (c.levels || []).join("\n");
  $("tfPayMethods").value = (c.payment_methods || []).join("\n");
  $("tfCustomFields").value = (c.custom_fields || []).join("\n");
  $("tfBuilderCard").classList.remove("hidden");
  $("tfBuilderCard").scrollIntoView({ behavior: "smooth" });
}

$("newTrainingFormBtn").addEventListener("click", openNewTrainingForm);
$("closeTfBuilderBtn").addEventListener("click", () => $("tfBuilderCard").classList.add("hidden"));
$("cancelTfBtn").addEventListener("click", () => $("tfBuilderCard").classList.add("hidden"));
$("refreshTfBtn").addEventListener("click", loadTrainingForms);

$("saveTfBtn").addEventListener("click", async () => {
  const editId = $("tfEditId").value;
  const payload = {
    title: $("tfTitle").value.trim(),
    slug: $("tfSlug").value.trim(),
    subtitle: $("tfSubtitle").value.trim(),
    tag: $("tfTag").value.trim() || "Inscrit Formation",
    cta_text: "✅ Valider mon inscription",
    form_kind: "training",
    config: {
      org_name: $("tfOrgName").value.trim() || "MSF'S English Learning Academy",
      badge: $("tfBadge").value.trim(),
      location: $("tfLocation").value.trim(),
      whatsapp_number: $("tfWhatsapp").value.trim(),
      tuition_fee: $("tfTuition").value.trim(),
      registration_fee: $("tfRegFee").value.trim(),
      total_fee: $("tfTotalFee").value.trim(),
      dossier_prefix: $("tfPrefix").value.trim() || "MSF",
      primary_color: $("tfColor").value || "#0077e6",
      sessions: linesToArray($("tfSessions").value),
      levels: linesToArray($("tfLevels").value),
      objectives: [
        "Communication & fluidité à l'oral",
        "Carrière & Anglais professionnel",
        "Préparation Examens (IELTS / TOEFL / TOEIC)",
        "Voyage & Études à l'étranger",
      ],
      payment_methods: linesToArray($("tfPayMethods").value),
      custom_fields: linesToArray($("tfCustomFields").value),
    },
  };
  try {
    if (editId) {
      await api(`/api/forms/${editId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toast("Formulaire mis à jour !", "success");
    } else {
      const res = await api("/api/forms", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toast(`Formulaire créé : ${res.public_url}`, "success");
    }
    $("tfBuilderCard").classList.add("hidden");
    loadTrainingForms();
  } catch (e) {
    toast(e.message, "error");
  }
});

async function loadFormSubmissions(fid) {
  const f = trainingFormsCache.find((x) => x.id === fid);
  $("tfSubTitle").textContent = `Inscrits : ${f ? f.title : "#" + fid}`;
  $("tfExportSubsBtn").href = `/api/forms/${fid}/submissions/export`;
  $("tfSubmissionsCard").classList.remove("hidden");

  const data = await api(`/api/forms/${fid}/submissions`);
  const items = data.items || [];
  $("tfSubCount").textContent = `${items.length} inscrit(s)`;
  $("tfSubsTbody").innerHTML = items.length
    ? items
        .map(
          (s) => `
        <tr>
          <td><code>${esc(s.dossier_num || "—")}</code></td>
          <td><strong>${esc(s.full_name || "—")}</strong></td>
          <td><div>${esc(s.email)}</div><div class="contact-role">${esc(s.phone || "")}</div></td>
          <td><div>${esc(s.session_choice || "—")}</div><div class="contact-role">${esc(s.level_choice || "")}</div></td>
          <td><div>${esc(s.payment_plan || "—")}</div><div class="contact-role">${[s.payment_method, s.payment_ref].filter(Boolean).map(esc).join(" · ")}</div></td>
          <td>${esc((s.submitted_at || "").replace("T", " "))}</td>
        </tr>
      `
        )
        .join("")
    : '<tr><td colspan="6" class="empty-cell">Aucune inscription enregistrée pour ce formulaire.</td></tr>';
  $("tfSubmissionsCard").scrollIntoView({ behavior: "smooth" });
}

// ---------------- ADMIN, SECURITY, GDPR & AUDIT ----------------

async function loadAdmin() {
  try {
    const data = await api("/api/admin/status");
    $("authEnabledToggle").checked = Boolean(data.auth_enabled);
    $("logoutBtn").classList.toggle("hidden", !data.auth_enabled);

    const uTbody = $("usersTbody");
    if (!data.users.length) {
      uTbody.innerHTML = '<tr><td colspan="4" class="empty-cell">Aucun compte créé (accès local libre).</td></tr>';
    } else {
      uTbody.innerHTML = data.users
        .map(
          (u) => `
          <tr>
            <td><strong>${esc(u.username)}</strong></td>
            <td>${esc(u.display_name)}</td>
            <td><span class="badge badge-tag">${esc(u.role)}</span></td>
            <td class="col-actions"><button class="icon-btn danger del-user-btn" data-id="${u.id}">🗑</button></td>
          </tr>
        `
        )
        .join("");
      uTbody.querySelectorAll(".del-user-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          if (!confirm("Supprimer cet utilisateur ?")) return;
          await api(`/api/admin/users/${btn.dataset.id}`, { method: "DELETE" });
          toast("Utilisateur supprimé", "success");
          loadAdmin();
        });
      });
    }

    const aTbody = $("auditTbody");
    if (!data.audits.length) {
      aTbody.innerHTML = '<tr><td colspan="4" class="empty-cell">Aucun événement dans le journal d\'audit.</td></tr>';
    } else {
      aTbody.innerHTML = data.audits
        .map(
          (a) => `
          <tr>
            <td>${esc(a.created_at.replace("T", " "))}</td>
            <td><strong>${esc(a.username)}</strong></td>
            <td><span class="badge badge-manual">${esc(a.action)}</span></td>
            <td>${esc(a.detail)}</td>
          </tr>
        `
        )
        .join("");
    }
  } catch (e) {
    toast(e.message, "error");
  }
}

$("refreshAdminBtn").addEventListener("click", loadAdmin);

$("addUserBtn").addEventListener("click", async () => {
  try {
    await api("/api/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: $("uUsername").value.trim(),
        display_name: $("uDisplay").value.trim(),
        password: $("uPassword").value,
        role: $("uRole").value,
      }),
    });
    $("uUsername").value = "";
    $("uDisplay").value = "";
    $("uPassword").value = "";
    toast("Utilisateur créé avec succès !", "success");
    loadAdmin();
  } catch (e) {
    toast(e.message, "error");
  }
});

$("authEnabledToggle").addEventListener("change", async (e) => {
  const enabled = e.target.checked;
  try {
    await api("/api/admin/auth-toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    toast(enabled ? "Protection par mot de passe activée !" : "Protection désactivée (mode local libre).", "success");
    loadAdmin();
  } catch (err) {
    e.target.checked = !enabled;
    toast(err.message, "error");
  }
});

$("gdprExportBtn").addEventListener("click", () => {
  const em = $("gdprEmailInput").value.trim();
  if (!em) return toast("Saisissez une adresse email.", "error");
  window.location.href = `/api/gdpr/export?email=${encodeURIComponent(em)}`;
});

$("gdprForgetBtn").addEventListener("click", async () => {
  const em = $("gdprEmailInput").value.trim();
  if (!em) return toast("Saisissez une adresse email.", "error");
  if (!confirm(`Effacer définitivement toutes les données liées à ${em} (Droit à l'oubli RGPD) ?`)) return;
  try {
    await api("/api/gdpr/forget", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: em }),
    });
    $("gdprEmailInput").value = "";
    toast(`Données effacées définitivement pour ${em}.`, "success");
    loadAdmin();
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  }
});

// ---------------- SAAS WORKSPACES, WHITE-LABEL, ONBOARDING, WYSIWYG & IMAP AUTO-STOP ----------------

let currentWorkspace = null;

function applyWorkspaceBranding(ws) {
  if (!ws) return;
  currentWorkspace = ws;
  const name = ws.name || "ContactsHub";
  const tagline = ws.tagline || "CRM & Centralisation Email";
  const color = ws.primary_color || "#2563eb";
  document.documentElement.style.setProperty("--primary", color);
  document.title = `${name} — Plateforme CRM & Emailing`;
  if ($("brandName")) $("brandName").textContent = name;
  if ($("brandTagline")) $("brandTagline").textContent = tagline;
  if ($("brandIcon")) {
    if (ws.logo_url && safeUrl(ws.logo_url) !== "#") {
      $("brandIcon").innerHTML = `<img src="${esc(safeUrl(ws.logo_url))}" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:8px;" />`;
    } else {
      $("brandIcon").textContent = (name.trim()[0] || "@").toUpperCase();
      $("brandIcon").style.background = color;
    }
  }
  // Fill Admin White-Label fields
  if ($("wsName")) $("wsName").value = ws.name || "";
  if ($("wsTagline")) $("wsTagline").value = ws.tagline || "";
  if ($("wsLogoUrl")) $("wsLogoUrl").value = ws.logo_url || "";
  if ($("wsPrimaryColor")) $("wsPrimaryColor").value = color;
  if ($("wsPrimaryColorHex")) $("wsPrimaryColorHex").value = color;
  if ($("wsCurrency")) $("wsCurrency").value = ws.currency || "FCFA";
  if ($("wsPublicBaseUrl")) $("wsPublicBaseUrl").value = ws.public_base_url || "";
  if ($("wsEmailSignature")) $("wsEmailSignature").value = ws.email_signature || "";
  if ($("wsImapHost")) $("wsImapHost").value = ws.imap_host || "";
  if ($("wsImapPort")) $("wsImapPort").value = ws.imap_port || 993;
  if ($("wsImapUser")) $("wsImapUser").value = ws.imap_username || "";
  if ($("wsImapPass")) $("wsImapPass").value = "";
}

async function loadWorkspaces() {
  try {
    const data = await api("/api/workspaces");
    applyWorkspaceBranding(data.current);
    const sel = $("workspaceSwitcher");
    if (sel) {
      sel.innerHTML = (data.items || [])
        .map((w) => `<option value="${w.id}" ${w.id === data.current.id ? "selected" : ""}>${esc(w.name)} (${w.contacts_count})</option>`)
        .join("");
    }
    if ($("wsListSummary")) {
      $("wsListSummary").textContent = `${(data.items || []).length} espace(s) actif(s) — Espace courant : ${data.current.name} (Plan ${(data.current.plan || "pro").toUpperCase()})`;
    }
  } catch (e) {
    console.warn("Workspace load error:", e);
  }
}

$("workspaceSwitcher")?.addEventListener("change", async (e) => {
  try {
    const res = await api("/api/workspaces/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_id: Number(e.target.value) }),
    });
    applyWorkspaceBranding(res.workspace);
    toast(`Espace actif : ${res.workspace.name}`, "success");
    refreshAll();
    if (!$("tab-crm").classList.contains("hidden")) loadKanban();
  } catch (err) {
    toast(err.message, "error");
  }
});

async function createNewWorkspacePrompt(nameVal) {
  const name = (nameVal || prompt("Nom du nouvel espace client SaaS (ex: Académie Alpha) :") || "").trim();
  if (!name) return;
  try {
    const res = await api("/api/workspaces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    toast(`Espace SaaS "${res.workspace.name}" créé et activé !`, "success");
    if ($("newWsNameInput")) $("newWsNameInput").value = "";
    await loadWorkspaces();
    refreshAll();
  } catch (err) {
    toast(err.message, "error");
  }
}

$("quickNewWsBtn")?.addEventListener("click", () => createNewWorkspacePrompt());
$("createWorkspaceBtn")?.addEventListener("click", () => createNewWorkspacePrompt($("newWsNameInput")?.value));

$("wsPrimaryColor")?.addEventListener("input", (e) => {
  if ($("wsPrimaryColorHex")) $("wsPrimaryColorHex").value = e.target.value;
});
$("wsPrimaryColorHex")?.addEventListener("input", (e) => {
  if (/^#[0-9a-fA-F]{6}$/.test(e.target.value) && $("wsPrimaryColor")) {
    $("wsPrimaryColor").value = e.target.value;
  }
});

$("saveBrandingBtn")?.addEventListener("click", async () => {
  try {
    const payload = {
      name: $("wsName").value.trim(),
      tagline: $("wsTagline").value.trim(),
      logo_url: $("wsLogoUrl").value.trim(),
      primary_color: $("wsPrimaryColorHex").value.trim() || $("wsPrimaryColor").value,
      currency: $("wsCurrency").value,
      public_base_url: $("wsPublicBaseUrl").value.trim(),
      email_signature: $("wsEmailSignature").value,
      imap_host: $("wsImapHost").value.trim(),
      imap_port: Number($("wsImapPort").value || 993),
      imap_username: $("wsImapUser").value.trim(),
    };
    const imapPass = $("wsImapPass").value;
    if (imapPass) payload.imap_password = imapPass;
    const res = await api("/api/workspaces/current", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    applyWorkspaceBranding(res.workspace);
    await loadWorkspaces();
    toast("Marque blanche & configuration SaaS enregistrées !", "success");
  } catch (err) {
    toast(err.message, "error");
  }
});

// IMAP Reply & Bounce Auto-Stop trigger
$("checkSeqRepliesBtn")?.addEventListener("click", async () => {
  const btn = $("checkSeqRepliesBtn");
  btn.disabled = true;
  btn.textContent = "⏳ Scan IMAP en cours…";
  try {
    const res = await api("/api/sequences/check-replies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    toast(
      `IMAP scanné (${res.scanned} emails) : ${res.replies_stopped} réponse(s) stoppée(s), ${res.bounces_stopped} bounce(s) détecté(s).`,
      "success"
    );
    loadSequences();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "📥 Vérifier réponses & bounces (IMAP)";
  }
});

// WYSIWYG Email Composer sync
let wysiwygHtmlMode = false;
const wyEditor = $("tplBodyVisual");
const wyRaw = $("tplBody");

function syncWysiwygToTextarea() {
  if (!wyEditor || !wyRaw || wysiwygHtmlMode) return;
  wyRaw.value = wyEditor.innerHTML;
}
wyEditor?.addEventListener("input", syncWysiwygToTextarea);

$("toggleWysiwygModeBtn")?.addEventListener("click", () => {
  if (!wyEditor || !wyRaw) return;
  wysiwygHtmlMode = !wysiwygHtmlMode;
  if (wysiwygHtmlMode) {
    wyRaw.value = wyEditor.innerHTML;
    wyEditor.classList.add("hidden");
    wyRaw.classList.remove("hidden");
    $("toggleWysiwygModeBtn").textContent = "👁 Mode Visuel";
  } else {
    wyEditor.innerHTML = sanitizeHtml(wyRaw.value);
    wyRaw.classList.add("hidden");
    wyEditor.classList.remove("hidden");
    $("toggleWysiwygModeBtn").textContent = "</> Mode HTML";
  }
});

$("wysiwygToolbar")?.querySelectorAll("button[data-cmd]").forEach((btn) => {
  btn.addEventListener("click", () => {
    wyEditor?.focus();
    document.execCommand(btn.dataset.cmd, false, null);
    syncWysiwygToTextarea();
  });
});

$("wysiwygToolbar")?.querySelectorAll("button[data-var]").forEach((btn) => {
  btn.addEventListener("click", () => {
    wyEditor?.focus();
    document.execCommand("insertText", false, btn.dataset.var);
    syncWysiwygToTextarea();
  });
});

$("wyInsertLinkBtn")?.addEventListener("click", () => {
  const url = prompt("URL du lien (ex: https://...):", "https://");
  if (!url) return;
  wyEditor?.focus();
  document.execCommand("createLink", false, url.trim());
  syncWysiwygToTextarea();
});

$("wyInsertCtaBtn")?.addEventListener("click", () => {
  const label = prompt("Texte du bouton CTA :", "Je m'inscris maintenant");
  if (!label) return;
  const url = prompt("Lien du bouton :", "https://");
  if (!url) return;
  const color = currentWorkspace?.primary_color || "#0077e6";
  const ctaHtml = `<p style="margin:14px 0;"><a href="${esc(url.trim())}" style="display:inline-block;background:${esc(color)};color:#ffffff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600;">${esc(label.trim())}</a></p>`;
  wyEditor?.focus();
  document.execCommand("insertHTML", false, ctaHtml);
  syncWysiwygToTextarea();
});

// Onboarding Wizard (3 steps)
function setOnboardingStep(step) {
  [1, 2, 3].forEach((n) => {
    $(`obPanel${n}`)?.classList.toggle("hidden", n !== step);
    document.querySelector(`.ob-step[data-ob="${n}"]`)?.classList.toggle("active", n === step);
  });
}

function openOnboardingWizard() {
  const ws = currentWorkspace || {};
  if ($("obName")) $("obName").value = ws.name || "MSF'S English Learning Academy";
  if ($("obTagline")) $("obTagline").value = ws.tagline || "CRM, Inscriptions & Croissance";
  if ($("obColor")) $("obColor").value = ws.primary_color || "#0077e6";
  if ($("obCurrency")) $("obCurrency").value = ws.currency || "FCFA";
  if ($("obBaseUrl")) $("obBaseUrl").value = ws.public_base_url || "";
  if ($("obImapHost")) $("obImapHost").value = ws.imap_host || "imap.gmail.com";
  if ($("obImapUser")) $("obImapUser").value = ws.imap_username || "";
  if ($("obImapPass")) $("obImapPass").value = "";
  if ($("obContactsSummary")) {
    $("obContactsSummary").textContent = `${$("statTotal")?.textContent || "0"} contacts dans l'espace "${ws.name || "MSF"}"`;
  }
  setOnboardingStep(1);
  $("onboardingModal")?.classList.remove("hidden");
}

$("openOnboardingBtn")?.addEventListener("click", openOnboardingWizard);
$("adminOpenOnboardingBtn")?.addEventListener("click", openOnboardingWizard);
$("closeOnboardingBtn")?.addEventListener("click", () => $("onboardingModal")?.classList.add("hidden"));

$("obNext1Btn")?.addEventListener("click", async () => {
  try {
    const res = await api("/api/workspaces/current", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("obName").value.trim(),
        tagline: $("obTagline").value.trim(),
        primary_color: $("obColor").value,
        currency: $("obCurrency").value,
        public_base_url: $("obBaseUrl").value.trim(),
      }),
    });
    applyWorkspaceBranding(res.workspace);
    setOnboardingStep(2);
  } catch (err) {
    toast(err.message, "error");
  }
});

$("obPrev2Btn")?.addEventListener("click", () => setOnboardingStep(1));
$("obNext2Btn")?.addEventListener("click", () => setOnboardingStep(3));
$("obPrev3Btn")?.addEventListener("click", () => setOnboardingStep(2));

$("obGoLocalBtn")?.addEventListener("click", () => {
  $("onboardingModal")?.classList.add("hidden");
  document.querySelector('.nav-item[data-tab="local"]')?.click();
});
$("obGoFormsBtn")?.addEventListener("click", () => {
  $("onboardingModal")?.classList.add("hidden");
  document.querySelector('.nav-item[data-tab="training-forms"]')?.click();
});
$("obGoPipelineBtn")?.addEventListener("click", () => {
  $("onboardingModal")?.classList.add("hidden");
  document.querySelector('.nav-item[data-tab="crm"]')?.click();
});

$("obFinishBtn")?.addEventListener("click", async () => {
  try {
    const payload = {
      imap_host: $("obImapHost").value.trim(),
      imap_username: $("obImapUser").value.trim(),
      onboarding_done: true,
    };
    if ($("obImapPass").value) payload.imap_password = $("obImapPass").value;
    const res = await api("/api/workspaces/current", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    applyWorkspaceBranding(res.workspace);
    $("onboardingModal")?.classList.add("hidden");
    toast("Espace SaaS prêt ! Bienvenue dans " + res.workspace.name, "success");
    loadWorkspaces();
  } catch (err) {
    toast(err.message, "error");
  }
});

// ---------------- INIT ----------------

function refreshAll() {
  loadStats();
  loadContacts();
}

loadWorkspaces();
checkDriveStatus();
refreshAll();


// ---------------- WHATSAPP BROADCASTS & AUTO-WELCOME ----------------

async function loadBroadcastHub() {
  try {
    const cfg = await api("/api/wa/auto-welcome");
    if ($("waAutoWelcomeEnabled")) $("waAutoWelcomeEnabled").checked = Boolean(cfg.enabled);
    if ($("waAutoWelcomeTpl")) $("waAutoWelcomeTpl").value = cfg.template || "";
  } catch {}
  loadBroadcastList();
}

async function loadBroadcastList() {
  try {
    const data = await api("/api/wa/broadcasts");
    const items = data.items || [];
    const tbody = $("bcListTbody");
    if (!tbody) return;
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty-cell">Aucune campagne WhatsApp lancée.</td></tr>';
      return;
    }
    const statusMap = {
      running: '<span class="badge badge-pro">🔄 En cours</span>',
      completed: '<span class="badge badge-st-client">✅ Terminée</span>',
      stopped: '<span class="badge badge-manual">⏹ Arrêtée</span>',
      pending: '<span class="badge badge-tag">⏳ En attente</span>',
    };
    tbody.innerHTML = items
      .map(
        (b) => `
        <tr>
          <td><strong>${esc(b.name)}</strong></td>
          <td>${statusMap[b.status] || esc(b.status)}</td>
          <td style="color:#15803d;font-weight:600;">${b.sent_count} / ${b.total_count}</td>
          <td style="color:#dc2626;">${b.failed_count}</td>
          <td>${b.delay_min_sec}–${b.delay_max_sec}s</td>
          <td>${esc((b.created_at || "").replace("T", " ").slice(0, 16))}</td>
          <td class="col-actions">
            ${b.status === "running" ? `<button class="btn btn-danger btn-sm bc-stop-btn" data-id="${b.id}">⏹ Stop</button>` : "—"}
          </td>
        </tr>
      `
      )
      .join("");
    tbody.querySelectorAll(".bc-stop-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api(`/api/wa/broadcasts/${btn.dataset.id}/stop`, { method: "POST" });
        toast("Campagne WhatsApp arrêtée.", "info");
        loadBroadcastList();
      });
    });
  } catch {}
}

function getBcFilters() {
  return {
    workspace_id: state.workspaceId || 1,
    status: $("bcFilterStatus")?.value || "",
    tag: ($("bcFilterTag")?.value || "").trim(),
    min_score: Number($("bcFilterScore")?.value || 0),
  };
}

$("saveWaAutoWelcomeBtn")?.addEventListener("click", async () => {
  try {
    await api("/api/wa/auto-welcome", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: $("waAutoWelcomeEnabled").checked,
        template: $("waAutoWelcomeTpl").value,
      }),
    });
    toast("Auto-répondeur WhatsApp enregistré !", "success");
  } catch (e) {
    toast(e.message, "error");
  }
});

$("bcPreviewBtn")?.addEventListener("click", async () => {
  try {
    const res = await api("/api/wa/broadcasts/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(getBcFilters()),
    });
    $("bcAudienceBadge").textContent = `👥 ${res.count} contact(s) joignable(s) sur WhatsApp`;
  } catch (e) {
    toast(e.message, "error");
  }
});

$("bcStartBtn")?.addEventListener("click", async () => {
  const msg = ($("bcMessageTpl")?.value || "").trim();
  if (!msg) return toast("Rédigez d'abord le message de la campagne.", "error");
  if (!confirm("Lancer cette campagne Broadcast WhatsApp ? Les messages seront envoyés progressivement avec délai de sécurité.")) return;
  try {
    const res = await api("/api/wa/broadcasts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: ($("bcName")?.value || "").trim(),
        message_tpl: msg,
        filters: getBcFilters(),
        delay_min_sec: Number($("bcDelayMin")?.value || 18),
        delay_max_sec: Number($("bcDelayMax")?.value || 40),
      }),
    });
    toast(`Broadcast lancé vers ${res.total_count} contact(s) !`, "success");
    loadBroadcastList();
  } catch (e) {
    toast(e.message, "error");
  }
});
$("refreshBcBtn")?.addEventListener("click", loadBroadcastList);

// ---------------- FACEBOOK (MESSENGER + LEAD ADS) & TIKTOK ----------------

let activeSocialSender = null;

async function loadSocialHub() {
  try {
    const cfg = await api("/api/social/config");
    const fb = cfg.facebook || {};
    const tt = cfg.tiktok || {};
    if ($("fbPageName")) $("fbPageName").value = fb.page_name || "";
    if ($("fbPageId")) $("fbPageId").value = fb.page_id || "";
    if ($("fbAccessToken")) $("fbAccessToken").value = fb.access_token || "";
    if ($("fbWebhookUrl")) $("fbWebhookUrl").value = fb.webhook_url || "";
    if ($("fbVerifyToken")) $("fbVerifyToken").value = fb.verify_token || "";
    if ($("fbLeadTag")) $("fbLeadTag").value = fb.lead_tag || "Facebook Ads";
    if ($("fbAutoWa")) $("fbAutoWa").checked = Boolean(fb.auto_welcome_wa);

    if ($("ttWebhookUrl")) $("ttWebhookUrl").value = tt.webhook_url || "";
    if ($("ttBioUrl")) $("ttBioUrl").value = tt.bio_url || "";
    if ($("ttPageName")) $("ttPageName").value = tt.page_name || "";
    if ($("ttLeadTag")) $("ttLeadTag").value = tt.lead_tag || "TikTok Leads";
    if ($("ttAutoWa")) $("ttAutoWa").checked = Boolean(tt.auto_welcome_wa);
  } catch {}
  loadSocialConversations();
}

$("saveFbCfgBtn")?.addEventListener("click", async () => {
  try {
    await api("/api/social/config/facebook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        page_name: $("fbPageName").value.trim(),
        page_id: $("fbPageId").value.trim(),
        access_token: $("fbAccessToken").value.trim(),
        verify_token: $("fbVerifyToken").value.trim(),
        lead_tag: $("fbLeadTag").value.trim(),
        auto_welcome_wa: $("fbAutoWa").checked,
      }),
    });
    toast("Configuration Facebook enregistrée !", "success");
    loadSocialHub();
  } catch (e) {
    toast(e.message, "error");
  }
});

$("saveTtCfgBtn")?.addEventListener("click", async () => {
  try {
    await api("/api/social/config/tiktok", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        page_name: $("ttPageName").value.trim(),
        bio_url: $("ttBioUrl").value.trim(),
        lead_tag: $("ttLeadTag").value.trim(),
        auto_welcome_wa: $("ttAutoWa").checked,
      }),
    });
    toast("Configuration TikTok enregistrée !", "success");
    loadSocialHub();
  } catch (e) {
    toast(e.message, "error");
  }
});

async function uploadSocialCsv(platform, inputEl, tagEl) {
  const file = inputEl.files?.[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  fd.append("tag", tagEl?.value?.trim() || (platform === "facebook" ? "Facebook Ads" : "TikTok Leads"));
  fd.append("workspace_id", String(state.workspaceId || 1));
  try {
    const data = await api(`/api/social/import-csv/${platform}`, { method: "POST", body: fd });
    toast(`Import ${platform.toUpperCase()} : ${data.imported} lead(s) ajouté(s), ${data.skipped} ignoré(s) !`, "success");
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    inputEl.value = "";
  }
}

$("fbCsvFile")?.addEventListener("change", (e) => uploadSocialCsv("facebook", e.target, $("fbLeadTag")));
$("ttCsvFile")?.addEventListener("change", (e) => uploadSocialCsv("tiktok", e.target, $("ttLeadTag")));

async function loadSocialConversations() {
  try {
    const data = await api("/api/social/conversations?platform=facebook");
    const items = data.items || [];
    const box = $("socialConvList");
    if (!box) return;
    if (!items.length) {
      box.innerHTML = '<div class="empty-cell" style="padding:24px 12px;">Aucun message Messenger reçu.<br/>Configurez votre Page Access Token & Webhook Meta ci-dessus.</div>';
      return;
    }
    box.innerHTML = items
      .map(
        (c) => `
        <div class="wa-conv-item ${activeSocialSender?.sender_id === c.sender_id ? "active" : ""}" data-sid="${esc(c.sender_id)}" data-name="${esc(c.sender_name || c.sender_id)}" data-cid="${c.contact_id || ""}">
          <div class="wa-conv-top">
            <span class="wa-conv-name">📘 ${esc(c.sender_name || c.sender_id)}</span>
            <span class="wa-conv-time">${esc((c.last_sent_at || "").slice(11, 16))}</span>
          </div>
          <div class="wa-conv-preview">${esc(c.last_body || "")}</div>
        </div>
      `
      )
      .join("");
    box.querySelectorAll(".wa-conv-item").forEach((el) => {
      el.addEventListener("click", () => {
        activeSocialSender = {
          platform: "facebook",
          sender_id: el.dataset.sid,
          sender_name: el.dataset.name,
          contact_id: el.dataset.cid ? Number(el.dataset.cid) : null,
        };
        openSocialChat(activeSocialSender);
      });
    });
  } catch {}
}

async function openSocialChat(conv) {
  $("socialChatTitle").textContent = conv.sender_name || conv.sender_id;
  $("socialChatSub").textContent = `Facebook Messenger · ID ${conv.sender_id}`;
  $("socialChatActions").innerHTML = conv.contact_id
    ? `<button class="btn btn-secondary btn-sm" id="socProfBtn">👁 Fiche 360°</button>`
    : `<button class="btn btn-secondary btn-sm" id="socConvertBtn">➕ Ajouter au CRM</button>`;
  $("socProfBtn")?.addEventListener("click", () => openProfileModal(conv.contact_id));
  $("socConvertBtn")?.addEventListener("click", async () => {
    const phone = prompt("Numéro de téléphone / WhatsApp (optionnel) :", "") || "";
    const res = await api("/api/social/convert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform: "facebook", sender_id: conv.sender_id, full_name: conv.sender_name, phone }),
    });
    if (res.ok) {
      toast("Contact Messenger ajouté au CRM !", "success");
      conv.contact_id = res.contact_id;
      openSocialChat(conv);
      refreshAll();
    }
  });
  const data = await api(`/api/social/messages?platform=facebook&sender_id=${encodeURIComponent(conv.sender_id)}`);
  const msgs = data.items || [];
  const box = $("socialChatMessages");
  box.innerHTML = msgs.length
    ? msgs
        .map(
          (m) => `
          <div class="wa-bubble ${m.from_me ? "out" : "in"}">
            <div>${esc(m.body)}</div>
            <div class="wa-bubble-meta">${esc((m.sent_at || "").replace("T", " ").slice(0, 16))}</div>
          </div>
        `
        )
        .join("")
    : '<div class="wa-empty-chat">Aucun message.</div>';
  box.scrollTop = box.scrollHeight;
}

$("socialSendBtn")?.addEventListener("click", async () => {
  if (!activeSocialSender) return toast("Sélectionnez une discussion Messenger.", "error");
  const text = ($("socialComposerInput")?.value || "").trim();
  if (!text) return;
  try {
    await api("/api/social/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sender_id: activeSocialSender.sender_id, text }),
    });
    $("socialComposerInput").value = "";
    openSocialChat(activeSocialSender);
    toast("Réponse Messenger envoyée !", "success");
  } catch (e) {
    toast(e.message, "error");
  }
});
$("refreshSocialInboxBtn")?.addEventListener("click", loadSocialConversations);

// ---------------- STUDENT PAYMENTS & RECEIPTS (TMONEY / FLOOZ) ----------------

function fmtFcfa(val) {
  return `${Math.round(Number(val || 0)).toLocaleString("fr-FR")} FCFA`;
}

async function loadPayments() {
  if ($("payDate") && !$("payDate").value) {
    $("payDate").value = new Date().toISOString().slice(0, 10);
  }
  try {
    const q = ($("paySearchInput")?.value || "").trim();
    const wsId = state.workspaceId || 1;
    const data = await api(`/api/payments?workspace_id=${wsId}&q=${encodeURIComponent(q)}`);
    const sum = data.summary || {};
    if ($("payKpiCollected")) $("payKpiCollected").textContent = fmtFcfa(sum.total_collected);
    if ($("payKpiRemaining")) $("payKpiRemaining").textContent = fmtFcfa(sum.total_remaining);
    if ($("payKpiCount")) $("payKpiCount").textContent = String(sum.count || 0);
    if ($("payKpiMethods")) {
      const bm = sum.by_method || {};
      const parts = Object.entries(bm).map(([k, v]) => `${esc(k)}: <strong>${fmtFcfa(v)}</strong>`);
      $("payKpiMethods").innerHTML = parts.length ? parts.join(" · ") : "—";
    }
    const items = data.items || [];
    const tbody = $("paymentsTbody");
    if (!tbody) return;
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="empty-cell">Aucun paiement enregistré.</td></tr>';
      return;
    }
    tbody.innerHTML = items
      .map(
        (p) => `
        <tr>
          <td><code>${esc(p.receipt_num)}</code></td>
          <td><strong>${esc(p.student_name)}</strong><div class="contact-role">${esc(p.student_phone || p.student_email || "")}</div></td>
          <td>${esc(p.course_name)}</td>
          <td><span class="badge badge-manual">${esc(p.payment_method)}</span> ${p.payment_ref ? `<span class="contact-role">${esc(p.payment_ref)}</span>` : ""}</td>
          <td style="color:#15803d;font-weight:700;">${fmtFcfa(p.amount_paid)}</td>
          <td style="color:${p.remaining_balance > 0 ? "#b45309" : "#15803d"};font-weight:600;">${p.effective_fee > 0 ? (p.remaining_balance > 0 ? fmtFcfa(p.remaining_balance) : "Soldé ✅") : "—"}</td>
          <td>${esc(p.paid_at)}</td>
          <td class="col-actions">
            <a class="icon-btn" href="/receipt/${p.id}" target="_blank" title="Voir / Imprimer Reçu PDF">🧾</a>
            ${p.student_phone ? `<button class="icon-btn pay-wa-btn" data-id="${p.id}" title="Envoyer reçu sur WhatsApp">💬</button>` : ""}
            <button class="icon-btn danger pay-del-btn" data-id="${p.id}" title="Supprimer">🗑</button>
          </td>
        </tr>
      `
      )
      .join("");
    tbody.querySelectorAll(".pay-wa-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/api/payments/${btn.dataset.id}/send-whatsapp`, { method: "POST" });
          toast("Reçu envoyé directement sur WhatsApp !", "success");
        } catch (e) {
          toast(e.message, "error");
        }
      });
    });
    tbody.querySelectorAll(".pay-del-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Supprimer ce paiement ?")) return;
        await api(`/api/payments/${btn.dataset.id}`, { method: "DELETE" });
        toast("Paiement supprimé.", "info");
        loadPayments();
      });
    });
  } catch (e) {
    toast(e.message, "error");
  }
}

$("savePaymentBtn")?.addEventListener("click", async () => {
  try {
    const res = await api("/api/payments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        student_name: $("payStudentName").value.trim(),
        student_phone: $("payStudentPhone").value.trim(),
        student_email: $("payStudentEmail").value.trim(),
        course_name: $("payCourseName").value.trim(),
        total_fee: Number($("payTotalFee").value || 0),
        amount_paid: Number($("payAmountPaid").value || 0),
        payment_method: $("payMethod").value,
        payment_ref: $("payRef").value.trim(),
        paid_at: $("payDate").value,
        notes: $("payNotes").value.trim(),
        workspace_id: state.workspaceId || 1,
      }),
    });
    toast(`Paiement enregistré ! Reçu ${res.receipt_num} généré.`, "success");
    ["payStudentName", "payStudentPhone", "payStudentEmail", "payAmountPaid", "payRef", "payNotes"].forEach((id) => ($(id).value = ""));
    loadPayments();
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  }
});
$("refreshPaymentsBtn")?.addEventListener("click", loadPayments);
$("paySearchInput")?.addEventListener("input", loadPayments);


// ---------------- AI SALES ASSISTANT IN WHATSAPP ----------------

$("waAiSuggestBtn")?.addEventListener("click", async () => {
  if (!waActiveChat) return toast("Sélectionnez d'abord une discussion WhatsApp.", "error");
  const btn = $("waAiSuggestBtn");
  const oldTxt = btn.textContent;
  btn.disabled = true;
  btn.textContent = "⏳ IA…";
  try {
    const res = await api("/api/ai/suggest-wa", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        contact_id: waActiveChat.contact_id,
        phone: waActiveChat.phone,
        jid: waActiveChat.jid,
        contact_name: waActiveChat.display_name,
        goal: "repondre",
      }),
    });
    $("waComposerInput").value = res.reply || "";
    $("waComposerInput").focus();
    toast(`Suggestion IA générée (${res.engine}) !`, "success");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = oldTxt;
  }
});

// ---------------- LOCAL RADAR (OSM FREE + GOOGLE MAPS) & AI CONFIG ----------------

let radarResultsCache = [];

async function loadRadarAndAiSettings() {
  try {
    const aiCfg = await api("/api/ai/config");
    if ($("aiProviderSelect")) $("aiProviderSelect").value = aiCfg.provider || "builtin";
    if ($("aiApiKeyInput")) $("aiApiKeyInput").value = aiCfg.api_key || "";
    if ($("aiBizContext")) $("aiBizContext").value = aiCfg.business_context || "";
  } catch {}
}

$("saveAiCfgBtn")?.addEventListener("click", async () => {
  try {
    await api("/api/ai/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: $("aiProviderSelect").value,
        api_key: $("aiApiKeyInput").value.trim(),
        business_context: $("aiBizContext").value.trim(),
      }),
    });
    toast("Paramètres de l'Assistant IA enregistrés !", "success");
  } catch (e) {
    toast(e.message, "error");
  }
});

$("saveGmapsKeyBtn")?.addEventListener("click", async () => {
  try {
    await api("/api/radar/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ google_maps_api_key: $("gmapsKeyInput").value.trim() }),
    });
    toast("Clé Google Maps enregistrée !", "success");
  } catch (e) {
    toast(e.message, "error");
  }
});

$("radarSearchBtn")?.addEventListener("click", async () => {
  const btn = $("radarSearchBtn");
  const st = $("radarStatusText");
  btn.disabled = true;
  st.textContent = "⏳ Recherche en cours sur la carte…";
  try {
    const res = await api("/api/radar/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        keyword: $("radarKeyword").value.trim() || "hôtel",
        city: $("radarCity").value.trim() || "Lomé, Togo",
        engine: $("radarEngine").value,
        enrich_emails: $("radarEnrichEmails").checked,
      }),
    });
    radarResultsCache = res.items || [];
    const tbody = $("radarResultsTbody");
    if (!radarResultsCache.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty-cell">Aucune entreprise trouvée pour ce mot-clé. Essayez un autre secteur (ex: hôtel, banque, école, restaurant).</td></tr>';
      $("radarImportBtn").classList.add("hidden");
      st.textContent = "0 résultat.";
      return;
    }
    tbody.innerHTML = radarResultsCache
      .map(
        (it, idx) => `
        <tr>
          <td class="col-check"><input type="checkbox" class="radar-row-check" data-idx="${idx}" checked /></td>
          <td><strong>${esc(it.name)}</strong><div class="contact-role">${esc(it.category || "")}</div></td>
          <td>${esc(it.phone || "—")}</td>
          <td>${esc(it.email || "—")}</td>
          <td>${it.website ? `<a href="${esc(safeUrl(it.website))}" target="_blank" rel="noopener noreferrer" class="badge badge-manual">🌐 Site</a>` : "—"}</td>
          <td class="contact-role">${esc(it.address || "")}</td>
        </tr>
      `
      )
      .join("");
    $("radarImportBtn").classList.remove("hidden");
    st.textContent = `✅ ${radarResultsCache.length} entreprise(s) trouvée(s) via ${res.engine === "google_maps" ? "Google Maps" : "OpenStreetMap (Gratuit)"} !`;
  } catch (e) {
    st.textContent = "";
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
  }
});

$("radarSelectAll")?.addEventListener("change", (e) => {
  document.querySelectorAll(".radar-row-check").forEach((cb) => (cb.checked = e.target.checked));
});

$("radarImportBtn")?.addEventListener("click", async () => {
  const selected = [];
  document.querySelectorAll(".radar-row-check:checked").forEach((cb) => {
    const it = radarResultsCache[Number(cb.dataset.idx)];
    if (it) selected.push(it);
  });
  if (!selected.length) return toast("Cochez au moins une entreprise à importer.", "error");
  try {
    const res = await api("/api/radar/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        items: selected,
        tag: $("radarTag").value.trim() || "Radar B2B",
        workspace_id: state.workspaceId || 1,
      }),
    });
    toast(`Radar importé : ${res.inserted} ajouté(s), ${res.updated} mis à jour dans le CRM !`, "success");
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  }
});

// ---------------- APPOINTMENTS & BOOKING HUB (/rdv) ----------------

async function loadBookingHub() {
  try {
    const cfg = await api("/api/booking/settings");
    if ($("bookingPublicUrl")) $("bookingPublicUrl").value = cfg.public_url || `${window.location.origin}/rdv`;
    if ($("bookingLocationInput")) $("bookingLocationInput").value = cfg.location || "";
    if ($("bookingSlotsInput")) $("bookingSlotsInput").value = (cfg.slots || []).join(", ");
    if ($("bookingTopicsInput")) $("bookingTopicsInput").value = (cfg.topics || []).join("\n");
  } catch {}
  loadAppointmentsList();
}

async function loadAppointmentsList() {
  try {
    const data = await api("/api/appointments");
    const items = data.items || [];
    const tbody = $("appointmentsTbody");
    if (!tbody) return;
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty-cell">Aucun rendez-vous réservé pour le moment. Partagez votre lien public !</td></tr>';
      return;
    }
    const stBadge = {
      confirmed: '<span class="badge badge-pro">✅ Confirmé</span>',
      completed: '<span class="badge badge-st-client">🎓 Terminé</span>',
      cancelled: '<span class="badge badge-manual">❌ Annulé</span>',
    };
    tbody.innerHTML = items
      .map(
        (a) => `
        <tr>
          <td><code>${esc(a.booking_ref)}</code></td>
          <td><strong>${esc(a.full_name)}</strong><div class="contact-role">${esc(a.phone)}${a.email ? " · " + esc(a.email) : ""}</div></td>
          <td>${esc(a.topic)}<div class="contact-role">${esc(a.mode)}</div></td>
          <td><strong>${esc(a.appointment_date)}</strong> à ${esc(a.appointment_time)}</td>
          <td>${stBadge[a.status] || esc(a.status)}</td>
          <td class="col-actions">
            <button class="icon-btn appt-wa-btn" data-id="${a.id}" title="Envoyer rappel WhatsApp">💬</button>
            <button class="icon-btn appt-done-btn" data-id="${a.id}" title="Marquer terminé">✅</button>
            <button class="icon-btn danger appt-cancel-btn" data-id="${a.id}" title="Annuler">✕</button>
          </td>
        </tr>
      `
      )
      .join("");
    tbody.querySelectorAll(".appt-wa-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/api/appointments/${btn.dataset.id}/remind-wa`, { method: "POST" });
          toast("Rappel de rendez-vous envoyé sur WhatsApp !", "success");
        } catch (e) {
          toast(e.message, "error");
        }
      });
    });
    tbody.querySelectorAll(".appt-done-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api(`/api/appointments/${btn.dataset.id}/status`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: "completed" }),
        });
        toast("Rendez-vous marqué terminé.", "success");
        loadAppointmentsList();
      });
    });
    tbody.querySelectorAll(".appt-cancel-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Annuler ce rendez-vous ?")) return;
        await api(`/api/appointments/${btn.dataset.id}/status`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: "cancelled" }),
        });
        toast("Rendez-vous annulé.", "info");
        loadAppointmentsList();
      });
    });
  } catch (e) {
    toast(e.message, "error");
  }
}

$("copyBookingUrlBtn")?.addEventListener("click", () => {
  const u = $("bookingPublicUrl")?.value || `${window.location.origin}/rdv`;
  navigator.clipboard.writeText(u);
  toast(`Lien RDV copié : ${u}`, "success");
});

$("saveBookingSettingsBtn")?.addEventListener("click", async () => {
  try {
    await api("/api/booking/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        slots: $("bookingSlotsInput").value,
        location: $("bookingLocationInput").value,
        topics: $("bookingTopicsInput").value,
      }),
    });
    toast("Paramètres de la page RDV enregistrés !", "success");
    loadBookingHub();
  } catch (e) {
    toast(e.message, "error");
  }
});
$("refreshApptsBtn")?.addEventListener("click", loadAppointmentsList);
