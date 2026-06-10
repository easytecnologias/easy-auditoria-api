const LOJA = "loja-106";
const REFRESH_INTERVAL_MS = 15000;

let alerts = [];
let health = [];
let activeFilter = "all";
let selectedAlert = null;
let videoTimer = null;
let videoSecond = 0;

const table = document.getElementById("alertsTable");
const drawer = document.getElementById("alertDrawer");
const backdrop = document.getElementById("drawerBackdrop");
const toast = document.getElementById("toast");

async function carregarAlertas() {
  try {
    const params = new URLSearchParams({ loja: LOJA, filter: activeFilter });
    const resp = await fetch(`/api/v1/alerts?${params}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    alerts = await resp.json();
  } catch (err) {
    alerts = [];
  }
  renderAlerts();
}

async function carregarHealth() {
  try {
    const resp = await fetch(`/api/v1/health?loja=${LOJA}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    health = await resp.json();
  } catch (err) {
    health = [];
  }
  renderHealth();
}

function renderAlerts() {
  const query = document.getElementById("searchInput").value.toLowerCase();
  const rows = alerts.filter(alert => {
    const filterMatch = activeFilter === "all"
      || (activeFilter === "critical" && alert.severity === "critical")
      || (activeFilter === "review" && alert.state !== "resolved")
      || (activeFilter === "resolved" && alert.state === "resolved");
    const text = `${alert.pdv} ${alert.receipt} ${alert.product} ${alert.event}`.toLowerCase();
    return filterMatch && text.includes(query);
  });

  if (rows.length === 0) {
    table.innerHTML = `
      <tr class="empty-row">
        <td colspan="7">Nenhum alerta encontrado.</td>
      </tr>
    `;
    return;
  }

  table.innerHTML = rows.map(alert => `
    <tr data-id="${alert.id}">
      <td><span class="severity ${alert.severity}"><i></i>${alert.severity === "critical" ? "Crítico" : alert.severity === "warning" ? "Atenção" : "Normal"}</span></td>
      <td>${alert.time}</td>
      <td class="receipt-cell"><strong>${alert.pdv}</strong><span>Cupom ${alert.receipt}</span></td>
      <td><div class="event-cell"><img class="mini-cctv" src="assets/frame-register.svg" alt=""><div><strong>${alert.event}</strong><span>${alert.subtitle}</span></div></div></td>
      <td class="product-cell"><strong>${alert.product}</strong><span>${alert.qty} · ${alert.value}</span></td>
      <td><div class="confidence"><span>${alert.confidence}%</span><i class="confidence-meter"><i style="width:${alert.confidence}%"></i></i></div></td>
      <td><span class="state-badge ${alert.state}">${alert.stateText}</span></td>
      <td><div class="row-actions"><button data-action="open" title="Revisar alerta"><i data-lucide="scan-search"></i></button><button data-action="video" title="Ver vídeo"><i data-lucide="play"></i></button></div></td>
    </tr>
  `).join("");

  table.querySelectorAll("tr").forEach(row => {
    row.addEventListener("click", event => {
      const alert = alerts.find(item => item.id === Number(row.dataset.id));
      if (event.target.closest("[data-action='video']")) {
        selectedAlert = alert;
        openVideo();
      } else {
        openDrawer(alert);
      }
    });
  });
  lucide.createIcons();
}

function renderHealth() {
  const grid = document.getElementById("healthGrid");
  if (health.length === 0) {
    grid.innerHTML = `<div class="health-row"><strong>Sem dados de saude ainda.</strong></div>`;
    return;
  }
  grid.innerHTML = health.map(item => `
    <div class="health-row">
      <strong>PDV ${item.pdv}</strong>
      ${serviceState(item.bridge)}
      ${serviceState(item.imhdx)}
      ${serviceState(item.audit)}
    </div>
  `).join("");
}

function serviceState(state) {
  const label = state === "online" ? "Online" : state === "warning" ? "Atenção" : "Parada";
  return `<span class="service-state ${state}"><i></i>${label}</span>`;
}

function openDrawer(alert) {
  selectedAlert = alert;
  document.getElementById("drawerTitle").textContent = alert.event;
  document.getElementById("cameraTime").textContent = alert.time;
  document.getElementById("detailPdv").textContent = alert.pdv;
  document.getElementById("detailReceipt").textContent = alert.receipt;
  document.getElementById("detailTime").textContent = alert.time;
  document.getElementById("detailProduct").textContent = alert.product;
  document.getElementById("detailQuantity").textContent = alert.qty;
  document.getElementById("detailValue").textContent = alert.value;
  document.getElementById("confidenceValue").textContent = `${alert.confidence}% confiança`;
  document.getElementById("analysisText").textContent = alert.analysis;
  document.getElementById("technicalNote").textContent = alert.note;
  const badge = document.getElementById("resultBadge");
  badge.textContent = alert.result;
  badge.className = `result-badge ${alert.result === "Confere" ? "success" : alert.result === "Inconclusivo" || alert.result === "Revisar" ? "warning" : "danger"}`;
  drawer.classList.add("open");
  backdrop.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
}

function closeDrawer() {
  drawer.classList.remove("open");
  backdrop.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
}

function showToast(message) {
  toast.querySelector("span").textContent = message;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2500);
}

function openVideo() {
  document.getElementById("videoModal").classList.add("open");
  document.querySelector(".video-meta span").textContent = `${selectedAlert.pdv} · Cupom ${selectedAlert.receipt}`;
  resetVideo();
}

function resetVideo() {
  clearInterval(videoTimer);
  videoTimer = null;
  videoSecond = 0;
  document.getElementById("videoProgress").style.width = "0%";
  document.getElementById("videoClock").textContent = "00:00 / 00:20";
  document.getElementById("playToggle").innerHTML = '<i data-lucide="play"></i>';
  lucide.createIcons();
}

function toggleVideo() {
  if (videoTimer) {
    clearInterval(videoTimer);
    videoTimer = null;
    document.getElementById("playToggle").innerHTML = '<i data-lucide="play"></i>';
  } else {
    document.getElementById("playToggle").innerHTML = '<i data-lucide="pause"></i>';
    videoTimer = setInterval(() => {
      videoSecond += 1;
      document.getElementById("videoProgress").style.width = `${videoSecond * 5}%`;
      document.getElementById("videoClock").textContent = `00:${String(videoSecond).padStart(2,"0")} / 00:20`;
      if (videoSecond >= 20) resetVideo();
    }, 1000);
  }
  lucide.createIcons();
}

async function enviarDecisao(alertaId, action) {
  try {
    await fetch(`/api/v1/alerts/${alertaId}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
  } catch (err) {
    // erro de rede nao deve travar a UI
  }
  await carregarAlertas();
}

document.querySelectorAll(".alert-tabs button").forEach(button => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".alert-tabs button").forEach(item => item.classList.remove("active"));
    button.classList.add("active");
    activeFilter = button.dataset.filter;
    carregarAlertas();
  });
});

document.getElementById("searchInput").addEventListener("input", renderAlerts);
document.getElementById("closeDrawer").addEventListener("click", closeDrawer);
backdrop.addEventListener("click", closeDrawer);
document.querySelectorAll(".frame-strip button").forEach(button => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".frame-strip button").forEach(item => item.classList.remove("active"));
    button.classList.add("active");
    document.getElementById("mainEvidence").src = button.dataset.frame;
  });
});
document.getElementById("saveButton").addEventListener("click", async () => {
  if (!selectedAlert) return;
  const receipt = selectedAlert.receipt;
  await enviarDecisao(selectedAlert.id, "save");
  showToast(`Ocorrência do cupom ${receipt} salva.`);
  closeDrawer();
});
document.getElementById("ignoreButton").addEventListener("click", async () => {
  if (!selectedAlert) return;
  const receipt = selectedAlert.receipt;
  await enviarDecisao(selectedAlert.id, "ignore");
  showToast(`Alerta do cupom ${receipt} ignorado.`);
  closeDrawer();
});
document.getElementById("videoButton").addEventListener("click", openVideo);
document.getElementById("closeVideo").addEventListener("click", () => {
  document.getElementById("videoModal").classList.remove("open");
  resetVideo();
});
document.getElementById("playToggle").addEventListener("click", toggleVideo);
document.querySelector(".mobile-menu").addEventListener("click", () => document.querySelector(".sidebar").classList.toggle("open"));
document.querySelectorAll(".nav-item[data-view]").forEach(item => {
  item.addEventListener("click", () => {
    document.querySelectorAll(".nav-item[data-view]").forEach(nav => nav.classList.remove("active"));
    item.classList.add("active");
    if (item.dataset.view !== "overview") showToast("Tela incluída na próxima etapa do protótipo.");
  });
});

carregarAlertas();
carregarHealth();
lucide.createIcons();

setInterval(carregarAlertas, REFRESH_INTERVAL_MS);
setInterval(carregarHealth, REFRESH_INTERVAL_MS);
