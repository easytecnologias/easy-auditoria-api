let LOJA = (window.APP_CONFIG || {}).LOJA || "loja-106";
const LOJA_NOME   = (window.APP_CONFIG || {}).LOJA_NOME  || "Loja 106";
const AMBIENTE    = (window.APP_CONFIG || {}).AMBIENTE   || "Produção";
const REFRESH_INTERVAL_MS = 15000;

// Aplica valores do config no HTML assim que o DOM estiver pronto
document.addEventListener("DOMContentLoaded", () => {
  const ambEl = document.getElementById("ambienteLabel");
  if (ambEl) ambEl.textContent = AMBIENTE;
});

// Formatador de moeda BRL com separador de milhar (R$ 29.871,00)
function fmtBRL(v) {
  return "R$ " + (v || 0).toLocaleString("pt-BR", {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
const TOKEN_KEY = "ea_token";

// ── Auth ──────────────────────────────────────────────
function getToken() { return localStorage.getItem(TOKEN_KEY); }
function setToken(t) { localStorage.setItem(TOKEN_KEY, t); }
function clearToken() { localStorage.removeItem(TOKEN_KEY); }

async function apiFetch(url, opts = {}) {
  const token = getToken();
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const resp = await fetch(url, { ...opts, headers });
  if (resp.status === 401) { mostrarLogin(); return resp; }
  return resp;
}

// Carrega imagem protegida com auth e retorna blob URL (evita token na URL)
const _protectedBlobUrls = new Set();
async function mediaObjectUrl(url) {
  const token = getToken();
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  const resp = await fetch(url, { headers });
  if (resp.status === 401) { mostrarLogin(); throw new Error("unauthorized"); }
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const blob = await resp.blob();
  const objectUrl = URL.createObjectURL(blob);
  _protectedBlobUrls.add(objectUrl);
  return objectUrl;
}

// Substitui data-auth-src por blob URLs autenticados
function hydrateProtectedMedia(root = document) {
  root.querySelectorAll("img[data-auth-src]").forEach(async img => {
    try { img.src = await mediaObjectUrl(img.dataset.authSrc); }
    catch { img.src = "assets/frame-register.svg"; }
  });
}

function mostrarLogin() {
  clearToken();
  document.getElementById("loginScreen").hidden = false;
  document.getElementById("appShell").hidden = true;
}

function mostrarApp(usuario) {
  document.getElementById("loginScreen").hidden = true;
  document.getElementById("appShell").hidden = false;
  const iniciais = usuario.nome.split(" ").map(p => p[0]).slice(0, 2).join("").toUpperCase();
  const perfis = { admin: "Administrador", supervisor: "Supervisor", operador: "Operador" };
  document.getElementById("profileAvatar").textContent = iniciais;
  document.getElementById("profileName").textContent = usuario.nome;
  document.getElementById("profileRole").textContent = perfis[usuario.perfil] || usuario.perfil;
  if (usuario.perfil === "admin" || usuario.perfil === "supervisor") {
    document.getElementById("navUsuarios").style.display = "";
  }
  // Carregar seletor de loja no topo
  _carregarSeletorLoja(usuario);
  if (usuario.perfil === "admin") {
    document.getElementById("navLojas").style.display = "";
  }
  lucide.createIcons();
}

async function _carregarSeletorLoja(usuario) {
  const nomeEl = document.getElementById("topbarLojaNome");
  if (!nomeEl) return;
  // Preenche só o nome inicial — a lista é carregada quando o dropdown abre
  if (usuario.loja_id) {
    LOJA = usuario.loja_id;
    // Buscar nome para exibir
    const r = await apiFetch("/api/v1/lojas");
    if (r.ok) {
      const lojas = await r.json();
      const mine = lojas.find(l => l.id === usuario.loja_id);
      nomeEl.textContent = mine ? mine.nome : usuario.loja_id;
    } else {
      nomeEl.textContent = usuario.loja_id;
    }
  } else {
    nomeEl.textContent = "Todas as lojas";
  }
}

async function verificarAuth() {
  const token = getToken();
  if (!token) { mostrarLogin(); return; }
  try {
    const resp = await fetch("/auth/me", { headers: { Authorization: `Bearer ${token}` } });
    if (!resp.ok) { mostrarLogin(); return; }
    const usuario = await resp.json();
    mostrarApp(usuario);
    iniciarApp();
  } catch {
    mostrarLogin();
  }
}

document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("loginBtn");
  const erro = document.getElementById("loginError");
  btn.disabled = true;
  btn.textContent = "Entrando...";
  erro.hidden = true;
  try {
    const resp = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: document.getElementById("loginEmail").value,
        senha: document.getElementById("loginPassword").value,
      }),
    });
    if (!resp.ok) {
      const data = await resp.json();
      erro.textContent = data.detail || "Email ou senha inválidos.";
      erro.hidden = false;
      return;
    }
    const { token, usuario } = await resp.json();
    setToken(token);
    mostrarApp(usuario);
    iniciarApp();
  } catch {
    erro.textContent = "Erro de conexão. Tente novamente.";
    erro.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Entrar";
  }
});

document.getElementById("profileMenu").addEventListener("click", (e) => {
  e.stopPropagation();
  document.getElementById("profileDropdown").classList.toggle("open");
});
document.addEventListener("click", () => {
  document.getElementById("profileDropdown").classList.remove("open");
});
document.getElementById("logoutBtn").addEventListener("click", () => {
  mostrarLogin();
});
document.getElementById("sidebarLogoutBtn")?.addEventListener("click", () => {
  clearToken();
  window.location.reload();
});

// ── App ───────────────────────────────────────────────
let alerts = [];
let health = [];
let activeFilter = "all";
let selectedAlert = null;
let selectedDate = formatDateInput(new Date());
let pdvFilterAll = true;
let selectedPdvs = new Set();
let pdvsConhecidos = [];
let auditIaItems = [];
let auditIaResult = "";

const table = document.getElementById("alertsTable");
const drawer = document.getElementById("alertDrawer");
const backdrop = document.getElementById("drawerBackdrop");
const varDrawer = document.getElementById("varDrawer");
const varBackdrop = document.getElementById("varDrawerBackdrop");
const toast = document.getElementById("toast");

function escapeText(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function carregarAlertas() {
  try {
    const params = new URLSearchParams({ loja: LOJA, filter: "all", data: selectedDate });
    if (!pdvFilterAll) selectedPdvs.forEach(pdv => params.append("pdv", pdv));
    const resp = await apiFetch(`/api/v1/alerts?${params}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    alerts = await resp.json();
  } catch (err) {
    // mantem os dados anteriores em caso de falha temporaria de rede
  }
  renderAlerts();
  renderAlertMetrics();
  renderOccurrenceTypes();
  // Atualizar view Alertas se estiver aberta
  if (document.getElementById("viewAlerts")?.style.display !== "none") {
    const dateInp = document.getElementById("alertsDateInput");
    if (dateInp) dateInp.value = selectedDate;
    _syncAlertDateBtns?.();
    renderAlertas2?.();
  }
}

async function carregarHealth() {
  try {
    const resp = await apiFetch(`/api/v1/health?loja=${LOJA}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    health = await resp.json();
  } catch (err) {
    // mantem os dados anteriores em caso de falha temporaria de rede
  }
  atualizarListaPdvs();
  renderHealth();
  renderHealthMetric();
}

function formatDateInput(d) {
  const ano = d.getFullYear();
  const mes = String(d.getMonth() + 1).padStart(2, "0");
  const dia = String(d.getDate()).padStart(2, "0");
  return `${ano}-${mes}-${dia}`;
}

function isHoje(dataStr) {
  return dataStr === formatDateInput(new Date());
}

function somarDias(dataStr, dias) {
  const [ano, mes, dia] = dataStr.split("-").map(Number);
  const dt = new Date(ano, mes - 1, dia);
  dt.setDate(dt.getDate() + dias);
  return formatDateInput(dt);
}

function atualizarRotuloData() {
  const span = document.getElementById("currentDate");
  const dateInput = document.getElementById("dateInput");
  dateInput.value = selectedDate;
  dateInput.max = formatDateInput(new Date());
  if (isHoje(selectedDate)) {
    span.textContent = "Hoje";
  } else {
    const [ano, mes, dia] = selectedDate.split("-").map(Number);
    const dt = new Date(ano, mes - 1, dia);
    span.textContent = dt.toLocaleDateString("pt-BR", { day: "2-digit", month: "short", year: "numeric" }).replace(".", "");
  }
  document.getElementById("nextDay").disabled = isHoje(selectedDate);
}

function mudarData(novaData) {
  selectedDate = novaData;
  atualizarRotuloData();
  carregarAlertas();
  carregarVendas(); carregarGeminiCredito();
  carregarItensCaixa();
  carregarStatsIA();
  // Recarregar cupons do PDV VAR se estiver visível
  if (document.getElementById("pdvVarSearch")?.style.display !== "none") {
    _carregarCuponsVar();
  }
  // Recarregar relatórios se estiver visível
  if (document.getElementById("viewReports")?.style.display !== "none") {
    iniciarViewRelatorios();
  }
}

function pdvSelecionado(pdv) {
  return pdvFilterAll || selectedPdvs.has(pdv);
}

function atualizarListaPdvs() {
  const novos = [...new Set(health.map(item => item.pdv))].sort();
  if (JSON.stringify(novos) === JSON.stringify(pdvsConhecidos)) return;
  pdvsConhecidos = novos;
  if (!pdvFilterAll) {
    selectedPdvs = new Set([...selectedPdvs].filter(pdv => pdvsConhecidos.includes(pdv)));
  }
  renderPdvFilter();
}

function renderPdvFilter() {
  document.getElementById("pdvFilterAll").checked = pdvFilterAll;
  const list = document.getElementById("pdvFilterList");
  list.innerHTML = pdvsConhecidos.map(pdv => `
    <label><input type="checkbox" data-pdv="${pdv}" ${pdvSelecionado(pdv) ? "checked" : ""}> PDV ${pdv}</label>
  `).join("");
  list.querySelectorAll("input[type='checkbox']").forEach(checkbox => {
    checkbox.addEventListener("change", () => {
      if (pdvFilterAll) {
        selectedPdvs = new Set(pdvsConhecidos);
        pdvFilterAll = false;
      }
      if (checkbox.checked) selectedPdvs.add(checkbox.dataset.pdv);
      else selectedPdvs.delete(checkbox.dataset.pdv);

      if (selectedPdvs.size === 0 || selectedPdvs.size === pdvsConhecidos.length) {
        pdvFilterAll = true;
        selectedPdvs = new Set();
      }
      aplicarFiltroPdv();
    });
  });
}

function atualizarRotuloFiltroPdvs() {
  const label = document.getElementById("pdvFilterLabel");
  if (pdvFilterAll) {
    label.textContent = "Todos os PDVs";
  } else if (selectedPdvs.size === 1) {
    label.textContent = `PDV ${[...selectedPdvs][0]}`;
  } else {
    label.textContent = `${selectedPdvs.size} PDVs`;
  }
}

function aplicarFiltroPdv() {
  renderPdvFilter();
  atualizarRotuloFiltroPdvs();
  renderHealth();
  renderHealthMetric();
  carregarAlertas();
  carregarVendas(); carregarGeminiCredito();
}

async function carregarVendas() {
  try {
    const params = new URLSearchParams({ loja: LOJA, data: selectedDate });
    if (!pdvFilterAll) selectedPdvs.forEach(pdv => params.append("pdv", pdv));
    const resp = await apiFetch(`/api/v1/sales?${params}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const vendas = await resp.json();
    document.getElementById("metricVendidoHoje").textContent =
      `R$ ${vendas.total.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    document.getElementById("metricCuponsFechados").textContent = vendas.cupons;
  } catch (err) {
    // mantem os dados anteriores em caso de falha temporaria de rede
  }
}

async function carregarGeminiCredito() {
  const STREAMER = (window.APP_CONFIG || {}).STREAMER_URL || "";
  const TOKEN    = (window.APP_CONFIG || {}).STREAMER_TOKEN || "";
  if (!STREAMER || !TOKEN) return;
  try {
    const r = await fetch(`${STREAMER}/gemini-stats-total?token=${TOKEN}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    const restante = Number(d.credito_restante_brl || 0);
    const gasto    = Number(d.gasto_total_brl || 0);
    const fotos    = d.fotos_analisadas || 0;
    const el = document.getElementById("metricGeminiCredito");
    const det = document.getElementById("metricGeminiDetalhe");
    if (el) {
      el.textContent = `R$ ${restante.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      el.style.color = restante < 5 ? "#c92a2a" : (restante < 20 ? "#b86b00" : "");
    }
    if (det) det.textContent = `${fotos} fotos · R$ ${gasto.toLocaleString("pt-BR", { minimumFractionDigits: 4, maximumFractionDigits: 4 })} gastos`;
  } catch (e) {
    // mantem valores anteriores em caso de falha temporaria
  }
}

function renderAlerts() {
  const query = document.getElementById("searchInput").value.toLowerCase();
  // Alertas recentes mostra só divergências reais — itens conferidos ficam na Auditoria IA
  const alertasReais = alerts.filter(a => a.severity !== "ok");
  const rows = alertasReais.filter(alert => {
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
      <td><div class="event-cell"><img class="mini-cctv" src="${alert.imageUrl || 'assets/frame-register.svg'}" ${alert.imageUrl ? `loading="lazy" onerror="this.src='assets/frame-register.svg';this.onerror=null"` : ''} alt=""><div><strong>${alert.event}</strong><span>${alert.subtitle}</span></div></div></td>
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
        document.getElementById("videoButton").click();
      } else {
        openDrawer(alert);
      }
    });
  });
  hydrateProtectedMedia(table);
  lucide.createIcons();
}

function renderHealth() {
  const grid = document.getElementById("healthGrid");
  const filtrado = health.filter(item => pdvSelecionado(item.pdv));
  if (filtrado.length === 0) {
    grid.innerHTML = `<div class="health-row"><strong>Sem dados de saude ainda.</strong></div>`;
    return;
  }
  grid.innerHTML = filtrado.map(item => `
    <div class="health-row" style="cursor:pointer" title="Abrir monitor do PDV ${item.pdv}" onclick="abrirMonitorTecnico(${item.pdv})">
      <strong>PDV ${String(item.pdv).padStart(2,"0")}</strong>
      ${serviceState(item.bridge)}
      ${serviceState(item.imhdx)}
      ${serviceState(item.audit)}
      <i data-lucide="chevron-right" style="width:13px;height:13px;color:var(--muted);flex-shrink:0"></i>
    </div>
  `).join("");
  lucide.createIcons();
}

function serviceState(state) {
  const label = state === "online" ? "Online" : state === "warning" ? "Atenção" : "Parada";
  return `<span class="service-state ${state}"><i></i>${label}</span>`;
}

function renderHealthMetric() {
  const filtrado = health.filter(item => pdvSelecionado(item.pdv));
  const total = filtrado.length;
  const online = filtrado.filter(item => item.bridge === "online" && item.imhdx === "online" && item.audit === "online").length;
  document.getElementById("metricPdvsTotal").firstChild.textContent = `${total} `;
  document.getElementById("metricPdvsOnline").textContent = `/ ${online} online`;
}

function renderAlertMetrics() {
  // Métricas de Alertas consideram só divergências reais — itens conferidos (severity "ok") são Auditoria IA
  const alertasReais = alerts.filter(a => a.severity !== "ok");
  const total = alertasReais.length;
  const pendentes = alertasReais.filter(alert => alert.state !== "resolved").length;
  const criticos = alertasReais.filter(alert => alert.severity === "critical" && alert.state !== "resolved").length;
  const emRevisao = alertasReais.filter(alert => alert.state !== "resolved" && alert.severity !== "critical").length;
  const resolvidos = alertasReais.filter(alert => alert.state === "resolved").length;

  document.getElementById("navAlertsBadge").textContent = pendentes;
  document.getElementById("notifBadge").textContent = pendentes;

  document.getElementById("metricAlertasPendentes").firstChild.textContent = `${pendentes} `;
  document.getElementById("metricAlertasCriticos").textContent = `${criticos} críticos`;

  document.getElementById("countAll").textContent = total;
  document.getElementById("countCritical").textContent = criticos;
  document.getElementById("countReview").textContent = emRevisao;
  document.getElementById("countResolved").textContent = resolvidos;

  // Card IA Divergências — alertas SmolVLM com resultado por estado
  const divs = alerts.filter(a => a.resultado === "DIVERGENCIA_CATEGORIA" || a.event === "Categoria divergente");
  const confirmadas = divs.filter(a => a.state === "resolved").length;
  const ignoradas   = divs.filter(a => a.state === "ignored").length;
  const emRevisaoDiv = divs.filter(a => a.state !== "resolved" && a.state !== "ignored").length;
  const elDiv = document.getElementById("metricDivergencias");
  const elDet = document.getElementById("metricDivergenciasDetalhe");
  if (elDiv) elDiv.textContent = divs.length;
  if (elDet) elDet.textContent = `${confirmadas} confirmadas · ${ignoradas} ignoradas${emRevisaoDiv ? ` · ${emRevisaoDiv} pendentes` : ''}`;

  document.getElementById("alertsFooterText").textContent = `Mostrando ${total} alertas de hoje`;
}

function renderOccurrenceTypes() { /* removido — seção retirada do dashboard */ }

const FRAME_FALLBACKS = {
  before: "assets/frame-before.svg",
  register: "assets/frame-register.svg",
  after: "assets/frame-after.svg",
};

const PANEL_WIDTH = 640;
const PANEL_HEIGHT = 520;

function aplicarEvidencia(imageUrl) {
  const frameButtons = document.querySelectorAll(".frame-strip button");

  function _renderFrames(src) {
    const img = new Image();
    img.onload = () => {
      const numPanels = Math.max(1, Math.round(img.naturalWidth / PANEL_WIDTH));
      const panelUrls = [];
      for (let i = 0; i < numPanels; i++) {
        const canvas = document.createElement("canvas");
        canvas.width = PANEL_WIDTH;
        canvas.height = PANEL_HEIGHT;
        canvas.getContext("2d").drawImage(
          img, i * PANEL_WIDTH, 0, PANEL_WIDTH, PANEL_HEIGHT, 0, 0, PANEL_WIDTH, PANEL_HEIGHT
        );
        panelUrls.push(canvas.toDataURL("image/jpeg"));
      }
      frameButtons.forEach((button, index) => {
        button.dataset.frame = panelUrls[Math.min(index, numPanels - 1)];
      });
      document.querySelectorAll(".frame-strip button").forEach(item => item.classList.remove("active"));
      const registerButton = frameButtons[1] || frameButtons[0];
      registerButton.classList.add("active");
      document.getElementById("mainEvidence").src = registerButton.dataset.frame;
    };
    img.onerror = () => {
      document.getElementById("mainEvidence").src = FRAME_FALLBACKS.register;
      frameButtons.forEach((button, index) => {
        const frame = index === 0 ? "before" : index === 2 ? "after" : "register";
        button.dataset.frame = FRAME_FALLBACKS[frame];
      });
    };
    img.src = src;
  }

  if (!imageUrl) {
    _renderFrames(FRAME_FALLBACKS.register);
    return;
  }
  // URLs protegidas precisam de Bearer token — buscar via fetch e criar blob URL
  mediaObjectUrl(imageUrl)
    .then(blobUrl => _renderFrames(blobUrl))
    .catch(() => _renderFrames(FRAME_FALLBACKS.register));
}

function _parsearAnaliseIA(analysis, note) {
  // Tenta extrair campos estruturados do texto de comparacao_pdv
  // Formato: "Cupom: PRODUTO (cat: CAT)\nCLIP: CAT (X%) | SmolVLM: RESULTADO"
  const result = { produto: "—", categoria: "—", clip: "—", smolvlm: "—", obs: "" };
  if (!analysis) return result;

  const linhas = analysis.split("\n");
  for (const linha of linhas) {
    // "Cupom: Bisc Trakinas (cat: BISCOITO)"
    const mCupom = linha.match(/Cupom:\s*(.+?)\s*\(cat:\s*([^)]+)\)/i);
    if (mCupom) { result.produto = mCupom[1].trim(); result.categoria = mCupom[2].trim(); }

    // "CLIP: BISCOITO (45%) | SmolVLM: SUSPICIOUS"
    const mClip = linha.match(/CLIP:\s*([^\s(]+)\s*\(([^)]+)\)/i);
    if (mClip) result.clip = `${mClip[1]} (${mClip[2]})`;

    const mVlm = linha.match(/SmolVLM:\s*(.+?)(\s*\(.*\))?\s*$/i);
    if (mVlm) result.smolvlm = mVlm[1].trim();
  }
  // Se nada do formato antigo (CLIP/SmolVLM) bateu, é análise Gemini em texto livre — mostra inteira
  if (result.clip === "—" && result.smolvlm === "—" && analysis) {
    result.smolvlm = analysis.replace(/^Groq Vision[^:]*:\s*/i, "").replace(/^Gemini Vision[^:]*:\s*/i, "");
  }
  if (note) result.obs = note;
  return result;
}

function openDrawer(alert) {
  selectedAlert = alert;

  // Header
  document.getElementById("drawerTitle").textContent = alert.product || alert.event;
  document.getElementById("drawerPdvLabel").textContent = alert.pdv || "—";
  document.getElementById("drawerCameraLabel").textContent = alert.pdv || "—";
  document.getElementById("cameraTime").textContent = alert.time || "—";

  // Badge resultado
  const badge = document.getElementById("resultBadge");
  badge.textContent = alert.result || "Divergência";
  badge.className = `result-badge ${alert.result === "Confere" ? "success" : alert.result === "Inconclusivo" || alert.result === "Revisar" ? "warning" : "danger"}`;

  // Diagnóstico IA
  const ia = _parsearAnaliseIA(alert.analysis, alert.note);
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val || "—"; };
  set("iaProdutoRegistrado", alert.product);
  set("iaSmolvlm", ia.smolvlm !== "—" ? ia.smolvlm : (alert.analysis || "Sem análise registrada"));
  const obsLinha = document.getElementById("iaObsLinha");
  if (ia.obs && obsLinha) {
    obsLinha.style.display = "";
    set("iaObs", ia.obs);
  } else if (obsLinha) {
    obsLinha.style.display = "none";
  }

  // Dados do item
  set("detailProduct",  alert.product);
  set("detailValue",    alert.value);
  set("detailQuantity", alert.qty);
  set("detailReceipt",  alert.receipt);
  set("detailPdv",      alert.pdv);
  set("detailTime",     alert.time);

  // Foto
  const mainEv = document.getElementById("mainEvidence");
  mainEv.src = "assets/frame-register.svg";
  if (alert.imageUrl) {
    if (alert.imageUrl.startsWith('/streamer/')) {
      // URLs do streamer já trazem o token próprio na query string — não usar JWT do usuário aqui
      mainEv.src = alert.imageUrl;
    } else if (alert.imageUrl.startsWith('/api/')) {
      mediaObjectUrl(alert.imageUrl)
        .then(blobUrl => { mainEv.src = blobUrl; })
        .catch(() => {});
    }
  }

  drawer.classList.add("open");
  backdrop.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
}

function closeDrawer() {
  // Reset foto/vídeo para próxima abertura
  const vid = document.getElementById("drawerVideo");
  const img = document.getElementById("mainEvidence");
  const btn = document.getElementById("videoButton");
  if (vid) { vid.pause(); vid.src = ""; vid.style.display = "none"; }
  if (img) img.style.display = "";
  if (btn) { btn.innerHTML = '<i data-lucide="play"></i>Ver vídeo'; lucide.createIcons(); }
  document.getElementById("drawerVideoLoading").style.display = "none";
  drawer.classList.remove("open");
  backdrop.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
}

let _voltarParaCupomsAposVideo = false;

function openVarDrawer() {
  varDrawer.classList.add("open");
  varBackdrop.classList.add("open");
  varDrawer.setAttribute("aria-hidden", "false");
}

function closeVarDrawer() {
  varDrawer.classList.remove("open");
  varBackdrop.classList.remove("open");
  varDrawer.setAttribute("aria-hidden", "true");
  // Restaurar tab bar para próxima abertura (pode ter sido ocultada pelo vídeo genérico)
  const tabBar = varDrawer.querySelector(".var-tab-bar");
  if (tabBar) tabBar.style.display = "";

  // Se o vídeo foi aberto a partir de Cupons, devolve o usuário pra lá em vez de deixá-lo no PDV VAR
  if (_voltarParaCupomsAposVideo) {
    _voltarParaCupomsAposVideo = false;
    if (_varCuponsTimer) { clearInterval(_varCuponsTimer); _varCuponsTimer = null; }
    document.getElementById("pdvCardsGrid").style.display = "";
    document.getElementById("pdvVarSearch").style.display = "none";
    document.getElementById("viewPdvCards").style.display = "none";
    document.getElementById("viewReceipts").style.display = "";
    document.querySelectorAll(".nav-item[data-view]").forEach(n => n.classList.remove("active"));
    document.querySelectorAll(".nav-item[data-view='receipts']").forEach(n => n.classList.add("active"));
    iniciarViewCupons();
  }
}

// ── Vídeo genérico no varDrawer (alertas, consultar) ──────────────────────
function _abrirVideoVarDrawer(titulo, breadcrumb, videoSrc) {
  const STREAMER = (window.APP_CONFIG || {}).STREAMER_URL || "";
  const TOKEN    = (window.APP_CONFIG || {}).STREAMER_TOKEN || "";

  document.getElementById("varResultModalTitle").textContent = titulo;
  document.getElementById("varResultModalBreadcrumb").textContent = breadcrumb;

  // Ocultar tabs, mostrar só vídeo
  const tabBar = varDrawer.querySelector(".var-tab-bar");
  if (tabBar) tabBar.style.display = "none";

  const body = document.getElementById("varResultModalBody");
  body.innerHTML = `
    <div class="var-inline-player">
      <video id="varVideoGenerico" controls playsinline webkit-playsinline preload="metadata"
             style="width:100%;display:none;background:#000"></video>
      <div id="varVideoGenericoStatus" style="text-align:center;padding:32px;color:var(--muted)">
        <i data-lucide="loader-circle" style="width:32px;height:32px;animation:spin 1s linear infinite"></i>
        <p style="margin-top:8px">Gerando vídeo…</p>
      </div>
      <div id="varVideoGenericoErr" hidden style="text-align:center;padding:32px;color:var(--muted)">
        <i data-lucide="video-off" style="width:32px;height:32px"></i>
        <p style="margin-top:8px">Vídeo não disponível para este evento.</p>
      </div>
    </div>`;
  lucide.createIcons();
  openVarDrawer();

  const video  = document.getElementById("varVideoGenerico");
  const status = document.getElementById("varVideoGenericoStatus");
  const err    = document.getElementById("varVideoGenericoErr");

  const onOk  = () => { status.hidden = true; video.style.display = ""; };
  const onErr = () => { status.hidden = true; err.hidden = false; };

  const _isMobile = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);

  if (videoSrc.includes('/clip?')) {
    fetch(videoSrc)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (!d?.token) { onErr(); return; }
        video.src = `${STREAMER}/clip/${d.token}?token=${TOKEN}`;
        video.addEventListener("loadedmetadata", onOk, { once: true });
        video.addEventListener("error", onErr, { once: true });
        video.load();
      })
      .catch(onErr);
  } else {
    video.src = videoSrc;
    video.addEventListener("loadedmetadata", onOk, { once: true });
    video.addEventListener("error", onErr, { once: true });
    video.load();
  }
}

function showToast(message) {
  toast.querySelector("span").textContent = message;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2500);
}

function openVideo() {
  document.getElementById("videoModal").classList.add("open");
  document.getElementById("videoMeta").textContent = `${selectedAlert.pdv} · Cupom ${selectedAlert.receipt}`;
  resetVideo();

  const video = document.getElementById("eventVideo");
  const unavailable = document.getElementById("videoUnavailable");
  video.hidden = false;
  unavailable.hidden = true;
  video.onerror = () => { video.hidden = true; unavailable.hidden = false; };

  // Para alertas SmolVLM (imageUrl do streamer), gerar vídeo pelo timestamp
  // Tem prioridade sobre videoUrl da API (que não tem arquivo para esses alertas)
  let videoSrc = null;
  if (selectedAlert.imageUrl && selectedAlert.imageUrl.startsWith('/streamer/snapshot')) {
    try {
      const url = new URL(selectedAlert.imageUrl, location.href);
      const ts = url.searchParams.get('ts');
      const token = url.searchParams.get('token') || (window.APP_CONFIG||{}).STREAMER_TOKEN || '';
      if (ts) {
        // Forçar formato ISO para compatibilidade entre browsers
        const dt = new Date(ts.replace(' ', 'T').replace('+', 'T'));
        const fmt = d => {
          const p = n => String(n).padStart(2,'0');
          return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
        };
        const start = fmt(new Date(dt.getTime() - 15000));
        const end   = fmt(new Date(dt.getTime() + 15000));
        const STREAMER = (window.APP_CONFIG||{}).STREAMER_URL || '/streamer';
        videoSrc = `${STREAMER}/clip?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&token=${token}`;
      }
    } catch(e) {}
  }
  // Fallback: usar videoUrl da API (alertas normais)
  if (!videoSrc) videoSrc = selectedAlert.videoUrl;

  if (!videoSrc) { video.hidden = true; unavailable.hidden = false; return; }

  const _isMobile = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);

  // Vídeo do streamer — /clip gera MP4 completo (retorna JSON com token)
  if (videoSrc.includes('/streamer/') && !videoSrc.includes('/api/v1/')) {
    if (videoSrc.includes('/clip?')) {
      // /clip?... retorna {token: "xxx"} — buscar e depois reproduzir /clip/{token}
      const STREAMER = (window.APP_CONFIG||{}).STREAMER_URL || '/streamer';
      const TOKEN = (window.APP_CONFIG||{}).STREAMER_TOKEN || '';
      fetch(videoSrc)
        .then(r => r.ok ? r.json() : null)
        .then(d => {
          if (!d?.token) { video.hidden = true; unavailable.hidden = false; return; }
          video.src = `${STREAMER}/clip/${d.token}?token=${TOKEN}`;
          video.load(); video.play().catch(() => {});
        })
        .catch(() => { video.hidden = true; unavailable.hidden = false; });
    } else {
      // Streaming fMP4 direto (usado pelo desktop em outros contextos)
      video.src = videoSrc; video.load(); video.play().catch(() => {});
    }
  } else {
    // URL protegida da API — usar blob
    mediaObjectUrl(videoSrc)
      .then(blobUrl => { video.src = blobUrl; video.load(); video.play().catch(() => {}); })
      .catch(() => { video.hidden = true; unavailable.hidden = false; });
  }
}

function resetVideo() {
  const video = document.getElementById("eventVideo");
  video.pause();
  video.currentTime = 0;
}

async function enviarDecisao(alertaId, action) {
  const obs = document.getElementById("drawerObsText")?.value?.trim() || "";
  try {
    await apiFetch(`/api/v1/alerts/${alertaId}/decision`, {
      method: "POST",
      body: JSON.stringify({ action, observacao: obs }),
    });
  } catch (err) {
    // erro de rede nao deve travar a UI
  }
  if (document.getElementById("drawerObsText")) document.getElementById("drawerObsText").value = "";
  await carregarAlertas();
}

document.querySelectorAll(".alert-tabs button").forEach(button => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".alert-tabs button").forEach(item => item.classList.remove("active"));
    button.classList.add("active");
    activeFilter = button.dataset.filter;
    renderAlerts();
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
document.getElementById("videoButton").addEventListener("click", () => {
  if (!selectedAlert) return;
  const img     = document.getElementById("mainEvidence");
  const vid     = document.getElementById("drawerVideo");
  const loading = document.getElementById("drawerVideoLoading");
  const btn     = document.getElementById("videoButton");

  // Toggle: se vídeo visível → volta para foto
  if (vid.style.display !== "none") {
    vid.pause(); vid.src = "";
    vid.style.display = "none";
    loading.style.display = "none";
    img.style.display = "";
    btn.innerHTML = '<i data-lucide="play"></i>Ver vídeo';
    lucide.createIcons();
    return;
  }

  // Montar URL do clip
  let videoSrc = null;
  if (selectedAlert.imageUrl && selectedAlert.imageUrl.startsWith('/streamer/snapshot')) {
    try {
      const url   = new URL(selectedAlert.imageUrl, location.href);
      const ts    = url.searchParams.get('ts');
      const token = url.searchParams.get('token') || (window.APP_CONFIG||{}).STREAMER_TOKEN || '';
      if (ts) {
        const dt  = new Date(ts.replace(' ','T'));
        const fmt = d => { const p = n => String(n).padStart(2,'0'); return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; };
        const STREAMER = (window.APP_CONFIG||{}).STREAMER_URL || '/streamer';
        videoSrc = `${STREAMER}/clip?start=${encodeURIComponent(fmt(new Date(dt.getTime()-15000)))}&end=${encodeURIComponent(fmt(new Date(dt.getTime()+15000)))}&token=${token}`;
      }
    } catch(e) {}
  }
  if (!videoSrc) { showToast("Sem vídeo para este alerta."); return; }

  // Mostrar loading, esconder foto
  img.style.display = "none";
  loading.style.display = "flex";
  vid.style.display = "none";
  btn.innerHTML = '<i data-lucide="image"></i>Ver foto';
  lucide.createIcons();

  const STREAMER = (window.APP_CONFIG||{}).STREAMER_URL || '/streamer';
  const TOKEN    = (window.APP_CONFIG||{}).STREAMER_TOKEN || '';

  fetch(videoSrc)
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      loading.style.display = "none";
      if (!d?.token) { img.style.display = ""; btn.innerHTML = '<i data-lucide="play"></i>Ver vídeo'; lucide.createIcons(); showToast("Vídeo não disponível."); return; }
      vid.src = `${STREAMER}/clip/${d.token}?token=${TOKEN}`;
      vid.style.display = "";
      vid.load(); vid.play().catch(() => {});
    })
    .catch(() => {
      loading.style.display = "none";
      img.style.display = "";
      btn.innerHTML = '<i data-lucide="play"></i>Ver vídeo';
      lucide.createIcons();
      showToast("Erro ao carregar vídeo.");
    });
});
document.getElementById("closeVideo").addEventListener("click", () => {
  document.getElementById("videoModal").classList.remove("open");
  resetVideo();
});
function _closeMobileSidebar() {
  document.querySelector(".sidebar").classList.remove("open");
  document.getElementById("mobileBackdrop")?.classList.remove("open");
}
function _openMobileSidebar() {
  document.querySelector(".sidebar").classList.add("open");
  document.getElementById("mobileBackdrop")?.classList.add("open");
}
document.querySelector(".mobile-menu").addEventListener("click", () => {
  const isOpen = document.querySelector(".sidebar").classList.contains("open");
  isOpen ? _closeMobileSidebar() : _openMobileSidebar();
});
document.getElementById("mobileBackdrop")?.addEventListener("click", _closeMobileSidebar);
document.querySelectorAll(".nav-group-toggle").forEach(toggle => {
  toggle.addEventListener("click", () => {
    toggle.closest(".nav-group").classList.toggle("open");
  });
});

const VIEWS = ["viewUsers", "viewLojas", "viewPdvCards", "viewReceipts", "viewConsultar", "viewAlerts", "viewAuditIa", "viewReports", "viewConfigLoja", "viewConfigAuditoria", "viewConfigCamera", "viewConfigNotificacoes"];

document.querySelectorAll(".nav-item[data-view]").forEach(item => {
  item.addEventListener("click", () => {
    document.querySelectorAll(".nav-item[data-view]").forEach(nav => nav.classList.remove("active"));
    item.classList.add("active");
    _closeMobileSidebar(); // fecha menu no mobile ao navegar
    const view = item.dataset.view;
    const mainWorkspace = document.querySelector(".workspace:not([id])");
    let isSubView = false;
    VIEWS.forEach(id => {
      const el = document.getElementById(id);
      const show = (id === "viewUsers" && (view === "users" || view === "config-usuarios")) ||
                   (id === "viewLojas" && view === "lojas") ||
                   (id === "viewPdvCards" && view === "terminals") ||
                   (id === "viewReceipts" && view === "receipts") ||
                   (id === "viewConsultar" && view === "consultar") ||
                   (id === "viewAlerts" && view === "alerts") ||
                   (id === "viewAuditIa" && view === "auditIa") ||
                   (id === "viewReports" && view === "reports") ||
                   (id === "viewConfigLoja" && view === "config-loja") ||
                   (id === "viewConfigAuditoria" && view === "config-auditoria") ||
                   (id === "viewConfigCamera" && view === "config-camera") ||
                   (id === "viewConfigNotificacoes" && view === "config-notificacoes");
      if (el) el.style.display = show ? "" : "none";
      if (show) isSubView = true;
    });
    if (mainWorkspace) mainWorkspace.style.display = isSubView ? "none" : "";
    if (view === "users" || view === "config-usuarios") carregarUsuarios();
    else if (view === "lojas") carregarLojas();
    else if (view === "terminals") carregarCardsPdv();
    else if (view === "receipts") iniciarViewCupons();
    else if (view === "consultar") iniciarViewConsultar();
    else if (view === "alerts") iniciarViewAlertas();
    else if (view === "auditIa") iniciarViewAuditIa();
    else if (view === "reports") iniciarViewRelatorios();
    else if (view === "config-loja") iniciarViewConfigLoja();
    else if (view === "config-auditoria") iniciarViewConfigAuditoria();
    else if (view === "config-camera") iniciarViewConfigCamera();
    else if (view === "config-notificacoes") iniciarViewConfigNotificacoes();
  });
});

document.getElementById("lojaFilterButton").addEventListener("click", async () => {
  const menu = document.getElementById("lojaFilterMenu");
  const lista = document.getElementById("lojaFilterList");
  // Carregar lojas na abertura (evita problema de timing)
  if (lista && lista.children.length === 0) {
    const r = await apiFetch("/api/v1/lojas");
    if (r.ok) {
      const lojas = await r.json();
      const nomeEl = document.getElementById("topbarLojaNome");
      const todasChecked = !LOJA || lojas.every(l => l.id !== LOJA);
      lista.innerHTML =
        `<label><input type="checkbox" id="lojaFilterAll" ${todasChecked?"checked":""}> Todas as lojas</label>` +
        `<hr>` +
        (lojas.map(l =>
          `<label><input type="checkbox" class="lojaCheck" value="${l.id}" ${LOJA===l.id&&!todasChecked?"checked":""}> ${l.nome}</label>`
        ).join("") || `<label style="color:var(--muted);pointer-events:none">Nenhuma loja cadastrada</label>`);
      if (todasChecked && nomeEl) nomeEl.textContent = "Todas as lojas";
      const allChk = lista.querySelector("#lojaFilterAll");
      allChk?.addEventListener("change", () => {
        lista.querySelectorAll(".lojaCheck").forEach(c => c.checked = false);
        LOJA = lojas.length > 0 ? lojas[0].id : (LOJA || "");
        if (nomeEl) nomeEl.textContent = "Todas as lojas";
        lista.innerHTML = "";
        menu.classList.remove("open");
        carregarAlertas(); carregarVendas(); carregarGeminiCredito(); carregarHealth();
      });
      lista.querySelectorAll(".lojaCheck").forEach(inp => {
        inp.addEventListener("change", () => {
          if (allChk) allChk.checked = false;
          lista.querySelectorAll(".lojaCheck").forEach(c => { if (c !== inp) c.checked = false; });
          const loja = lojas.find(l => l.id === inp.value);
          LOJA = inp.value;
          if (nomeEl) nomeEl.textContent = loja ? loja.nome : LOJA;
          lista.innerHTML = "";
          menu.classList.remove("open");
          carregarAlertas(); carregarVendas(); carregarGeminiCredito(); carregarHealth();
        });
      });
    }
  }
  menu.classList.toggle("open");
});
document.addEventListener("click", e => {
  if (!e.target.closest(".store-selector")) {
    document.getElementById("lojaFilterMenu")?.classList.remove("open");
  }
});

document.getElementById("pdvFilterButton").addEventListener("click", () => {
  document.getElementById("pdvFilterMenu").classList.toggle("open");
});
document.addEventListener("click", event => {
  if (!document.querySelector(".pdv-filter").contains(event.target)) {
    document.getElementById("pdvFilterMenu").classList.remove("open");
  }
});
document.getElementById("pdvFilterAll").addEventListener("change", event => {
  pdvFilterAll = event.target.checked;
  selectedPdvs = pdvFilterAll ? new Set() : new Set(pdvsConhecidos);
  aplicarFiltroPdv();
});

document.getElementById("prevDay").addEventListener("click", () => mudarData(somarDias(selectedDate, -1)));
document.getElementById("nextDay").addEventListener("click", () => {
  if (!isHoje(selectedDate)) mudarData(somarDias(selectedDate, 1));
});
document.getElementById("dateLabelButton").addEventListener("click", () => {
  const input = document.getElementById("dateInput");
  if (input.showPicker) input.showPicker();
  else input.focus();
});
document.getElementById("dateInput").addEventListener("change", event => mudarData(event.target.value));

// ── Usuários ──────────────────────────────────────────
let usuarioEditandoId = null;
let usuarioSenhaId = null;
const PERFIL_LABELS = { admin: "Administrador", supervisor: "Supervisor", operador: "Operador" };

async function carregarUsuarios() {
  const resp = await apiFetch("/api/v1/usuarios");
  if (!resp.ok) return;
  const usuarios = await resp.json();
  const tbody = document.getElementById("usuariosTable");
  if (usuarios.length === 0) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="6">Nenhum usuário cadastrado.</td></tr>`;
    return;
  }
  tbody.innerHTML = usuarios.map(u => `
    <tr>
      <td><strong>${u.nome}</strong></td>
      <td>${u.email}</td>
      <td><span class="state-badge ${u.perfil === 'admin' ? 'resolved' : u.perfil === 'supervisor' ? 'review' : 'pending'}">${PERFIL_LABELS[u.perfil] || u.perfil}</span></td>
      <td>${u.loja_id || '<span style="color:var(--muted)">Global</span>'}</td>
      <td><span class="${u.ativo ? 'badge-ativo' : 'badge-inativo'}">${u.ativo ? 'Ativo' : 'Inativo'}</span></td>
      <td>
        <div class="row-actions">
          <button data-action="edit" data-id="${u.id}" title="Editar"><i data-lucide="pencil"></i></button>
          <button data-action="senha" data-id="${u.id}" data-nome="${u.nome}" title="Trocar senha"><i data-lucide="key-round"></i></button>
          <button data-action="toggle" data-id="${u.id}" data-ativo="${u.ativo}" title="${u.ativo ? 'Desativar' : 'Reativar'}"><i data-lucide="${u.ativo ? 'user-x' : 'user-check'}"></i></button>
        </div>
      </td>
    </tr>
  `).join("");
  tbody.querySelectorAll("button[data-action]").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.id);
      const u = usuarios.find(x => x.id === id);
      if (btn.dataset.action === "edit") abrirModalUsuario(u);
      else if (btn.dataset.action === "senha") abrirModalSenha(id, btn.dataset.nome);
      else if (btn.dataset.action === "toggle") toggleUsuario(id, btn.dataset.ativo === "1" || btn.dataset.ativo === "true");
    });
  });
  lucide.createIcons();
}

function abrirModalUsuario(usuario = null) {
  usuarioEditandoId = usuario ? usuario.id : null;
  document.getElementById("modalUsuarioTitulo").textContent = usuario ? "Editar usuário" : "Novo usuário";
  document.getElementById("uNome").value = usuario?.nome || "";
  document.getElementById("uEmail").value = usuario?.email || "";
  document.getElementById("uPerfil").value = usuario?.perfil || "";
  document.getElementById("uLoja").value = usuario?.loja_id || "";
  document.getElementById("uSenha").value = "";
  document.getElementById("uSenhaLabel").style.display = usuario ? "none" : "flex";
  document.getElementById("uSenha").required = !usuario;
  document.getElementById("modalUsuarioErro").hidden = true;
  document.getElementById("modalUsuario").style.display = "flex";
  lucide.createIcons();
}

function fecharModalUsuario() {
  document.getElementById("modalUsuario").style.display = "none";
}

function abrirModalSenha(id, nome) {
  usuarioSenhaId = id;
  document.getElementById("modalSenhaNome").textContent = `Usuário: ${nome}`;
  document.getElementById("novaSenha").value = "";
  document.getElementById("modalSenhaErro").hidden = true;
  document.getElementById("modalSenha").style.display = "flex";
}

function fecharModalSenha() {
  document.getElementById("modalSenha").style.display = "none";
}

async function toggleUsuario(id, ativo) {
  const resp = await apiFetch(`/api/v1/usuarios/${id}`, {
    method: "PUT",
    body: JSON.stringify({ ativo: ativo ? 0 : 1 }),
  });
  if (resp.ok) { showToast(ativo ? "Usuário desativado." : "Usuário reativado."); carregarUsuarios(); }
}

document.getElementById("btnNovoUsuario").addEventListener("click", () => abrirModalUsuario());
document.getElementById("closeModalUsuario").addEventListener("click", fecharModalUsuario);
document.getElementById("cancelarModalUsuario").addEventListener("click", fecharModalUsuario);
document.getElementById("closeModalSenha").addEventListener("click", fecharModalSenha);
document.getElementById("cancelarModalSenha").addEventListener("click", fecharModalSenha);

document.getElementById("formUsuario").addEventListener("submit", async (e) => {
  e.preventDefault();
  const erro = document.getElementById("modalUsuarioErro");
  erro.hidden = true;
  const body = {
    nome: document.getElementById("uNome").value,
    email: document.getElementById("uEmail").value,
    perfil: document.getElementById("uPerfil").value,
    loja_id: document.getElementById("uLoja").value || null,
  };
  if (!usuarioEditandoId) body.senha = document.getElementById("uSenha").value;

  const resp = await apiFetch(
    usuarioEditandoId ? `/api/v1/usuarios/${usuarioEditandoId}` : "/api/v1/usuarios",
    { method: usuarioEditandoId ? "PUT" : "POST", body: JSON.stringify(body) }
  );
  if (!resp.ok) {
    const data = await resp.json();
    erro.textContent = data.detail || "Erro ao salvar.";
    erro.hidden = false;
    return;
  }
  fecharModalUsuario();
  showToast(usuarioEditandoId ? "Usuário atualizado." : "Usuário criado.");
  carregarUsuarios();
});

document.getElementById("formSenha").addEventListener("submit", async (e) => {
  e.preventDefault();
  const erro = document.getElementById("modalSenhaErro");
  erro.hidden = true;
  const resp = await apiFetch(`/api/v1/usuarios/${usuarioSenhaId}/senha`, {
    method: "POST",
    body: JSON.stringify({ nova_senha: document.getElementById("novaSenha").value }),
  });
  if (!resp.ok) {
    const data = await resp.json();
    erro.textContent = data.detail || "Erro ao salvar.";
    erro.hidden = false;
    return;
  }
  fecharModalSenha();
  showToast("Senha atualizada.");
});

// ── PDV Cards / VAR ───────────────────────────────────
let varPdvSelecionado = null;
let varHealthData = [];

async function carregarCardsPdv() {
  document.getElementById("pdvCardsGrid").style.display = "";
  document.getElementById("pdvVarSearch").style.display = "none";
  try {
    const resp = await apiFetch(`/api/v1/health?loja=${LOJA}`);
    if (!resp.ok) return;
    varHealthData = await resp.json();
  } catch { return; }

  const container = document.getElementById("pdvCardsContainer");
  if (varHealthData.length === 0) {
    container.innerHTML = `<p style="color:var(--muted);font-size:13px">Nenhum PDV com dados de saúde ainda. Configure o bridge e aguarde o primeiro heartbeat.</p>`;
    return;
  }

  const dot = s => `<span class="pdv-status-dot ${s === "online" ? "online" : s === "warning" ? "warning" : "offline"}"></span>`;
  const pill = (label, s) => `<span class="pdv-status-pill">${dot(s)}${label}</span>`;
  container.innerHTML = varHealthData.map(h => {
    const geral = (h.bridge === "online" && h.imhdx === "online") ? "online" : "offline";
    return `
    <div class="pdv-card" data-pdv="${h.pdv}">
      <div class="pdv-card-header">
        <div class="pdv-card-icon"><i data-lucide="monitor"></i></div>
        <div class="pdv-card-title">
          <div class="pdv-card-name">PDV ${String(h.pdv).padStart(2,"0")}</div>
          <div class="pdv-card-loja">${LOJA_NOME}</div>
        </div>
        <span class="pdv-overall-badge ${geral}">${geral === "online" ? "Online" : "Atenção"}</span>
      </div>
      <div class="pdv-card-status">
        ${pill("Bridge", h.bridge)}
        ${pill("iMHDX", h.imhdx)}
        ${pill("Auditoria", h.audit)}
      </div>
      <div class="pdv-card-actions">
        <button data-pdv-search="${h.pdv}" title="Consultar cupom"><i data-lucide="search"></i></button>
        <button data-pdv-live="${h.pdv}" title="Câmera ao vivo"><i data-lucide="video"></i></button>
      </div>
    </div>`;
  }).join("");

  container.querySelectorAll("[data-pdv-search]").forEach(el => {
    el.addEventListener("click", e => {
      e.stopPropagation();
      abrirVarSearch(el.dataset.pdvSearch);
    });
  });
  container.querySelectorAll("[data-pdv-live]").forEach(el => {
    el.addEventListener("click", e => {
      e.stopPropagation();
      abrirCameraAoVivo(el.dataset.pdvLive);
    });
  });
  lucide.createIcons();
}

// ── Câmera ao vivo (stream MJPEG contínuo via streamer) ──────────────────────
function abrirCameraAoVivo(pdv) {
  const STREAMER = (window.APP_CONFIG || {}).STREAMER_URL || "";
  const TOKEN    = (window.APP_CONFIG || {}).STREAMER_TOKEN || "";
  const modal   = document.getElementById("liveCameraModal");
  const img     = document.getElementById("liveCameraImg");
  const label   = document.getElementById("liveCameraLabel");
  const loading = document.getElementById("liveCameraLoading");
  if (!modal || !img) return;

  label.textContent = `PDV ${String(pdv).padStart(2,"0")} · ${LOJA_NOME}`;
  loading.style.display = "flex";
  modal.style.display = "flex";
  img.onload = () => { loading.style.display = "none"; };
  // <img> com fonte MJPEG: o navegador renderiza os frames conforme chegam, sem JS de polling
  img.src = `${STREAMER}/live-stream?token=${TOKEN}&_=${Date.now()}`;
}

function fecharCameraAoVivo() {
  const modal   = document.getElementById("liveCameraModal");
  const img     = document.getElementById("liveCameraImg");
  const loading = document.getElementById("liveCameraLoading");
  if (img) { img.onload = null; img.src = ""; }
  if (loading) loading.style.display = "none";
  if (modal) modal.style.display = "none";
}

// ── Monitor Técnico ──────────────────────────────────────────────────────────
function fecharMonitorTecnico() {
  document.getElementById("monitorTecnicoModal").style.display = "none";
}

async function abrirMonitorTecnico(pdvParam) {
  const modal = document.getElementById("monitorTecnicoModal");
  const body  = document.getElementById("monitorTecnicoBody");
  const label = document.getElementById("monitorPdvLabel");
  const STREAMER = (window.APP_CONFIG || {}).STREAMER_URL || "";
  const TOKEN    = (window.APP_CONFIG || {}).STREAMER_TOKEN || "";

  const disponiveis = health.filter(item => pdvSelecionado(item.pdv));

  // Se nenhum PDV foi especificado e há mais de um, mostra seletor
  if (!pdvParam && disponiveis.length > 1) {
    label.textContent = `${disponiveis.length} PDVs · ${LOJA_NOME}`;
    modal.style.display = "flex";
    lucide.createIcons();
    body.innerHTML = `
      <p style="font-size:13px;color:var(--muted);margin-bottom:12px">Selecione o PDV para inspecionar:</p>
      <div style="display:flex;flex-direction:column;gap:8px">
        ${disponiveis.map(item => {
          const geral = (item.bridge === "online" && item.imhdx === "online") ? "#2f9e44" : "#e03131";
          return `<button onclick="abrirMonitorTecnico(${item.pdv})"
            style="display:flex;align-items:center;gap:12px;padding:14px 16px;border:1px solid var(--border);border-radius:10px;background:var(--bg);cursor:pointer;text-align:left">
            <span style="width:10px;height:10px;border-radius:50%;background:${geral};flex-shrink:0"></span>
            <div>
              <div style="font-weight:700;font-size:14px">PDV ${String(item.pdv).padStart(2,"0")}</div>
              <div style="font-size:11px;color:var(--muted)">Bridge: ${item.bridge} · iMHDX: ${item.imhdx} · Audit: ${item.audit}</div>
            </div>
            <i data-lucide="chevron-right" style="width:15px;height:15px;color:var(--muted);margin-left:auto"></i>
          </button>`;
        }).join("")}
      </div>`;
    lucide.createIcons();
    return;
  }

  const pdv = pdvParam || disponiveis[0]?.pdv || 1;
  const temMultiplos = disponiveis.length > 1;

  label.textContent = `PDV ${String(pdv).padStart(2,"0")} · ${LOJA_NOME}`;
  modal.style.display = "flex";
  lucide.createIcons();

  // ── Coleta paralela de dados ─────────────────────────────────────────────
  const t0 = Date.now();
  const [statsRes, geminiRes, snapshotRes] = await Promise.allSettled([
    fetch(`${STREAMER}/stats?date=${selectedDate}&token=${TOKEN}`).then(r => r.ok ? r.json() : null),
    fetch(`${STREAMER}/gemini-stats-total?token=${TOKEN}`).then(r => r.ok ? r.json() : null),
    fetch(`${STREAMER}/live-snapshot?token=${TOKEN}&_=${Date.now()}`).then(r => ({ ok: r.ok, ms: Date.now() - t0 })),
  ]);
  const stats  = statsRes.value;
  const gemini = geminiRes.value;
  const dvr    = snapshotRes.value || { ok: false, ms: 0 };

  const ok  = s => `<span style="color:#2f9e44;font-weight:600">● Online</span>`;
  const off = s => `<span style="color:#e03131;font-weight:600">● Offline</span>`;
  const warn = s => `<span style="color:#f08c00;font-weight:600">● Atenção</span>`;
  const dot = s => s === "online" ? ok() : s === "warning" ? warn() : off();

  const sectionStyle = "background:var(--bg);border-radius:10px;padding:14px 16px";
  const rowStyle = "display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--border);font-size:13px";
  const lastRowStyle = "display:flex;justify-content:space-between;align-items:center;padding:7px 0;font-size:13px";

  const h = health.find(x => x.pdv == pdv) || {};

  body.innerHTML = `
    <!-- Conectividade dos serviços -->
    <div style="${sectionStyle}">
      <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.05em;text-transform:uppercase;margin-bottom:10px">Serviços</div>
      <div style="${rowStyle}"><span>Bridge (spy file)</span>${dot(h.bridge)}</div>
      <div style="${rowStyle}"><span>iMHDX (DVR)</span>${dot(h.imhdx)}</div>
      <div style="${rowStyle}"><span>Auditoria IA</span>${dot(h.audit)}</div>
      <div style="${lastRowStyle}"><span>Câmera ao vivo (snapshot)</span>${dvr.ok ? `<span style="color:#2f9e44;font-weight:600">● OK <span style="font-weight:400;color:var(--muted)">${dvr.ms}ms</span></span>` : `<span style="color:#e03131;font-weight:600">● Sem resposta</span>`}</div>
    </div>

    <!-- Atividade do dia -->
    <div style="${sectionStyle}">
      <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.05em;text-transform:uppercase;margin-bottom:10px">Atividade — ${selectedDate}</div>
      <div style="${rowStyle}"><span>Cupons processados</span><strong>${stats?.total_cupons ?? '—'}</strong></div>
      <div style="${lastRowStyle}"><span>Itens registrados</span><strong>${stats?.total_itens ?? '—'}</strong></div>
    </div>

    <!-- Gemini / IA -->
    <div style="${sectionStyle}">
      <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.05em;text-transform:uppercase;margin-bottom:10px">IA Gemini</div>
      <div style="${rowStyle}"><span>Crédito restante</span><strong style="color:${(gemini?.credito_restante_brl||0)<5?'#e03131':(gemini?.credito_restante_brl||0)<20?'#f08c00':'#2f9e44'}">R$ ${Number(gemini?.credito_restante_brl||0).toLocaleString("pt-BR",{minimumFractionDigits:2,maximumFractionDigits:2})}</strong></div>
      <div style="${rowStyle}"><span>Fotos analisadas (total)</span><strong>${gemini?.total_fotos ?? '—'}</strong></div>
      <div style="${lastRowStyle}"><span>Custo total</span><strong>R$ ${Number(gemini?.total_gasto_brl||0).toLocaleString("pt-BR",{minimumFractionDigits:2,maximumFractionDigits:2})}</strong></div>
    </div>

    <!-- Ações rápidas -->
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button onclick="fecharMonitorTecnico();abrirCameraAoVivo(${pdv})" style="flex:1;min-width:140px;height:38px;border:1px solid var(--border);border-radius:8px;background:var(--surface);cursor:pointer;font-size:13px;font-weight:600;display:flex;align-items:center;justify-content:center;gap:6px;color:var(--primary)">
        <i data-lucide="video" style="width:15px;height:15px"></i> Câmera ao vivo
      </button>
      <button onclick="mudarData(selectedDate);fecharMonitorTecnico()" style="flex:1;min-width:140px;height:38px;border:1px solid var(--border);border-radius:8px;background:var(--surface);cursor:pointer;font-size:13px;font-weight:600;display:flex;align-items:center;justify-content:center;gap:6px">
        <i data-lucide="refresh-cw" style="width:15px;height:15px"></i> Recarregar dados
      </button>
      ${temMultiplos ? `<button onclick="abrirMonitorTecnico()" style="flex:1;min-width:140px;height:38px;border:1px solid var(--border);border-radius:8px;background:var(--surface);cursor:pointer;font-size:13px;font-weight:600;display:flex;align-items:center;justify-content:center;gap:6px">
        <i data-lucide="arrow-left" style="width:15px;height:15px"></i> Outros PDVs
      </button>` : ""}
    </div>
  `;
  lucide.createIcons();
}

let _varCuponsTimer = null;

function abrirVarSearch(pdv) {
  varPdvSelecionado = pdv;
  document.getElementById("pdvCardsGrid").style.display = "none";
  document.getElementById("pdvVarSearch").style.display = "";
  document.getElementById("varBreadcrumb").textContent = `PDV ${String(pdv).padStart(2,"0")} · ${LOJA_NOME}`;
  document.getElementById("varCupomInput").value = "";
  document.getElementById("varItemInput").value = "";
  lucide.createIcons();
  _carregarCuponsVar();
  // Auto-refresh a cada 15s enquanto a página estiver aberta
  if (_varCuponsTimer) clearInterval(_varCuponsTimer);
  _varCuponsTimer = setInterval(() => {
    if (document.getElementById("pdvVarSearch")?.style.display !== "none") {
      _carregarCuponsVar();
    }
  }, 15000);
}

async function _carregarCuponsVar() {
  const STREAMER = (window.APP_CONFIG||{}).STREAMER_URL || "";
  const TOKEN    = (window.APP_CONFIG||{}).STREAMER_TOKEN || "";
  const today    = selectedDate; // usa o seletor global do topo
  const tbody    = document.getElementById("varCuponsBody");
  const resumo   = document.getElementById("varCuponsResumo");
  if (!tbody) return;

  try {
    const r = await fetch(`${STREAMER}/cupons?date=${today}&token=${TOKEN}`);
    if (!r.ok) throw new Error("streamer offline");
    const d = await r.json();
    const cupons = (d.cupons || []).slice().reverse(); // mais recente primeiro

    // Mapear alertas por cupom
    const alertasPorCupom = {};
    (alerts || []).forEach(a => {
      const num = String(a.receipt || "").replace(/\D/g,"");
      alertasPorCupom[num] = (alertasPorCupom[num] || 0) + 1;
    });

    if (!cupons.length) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="8" style="text-align:center;padding:24px;color:var(--muted)">Nenhum cupom hoje.</td></tr>`;
      return;
    }

    if (resumo) resumo.textContent = `${cupons.length} cupons · ${cupons.filter(c=>c.fechou).length} fechados`;

    tbody.innerHTML = cupons.slice(0,10).map(c => {
      const numStr = String(c.numero||"");
      const nalerts = alertasPorCupom[numStr] || 0;
      const badge = nalerts > 0
        ? `<span data-badge-cupom="${c.numero}" style="display:inline-flex;align-items:center;gap:3px;background:#fff5f5;color:#c92a2a;border:1px solid #ffc9c9;border-radius:12px;padding:2px 8px;font-size:10px;font-weight:700;white-space:nowrap;cursor:pointer" title="Ver alertas do cupom ${c.numero}"><i data-lucide="triangle-alert" style="width:10px;height:10px"></i>${nalerts}</span>`
        : `<span style="display:inline-flex;align-items:center;gap:3px;background:#ebfbee;color:#2f9e44;border:1px solid #b2f2bb;border-radius:12px;padding:2px 8px;font-size:10px;font-weight:700">✓</span>`;
      const total = fmtBRL(c.total);
      const topItem = c.item_top ? `<span style="color:var(--primary);margin-right:4px">★</span>${c.item_top}` : '<span style="color:var(--border)">—</span>';
      return `<tr style="cursor:pointer" data-cupom="${c.numero}">
        <td>${(c.abriu||"").slice(0,5)}</td>
        <td><strong>${c.numero}</strong></td>
        <td style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${c.operador||"—"}</td>
        <td style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px">${topItem}</td>
        <td style="text-align:center">${c.itens||0}</td>
        <td style="text-align:right;font-weight:600;white-space:nowrap">${total}</td>
        <td style="text-align:center">${badge}</td>
        <td style="text-align:center">
          <div style="display:flex;justify-content:center;gap:4px">
            <button class="icon-button" data-action="nota" data-cupom="${c.numero}" title="Ver cupom" style="border:1px solid var(--border);border-radius:6px;width:30px;height:30px"><i data-lucide="file-text" style="width:14px;height:14px"></i></button>
            <button class="icon-button" data-action="video" data-cupom="${c.numero}" title="Ver vídeo" style="border:1px solid var(--border);border-radius:6px;width:30px;height:30px"><i data-lucide="play-circle" style="width:14px;height:14px;color:var(--primary)"></i></button>
          </div>
        </td>
      </tr>`;
    }).join("");

    lucide.createIcons();

    tbody.querySelectorAll("tr[data-cupom]").forEach(row => {
      row.addEventListener("click", e => {
        const btn = e.target.closest("button[data-action]");
        const badge = e.target.closest("[data-badge-cupom]");
        if (badge) {
          // Navegar para Alertas filtrado pelo cupom
          const cupom = badge.dataset.badgeCupom;
          const alertsBtn = document.querySelector(".nav-item[data-view='alerts']");
          if (alertsBtn) alertsBtn.click();
          setTimeout(() => {
            const inp = document.getElementById("searchInput2");
            if (inp) { inp.value = cupom; inp.dispatchEvent(new Event("input")); }
          }, 100);
          return;
        }
        if (btn?.dataset.action === "nota") { abrirCupomDrawer(btn.dataset.cupom); return; }
        if (btn?.dataset.action === "video") { abrirVideoCompra(btn.dataset.cupom); return; }
        abrirCupomDrawer(row.dataset.cupom);
      });
    });

  } catch(e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="8" style="text-align:center;padding:24px;color:var(--muted)">Streamer offline — não foi possível carregar cupons.</td></tr>`;
  }
}

document.getElementById("btnVoltarCards").addEventListener("click", () => {
  if (_varCuponsTimer) { clearInterval(_varCuponsTimer); _varCuponsTimer = null; }
  closeVarDrawer();
  document.getElementById("pdvCardsGrid").style.display = "";
  document.getElementById("pdvVarSearch").style.display = "none";
});


document.querySelector('input[name="varTipo"]').addEventListener && document.querySelectorAll('input[name="varTipo"]').forEach(r => {
  r.addEventListener("change", () => {
    document.getElementById("varItemField").style.display =
      document.querySelector('input[name="varTipo"]:checked').value === "item" ? "" : "none";
  });
});

document.getElementById("closeVarResult").addEventListener("click", closeVarDrawer);
varBackdrop.addEventListener("click", closeVarDrawer);

let varResultLista = [];
let varAbaAtiva = "fotos";
let varTipoAtivo = "all";

function renderVarBody() {
  const body = document.getElementById("varResultModalBody");
  if (varResultLista.length === 0) {
    // Aba Fotos e Vídeo podem funcionar via spy file mesmo sem eventos no banco
    const semEventosOk = varAbaAtiva === "fotos" ||
                         (varAbaAtiva === "video" && varTipoAtivo === "all");
    if (!semEventosOk) {
      body.innerHTML = `<div class="var-empty"><i data-lucide="search-x" style="width:32px;height:32px;margin-bottom:10px;color:var(--muted)"></i><br>Nenhum evento encontrado para este cupom.</div>`;
      lucide.createIcons();
      return;
    }
  }
  if (varAbaAtiva === "fotos") {
    const STREAMER_URL_F   = (window.APP_CONFIG || {}).STREAMER_URL   || "";
    const TOKEN_STREAMER_F = (window.APP_CONFIG || {}).STREAMER_TOKEN || "";
    const cupomNumF = varResultLista[0]?.receipt || document.getElementById("varCupomInput").value.trim();

    // ── Layout: foto grande + lista de itens clicável ────────────────────────
    body.innerHTML = `
      <div class="var-foto-viewer">
        <div class="var-foto-main">
          <img id="varFotoMain" src="assets/frame-register.svg" alt="Snapshot"
            style="width:100%;height:auto;object-fit:contain;background:#111;border-radius:8px;display:block">
          <div id="varFotoLabel" style="font-size:11px;color:var(--muted);margin-top:4px;text-align:center">—</div>
        </div>
        <div id="varFotoLista" style="margin-top:12px;display:flex;flex-direction:column;gap:4px;overflow-y:auto;max-height:340px"></div>
      </div>`;

    const mainImg   = document.getElementById("varFotoMain");
    const mainLabel = document.getElementById("varFotoLabel");
    const lista     = document.getElementById("varFotoLista");

    function _setFoto(src, label, useAuth) {
      mainLabel.textContent = label || "—";
      if (!src) { mainImg.src = "assets/frame-register.svg"; return; }
      // Mostrar loading enquanto busca
      mainImg.style.opacity = "0.3";
      mainImg.src = "assets/frame-register.svg";
      const doLoad = (url) => {
        const tmp = new Image();
        tmp.onload = () => { mainImg.src = url; mainImg.style.opacity = "1"; };
        tmp.onerror = () => { mainImg.style.opacity = "1"; };
        tmp.src = url;
      };
      if (useAuth) {
        mediaObjectUrl(src).then(blob => doLoad(blob)).catch(() => { mainImg.style.opacity = "1"; });
      } else {
        // Fetch com token via header para evitar token na URL visível ao browser
        fetch(src).then(r => r.ok ? r.blob() : null)
          .then(b => { if (b) doLoad(URL.createObjectURL(b)); else mainImg.style.opacity = "1"; })
          .catch(() => { mainImg.style.opacity = "1"; });
      }
    }

    function _buildRow(time, product, valueStr, active, onClick) {
      const row = document.createElement("div");
      row.className = "var-foto-row" + (active ? " active" : "");
      row.innerHTML = `
        <span class="var-foto-row-time">${(time || "").slice(0,5)}</span>
        <span class="var-foto-row-prod">${product || ""}</span>
        <span class="var-foto-row-val">${valueStr || ""}</span>`;
      row.addEventListener("click", () => {
        lista.querySelectorAll(".var-foto-row").forEach(r => r.classList.remove("active"));
        row.classList.add("active");
        onClick();
      });
      return row;
    }

    if (varResultLista.length > 0) {
      // Cupom COM eventos no banco — usar imageUrl existente ou snapshot do DVR
      varResultLista.forEach((a, i) => {
        const row = _buildRow(a.time, a.product, a.value, i === 0, () => {
          if (a.imageUrl) {
            _setFoto(a.imageUrl, `${a.time} · ${a.product}`, true);
          } else if (a.timestamp && STREAMER_URL_F) {
            _setFoto(
              `${STREAMER_URL_F}/snapshot?ts=${encodeURIComponent(a.timestamp)}&token=${TOKEN_STREAMER_F}`,
              `${a.time} · ${a.product}`, false
            );
          }
        });
        lista.appendChild(row);
      });
      // Exibir foto do primeiro item
      const first = varResultLista[0];
      if (first.imageUrl) {
        _setFoto(first.imageUrl, `${first.time} · ${first.product}`, true);
      } else if (first.timestamp && STREAMER_URL_F) {
        _setFoto(
          `${STREAMER_URL_F}/snapshot?ts=${encodeURIComponent(first.timestamp)}&token=${TOKEN_STREAMER_F}`,
          `${first.time} · ${first.product}`, false
        );
      }
    } else if (STREAMER_URL_F) {
      // Cupom SEM eventos — buscar itens do spy file e snapshots do DVR
      lista.innerHTML = `<div style="padding:12px;text-align:center;color:var(--muted)">Carregando itens…</div>`;
      fetch(`${STREAMER_URL_F}/cupom/${cupomNumF}/items?token=${TOKEN_STREAMER_F}`)
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          lista.innerHTML = "";
          if (!data?.itens?.length) {
            lista.innerHTML = `<div style="padding:12px;color:var(--muted)">Sem itens encontrados.</div>`;
            return;
          }
          data.itens.forEach((it, i) => {
            const snapUrl = `${STREAMER_URL_F}/snapshot?ts=${encodeURIComponent(it.timestamp)}&token=${TOKEN_STREAMER_F}`;
            const row = _buildRow(
              it.time, it.desc,
              fmtBRL(it.value),
              i === 0,
              () => _setFoto(snapUrl, `${it.time} · ${it.desc}`, false)
            );
            lista.appendChild(row);
          });
          if (data.itens[0]) {
            const f = data.itens[0];
            _setFoto(
              `${STREAMER_URL_F}/snapshot?ts=${encodeURIComponent(f.timestamp)}&token=${TOKEN_STREAMER_F}`,
              `${f.time} · ${f.desc}`, false
            );
          }
        }).catch(() => {
          lista.innerHTML = `<div style="padding:12px;color:var(--muted)">Erro ao carregar itens.</div>`;
        });
    }
    lucide.createIcons();
    return;
  } else if (varTipoAtivo === "all") {
    const cupomNum = varResultLista[0]?.receipt || document.getElementById("varCupomInput").value.trim();
    const pdvPad = String(varPdvSelecionado).padStart(3, "0");
    const videoSrc = `/api/v1/cupom_video?cupom=${cupomNum}&pdv=${pdvPad}&loja=${LOJA}`;
    const STREAMER_URL   = (window.APP_CONFIG || {}).STREAMER_URL   || "";
    const TOKEN_STREAMER = (window.APP_CONFIG || {}).STREAMER_TOKEN || "";
    body.innerHTML = `
      <div class="var-inline-player">
        <video id="varVideoEl" controls playsinline webkit-playsinline style="display:none"></video>
        <div id="varVideoStatus">
          <div id="varVideoLoading" hidden style="text-align:center;padding:24px;color:var(--muted)">
            <i data-lucide="loader-circle" style="width:32px;height:32px;animation:spin 1s linear infinite"></i>
            <p style="margin-top:8px">Gerando vídeo… aguarde</p>
          </div>
          <div id="varVideoGerarBox" style="text-align:center;padding:24px">
            <i data-lucide="video-off" style="width:32px;height:32px;color:var(--muted)"></i>
            <p style="color:var(--muted);margin:8px 0 16px">Vídeo não disponível ainda</p>
            <button id="btnGerarVideo" class="primary-action" style="gap:6px">
              <i data-lucide="clapperboard"></i> Gerar vídeo da compra
            </button>
          </div>
        </div>
        <p class="var-video-label" id="varVideoLabel" hidden>Compra completa · cupom ${cupomNum}</p>
      </div>
      <div class="var-video-timeline" id="varVideoTimeline">
        ${varResultLista.map((a, i) => `
          <div class="var-timeline-item" data-ts="${a.timestamp || ''}" data-id="${a.id}">
            <span class="var-timeline-time">${(a.time || '').slice(0,5)}</span>
            <span class="var-timeline-product">${a.product || ''}</span>
            <span class="var-timeline-qty">${a.qty || ''}</span>
            <span class="var-timeline-price">${a.value || ''}</span>
          </div>
        `).join("")}
      </div>`;

    lucide.createIcons();

    // ── Estado do player ──────────────────────────────────────────────────────
    const videoEl   = document.getElementById("varVideoEl");
    const gerarBox  = document.getElementById("varVideoGerarBox");
    const loadingBox = document.getElementById("varVideoLoading");
    const labelEl   = document.getElementById("varVideoLabel");

    const WORKER_CAP_MS  = 300000;  // 5 min cap para arquivo gerado pelo worker
    const POLL_TIMEOUT   = 120000;  // 2 min timeout no poll

    let _videoStartEpoch = null;  // epoch do instante 0:00 do vídeo atual
    let _lastCurrentRow  = null;
    let _pollTimer       = null;
    let _pollStart       = null;

    // ── Cálculo da janela de vídeo ────────────────────────────────────────────
    // capMs = 0 → sem cap (streaming); > 0 → centra e limita (worker/arquivo)
    function _calcWindow(tss, capMs = 0) {
      if (!tss.length) return null;
      const toMs = s => new Date(s.replace(" ","T")).getTime();
      let start = toMs(tss[0]) - 5000;
      let end   = toMs(tss[tss.length - 1]) + 25000;
      if (capMs > 0 && end - start > capMs) {
        const mid = (start + end) / 2;
        start = mid - capMs / 2;
        end   = mid + capMs / 2;
      }
      return { startMs: start, endMs: end };
    }

    function _fmtLocal(ms) {
      const d = new Date(ms), p = n => String(n).padStart(2,"0");
      return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }

    // ── Sincronização timeline ↔ vídeo ────────────────────────────────────────
    function _sincTimeline(currentSec) {
      if (_videoStartEpoch === null) return;
      const now = _videoStartEpoch + currentSec * 1000;
      const tl  = document.getElementById("varVideoTimeline");
      if (!tl) return;
      let cur = null;
      tl.querySelectorAll(".var-timeline-item").forEach(row => {
        const ts = row.dataset.ts;
        if (!ts) return;
        const rowMs = new Date(ts.replace(" ","T")).getTime();
        if (rowMs <= now) { row.classList.add("done"); row.classList.remove("current"); cur = row; }
        else              { row.classList.remove("done","current"); }
      });
      if (cur) {
        cur.classList.remove("done"); cur.classList.add("current");
        if (cur !== _lastCurrentRow) {
          _lastCurrentRow = cur;
          cur.scrollIntoView({ behavior:"smooth", block:"start" });
        }
      }
    }

    // Registrar timeupdate UMA vez aqui (cobre probe + streaming + arquivo)
    let _autoSeeked = false;
    videoEl.addEventListener("timeupdate", () => {
      const t = videoEl.currentTime;
      _sincTimeline(t);
      // Auto-seek: se passou mais de 2s e nenhum item ficou verde ainda,
      // e o vídeo é seekable (arquivo), pular para 5s antes do primeiro item
      if (!_autoSeeked && t > 2 && _videoStartEpoch !== null && videoEl.seekable?.length > 0) {
        const rows = document.querySelectorAll("#varVideoTimeline .var-timeline-item[data-ts]");
        if (rows.length > 0) {
          const firstMs = new Date(rows[0].dataset.ts.replace(" ","T")).getTime();
          const gapSec = (firstMs - _videoStartEpoch) / 1000;
          if (gapSec > 20 && t < gapSec - 10) {
            _autoSeeked = true;
            videoEl.currentTime = Math.max(0, gapSec - 5);
          } else {
            _autoSeeked = true; // não precisa de seek
          }
        }
      }
    });

    // ── Definir _videoStartEpoch ──────────────────────────────────────────────
    function _definirStartEpoch(overrideMs) {
      if (_videoStartEpoch !== null) return;
      if (overrideMs != null) { _videoStartEpoch = overrideMs; return; }
      // Tentar via varResultLista (eventos do banco)
      const tss = varResultLista.map(a => a.timestamp || "").filter(Boolean).sort();
      if (tss.length) {
        const win = _calcWindow(tss);
        if (win) { _videoStartEpoch = win.startMs; return; }
      }
      // Fallback: spy file (cupom sem eventos no banco)
      fetch(`${STREAMER_URL}/cupom/${cupomNum}/info?token=${TOKEN_STREAMER}`)
        .then(r => r.ok ? r.json() : null)
        .then(d => { if (d?.start_time) _videoStartEpoch = new Date(d.start_time.replace(" ","T")).getTime(); })
        .catch(() => {});
    }

    const _isMobile = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);

    // ── Exibir vídeo (arquivo salvo no servidor) ──────────────────────────────
    function _mostrarVideoArquivo(src) {
      if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
      loadingBox.hidden = true; gerarBox.hidden = true;
      videoEl.src = src; videoEl.style.display = ""; labelEl.hidden = false;
      videoEl.addEventListener("loadedmetadata", () => _definirStartEpoch(), { once: true });
      videoEl.load(); videoEl.play().catch(() => {});
    }

    // ── Mostrar falha ─────────────────────────────────────────────────────────
    function _mostrarFalha(msg) {
      if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
      loadingBox.hidden = true; gerarBox.hidden = false;
      gerarBox.querySelector("p").textContent = msg || "Sem gravação no DVR para este período.";
      const btn = document.getElementById("btnGerarVideo");
      if (btn) { btn.disabled = true; btn.innerHTML = '<i data-lucide="video-off"></i> Sem gravação no DVR'; lucide.createIcons(); }
    }

    // ── Poll: aguardar worker gerar o arquivo ─────────────────────────────────
    function _iniciarPoll() {
      if (_pollTimer) clearInterval(_pollTimer);
      _pollStart = Date.now();
      _pollTimer = setInterval(async () => {
        if (Date.now() - _pollStart > POLL_TIMEOUT) { _mostrarFalha("Tempo esgotado — sem gravação no DVR."); return; }
        try {
          const sr = await fetch(`/api/v1/cupom_video/status?cupom=${cupomNum}&pdv=${pdvPad}&loja=${LOJA}`);
          if (sr.ok && (await sr.json()).status === "failed" && Date.now() - _pollStart > 3000) {
            _mostrarFalha("DVR sem gravação para este período."); return;
          }
          const r = await fetch(videoSrc);
          if (r.ok) { clearInterval(_pollTimer); _pollTimer = null; _mostrarVideoArquivo(videoSrc + "&t=" + Date.now()); }
        } catch {}
      }, 4000);
    }

    // ── Método antigo: worker gera e sobe o arquivo ───────────────────────────
    async function _usarMetodoAntigo(win) {
      try {
        const params = new URLSearchParams({ cupom: cupomNum, pdv: pdvPad, start_time: win.start_time, end_time: win.end_time, loja: LOJA });
        const r = await apiFetch(`/api/v1/cupom_video/request?${params}`, { method: "POST" });
        if (!r.ok) { loadingBox.hidden = true; gerarBox.hidden = false; return; }
        const data = await r.json();
        if (data.status === "ready") _mostrarVideoArquivo(videoSrc);
        else _iniciarPoll();
      } catch { loadingBox.hidden = true; gerarBox.hidden = false; }
    }

    // ── Probe: verificar se existe vídeo via status endpoint (sem 404 no console) ──
    const statusUrl = `/api/v1/cupom_video/status?cupom=${cupomNum}&pdv=${pdvPad}&loja=${LOJA}`;
    fetch(statusUrl).then(r => r.ok ? r.json() : null).then(d => {
      if (d?.status === "done") _mostrarVideoArquivo(videoSrc);
    }).catch(() => {});

    // ── Buscar itens do spy file (cupom sem eventos no banco) ─────────────────
    if (varResultLista.length === 0) {
      fetch(`${STREAMER_URL}/cupom/${cupomNum}/items?token=${TOKEN_STREAMER}`)
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (!data?.itens?.length) return;
          const tl = document.getElementById("varVideoTimeline");
          if (!tl) return;
          tl.innerHTML = data.itens.map(it => {
            const q = it.qty;
            const qtyStr = (q % 1 === 0) ? `${q.toFixed(0)}x` : `${q.toFixed(3).replace(".",",")} kg`;
            return `
            <div class="var-timeline-item" data-ts="${it.timestamp}">
              <span class="var-timeline-time">${it.time.slice(0,5)}</span>
              <span class="var-timeline-product">${it.desc}</span>
              <span class="var-timeline-qty">${qtyStr}</span>
              <span class="var-timeline-price">R$ ${it.value.toLocaleString("pt-BR", {minimumFractionDigits:2,maximumFractionDigits:2})}</span>
            </div>`;
          }).join("");
        }).catch(() => {});
    }

    // ── Botão Gerar vídeo ─────────────────────────────────────────────────────
    document.getElementById("btnGerarVideo")?.addEventListener("click", async () => {
      gerarBox.hidden = true; loadingBox.hidden = false;

      const semEventos = varResultLista.length === 0;
      const tss = varResultLista.map(a => a.timestamp || "").filter(Boolean).sort();
      const win        = _calcWindow(tss);
      const winCapped  = _calcWindow(tss, WORKER_CAP_MS);

      // Montar URL do streamer
      let streamSrc;
      if (semEventos) {
        // Para cupom sem eventos: usar /info como probe para obter start_time real
        // antes de iniciar o stream (evita 200 OK seguido de silêncio)
        try {
          const infoR = await fetch(`${STREAMER_URL}/cupom/${cupomNum}/info?token=${TOKEN_STREAMER}`);
          if (infoR.status === 425) {
            loadingBox.hidden = true; gerarBox.hidden = false;
            gerarBox.querySelector("p").textContent = "Gravação disponível em ~2 minutos (DVR ainda gravando).";
            const btn = document.getElementById("btnGerarVideo");
            if (btn) { btn.disabled = false; btn.innerHTML = '<i data-lucide="clock"></i> Tentar em 2 min'; lucide.createIcons(); }
            return;
          }
          if (!infoR.ok) {
            loadingBox.hidden = true; gerarBox.hidden = false;
            gerarBox.querySelector("p").textContent = "Sem gravação no DVR para este período.";
            return;
          }
          const info = await infoR.json();
          _videoStartEpoch = new Date(info.start_time.replace(" ","T")).getTime();
          const sp = new URLSearchParams({ start: info.start_time, end: info.end_time, token: TOKEN_STREAMER, skip_dhav: "1" });
          streamSrc = `${STREAMER_URL}/?${sp}`;
        } catch {
          loadingBox.hidden = true; gerarBox.hidden = false; return;
        }
      } else if (win) {
        // Probe: descobre o start_time real após ajuste DHAV
        try {
          const probeParams = new URLSearchParams({ start: _fmtLocal(win.startMs), end: _fmtLocal(win.endMs), token: TOKEN_STREAMER });
          const pr = await fetch(`${STREAMER_URL}/probe?${probeParams}`);
          if (pr.status === 425) {
            // Compra muito recente — DVR ainda gravando
            loadingBox.hidden = true; gerarBox.hidden = false;
            gerarBox.querySelector("p").textContent = "Gravação disponível em ~2 minutos (DVR ainda gravando).";
            const btn = document.getElementById("btnGerarVideo");
            if (btn) { btn.disabled = false; btn.innerHTML = '<i data-lucide="clock"></i> Tentar em 2 min'; lucide.createIcons(); }
            return;
          }
          if (!pr.ok) { loadingBox.hidden = true; gerarBox.hidden = false; return; }
          const pd = await pr.json();
          _videoStartEpoch = new Date(pd.start_time.replace(" ","T")).getTime();
          streamSrc = `${STREAMER_URL}/?${new URLSearchParams({ start: pd.start_time, end: pd.end_time, token: TOKEN_STREAMER, skip_dhav: "1" })}`;
        } catch { loadingBox.hidden = true; gerarBox.hidden = false; return; }
      } else {
        loadingBox.hidden = true; gerarBox.hidden = false; return;
      }

      // Timer começa AQUI (após /info ou /probe já terem verificado DHAV)
      // Cobre: stream startup + ffmpeg first fragment + margem de segurança
      const fallbackTimer = setTimeout(() => {
        videoEl.removeEventListener("loadedmetadata", onOk);
        videoEl.removeEventListener("error", onErr);
        videoEl.src = "";
        (semEventos || !winCapped) ? _mostrarFalha("Tempo esgotado — tente novamente.") : _usarMetodoAntigo({ start_time: _fmtLocal(winCapped.startMs), end_time: _fmtLocal(winCapped.endMs) });
      }, 40000);

      function onOk() {
        clearTimeout(fallbackTimer); videoEl.removeEventListener("error", onErr);
        loadingBox.hidden = true; videoEl.style.display = ""; videoEl.play().catch(() => {}); labelEl.hidden = false;
        if (semEventos) _definirStartEpoch();
      }
      function onErr() {
        clearTimeout(fallbackTimer); videoEl.removeEventListener("loadedmetadata", onOk);
        videoEl.src = "";
        if (!semEventos && winCapped) {
          _usarMetodoAntigo({ start_time: _fmtLocal(winCapped.startMs), end_time: _fmtLocal(winCapped.endMs) });
        } else {
          loadingBox.hidden = true; gerarBox.hidden = false;
          const btn = document.getElementById("btnGerarVideo");
          if (btn) { btn.disabled = false; btn.innerHTML = '<i data-lucide="rotate-ccw"></i> Tentar novamente'; lucide.createIcons(); }
        }
      }

      videoEl.addEventListener("loadedmetadata", onOk, { once: true });
      videoEl.addEventListener("error", onErr, { once: true });

      if (_isMobile) {
        // Mobile: streaming fMP4 não funciona — gerar clip MP4 completo via /clip
        clearTimeout(fallbackTimer); // pausar timer enquanto gera o clip
        const sp = new URL(streamSrc, location.href).searchParams;
        const clipParams = new URLSearchParams({
          start: sp.get("start") || "", end: sp.get("end") || "", token: sp.get("token") || ""
        });
        const pEl = loadingBox.querySelector("p");
        if (pEl) pEl.textContent = "Gerando clipe para mobile…";
        fetch(`${STREAMER_URL}/clip?${clipParams}`)
          .then(r => r.ok ? r.json() : null)
          .then(d => {
            if (!d?.token) { onErr(); return; }
            // video.src direto — MP4 com Content-Length permite play progressivo
            // O browser usa o mesmo trust de cert da sessão (sem re-bloqueio)
            const mobileTimer = setTimeout(() => {
              videoEl.removeEventListener("loadedmetadata", onOk);
              videoEl.removeEventListener("error", onErr);
              videoEl.src = ""; _mostrarFalha("Tempo esgotado.");
            }, 30000);
            videoEl.addEventListener("loadedmetadata", () => clearTimeout(mobileTimer), { once: true });
            videoEl.addEventListener("error", () => { clearTimeout(mobileTimer); onErr(); }, { once: true });
            videoEl.src = `${STREAMER_URL}/clip/${d.token}?token=${clipParams.get("token")}`;
            videoEl.load();
          })
          .catch(() => onErr());
      } else {
        videoEl.src = streamSrc; videoEl.load();
      }
    });

    // ── Click em item da timeline → seek ──────────────────────────────────────
    body.querySelectorAll(".var-timeline-item").forEach(row => {
      row.style.cursor = "pointer";
      row.addEventListener("click", () => {
        const ts = row.dataset.ts;
        if (!ts || _videoStartEpoch === null || videoEl.style.display === "none") return;
        if (!videoEl.seekable?.length) return;  // live stream: sem seek
        const seekTo = (new Date(ts.replace(" ","T")).getTime() - _videoStartEpoch) / 1000;
        if (seekTo >= 0 && seekTo < videoEl.duration) { videoEl.currentTime = seekTo; videoEl.play().catch(() => {}); }
      });
    });
    return; // lucide already called above
  } else {
    body.innerHTML = varResultLista.map(a => `
      <div class="var-event-card" data-id="${a.id}" style="cursor:pointer">
        <div class="var-event-thumb" style="display:flex;align-items:center;justify-content:center;background:#15282f">
          <i data-lucide="play-circle" style="width:32px;height:32px;color:#fff"></i>
        </div>
        <div class="var-event-info">
          <div class="var-event-top">
            <span class="severity ${a.severity}"><i></i>${a.severity === "critical" ? "Crítico" : a.severity === "warning" ? "Atenção" : "Normal"}</span>
            <span class="var-event-time">${a.time}</span>
          </div>
          <span class="var-event-product">${a.product}</span>
          <span class="var-event-sub">${a.qty} · ${a.value}</span>
        </div>
        <div class="var-event-actions">
          <button class="secondary-action" data-id="${a.id}"><i data-lucide="play"></i></button>
        </div>
      </div>
    `).join("");
    body.querySelectorAll(".var-event-card").forEach(card => {
      card.addEventListener("click", () => {
        const alert = varResultLista.find(a => a.id === Number(card.dataset.id));
        if (!alert) return;
        selectedAlert = alert;
        closeVarDrawer();
        openDrawer(alert);
        setTimeout(() => document.getElementById("videoButton").click(), 100);
      });
    });
  }
  lucide.createIcons();
}

document.querySelectorAll(".var-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".var-tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    varAbaAtiva = tab.dataset.tab;
    renderVarBody();
  });
});

document.getElementById("formVarCupom").addEventListener("submit", async (e) => {
  e.preventDefault();
  const cupom = document.getElementById("varCupomInput").value.trim();
  if (!cupom || !varPdvSelecionado) return;
  const tipo = document.querySelector('input[name="varTipo"]:checked').value;
  const itemFiltro = tipo === "item" ? document.getElementById("varItemInput").value.trim().toLowerCase() : "";

  const btn = e.target.querySelector("button[type=submit]");
  btn.disabled = true;
  const params = new URLSearchParams({ loja: LOJA, cupom });
  params.append("pdv", varPdvSelecionado);
  const resp = await apiFetch(`/api/v1/alerts?${params}`);
  btn.disabled = false;
  if (!resp.ok) return;
  let lista = await resp.json();

  if (itemFiltro) lista = lista.filter(a => a.product.toLowerCase().includes(itemFiltro));
  varResultLista = lista;
  varTipoAtivo = tipo;

  document.getElementById("varResultModalBreadcrumb").textContent =
    `PDV ${String(varPdvSelecionado).padStart(2,"0")} · ${LOJA_NOME}`;
  document.getElementById("varResultModalTitle").textContent =
    `Cupom ${cupom}` + (lista.length ? ` — ${lista.length} evento${lista.length !== 1 ? "s" : ""}` : "");

  varAbaAtiva = "fotos";
  document.querySelectorAll(".var-tab").forEach(t => t.classList.toggle("active", t.dataset.tab === "fotos"));
  renderVarBody();
  openVarDrawer();
});

// ── PDVs ──────────────────────────────────────────────
// ── View Cupons ───────────────────────────────────────────────────────────────
const STREAMER_BASE  = (window.APP_CONFIG || {}).STREAMER_URL   || "";
const STREAMER_TOKEN = (window.APP_CONFIG || {}).STREAMER_TOKEN || "";

let _cuponsTodos = [];  // cache para filtro local

// ── Paginação genérica ────────────────────────────────────────────────────────
const POR_PAGINA = 25;

function _renderPaginacao(idInfo, idBtns, idPag, paginaAtual, total, onPage) {
  const totalPags = Math.ceil(total / POR_PAGINA);
  const info = document.getElementById(idInfo);
  const btns = document.getElementById(idBtns);
  const pag  = document.getElementById(idPag);
  if (!btns || !pag) return;
  if (totalPags <= 1) { pag.style.display = "none"; if (info) info.textContent = `${total} registros`; return; }
  pag.style.display = "";
  const inicio = (paginaAtual - 1) * POR_PAGINA + 1;
  const fim = Math.min(paginaAtual * POR_PAGINA, total);
  if (info) info.textContent = `${inicio}–${fim} de ${total}`;
  btns.innerHTML = "";
  const addBtn = (label, page, disabled, active) => {
    const b = document.createElement("button");
    b.className = "paginacao-btn" + (active ? " active" : "");
    b.textContent = label; b.disabled = disabled;
    b.addEventListener("click", () => onPage(page));
    btns.appendChild(b);
  };
  addBtn("‹", paginaAtual - 1, paginaAtual === 1, false);
  const start = Math.max(1, paginaAtual - 2), end = Math.min(totalPags, paginaAtual + 2);
  if (start > 1) { addBtn("1", 1, false, false); if (start > 2) btns.insertAdjacentHTML("beforeend", `<span style="padding:0 4px;color:var(--muted)">…</span>`); }
  for (let i = start; i <= end; i++) addBtn(i, i, false, i === paginaAtual);
  if (end < totalPags) { if (end < totalPags - 1) btns.insertAdjacentHTML("beforeend", `<span style="padding:0 4px;color:var(--muted)">…</span>`); addBtn(totalPags, totalPags, false, false); }
  addBtn("›", paginaAtual + 1, paginaAtual === totalPags, false);
}

function iniciarViewCupons() {
  const pad = n => String(n).padStart(2,"0");
  const hoje = new Date();
  const todayStr = `${hoje.getFullYear()}-${pad(hoje.getMonth()+1)}-${pad(hoje.getDate())}`;
  const input = document.getElementById("cuponsDateInput");
  if (!input.value) input.value = todayStr;
  // Marcar botão "Hoje" como ativo
  document.querySelectorAll(".cupons-quick").forEach(b => b.classList.toggle("active", b.dataset.days === "0"));
  carregarCupons(input.value);
}

let _cuponsPagAtual = 1;
let _cuponsListaFiltrada = [];

function _aplicarFiltrosCupons(pagina) {
  pagina = pagina || 1;
  _cuponsPagAtual = pagina;
  const busca   = (document.getElementById("cuponsSearch")?.value || "").toLowerCase();
  const op      = document.getElementById("cuponsOperadorFilter")?.value || "";
  const periodo = document.getElementById("cuponsPeriodoFilter")?.value || "";
  const PERIODOS = { manha: [6,12], tarde: [12,18], noite: [18,23] };

  _cuponsListaFiltrada = _cuponsTodos.filter(c => {
    if (op && c.operador !== op) return false;
    if (busca && !c.numero.includes(busca) && !(c.operador||"").toLowerCase().includes(busca)) return false;
    if (periodo && PERIODOS[periodo]) {
      const h = parseInt((c.abriu || "00").slice(0,2));
      const [min, max] = PERIODOS[periodo];
      if (h < min || h >= max) return false;
    }
    return true;
  });

  const tbody = document.getElementById("cuponsTableBody");
  const footer = document.getElementById("cuponsFooter");
  if (!_cuponsListaFiltrada.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="8">Nenhum cupom encontrado com esses filtros.</td></tr>`;
    footer.textContent = "0 cupons";
    document.getElementById("cuponsPaginacao").style.display = "none";
    return;
  }
  const pagSlice = _cuponsListaFiltrada.slice((pagina-1)*POR_PAGINA, pagina*POR_PAGINA);
  const totalVal = _cuponsListaFiltrada.reduce((s, c) => s + (c.total || 0), 0);

  // DVR leva ~90-120s após o fechamento do cupom pra disponibilizar foto/vídeo
  const _agora = Date.now();
  const _videoDisponivel = c => {
    if (!c.fechou || !isHoje(selectedDate)) return true; // dias passados: sempre disponível
    const [hh, mm, ss] = c.fechou.split(":").map(Number);
    const fechouMs = new Date(selectedDate + "T00:00:00").setHours(hh, mm, ss || 0, 0);
    return (_agora - fechouMs) >= 120000;
  };

  tbody.innerHTML = pagSlice.map(c => {
    const disponivel = _videoDisponivel(c);
    const corFaixa = disponivel ? "#2f9e44" : "#f59f00";
    const tituloFaixa = disponivel ? "Vídeo e fotos disponíveis" : "DVR ainda processando — disponível em instantes";
    return `
    <tr class="cupons-row" data-cupom="${c.numero}" style="border-left:3px solid ${corFaixa}" title="${tituloFaixa}">
      <td>${c.abriu ? c.abriu.slice(0,5) : '—'}</td>
      <td><strong>${c.numero}</strong></td>
      <td class="cupons-op">${c.operador || '—'}</td>
      <td style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${c.item_top||''}">${c.item_top ? `<span style="color:var(--primary);font-weight:600;margin-right:4px">★</span>${c.item_top}` : '<span style="color:var(--border)">—</span>'}</td>
      <td style="text-align:right;font-size:12px;white-space:nowrap">${c.item_top_valor > 0 ? `R$ ${c.item_top_valor.toLocaleString("pt-BR", {minimumFractionDigits:2,maximumFractionDigits:2})}` : '<span style="color:var(--border)">—</span>'}</td>
      <td class="cupons-col-itens" style="text-align:center">${c.itens}</td>
      <td style="text-align:right;font-weight:600;white-space:nowrap">${fmtBRL(c.total)}</td>
      <td>
        <div style="display:flex;justify-content:center;gap:4px">
          <button class="icon-button cupom-btn-nota" data-cupom="${c.numero}" title="Ver cupom"><i data-lucide="file-text" style="width:16px;height:16px"></i></button>
          <button class="icon-button cupom-btn-video" data-cupom="${c.numero}" title="${tituloFaixa}" ${disponivel ? "" : "disabled style=\"opacity:.4;cursor:not-allowed\""}><i data-lucide="play-circle" style="width:16px;height:16px;color:${disponivel ? "var(--primary)" : "var(--muted)"}"></i></button>
        </div>
      </td>
    </tr>`;
  }).join("");
  footer.textContent = `${_cuponsListaFiltrada.length} de ${_cuponsTodos.length} cupons · Total R$ ${totalVal.toLocaleString("pt-BR", {minimumFractionDigits:2,maximumFractionDigits:2})}`;
  lucide.createIcons();
  _renderPaginacao("cuponsPaginacaoInfo","cuponsPaginacaoBtns","cuponsPaginacao", pagina, _cuponsListaFiltrada.length, p => _aplicarFiltrosCupons(p));

  // Reaplicar highlight da linha que foi visitada (persiste após re-renders)
  if (_cupomLinhaAtiva) {
    const linhaAtiva = tbody.querySelector(`tr[data-cupom="${_cupomLinhaAtiva}"]`);
    if (linhaAtiva) linhaAtiva.classList.add("row-visited");
  }

  tbody.querySelectorAll(".cupom-btn-nota").forEach(btn => {
    btn.addEventListener("click", e => { e.stopPropagation(); abrirCupomDrawer(btn.dataset.cupom); });
  });
  tbody.querySelectorAll(".cupom-btn-video").forEach(btn => {
    btn.addEventListener("click", e => {
      e.stopPropagation();
      const num = btn.dataset.cupom;
      _cupomLinhaAtiva = num;
      _voltarParaCupomsAposVideo = true;
      varPdvSelecionado = 1;
      document.getElementById("viewReceipts").style.display = "none";
      document.getElementById("viewPdvCards").style.display = "";
      document.getElementById("pdvCardsGrid").style.display = "none";
      document.getElementById("pdvVarSearch").style.display = "";
      document.getElementById("varCupomInput").value = num;
      document.querySelector('input[name="varTipo"][value="all"]').checked = true;
      document.querySelectorAll(".nav-item[data-view]").forEach(n => n.classList.remove("active"));
      document.querySelectorAll(".nav-item[data-view='terminals']").forEach(n => n.classList.add("active"));
      document.getElementById("formVarCupom").dispatchEvent(new Event("submit"));
    });
  });
}

async function carregarCupons(dateStr) {
  _cuponsTodos = [];
  const tbody = document.getElementById("cuponsTableBody");
  tbody.innerHTML = `<tr class="empty-row"><td colspan="6"><i data-lucide="loader-circle" style="width:16px;animation:spin 1s linear infinite"></i> Carregando…</td></tr>`;
  lucide.createIcons();
  try {
    const r = await fetch(`${STREAMER_BASE}/cupons?date=${dateStr}&token=${STREAMER_TOKEN}`);
    if (!r.ok) { tbody.innerHTML = `<tr class="empty-row"><td colspan="6">Erro ao carregar cupons.</td></tr>`; return; }
    const data = await r.json();
    _cuponsTodos = (data.cupons || []).filter(c => c.fechou).reverse();

    // Preencher filtro de operadores
    const ops = [...new Set(_cuponsTodos.map(c => c.operador).filter(Boolean))].sort();
    const sel = document.getElementById("cuponsOperadorFilter");
    if (sel) {
      const current = sel.value;
      sel.innerHTML = `<option value="">Todos os operadores</option>` +
        ops.map(o => `<option value="${o}"${o===current?' selected':''}>${o}</option>`).join("");
    }
    _aplicarFiltrosCupons();
  } catch(e) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="6">Erro de conexão com o PDV.</td></tr>`;
  }
}

// Botões de data rápida
document.querySelectorAll(".cupons-quick").forEach(btn => {
  btn.addEventListener("click", () => {
    const d = new Date();
    d.setDate(d.getDate() - parseInt(btn.dataset.days));
    const pad = n => String(n).padStart(2,"0");
    const dateStr = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
    document.getElementById("cuponsDateInput").value = dateStr;
    document.querySelectorAll(".cupons-quick").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    carregarCupons(dateStr);
  });
});
document.getElementById("btnCarregarCupons")?.addEventListener("click", () => {
  const d = document.getElementById("cuponsDateInput").value;
  if (d) carregarCupons(d);
});
document.getElementById("cuponsDateInput")?.addEventListener("change", e => {
  document.querySelectorAll(".cupons-quick").forEach(b => b.classList.remove("active"));
  carregarCupons(e.target.value);
});
document.getElementById("cuponsSearch")?.addEventListener("input", () => _aplicarFiltrosCupons(1));
document.getElementById("cuponsOperadorFilter")?.addEventListener("change", () => _aplicarFiltrosCupons(1));
document.getElementById("cuponsPeriodoFilter")?.addEventListener("change", () => _aplicarFiltrosCupons(1));

// ── Receipt Drawer ────────────────────────────────────────────────────────────
let _cupomLinhaAtiva = null; // número do cupom que teve o drawer aberto por último

function openReceiptDrawer() {
  document.getElementById("receiptDrawer").classList.add("open");
  document.getElementById("receiptDrawer").setAttribute("aria-hidden","false");
  document.getElementById("receiptDrawerBackdrop").classList.add("open");
}
function closeReceiptDrawer() {
  document.getElementById("receiptDrawer").classList.remove("open");
  document.getElementById("receiptDrawer").setAttribute("aria-hidden","true");
  document.getElementById("receiptDrawerBackdrop").classList.remove("open");
  // Destacar a linha que foi aberta
  if (_cupomLinhaAtiva) {
    const row = document.querySelector(`tr[data-cupom="${_cupomLinhaAtiva}"]`);
    if (row) row.classList.add("row-visited");
  }
}
document.getElementById("closeReceiptDrawer")?.addEventListener("click", closeReceiptDrawer);
document.getElementById("receiptDrawerBackdrop")?.addEventListener("click", closeReceiptDrawer);

async function abrirVideoCompra(cupomNum) {
  await abrirCupomDrawer(cupomNum);
  setTimeout(() => document.getElementById("btnVerVideoFromReceipt")?.click(), 300);
}

async function abrirCupomDrawer(cupomNum) {
  // Guardar qual linha foi aberta e remover highlight anterior
  document.querySelectorAll("tr.row-visited").forEach(r => r.classList.remove("row-visited"));
  _cupomLinhaAtiva = cupomNum;
  document.getElementById("receiptDrawerTitle").textContent = `Cupom ${cupomNum}`;
  document.getElementById("receiptDrawerBody").innerHTML =
    `<div style="padding:32px;text-align:center;color:var(--muted)"><i data-lucide="loader-circle" style="width:24px;animation:spin 1s linear infinite"></i></div>`;
  lucide.createIcons();
  openReceiptDrawer();

  try {
    const r = await fetch(`${STREAMER_BASE}/cupom/${cupomNum}/receipt?token=${STREAMER_TOKEN}`);
    if (!r.ok) {
      document.getElementById("receiptDrawerBody").innerHTML = `<p style="padding:24px;color:var(--muted)">Cupom não encontrado no spy file.</p>`;
      return;
    }
    const d = await r.json();
    document.getElementById("receiptDrawerEyebrow").textContent = `${d.data} · ${d.operador || '—'}`;
    document.getElementById("receiptDrawerTitle").textContent = `Cupom ${d.numero}`;

    const fmtVal = v => `R$ ${(v||0).toLocaleString("pt-BR", {minimumFractionDigits:2,maximumFractionDigits:2})}`;
    const itensHTML = (d.itens || []).map(it => `
      <tr>
        <td style="padding:6px 8px">${it.time.slice(0,5)}</td>
        <td style="padding:6px 8px">${it.desc}</td>
        <td style="padding:6px 8px;text-align:center">${it.qty % 1 === 0 ? it.qty.toFixed(0)+'x' : it.qty.toFixed(3).replace(".",",")}</td>
        <td style="padding:6px 8px;text-align:right">${fmtVal(it.vunit)}</td>
        <td style="padding:6px 8px;text-align:right;font-weight:600">${fmtVal(it.vtotal)}</td>
      </tr>`).join("");

    const pagHTML = (d.pagamentos || []).map(p => `
      <div style="display:flex;justify-content:space-between;padding:4px 0">
        <span>${p.forma}</span><strong>${fmtVal(p.valor)}</strong>
      </div>`).join("");

    document.getElementById("receiptDrawerBody").innerHTML = `
      <div id="printArea" style="padding:4px 0">
        <div style="background:var(--bg);border-radius:8px;padding:14px 16px;margin-bottom:16px">
          <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--muted)">
            <span>Abertura: ${d.abriu}</span><span>Fechamento: ${d.fechou || '—'}</span>
          </div>
          <div style="font-size:12px;color:var(--muted);margin-top:2px">Operador: ${d.operador || '—'}</div>
        </div>

        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead>
            <tr style="border-bottom:2px solid var(--border)">
              <th style="padding:6px 8px;text-align:left;color:var(--muted);font-weight:600">Hr</th>
              <th style="padding:6px 8px;text-align:left;color:var(--muted);font-weight:600">Produto</th>
              <th style="padding:6px 8px;text-align:center;color:var(--muted);font-weight:600">Qtd</th>
              <th style="padding:6px 8px;text-align:right;color:var(--muted);font-weight:600">Unit</th>
              <th style="padding:6px 8px;text-align:right;color:var(--muted);font-weight:600">Total</th>
            </tr>
          </thead>
          <tbody>${itensHTML}</tbody>
        </table>

        <div style="border-top:1px solid var(--border);margin-top:12px;padding-top:12px">
          <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px">
            <span style="color:var(--muted)">Subtotal</span><span>${fmtVal(d.subtotal || d.total)}</span>
          </div>
          <div style="font-size:13px;color:var(--muted);margin-bottom:8px">${pagHTML}</div>
          <div style="display:flex;justify-content:space-between;font-size:16px;font-weight:700;border-top:2px solid var(--border);padding-top:10px">
            <span>Total</span><span style="color:var(--primary)">${fmtVal(d.total)}</span>
          </div>
        </div>

        <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:8px">
          <button id="btnVerVideoFromReceipt" class="primary-action" style="display:flex;width:100%;justify-content:center;align-items:center;gap:8px"
            data-cupom="${d.numero}">
            <i data-lucide="play-circle"></i> Ver vídeo da compra
          </button>
          <button id="btnGeminiAnalisar" style="display:flex;width:100%;justify-content:center;align-items:center;gap:8px;padding:10px 16px;border-radius:8px;border:1px solid #4285f4;background:#f0f4ff;color:#1a56db;font-weight:600;cursor:pointer;font-size:13px;margin-top:8px"
            data-cupom="${d.numero}">
            <i data-lucide="sparkles" style="width:16px;height:16px"></i> Analisar com Gemini
          </button>
          <div id="geminiResultado" style="display:none;font-size:12px;padding:10px;background:var(--bg);border-radius:8px;border:1px solid var(--border);margin-top:6px"></div>
        </div>
      </div>`;
    lucide.createIcons();

    document.getElementById("btnGeminiAnalisar")?.addEventListener("click", async () => {
      const num = document.getElementById("btnGeminiAnalisar").dataset.cupom;
      const btn = document.getElementById("btnGeminiAnalisar");
      const res = document.getElementById("geminiResultado");
      const STREAMER = (window.APP_CONFIG || {}).STREAMER_URL || "";
      const TOKEN    = (window.APP_CONFIG || {}).STREAMER_TOKEN || "";
      btn.disabled = true;
      btn.innerHTML = `<i data-lucide="loader-circle" style="width:16px;height:16px;animation:spin 1s linear infinite"></i> Analisando com Gemini…`;
      lucide.createIcons();
      res.style.display = "none";
      try {
        const r = await fetch(`${STREAMER}/gemini-analyze?cupom=${num}&token=${TOKEN}`, { method: "POST" });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || "Erro");
        const sus  = data.alertas || 0;
        const ok   = data.ok || 0;
        const inc  = data.inconclusivos || 0;
        const brl  = data.custo_brl ? `R$ ${data.custo_brl.toFixed(4)}` : "";
        const ms   = data.tempo_ms ? `${(data.tempo_ms/1000).toFixed(1)}s` : "";
        const cor  = sus > 0 ? "#c92a2a" : "#2f9e44";
        res.style.display = "block";
        res.innerHTML = `<strong style="color:${cor}">${sus > 0 ? "⚠ " + sus + " ALERTA(S)" : "✓ Sem divergências"}</strong>
          &nbsp;·&nbsp; ${ok} ok &nbsp;·&nbsp; ${inc} inconclusivos<br>
          <span style="color:var(--muted);font-size:11px">${data.itens_analisados} itens · ${ms} · custo ${brl} — resultados na Auditoria IA</span>`;
        btn.innerHTML = `<i data-lucide="sparkles" style="width:16px;height:16px"></i> Analisado ✓`;
        lucide.createIcons();
      } catch(e) {
        res.style.display = "block";
        res.innerHTML = `<span style="color:#c92a2a">Erro: ${e.message}</span>`;
        btn.innerHTML = `<i data-lucide="sparkles" style="width:16px;height:16px"></i> Analisar com Gemini`;
        btn.disabled = false;
        lucide.createIcons();
      }
    });

    document.getElementById("btnVerVideoFromReceipt")?.addEventListener("click", () => {
      const num = document.getElementById("btnVerVideoFromReceipt").dataset.cupom;
      closeReceiptDrawer();
      // Marca que viemos de Cupons — ao fechar o vídeo, devolve pra cá em vez de ficar no PDV VAR
      _voltarParaCupomsAposVideo = true;
      // Navegar para VAR com esse cupom (necessário pra popular o player de vídeo)
      varPdvSelecionado = 1;
      document.getElementById("viewReceipts").style.display = "none";
      document.getElementById("viewPdvCards").style.display = "";
      document.getElementById("pdvCardsGrid").style.display = "none";
      document.getElementById("pdvVarSearch").style.display = "";
      document.getElementById("varCupomInput").value = num;
      document.querySelector('input[name="varTipo"][value="all"]').checked = true;
      document.querySelectorAll(".nav-item[data-view]").forEach(n => n.classList.remove("active"));
      document.querySelectorAll(".nav-item[data-view='terminals']").forEach(n => n.classList.add("active"));
      document.getElementById("formVarCupom").dispatchEvent(new Event("submit"));
    });
  } catch(e) {
    document.getElementById("receiptDrawerBody").innerHTML = `<p style="padding:24px;color:var(--muted)">Erro ao carregar cupom.</p>`;
  }
}

document.getElementById("btnImprimirCupom")?.addEventListener("click", () => {
  const area = document.getElementById("printArea");
  if (!area) return;
  const title = document.getElementById("receiptDrawerTitle").textContent;
  const eyebrow = document.getElementById("receiptDrawerEyebrow").textContent;
  const w = window.open("", "_blank", "width=400,height=600");
  w.document.write(`
    <html><head><title>${title}</title>
    <style>
      body { font-family: monospace; font-size: 12px; margin: 16px; }
      table { width: 100%; border-collapse: collapse; }
      th, td { padding: 4px 6px; }
      th { border-bottom: 1px solid #000; text-align: left; }
      .right { text-align: right; }
      .center { text-align: center; }
      .total { font-size: 16px; font-weight: bold; border-top: 2px solid #000; padding-top: 8px; margin-top: 8px; }
      h3 { margin: 0 0 4px; }
      .sub { color: #666; font-size: 11px; }
    </style></head><body>
    <h3>${title}</h3>
    <div class="sub">${eyebrow}</div>
    <hr>
    ${area.innerHTML}
    </body></html>`);
  w.document.close();
  w.print();
});

// ── PDVs ───────────────────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════
// TELA CONSULTAR
// ═══════════════════════════════════════════════════════════════════════════

(function() {
  let _consultarModo = "cupons";   // "cupons" | "consultas"
  let _consultarDate = new Date(); // data selecionada
  let _consultarTimer = null;

  function _fmtDate(d) {
    const y = d.getFullYear(), m = String(d.getMonth()+1).padStart(2,'0'), day = String(d.getDate()).padStart(2,'0');
    return `${y}-${m}-${day}`;
  }
  function _fmtBRL(v) { return 'R$ ' + (v||0).toLocaleString("pt-BR", {minimumFractionDigits:2,maximumFractionDigits:2}); }
  function _fmtQty(q, u) {
    const n = parseFloat(q)||0;
    return u === 'Kg' ? n.toFixed(3).replace('.',',') + ' kg' : n.toFixed(0) + 'x';
  }

  let _consultarLista = [];
  let _consultarPagAtual = 1;

  function _renderConsultar(pagina) {
    pagina = pagina || 1;
    _consultarPagAtual = pagina;
    const busca    = (document.getElementById("consultarSearch")?.value || "").toLowerCase();
    const operador = document.getElementById("consultarOperadorFilter")?.value || "";
    const periodo  = document.getElementById("consultarPeriodoFilter")?.value || "";
    const tbody = document.getElementById("consultarConsultasBody");
    const empty = document.getElementById("consultarConsultasEmpty");
    tbody.innerHTML = "";

    // Mais recente primeiro
    const listaFiltrada = [..._consultarLista].reverse().filter(c => {
      if (busca && !`${c.desc} ${c.operador} ${c.cupom}`.toLowerCase().includes(busca)) return false;
      if (operador && c.operador !== operador) return false;
      if (periodo) {
        const h = parseInt((c.time||"").slice(0,2));
        if (periodo === "manha" && (h < 6  || h >= 12)) return false;
        if (periodo === "tarde" && (h < 12 || h >= 18)) return false;
        if (periodo === "noite" && (h < 18 || h >= 23)) return false;
      }
      return true;
    });

    if (!listaFiltrada.length) { empty.style.display = ""; document.getElementById("consultarPaginacao").style.display = "none"; return; }
    empty.style.display = "none";
    const lista = listaFiltrada.slice((pagina-1)*POR_PAGINA, pagina*POR_PAGINA);

    // DVR leva ~90-120s após a consulta pra disponibilizar foto/vídeo
    const _hojeStr = _fmtDate(new Date());
    const _agoraMs = Date.now();
    const _disponivel = c => {
      if (_fmtDate(_consultarDate) !== _hojeStr) return true; // dias passados: sempre disponível
      const ts = (c.timestamp || "").replace(" ", "T");
      if (!ts) return true;
      const tsMs = new Date(ts).getTime();
      if (isNaN(tsMs)) return true;
      return (_agoraMs - tsMs) >= 120000;
    };

    lista.forEach(c => {
      const disp = _disponivel(c);
      const corFaixa = disp ? "#2f9e44" : "#f59f00";
      const tituloFaixa = disp ? "Vídeo e fotos disponíveis" : "DVR ainda processando — disponível em instantes";
      const tr = document.createElement("tr");
      tr.className = "cupons-row";
      tr.style.borderLeft = `3px solid ${corFaixa}`;
      tr.title = tituloFaixa;
      tr.innerHTML = `
        <td>${(c.time||"").slice(0,5)}</td>
        <td>#${c.cupom||"—"}</td>
        <td>
          <div>${c.desc||"—"}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">${c.acao_label||""}</div>
        </td>
        <td style="font-size:12px;color:var(--muted)">${c.cod||"—"}</td>
        <td style="font-size:12px">${c.operador||"—"}</td>
        <td>${_fmtQty(c.qty, c.unit)}</td>
        <td style="text-align:right">${_fmtBRL(c.vtotal)}</td>
        <td style="text-align:center"><button class="icon-button btn-ver-item" title="${tituloFaixa}" ${disp ? "" : "disabled"} style="width:36px;height:36px;border:1px solid var(--border);border-radius:8px;background:var(--card);${disp ? "" : "opacity:.4;cursor:not-allowed"}"><i data-lucide="play-circle" style="width:16px;height:16px;color:${disp ? "var(--primary)" : "var(--muted)"}"></i></button></td>`;
      if (disp) {
        tr.querySelector(".btn-ver-item").addEventListener("click", e => { e.stopPropagation(); _abrirVideoConsulta(c); });
        tr.addEventListener("click", () => _abrirVideoConsulta(c));
      }
      tbody.appendChild(tr);
    });
    lucide.createIcons();
    _renderPaginacao("consultarPaginacaoInfo","consultarPaginacaoBtns","consultarPaginacao", pagina, listaFiltrada.length, p => _renderConsultar(p));
  }

  function _carregarConsultar() {
    const STREAMER = (window.APP_CONFIG||{}).STREAMER_URL || "";
    const TOKEN    = (window.APP_CONFIG||{}).STREAMER_TOKEN || "";
    const dateStr  = _fmtDate(_consultarDate);
    const loading  = document.getElementById("consultarLoading");
    const tabela   = document.getElementById("consultarTabelaConsultas");
    loading.style.display = "";
    tabela.style.display  = "none";

    const input = document.getElementById("consultarDataInput");
    if (input) input.value = dateStr;

    fetch(`${STREAMER}/consultas?date=${dateStr}&token=${TOKEN}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        loading.style.display = "none";
        tabela.style.display  = "";
        _consultarLista = (d && d.consultas) || [];
        const ops = [...new Set(_consultarLista.map(c => c.operador).filter(Boolean))].sort();
        const sel = document.getElementById("consultarOperadorFilter");
        if (sel) sel.innerHTML = '<option value="">Todos os operadores</option>' + ops.map(o => `<option value="${o}">${o}</option>`).join("");
        _renderConsultar();
      })
      .catch(() => { loading.style.display = "none"; tabela.style.display = ""; });
  }

  // ── Vídeo da consulta — abre no varDrawer lateral ─────────────────────
  function _abrirVideoConsulta(c) {
    const STREAMER = (window.APP_CONFIG||{}).STREAMER_URL || "";
    const TOKEN    = (window.APP_CONFIG||{}).STREAMER_TOKEN || "";

    // Calcular janela ±10s
    const dt  = new Date((c.timestamp||"").replace(" ","T"));
    const fmt = d => { const p = n => String(n).padStart(2,"0"); return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; };
    const start = fmt(new Date(dt.getTime() - 10000));
    const end   = fmt(new Date(dt.getTime() + 10000));
    const clipUrl = `${STREAMER}/clip?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&token=${TOKEN}`;

    // Preencher header do varDrawer
    document.getElementById("varResultModalBreadcrumb").textContent = `PDV ${String(varPdvSelecionado||1).padStart(2,"0")} · ${c.cupom ? "Cupom #"+c.cupom : (c.time||"").slice(0,8)}`;
    document.getElementById("varResultModalTitle").textContent = c.desc || c.cod || "Item consultado";

    // Ocultar tabs do cupom
    const tabBar = varDrawer.querySelector(".var-tab-bar");
    if (tabBar) tabBar.style.display = "none";

    // Montar body: vídeo + informações abaixo
    const body = document.getElementById("varResultModalBody");
    body.innerHTML = `
      <div class="var-inline-player">
        <video id="cvDrawerVideo" controls playsinline webkit-playsinline preload="metadata"
               style="width:100%;display:none;background:#000;max-height:45vh;object-fit:cover"></video>
        <div id="cvDrawerLoading" style="text-align:center;padding:32px;color:var(--muted)">
          <i data-lucide="loader-circle" style="width:32px;height:32px;animation:spin 1s linear infinite"></i>
          <p style="margin-top:8px;font-size:13px">Gerando vídeo…</p>
        </div>
        <div id="cvDrawerErro" hidden style="text-align:center;padding:32px;color:var(--muted)">
          <i data-lucide="video-off" style="width:32px;height:32px"></i>
          <p style="margin-top:8px;font-size:13px">Vídeo não disponível para este item.</p>
        </div>
      </div>
      <div style="padding:14px 16px;display:flex;flex-direction:column;gap:10px">
        <dl class="event-data">
          <div><dt>Item</dt><dd>${c.desc || c.cod || "—"}</dd></div>
          <div><dt>Horário</dt><dd>${(c.time||"").slice(0,8)}</dd></div>
          <div><dt>Quantidade</dt><dd>${_fmtQty(c.qty, c.unit)}</dd></div>
          <div><dt>Valor</dt><dd>${_fmtBRL(c.vtotal)}</dd></div>
          <div><dt>Operador</dt><dd>${c.operador||"—"}</dd></div>
          <div><dt>Cupom</dt><dd>${c.cupom||"—"}</dd></div>
        </dl>
        ${c.consultas && c.consultas.length ? `
        <div class="ia-diagnostico">
          <div class="ia-diagnostico-header"><i data-lucide="search"></i><strong>Consultas do item</strong></div>
          <div class="ia-diagnostico-body">
            ${c.consultas.map(q => `<div class="ia-linha"><span class="ia-label">${q.type||"Consulta"}</span><span>${q.time||""}</span></div>`).join("")}
          </div>
        </div>` : ""}
      </div>`;

    lucide.createIcons();
    openVarDrawer();

    const video   = document.getElementById("cvDrawerVideo");
    const loading = document.getElementById("cvDrawerLoading");
    const erro    = document.getElementById("cvDrawerErro");

    const timeout = setTimeout(() => { loading.style.display = "none"; erro.hidden = false; }, 90000);

    fetch(clipUrl)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        clearTimeout(timeout);
        if (!d?.token) { loading.style.display = "none"; erro.hidden = false; return; }
        video.src = `${STREAMER}/clip/${d.token}?token=${TOKEN}`;
        video.style.display = "";
        loading.style.display = "none";
        video.addEventListener("error", () => { video.style.display = "none"; erro.hidden = false; }, { once: true });
        video.play().catch(() => {});
      })
      .catch(() => { clearTimeout(timeout); loading.style.display = "none"; erro.hidden = false; });
  }

  // ── Inicialização ──────────────────────────────────────────────────────
  window.iniciarViewConsultar = function() {
    _consultarDate = new Date();
    _carregarConsultar();
  };

  document.addEventListener("DOMContentLoaded", () => {
    // Botões de data rápida
    document.querySelectorAll(".consultar-quick").forEach(b => {
      b.addEventListener("click", () => {
        document.querySelectorAll(".consultar-quick").forEach(x => x.classList.remove("active", "cupons-quick-active"));
        b.classList.add("active");
        const d = new Date();
        d.setDate(d.getDate() - parseInt(b.dataset.days));
        _consultarDate = d;
        const inp = document.getElementById("consultarDataInput");
        if (inp) inp.value = _fmtDate(d);
        _carregarConsultar();
      });
    });

    // Input de data
    const inp = document.getElementById("consultarDataInput");
    if (inp) {
      inp.addEventListener("change", () => {
        if (!inp.value) return;
        document.querySelectorAll(".consultar-quick").forEach(x => x.classList.remove("active"));
        _consultarDate = new Date(inp.value + "T12:00:00");
        _carregarConsultar();
      });
    }

    // Filtros locais (busca, operador, período) — sempre volta pra página 1
    document.getElementById("consultarSearch")?.addEventListener("input", () => _renderConsultar(1));
    document.getElementById("consultarOperadorFilter")?.addEventListener("change", () => _renderConsultar(1));
    document.getElementById("consultarPeriodoFilter")?.addEventListener("change", () => _renderConsultar(1));

    // Botão atualizar
    const btnR = document.getElementById("btnConsultarRefresh");
    if (btnR) btnR.addEventListener("click", _carregarConsultar);

    // Fechar modal ao clicar fora
    const modal = document.getElementById("consultaVideoModal");
    if (modal) {
      modal.addEventListener("click", e => {
        if (e.target === modal) fecharConsultaVideoModal();
      });
    }
  });
})();

let activeFilter2 = "all";
let _alertsPagAtual = 1;

function iniciarViewAlertas() {
  _alertsPagAtual = 1;

  // Sincronizar input de data com selectedDate atual
  const dateInp = document.getElementById("alertsDateInput");
  if (dateInp) {
    dateInp.value = selectedDate;
    dateInp.max = formatDateInput(new Date());
  }
  // Marcar botão Hoje/Ontem/Anteontem correto
  _syncAlertDateBtns();
  renderAlertas2();

  // Botões de data rápida
  document.querySelectorAll(".alerts2-date").forEach(btn => {
    btn.addEventListener("click", () => {
      const d = new Date();
      d.setDate(d.getDate() - parseInt(btn.dataset.days || "0"));
      selectedDate = formatDateInput(d);
      if (dateInp) dateInp.value = selectedDate;
      _syncAlertDateBtns();
      _alertsPagAtual = 1;
      carregarAlertas();
    });
  });

  // Input de data manual
  if (dateInp) {
    dateInp.addEventListener("change", () => {
      if (!dateInp.value) return;
      selectedDate = dateInp.value;
      document.querySelectorAll(".alerts2-date").forEach(b => b.classList.remove("active"));
      _alertsPagAtual = 1;
      carregarAlertas();
    });
  }

  document.getElementById("searchInput2")?.addEventListener("input", () => { _alertsPagAtual = 1; renderAlertas2(); });
  document.getElementById("btnAlertsRefresh")?.addEventListener("click", () => {
    carregarAlertas();
  });
  document.querySelectorAll(".alerts2-filter").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".alerts2-filter").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      activeFilter2 = btn.dataset.filter || "all";
      _alertsPagAtual = 1;
      renderAlertas2();
    });
  });
}

function _syncAlertDateBtns() {
  document.querySelectorAll(".alerts2-date").forEach(btn => {
    const d = new Date();
    d.setDate(d.getDate() - parseInt(btn.dataset.days || "0"));
    btn.classList.toggle("active", formatDateInput(d) === selectedDate);
  });
}

function renderAlertas2() {
  const query = (document.getElementById("searchInput2")?.value || "").toLowerCase();
  const table2 = document.getElementById("alertsTable2");
  if (!table2) return;

  // Alertas mostra só divergências reais — itens conferidos (severity "ok") ficam na Auditoria IA
  const alertasReais = alerts.filter(a => a.severity !== "ok");

  // Atualizar badges
  document.getElementById("countAll2").textContent = alertasReais.length;
  document.getElementById("countCritical2").textContent = alertasReais.filter(a => a.severity === "critical").length;
  document.getElementById("countReview2").textContent = alertasReais.filter(a => a.state !== "resolved").length;
  document.getElementById("countResolved2").textContent = alertasReais.filter(a => a.state === "resolved").length;

  const filtrados = alertasReais.filter(a => {
    const filterMatch = activeFilter2 === "all"
      || (activeFilter2 === "critical" && a.severity === "critical")
      || (activeFilter2 === "review" && a.state !== "resolved")
      || (activeFilter2 === "resolved" && a.state === "resolved");
    const text = `${a.pdv} ${a.receipt} ${a.product} ${a.event}`.toLowerCase();
    return filterMatch && text.includes(query);
  });

  if (!filtrados.length) {
    table2.innerHTML = `<tr class="empty-row"><td colspan="8" style="text-align:center;padding:32px;color:var(--muted)">Nenhum alerta encontrado.</td></tr>`;
    document.getElementById("alertsPaginacao").style.display = "none";
    return;
  }

  const pagSlice = filtrados.slice((_alertsPagAtual - 1) * POR_PAGINA, _alertsPagAtual * POR_PAGINA);

  table2.innerHTML = pagSlice.map(alert => `
    <tr class="cupons-row" data-id="${alert.id}">
      <td><span class="severity ${alert.severity}"><i></i>${alert.severity === "critical" ? "Crítico" : alert.severity === "warning" ? "Atenção" : "Normal"}</span></td>
      <td>${alert.time}</td>
      <td class="receipt-cell"><strong>${alert.pdv}</strong><span>Cupom ${alert.receipt}</span></td>
      <td><div class="event-cell"><img class="mini-cctv" src="${alert.imageUrl || 'assets/frame-register.svg'}" ${alert.imageUrl ? `loading="lazy" onerror="this.src='assets/frame-register.svg';this.onerror=null"` : ''} alt=""><div><strong>${alert.event}</strong><span>${alert.subtitle}</span></div></div></td>
      <td class="product-cell"><strong>${alert.product}</strong><span>${alert.qty} · ${alert.value}</span></td>
      <td><div class="confidence"><span>${alert.confidence}%</span><i class="confidence-meter"><i style="width:${alert.confidence}%"></i></i></div></td>
      <td><span class="state-badge ${alert.state}">${alert.stateText}</span></td>
      <td><div class="row-actions"><button data-action="open" title="Revisar"><i data-lucide="scan-search"></i></button><button data-action="video" title="Ver vídeo"><i data-lucide="play"></i></button></div></td>
    </tr>`).join("");

  table2.querySelectorAll("tr").forEach(row => {
    row.addEventListener("click", event => {
      const a = alerts.find(x => x.id === Number(row.dataset.id));
      if (!a) return;
      if (event.target.closest("[data-action='video']")) { selectedAlert = a; document.getElementById("videoButton").click(); }
      else { openDrawer(a); }
    });
  });

  hydrateProtectedMedia(table2);
  lucide.createIcons();
  _renderPaginacao("alertsPaginacaoInfo","alertsPaginacaoBtns","alertsPaginacao",
    _alertsPagAtual, filtrados.length, p => { _alertsPagAtual = p; renderAlertas2(); });
}

function _triggerPipeline() {
  const itens = window._pipeItens;
  const s = window._pipeStats || {};
  atualizarPipeline(itens, s.fila, s.analisados, s.ok, s.alertas, s.media_s, s.ultimo_s, s.sem_dvr, s.descartado, s.historico_total, s.historico_ok, s.historico_suspeito);
}

function atualizarPipelineLegacy(itens, fila, analisados, ok, alertas, media_s, ultimo_s, sem_dvr, descartado, historico_total, historico_ok, historico_suspeito) {
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  set("pipeItens",      itens     != null ? Number(itens).toLocaleString("pt-BR") : "—");
  set("pipeFila",       fila      != null ? fila      : "—");
  set("pipeAnalisados", analisados != null ? analisados : "—");
  set("pipeOk",         ok        != null ? ok        : "—");
  set("pipeAlertas",    alertas   != null ? alertas   : "—");
  const pctAnalisados = itens > 0 ? ((analisados / itens) * 100).toFixed(1) : 0;
  const pctOk         = analisados > 0 ? ((ok / analisados) * 100).toFixed(1) : 0;
  const pctAlertas    = analisados > 0 ? ((alertas / analisados) * 100).toFixed(1) : 0;
  const semDvr = sem_dvr || 0;
  set("pipeAnalisadosPct", semDvr > 0 ? `${pctAnalisados}% · ${semDvr} sem DVR` : `${pctAnalisados}% do total`);
  const desc = descartado || 0;
  set("pipeDescartados", desc);
  set("pipeDescartadosSub", semDvr > 0 ? `hoje · ${semDvr} sem DVR` : 'hoje');
  const ht = historico_total || 0;
  set("pipeHistoricoTotal", ht.toLocaleString("pt-BR"));
  if (ht > 0) {
    const pctOkH = Math.round((historico_ok||0)/ht*100);
    set("pipeHistoricoSub", `${pctOkH}% OK · ${(historico_suspeito||0)} alertas`);
  }
  set("pipeOkPct",      `${pctOk}%`);
  set("pipeAlertasPct", `${pctAlertas}%`);
  if (ultimo_s || media_s) {
    set("pipeTempo", `⏱ ${ultimo_s || media_s}s/item`);
  }
  lucide.createIcons();
}

function atualizarPipeline(itens, fila, analisados, ok, alertas, media_s, ultimo_s, sem_dvr, descartado, historico_total, historico_ok, historico_suspeito) {
  const s = window._pipeStats || {};
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  const nItens = Number(itens || 0);
  const nFila = Number(fila || 0);
  const nOk = Number(ok || 0);
  const nSuspeitosIa = Number(alertas || 0);
  const nAlertasHumanos = alerts.filter(alert => alert.state !== "resolved").length;
  const nInconclusivos = Number(s.inconclusivos || 0);
  const nPulados = Number(s.pulados || 0);
  const semImagem = Number(sem_dvr || 0) + Number(descartado || 0);
  const nProcessados = Number(s.processados ?? (nOk + nSuspeitosIa + nInconclusivos + nPulados + semImagem));
  const medicao = s.medicao || {};
  const nEntrada = Number(medicao.itens_novos ?? nItens);
  const nPendentes = Math.max(0, Number(medicao.pendencia_pela_conta ?? (nEntrada - nProcessados)));
  const nIaTotal = Number(analisados || 0);
  const pctAnalisados = nEntrada > 0 ? ((nProcessados / nEntrada) * 100).toFixed(1) : "0.0";
  const pctOk = nIaTotal > 0 ? ((nOk / nIaTotal) * 100).toFixed(1) : "0.0";
  const pctSuspeitosIa = nIaTotal > 0 ? ((nSuspeitosIa / nIaTotal) * 100).toFixed(1) : "0.0";
  const pctInc = nIaTotal > 0 ? ((nInconclusivos / nIaTotal) * 100).toFixed(1) : "0.0";

  set("pipeItens", itens != null ? nItens.toLocaleString("pt-BR") : "-");
  set("pipeFila", itens != null ? nPendentes : "-");
  set("pipeFilaSub", s.fila_interna != null && s.fila_interna !== nPendentes ? `interna: ${s.fila_interna}` : "pendentes pela conta");
  set("pipeAnalisados", itens != null ? nProcessados : "-");
  set("pipeAnalisadosPct", `${pctAnalisados}% processados`);
  set("pipeTempo", (ultimo_s || media_s) ? `${ultimo_s || media_s}s/item` : "-");
  set("pipeDescartados", semImagem + nPulados);
  set("pipeDescartadosSub", `${nPulados} sem IA · ${sem_dvr || 0} sem DVR`);

  const inconclusiveCard = document.getElementById("pipeHistoricoTotal")?.closest(".pipeline-step");
  if (inconclusiveCard) {
    inconclusiveCard.style.background = "#fff9db";
    inconclusiveCard.querySelector(".pipeline-label").textContent = "Inconclusivos";
    inconclusiveCard.querySelector(".pipeline-sub").id = "pipeInconclusivosPct";
    const icon = inconclusiveCard.querySelector(".pipeline-icon");
    if (icon) icon.innerHTML = '<i data-lucide="circle-help" style="color:#e67700"></i>';
  }
  set("pipeHistoricoTotal", nInconclusivos);
  set("pipeInconclusivosPct", `${pctInc}%`);
  set("pipeOk", nOk);
  set("pipeOkPct", `${pctOk}%`);
  set("pipeAlertas", nAlertasHumanos);
  set("pipeAlertasPct", `${nSuspeitosIa} suspeitos IA · ${pctSuspeitosIa}%`);

  const histTotal = Number(historico_total || 0);
  set("pipeHistoricoResumo", histTotal.toLocaleString("pt-BR"));
  set("pipeHistoricoResumoSub", histTotal > 0
    ? `${historico_ok || 0} OK · ${historico_suspeito || 0} alertas · ${s.historico_inconclusivo || 0} inconclusivos`
    : "desde sempre");
  lucide.createIcons();
}

function atualizarPipeline(itens, fila, analisados, ok, alertas, media_s, ultimo_s, sem_dvr, descartado, historico_total, historico_ok, historico_suspeito) {
  const s = window._pipeStats || {};
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  const medicao = s.medicao || {};
  const isCupomMode = (medicao.audit_mode || s.audit_mode) === "cupom";
  const nItens = Number(itens || 0);
  const nOk = Number(ok || 0);
  const nSuspeitosIa = Number(alertas || 0);
  const nAlertasHumanos = alerts.filter(alert => alert.state !== "resolved").length;
  const nInconclusivos = Number(s.inconclusivos || 0);
  const nPulados = Number(s.pulados || 0);
  const semImagem = Number(sem_dvr || 0) + Number(descartado || 0);
  const nProcessados = Number(s.processados ?? (nOk + nSuspeitosIa + nInconclusivos + nPulados + semImagem));
  const nEntrada = Number(medicao.itens_novos ?? nItens);
  const nPendentes = Math.max(0, Number(medicao.pendencia_pela_conta ?? (nEntrada - nProcessados)));

  const entradaPipeline = isCupomMode ? Number(s.cupoms_enfileirados || 0) : nEntrada;
  const filaPipeline = isCupomMode ? Number(s.cupoms_fila || 0) : nPendentes;
  const emAnalisePipeline = isCupomMode ? Number(s.cupoms_em_analise || 0) : 0;
  const processadosPipeline = isCupomMode ? Number(s.cupoms_auditados || 0) : nProcessados;
  const okPipeline = isCupomMode ? Number(s.cupoms_aprovados || 0) : nOk;
  const suspeitosPipeline = isCupomMode ? Number(s.cupoms_suspeitos || 0) : nSuspeitosIa;
  const inconclusivosPipeline = isCupomMode ? Number(s.cupoms_inconclusivos || 0) : nInconclusivos;
  const incompletosPipeline = isCupomMode ? Number(s.cupoms_incompletos || 0) : (semImagem + nPulados);
  const pctAnalisados = entradaPipeline > 0 ? ((processadosPipeline / entradaPipeline) * 100).toFixed(1) : "0.0";
  const pctOk = processadosPipeline > 0 ? ((okPipeline / processadosPipeline) * 100).toFixed(1) : "0.0";
  const pctSuspeitosIa = processadosPipeline > 0 ? ((suspeitosPipeline / processadosPipeline) * 100).toFixed(1) : "0.0";
  const pctInc = processadosPipeline > 0 ? ((inconclusivosPipeline / processadosPipeline) * 100).toFixed(1) : "0.0";

  set("pipeItensLabel", isCupomMode ? "Cupons fechados" : "Itens no caixa");
  set("pipeItensSub", isCupomMode ? "enviados para auditoria" : "passaram pelo scanner");
  set("pipeFilaLabel", isCupomMode ? "Cupons na fila" : "Fila IA");
  set("pipeAnalisadosLabel", isCupomMode ? "Cupons auditados" : "Analisados");
  set("pipeItens", entradaPipeline.toLocaleString("pt-BR"));
  set("pipeFila", filaPipeline);
  set("pipeFilaSub", isCupomMode ? "aguardando analise" : (s.fila_interna != null && s.fila_interna !== nPendentes ? `interna: ${s.fila_interna}` : "pendentes pela conta"));
  set("pipeAnalisadosLabel", isCupomMode ? "Em analise" : "Analisados");
  set("pipeAnalisados", isCupomMode ? emAnalisePipeline : processadosPipeline);
  set("pipeAnalisadosPct", isCupomMode ? "cupons em andamento" : `${pctAnalisados}% processados`);
  set("pipeTempo", isCupomMode ? "" : ((ultimo_s || media_s) ? `${ultimo_s || media_s}s/item` : "-"));
  set("pipeDescartadosLabel", isCupomMode ? "Cupons auditados" : "Descartados");
  set("pipeDescartados", isCupomMode ? processadosPipeline : incompletosPipeline);
  set("pipeDescartadosSub", isCupomMode ? `${pctAnalisados}% processados` : `${nPulados} sem IA - ${sem_dvr || 0} sem DVR`);
  const auditedCard = document.getElementById("pipeDescartados")?.closest(".pipeline-step");
  if (auditedCard) {
    auditedCard.style.background = isCupomMode ? "#e3fafc" : "#f8f9fa";
    const auditedIcon = auditedCard.querySelector(".pipeline-icon");
    if (auditedIcon) auditedIcon.innerHTML = isCupomMode
      ? '<i data-lucide="clipboard-check" style="color:#0b7285"></i>'
      : '<i data-lucide="ban" style="color:#868e96"></i>';
    auditedCard.querySelectorAll(".pipeline-num, .pipeline-label").forEach(el => {
      el.style.color = isCupomMode ? "#0b7285" : "#868e96";
    });
  }

  const inconclusiveCard = document.getElementById("pipeHistoricoTotal")?.closest(".pipeline-step");
  if (inconclusiveCard) {
    inconclusiveCard.style.display = isCupomMode ? "none" : "";
    const previousArrow = inconclusiveCard.previousElementSibling;
    if (previousArrow?.classList?.contains("pipeline-arrow")) previousArrow.style.display = isCupomMode ? "none" : "";
    inconclusiveCard.style.background = "#fff9db";
    inconclusiveCard.querySelector(".pipeline-label").textContent = isCupomMode ? "Pendentes" : "Inconclusivos";
    inconclusiveCard.querySelector(".pipeline-sub").id = "pipeInconclusivosPct";
    const icon = inconclusiveCard.querySelector(".pipeline-icon");
    if (icon) icon.innerHTML = '<i data-lucide="circle-help" style="color:#e67700"></i>';
  }
  set("pipeHistoricoTotal", isCupomMode ? (filaPipeline + emAnalisePipeline) : inconclusivosPipeline);
  set("pipeInconclusivosPct", isCupomMode ? `${filaPipeline} fila - ${emAnalisePipeline} analise` : `${pctInc}%`);
  set("pipeOk", okPipeline);
  set("pipeOkPct", `${pctOk}%`);
  set("pipeAlertasLabel", isCupomMode ? "Reprovados" : "Alertas humanos");
  set("pipeAlertas", isCupomMode ? suspeitosPipeline : nAlertasHumanos);
  set("pipeAlertasPct", isCupomMode ? `${pctSuspeitosIa}% dos auditados` : `${nSuspeitosIa} suspeitos IA - ${pctSuspeitosIa}%`);

  const histTotal = Number(historico_total || 0);
  set("pipeHistoricoResumo", histTotal.toLocaleString("pt-BR"));
  set("pipeHistoricoResumoSub", histTotal > 0
    ? `${historico_ok || 0} OK · ${historico_suspeito || 0} alertas · ${s.historico_inconclusivo || 0} inconclusivos`
    : "desde sempre");
  lucide.createIcons();
}

async function carregarItensCaixa() {
  try {
    const STREAMER = (window.APP_CONFIG || {}).STREAMER_URL || "";
    const TOKEN    = (window.APP_CONFIG || {}).STREAMER_TOKEN || "";
    const today    = selectedDate;
    const r = await fetch(`${STREAMER}/stats?date=${today}&token=${TOKEN}`);
    if (!r.ok) return;
    const d = await r.json();
    const el  = document.getElementById("metricItensCaixa");
    const det = document.getElementById("metricItensCaixaDetalhe");
    if (el) el.textContent = (d.total_itens ?? "—").toLocaleString("pt-BR");
    if (det) det.textContent = `em ${d.total_cupons || 0} cupons`;
    window._pipeItens = d.total_itens;
    _triggerPipeline();
  } catch(e) {}
}

async function carregarStatsIA() {
  try {
    const STREAMER = (window.APP_CONFIG||{}).STREAMER_URL || "";
    const TOKEN    = (window.APP_CONFIG||{}).STREAMER_TOKEN || "";
    const today    = selectedDate;
    const r = await fetch(`${STREAMER}/vlm-stats?date=${today}&token=${TOKEN}`);
    if (!r.ok) return;
    const d = await r.json();
    const el  = document.getElementById("metricIAAprovados");
    const det = document.getElementById("metricIADetalhe");
    const elT = document.getElementById("metricIATempo");
    const detT = document.getElementById("metricIATempoDetalhe");
    if (el) el.textContent = d.aprovados ?? "—";
    if (det) {
      const taxa = d.taxa_aprovacao ? `${d.taxa_aprovacao}%` : "0%";
      det.textContent = `${d.suspeitos || 0} suspeitos de ${d.total || 0} · ${taxa}`;
    }
    if (elT) elT.textContent = d.ultimo_s ? `${d.ultimo_s}s` : (d.media_s ? `${d.media_s}s` : "—");
    if (detT) {
      const total = d.total || 0;
      detT.textContent = total > 0 ? `último · méd ${d.media_s || 0}s · ${total} itens` : "aguardando análises…";
    }
    const minMax = document.getElementById("metricIATempoMinMax");
    if (minMax && d.min_s && d.max_s) {
      minMax.textContent = `mín ${d.min_s}s · máx ${d.max_s}s`;
    }
    const elFila = document.getElementById("metricIAFila");
    const detFila = document.getElementById("metricIAFilaDetalhe");
    if (elFila) elFila.textContent = d.fila ?? 0;
    if (detFila) {
      const analisados = (d.medicao || {}).audit_mode === "cupom" ? (d.cupoms_auditados || 0) : (d.total || 0);
      const fila = d.fila || 0;
      detFila.textContent = fila > 0
        ? `${fila} aguardando · ${analisados} analisados`
        : `fila vazia · ${analisados} analisados`;
    }
    window._pipeStats = { fila: d.fila || 0, fila_interna: d.fila_interna || 0, medicao: d.medicao || null, analisados: d.total || 0, ok: d.aprovados || 0, alertas: d.suspeitos || 0, inconclusivos: d.inconclusivos || 0, pulados: d.pulados || 0, processados: d.processados || 0, media_s: d.media_s, ultimo_s: d.ultimo_s, sem_dvr: d.sem_dvr || 0, descartado: d.descartado || 0, historico_total: d.historico_total || 0, historico_ok: d.historico_ok || 0, historico_suspeito: d.historico_suspeito || 0, historico_inconclusivo: d.historico_inconclusivo || 0, cupoms_enfileirados: d.cupoms_enfileirados || 0, cupoms_auditados: d.cupoms_auditados || 0, cupoms_aprovados: d.cupoms_aprovados || 0, cupoms_suspeitos: d.cupoms_suspeitos || 0, cupoms_inconclusivos: d.cupoms_inconclusivos || 0, cupoms_incompletos: d.cupoms_incompletos || 0, cupoms_fila: d.cupoms_fila || 0 };
    window._pipeStats.cupoms_em_analise = d.cupoms_em_analise || 0;
    _triggerPipeline();
  } catch(e) {}
}

function iniciarViewAuditIa() {
  const dateInput = document.getElementById("auditIaDateInput");
  if (dateInput) {
    dateInput.value = selectedDate;
    if (!dateInput.dataset.bound) {
      dateInput.addEventListener("change", () => {
        selectedDate = dateInput.value || selectedDate;
        atualizarRotuloData();
        carregarAuditIa();
      });
      dateInput.dataset.bound = "1";
    }
  }

  document.querySelectorAll(".audit-result").forEach(btn => {
    if (!btn.dataset.bound) {
      btn.addEventListener("click", () => {
        auditIaResult = btn.dataset.result || "";
        document.querySelectorAll(".audit-result").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        carregarAuditIa();
      });
      btn.dataset.bound = "1";
    }
    btn.classList.toggle("active", (btn.dataset.result || "") === auditIaResult);
  });

  const search = document.getElementById("auditIaSearchInput");
  if (search && !search.dataset.bound) {
    search.addEventListener("input", renderAuditIa);
    search.dataset.bound = "1";
  }
  const refresh = document.getElementById("btnAuditIaRefresh");
  if (refresh && !refresh.dataset.bound) {
    refresh.addEventListener("click", carregarAuditIa);
    refresh.dataset.bound = "1";
  }

  carregarAuditIa();
}

async function carregarAuditIa() {
  // Auditoria IA mostra TODOS os itens analisados (Gemini) — aprovados e suspeitos
  const params = new URLSearchParams({ loja: LOJA, filter: "all", data: selectedDate });
  try {
    const resp = await apiFetch(`/api/v1/alerts?${params}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    let items = await resp.json();
    // Resultado vindo do backend: "result" já traduzido (Confere, Categoria divergente, etc)
    if (auditIaResult === "OK") items = items.filter(i => i.severity === "ok");
    else if (auditIaResult === "SUSPEITO") items = items.filter(i => i.severity !== "ok");
    auditIaItems = items;
    renderAuditIa();
  } catch (e) {
    auditIaItems = [];
    renderAuditIa();
  }
}

function renderAuditIa() {
  const tbody = document.getElementById("auditIaTable");
  const resumo = document.getElementById("auditIaResumo");
  if (!tbody) return;
  const q = (document.getElementById("auditIaSearchInput")?.value || "").trim().toLowerCase();
  const rows = auditIaItems.filter(item => {
    if (!q) return true;
    return [item.product, item.receipt, item.analysis, item.result, item.note]
      .some(v => String(v || "").toLowerCase().includes(q));
  });

  const ok = auditIaItems.filter(i => i.severity === "ok").length;
  const suspeito = auditIaItems.filter(i => i.severity !== "ok").length;
  if (resumo) {
    resumo.innerHTML = `<strong>${rows.length}</strong><small>${ok} aprovados · ${suspeito} suspeitos IA</small>`;
  }

  tbody.innerHTML = rows.length ? rows.map(item => {
    const sevLabel = item.severity === "critical" ? "Suspeito" : (item.severity === "warning" ? "Atenção" : "Aprovado");
    const subtitle = (item.analysis || "").replace(/^(PASSO \d:\s*)/i, "").slice(0, 70);
    return `
      <tr class="cupons-row" data-id="${item.id}">
        <td><span class="severity ${item.severity}"><i></i>${escapeText(sevLabel)}</span></td>
        <td>${escapeText(item.time || "-")}</td>
        <td class="receipt-cell"><strong>${escapeText(item.pdv || "-")}</strong><span>Cupom ${escapeText(item.receipt || "-")}</span></td>
        <td><div class="event-cell"><img class="mini-cctv" src="${item.imageUrl || 'assets/frame-register.svg'}" loading="lazy" onerror="this.src='assets/frame-register.svg';this.onerror=null" alt=""><div><strong>${escapeText(item.event || "-")}</strong><span>${escapeText(subtitle)}</span></div></div></td>
        <td class="product-cell"><strong>${escapeText(item.product || "-")}</strong><span>${escapeText(item.value || "-")}</span></td>
        <td><div class="confidence"><span>${item.confidence || 0}%</span><i class="confidence-meter"><i style="width:${item.confidence || 0}%"></i></i></div></td>
        <td><div class="row-actions"><button data-action="open" title="Revisar"><i data-lucide="scan-search"></i></button><button data-action="video" title="Ver vídeo"><i data-lucide="play"></i></button></div></td>
      </tr>`;
  }).join("") : `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:22px">Sem registros para o filtro</td></tr>`;
  tbody.querySelectorAll("tr[data-id]").forEach(row => {
    row.addEventListener("click", event => {
      const item = auditIaItems.find(i => String(i.id) === row.dataset.id);
      if (!item) return;
      if (event.target.closest("[data-action='video']")) {
        openDrawer(item);
        setTimeout(() => document.getElementById("videoButton").click(), 100);
      } else {
        openDrawer(item);
      }
    });
  });
  lucide.createIcons();
}

function auditIaCompatibilityDetails(item) {
  const raw = item?.compatibilidade_score;
  const score = raw === null || raw === undefined || raw === "" ? null : Number(raw);
  if (!Number.isFinite(score)) return "-";
  const cls = score >= 80 ? "status-ok" : (score >= 50 ? "status-review" : "status-alert");
  const risk = item.risco ? `<small>${escapeText(item.risco)}</small>` : "";
  return `<span class="${cls}">${score}%</span>${risk}`;
}

function auditIaPhysicalCell(item) {
  const blocked = item?.fisico_observado?.measurement_blocked || item?.measurement_blocked;
  const expected = item?.porte_esperado || item?.fisico_esperado?.size || "";
  const observed = item?.porte_observado || item?.fisico_observado?.size || "";
  const occRaw = item?.ocupacao_scanner ?? item?.fisico_observado?.scanner_occupancy;
  const occ = Number(occRaw);
  const main = blocked ? "ocluido" : (observed && observed !== "unknown" ? observed : "-");
  const sub = [];
  if (blocked) sub.push("mao/braco");
  if (!blocked && Number.isFinite(occ)) sub.push(`${(occ * 100).toFixed(0)}% scanner`);
  if (expected && expected !== "unknown") sub.push(`esp. ${expected}`);
  return `<strong>${escapeText(main)}</strong>${sub.length ? `<small>${escapeText(sub.join(" · "))}</small>` : ""}`;
}

function auditIaReasonSummary(item) {
  const reasons = Array.isArray(item?.compatibilidade_motivos) ? item.compatibilidade_motivos : [];
  const physical = auditIaPhysicalSummary(item);
  if (!reasons.length) return physical ? `${item?.motivo || "-"}: ${physical}` : (item?.motivo || "-");
  const main = reasons.slice(0, 2).map(r => `${r.ok ? "OK" : "X"} ${r.text || ""}`).join(" | ");
  return `${item?.motivo || "-"}: ${physical ? physical + " | " : ""}${main}`;
}

function auditIaPhysicalSummary(item) {
  const size = item?.porte_observado || item?.fisico_observado?.size || "";
  const occRaw = item?.ocupacao_scanner ?? item?.fisico_observado?.scanner_occupancy;
  const occ = Number(occRaw);
  const parts = [];
  if (size && size !== "unknown") parts.push(`visto ${size}`);
  if (Number.isFinite(occ)) parts.push(`ocupa ${(occ * 100).toFixed(0)}%`);
  return parts.join(", ");
}

function auditIaEvidenceButtons(item) {
  const buttons = [
    ["image_url", "Foto", "image"],
    ["measure_url", "Medição", "scan-search"],
    ["focus_url", "Foco", "focus"],
  ].map(([field, title, icon]) => {
    const url = auditIaEvidenceUrl(item, field);
    if (!url) return "";
    return `<button class="audit-photo-button" data-audit-image="${escapeText(url)}" title="${escapeText(title)}"><i data-lucide="${icon}"></i></button>`;
  }).filter(Boolean).join("");
  return buttons ? `<div class="audit-evidence-actions">${buttons}</div>` : "-";
}

function auditIaEvidenceUrl(item, field = "image_url") {
  const STREAMER = (window.APP_CONFIG || {}).STREAMER_URL || "";
  const TOKEN = (window.APP_CONFIG || {}).STREAMER_TOKEN || "";
  const value = item?.[field];
  if (!STREAMER || !TOKEN || !value) return "";
  const raw = String(value);
  const path = raw.startsWith("/streamer/") ? raw.slice("/streamer".length) : raw;
  const url = new URL(`${STREAMER}${path}`, location.href);
  url.searchParams.set("token", TOKEN);
  return url.pathname + url.search;
}

function iniciarApp() {
  atualizarRotuloData();
  carregarAlertas();
  carregarHealth();
  carregarVendas(); carregarGeminiCredito();

  setInterval(() => {
    if (isHoje(selectedDate)) carregarAlertas();
  }, REFRESH_INTERVAL_MS);
  setInterval(carregarHealth, REFRESH_INTERVAL_MS);
  carregarStatsIA();
  setInterval(carregarStatsIA, 30000);
  carregarItensCaixa();
  setInterval(carregarItensCaixa, 30000);
  setInterval(() => {
    if (isHoje(selectedDate)) carregarVendas(); carregarGeminiCredito();
  }, REFRESH_INTERVAL_MS);
}

// ── Relatórios ──────────────────────────────────────────────────────────────
document.getElementById("btnImprimirRelatorio")?.addEventListener("click", () => window.print());

let _rptTimer = null;

function _iniciarAutoRefreshRelatorios() {
  if (_rptTimer) clearInterval(_rptTimer);
  _rptTimer = setInterval(() => {
    if (document.getElementById("viewReports")?.style.display !== "none") {
      iniciarViewRelatorios();
    } else {
      clearInterval(_rptTimer);
      _rptTimer = null;
    }
  }, 60000); // atualiza a cada 1 minuto
}

async function iniciarViewRelatorios() {
  const STREAMER = (window.APP_CONFIG||{}).STREAMER_URL || "";
  const TOKEN    = (window.APP_CONFIG||{}).STREAMER_TOKEN || "";
  const date     = selectedDate;

  // Atualizar subtítulo com a data e hora da última atualização
  const sub = document.getElementById("rptSubtitulo");
  if (sub) {
    const d = new Date(date + "T12:00:00");
    const agora = new Date().toLocaleTimeString("pt-BR", {hour:"2-digit", minute:"2-digit"});
    sub.textContent = `${d.toLocaleDateString("pt-BR", {weekday:"long", day:"numeric", month:"long"})} · atualizado às ${agora}`;
  }
  _iniciarAutoRefreshRelatorios();

  // Buscar cupons e alertas em paralelo
  const [cuponResp, statsResp] = await Promise.all([
    fetch(`${STREAMER}/cupons?date=${date}&token=${TOKEN}`).then(r => r.ok ? r.json() : {cupons:[]}),
    fetch(`${STREAMER}/vlm-stats?date=${date}&token=${TOKEN}`).then(r => r.ok ? r.json() : {}),
  ]);

  const cupons  = cuponResp.cupons || [];
  const alertsD = alerts || [];  // usa alertas já carregados

  // ── KPIs ──────────────────────────────────────────────────────────
  const totalVendas = cupons.reduce((s, c) => s + (c.total||0), 0);
  const totalItens  = cupons.reduce((s, c) => s + (c.itens||0), 0);
  const totalCupons = cupons.filter(c => c.fechou).length;
  const ticketMedio = totalCupons > 0 ? totalVendas / totalCupons : 0;
  const aprovados   = statsResp.aprovados || 0;
  const suspeitos   = statsResp.suspeitos || 0;
  const taxaIA      = statsResp.taxa_aprovacao || 0;

  const fmt = v => `R$ ${v.toLocaleString("pt-BR", {minimumFractionDigits:2,maximumFractionDigits:2})}`;

  document.getElementById("rptKpis").innerHTML = [
    { label:"Vendido no dia",   value: fmt(totalVendas),  sub: `${totalCupons} cupons` },
    { label:"Itens vendidos",   value: totalItens.toLocaleString("pt-BR"), sub: `${(totalItens/Math.max(totalCupons,1)).toFixed(1)} itens/cupom` },
    { label:"Ticket médio",     value: fmt(ticketMedio),  sub: "por cupom fechado" },
    { label:"IA aprovados",     value: aprovados,         sub: `${taxaIA}% de aprovação` },
    { label:"Alertas IA",       value: suspeitos,         sub: `de ${aprovados+suspeitos} analisados` },
  ].map(k => `
    <div class="rpt-kpi">
      <div class="rpt-kpi-label">${k.label}</div>
      <div class="rpt-kpi-value">${k.value}</div>
      <div class="rpt-kpi-sub">${k.sub}</div>
    </div>`).join("");

  // ── Vendas por hora ──────────────────────────────────────────────
  const porHora = {};
  const alertsPorHora = {};
  cupons.forEach(c => {
    const h = (c.abriu||"").slice(0,2);
    if (!h) return;
    porHora[h] = (porHora[h]||0) + (c.total||0);
  });
  alertsD.forEach(a => {
    const h = (a.time||"").slice(0,2);
    if (h) alertsPorHora[h] = (alertsPorHora[h]||0) + 1;
  });
  const horas = Array.from({length:24},(_,i)=>String(i).padStart(2,"0")).filter(h=>porHora[h]>0);
  const maxV = Math.max(...horas.map(h=>porHora[h]||0), 1);
  const BAR_MAX_PX = 100;
  const chartEl = document.getElementById("rptVendasHora");
  chartEl.innerHTML = horas.map(h => {
    const px = Math.max(Math.round(((porHora[h]||0)/maxV)*BAR_MAX_PX), 3);
    const temAlerta = alertsPorHora[h] > 0;
    const val = fmtBRL(porHora[h]||0);
    return `<div class="rpt-col-wrap" data-val="${val}" data-h="${h}h" title="${h}h · ${val}">
      <div class="rpt-col ${temAlerta?'has-alert':''}" style="height:${px}px"></div>
      <div class="rpt-col-label">${h}</div>
    </div>`;
  }).join("") || "<p style='color:var(--muted);font-size:12px'>Sem dados</p>";

  // Tooltip ao clicar (funciona em touch/mobile)
  let _rptTooltip = null;
  chartEl.querySelectorAll(".rpt-col-wrap").forEach(col => {
    col.addEventListener("click", e => {
      if (_rptTooltip) { _rptTooltip.remove(); _rptTooltip = null; }
      const tip = document.createElement("div");
      tip.style.cssText = "position:absolute;background:#172026;color:white;border-radius:6px;padding:4px 8px;font-size:11px;font-weight:600;white-space:nowrap;pointer-events:none;z-index:10;transform:translateX(-50%)";
      tip.textContent = `${col.dataset.h} · ${col.dataset.val}`;
      col.style.position = "relative";
      col.appendChild(tip);
      _rptTooltip = tip;
      setTimeout(() => { if (_rptTooltip === tip) { tip.remove(); _rptTooltip = null; } }, 3000);
      e.stopPropagation();
    });
  });
  document.addEventListener("click", () => { if (_rptTooltip) { _rptTooltip.remove(); _rptTooltip = null; } }, { once: true });

  // ── Alertas por categoria ────────────────────────────────────────
  const porCat = {};
  alertsD.forEach(a => {
    const analysis = a.analysis || "";
    const m = analysis.match(/cat:\s*([^)]+)/i);
    const cat = m ? m[1].trim() : "Outros";
    porCat[cat] = (porCat[cat]||0) + 1;
  });
  const maxCat = Math.max(...Object.values(porCat), 1);
  const CAT_LABEL = {
    OUTROS:"Outros", PAO:"Pão", LEGUME_VERDURA:"Legume/Verdura",
    FRUTA:"Fruta", LATICINIOS:"Laticínios", CARNE_SUINA:"Carne Suína",
    CARNE_BOVINA:"Carne Bovina", BISCOITO:"Biscoito", REFRIGERANTE:"Refrigerante",
    FRIOS:"Frios", FRANGO:"Frango", HIGIENE:"Higiene", LIMPEZA:"Limpeza",
    SALGADINHO:"Salgadinho", DOCE_CHOCOLATE:"Doce/Choc", SUCO_AGUA:"Suco/Água",
    CERVEJA:"Cerveja", OVOS:"Ovos", CONGELADO:"Congelado",
    CEREAIS_GRAOS:"Cereais/Grãos", MERCEARIA:"Mercearia", PEIXE:"Peixe",
  };
  const topCats = Object.entries(porCat).sort((a,b)=>b[1]-a[1]).slice(0,8);
  document.getElementById("rptAlertasCat").innerHTML = topCats.length ? topCats.map(([cat,n]) => `
    <div class="rpt-bar-row">
      <div class="rpt-bar-label" title="${cat}">${CAT_LABEL[cat]||cat}</div>
      <div class="rpt-bar-track">
        <div class="rpt-bar-fill danger" style="width:${Math.round(n/maxCat*100)}%"></div>
      </div>
      <div class="rpt-bar-val">${n}</div>
    </div>`).join("") : "<p style='color:var(--muted);font-size:12px'>Sem alertas</p>";

  // ── Top operadores ───────────────────────────────────────────────
  const porOp = {};
  cupons.forEach(c => {
    const op = c.operador || "—";
    if (!porOp[op]) porOp[op] = {total:0, cupons:0, alertas:0};
    porOp[op].total  += c.total||0;
    porOp[op].cupons += 1;
  });
  alertsD.forEach(a => {
    // Tentar mapear PDV/cupom para operador
    const cupom = String(a.receipt||"").replace(/\D/g,"");
    const c = cupons.find(x=>x.numero===cupom);
    if (c?.operador) porOp[c.operador] = porOp[c.operador]||{total:0,cupons:0,alertas:0};
    if (c?.operador) porOp[c.operador].alertas++;
  });
  const topOps = Object.entries(porOp).sort((a,b)=>b[1].total-a[1].total).slice(0,6);
  document.getElementById("rptOperadores").innerHTML = topOps.length ? topOps.map(([op,d],i) => {
    const ticket = d.cupons > 0 ? d.total / d.cupons : 0;
    return `<div class="rpt-rank-row">
      <div class="rpt-rank-num">${i+1}</div>
      <div style="flex:1;min-width:0">
        <div class="rpt-rank-name">${op}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:2px">${d.cupons} cupons · ticket médio ${fmtBRL(ticket)}</div>
      </div>
      <div class="rpt-rank-val">${fmtBRL(d.total)}</div>
    </div>`;
  }).join("") : "<p style='color:var(--muted);font-size:12px'>Sem dados</p>";

  // ── Produtos mais alertados ──────────────────────────────────────
  const porProd = {};
  alertsD.forEach(a => { const p = a.product||"?"; porProd[p]=(porProd[p]||0)+1; });
  const topProds = Object.entries(porProd).sort((a,b)=>b[1]-a[1]).slice(0,10);
  const maxProd = Math.max(...topProds.map(x=>x[1]),1);
  document.getElementById("rptProdutos").innerHTML = topProds.length ? `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
      ${topProds.map(([p,n],i)=>`
      <div class="rpt-bar-row" style="margin:0">
        <div class="rpt-bar-label" style="width:20px">${i+1}</div>
        <div style="flex:1;min-width:0">
          <div style="font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:3px" title="${p}">${p}</div>
          <div class="rpt-bar-track" style="height:8px">
            <div class="rpt-bar-fill amber" style="height:8px;width:${Math.round(n/maxProd*100)}%"></div>
          </div>
        </div>
        <div class="rpt-bar-val" style="min-width:24px">${n}</div>
      </div>`).join("")}
    </div>` : "<p style='color:var(--muted);font-size:12px'>Sem dados de alerta</p>";

  lucide.createIcons();
}

verificarAuth();

// ── Lojas ──────────────────────────────────────────────────────────────
let _lojaEditandoId = null;

async function carregarLojas() {
  const resp = await apiFetch("/api/v1/lojas");
  if (!resp.ok) return;
  const lojas = await resp.json();
  const tbody = document.getElementById("lojasTable");
  if (lojas.length === 0) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="6">Nenhuma loja cadastrada.</td></tr>`;
    return;
  }
  tbody.innerHTML = lojas.map(l => {
    const token = l.api_token || "";
    const mask = token ? token.slice(0,8) + "••••••••" + token.slice(-4) : "—";
    const criado = l.criado_em ? new Date(l.criado_em).toLocaleDateString("pt-BR") : "—";
    return `<tr>
      <td><strong>${l.nome}</strong></td>
      <td><code style="font-size:12px">${l.id}</code></td>
      <td>${l.pdv_nome || '<span style="color:var(--muted)">—</span>'}</td>
      <td>
        <div style="display:flex;align-items:center;gap:6px">
          <code style="font-size:11px;color:var(--muted)">${mask}</code>
          <button data-laction="copy" data-token="${token}" title="Copiar token" style="padding:2px 6px"><i data-lucide="copy" style="width:14px;height:14px"></i></button>
          <button data-laction="regen" data-id="${l.id}" data-nome="${l.nome}" title="Regenerar token" style="padding:2px 6px"><i data-lucide="refresh-cw" style="width:14px;height:14px"></i></button>
        </div>
      </td>
      <td>${criado}</td>
      <td>
        <div class="row-actions">
          <button data-laction="edit" data-id="${l.id}" title="Editar"><i data-lucide="pencil"></i></button>
          <button data-laction="del" data-id="${l.id}" data-nome="${l.nome}" title="Excluir"><i data-lucide="trash-2" style="color:#c92a2a"></i></button>
        </div>
      </td>
    </tr>`;
  }).join("");
  tbody.querySelectorAll("button[data-laction]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const a = btn.dataset.laction;
      if (a === "copy") {
        navigator.clipboard.writeText(btn.dataset.token).then(() => showToast("Token copiado!"));
      } else if (a === "edit") {
        const l = lojas.find(x => x.id === btn.dataset.id);
        _abrirModalLoja(l);
      } else if (a === "del") {
        if (!confirm(`Excluir loja "${btn.dataset.nome}"?`)) return;
        const r = await apiFetch(`/api/v1/lojas/${btn.dataset.id}`, { method: "DELETE" });
        if (r.ok) { showToast("Loja excluída."); carregarLojas(); }
      } else if (a === "regen") {
        if (!confirm(`Regenerar token de "${btn.dataset.nome}"?\nO PDV instalado vai parar até ser reconfigurado.`)) return;
        const r = await apiFetch(`/api/v1/lojas/${btn.dataset.id}/token`, { method: "POST" });
        if (r.ok) { const d = await r.json(); _mostrarLojaToken(d.api_token, true); }
      }
    });
  });
  lucide.createIcons();
}

function _mostrarLojaToken(token, isRegen = false) {
  document.getElementById("lojaTokenValor").textContent = token;
  document.getElementById("lojaTokenInline").textContent = token;
  document.getElementById("modalLojaTokenTitulo").textContent = isRegen ? "Token regenerado" : "Token gerado";
  document.getElementById("modalLojaTokenDesc").textContent = isRegen
    ? "Atualize AUDITORIA_API_TOKEN no arquivo /etc/pdv-telegram-assistant.env no PDV e reinicie os serviços."
    : "Copie o token e use no instalador PDV. Não será exibido novamente.";
  document.getElementById("lojaTokenInstalador").style.display = isRegen ? "none" : "";
  document.getElementById("modalLojaToken").style.display = "flex";
  lucide.createIcons();
}

function _abrirModalLoja(loja = null) {
  _lojaEditandoId = loja ? loja.id : null;
  document.getElementById("modalLojaTitulo").textContent = loja ? "Editar loja" : "Nova loja";
  document.getElementById("lId").value = loja?.id || "";
  document.getElementById("lId").disabled = !!loja;
  document.getElementById("lNome").value = loja?.nome || "";
  document.getElementById("lPdvNome").value = loja?.pdv_nome || "";
  document.getElementById("btnSalvarLoja").textContent = loja ? "Salvar" : "Criar loja";
  document.getElementById("modalLojaErro").hidden = true;
  document.getElementById("modalLoja").style.display = "flex";
  lucide.createIcons();
}

document.getElementById("btnNovaLoja").addEventListener("click", () => _abrirModalLoja());
document.getElementById("closeModalLoja").addEventListener("click", () => document.getElementById("modalLoja").style.display = "none");
document.getElementById("cancelarModalLoja").addEventListener("click", () => document.getElementById("modalLoja").style.display = "none");
document.getElementById("closeModalLojaToken").addEventListener("click", () => document.getElementById("modalLojaToken").style.display = "none");
document.getElementById("fecharModalLojaToken").addEventListener("click", () => { document.getElementById("modalLojaToken").style.display = "none"; carregarLojas(); });
document.getElementById("btnCopiarLojaToken").addEventListener("click", () => {
  navigator.clipboard.writeText(document.getElementById("lojaTokenValor").textContent).then(() => showToast("Token copiado!"));
});

document.getElementById("formLoja").addEventListener("submit", async (e) => {
  e.preventDefault();
  const erro = document.getElementById("modalLojaErro");
  erro.hidden = true;
  const body = {
    id: document.getElementById("lId").value.trim().toLowerCase(),
    nome: document.getElementById("lNome").value.trim(),
    pdv_nome: document.getElementById("lPdvNome").value.trim() || undefined,
  };
  const url = _lojaEditandoId ? `/api/v1/lojas/${_lojaEditandoId}` : "/api/v1/lojas";
  const method = _lojaEditandoId ? "PUT" : "POST";
  const resp = await apiFetch(url, { method, body: JSON.stringify(body) });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    erro.textContent = data.detail || "Erro ao salvar.";
    erro.hidden = false;
    return;
  }
  document.getElementById("modalLoja").style.display = "none";
  if (!_lojaEditandoId) {
    const data = await resp.json();
    showToast("Loja criada!");
    _mostrarLojaToken(data.api_token, false);
  } else {
    showToast("Loja atualizada.");
    carregarLojas();
  }
});

// Sino de notificação → navegar direto para Alertas
document.querySelector(".notification-button")?.addEventListener("click", () => {
  const alertsBtn = document.querySelector(".nav-item[data-view='alerts']");
  if (alertsBtn) alertsBtn.click();
});

// ── Configurações ─────────────────────────────────────────────────────────────
let _cfgPdv = null;
let _cfgPdvLista = [];

async function _cfgCarregarPdvs() {
  if (_cfgPdvLista.length) return;
  try {
    const r = await apiFetch(`/api/v1/health?loja=${LOJA}`);
    if (r.ok) {
      const d = await r.json();
      _cfgPdvLista = [...new Set(d.map(h => h.pdv))].sort();
    }
  } catch {}
  if (!_cfgPdvLista.length) _cfgPdvLista = ["001"];
  if (!_cfgPdv) _cfgPdv = _cfgPdvLista[0];
}

function _cfgRenderTabs(containerId, onSelect) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (_cfgPdvLista.length <= 1) {
    // Só um PDV — mostra label fixo indicando qual está sendo configurado
    el.innerHTML = `<div class="config-pdv-selector"><span class="config-pdv-tab active" style="cursor:default">PDV ${String(_cfgPdv || _cfgPdvLista[0] || "001").padStart(3,"0")}</span></div>`;
    return;
  }
  el.innerHTML = `<div class="config-pdv-selector">${
    _cfgPdvLista.map(p => `<button class="config-pdv-tab${p === _cfgPdv ? " active" : ""}" data-pdv="${p}">PDV ${String(p).padStart(3,"0")}</button>`).join("")
  }</div>`;
  el.querySelectorAll(".config-pdv-tab").forEach(btn => {
    btn.addEventListener("click", () => { _cfgPdv = btn.dataset.pdv; onSelect(); });
  });
}

function _cfgSet(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.type === "checkbox") el.checked = (val !== false && val !== 0 && val !== null);
  else el.value = (val !== undefined && val !== null) ? val : "";
}

// ── Config Loja & PDVs ────────────────────────────────────────────────────────
async function iniciarViewConfigLoja() {
  const r = await apiFetch(`/api/v1/config?loja=${LOJA}`);
  const cfg = r.ok ? await r.json() : {};
  _cfgSet("cfgLojaNome", cfg.loja_nome || LOJA_NOME);
  _cfgSet("cfgLojaAmbiente", cfg.ambiente || AMBIENTE);
  _cfgSet("cfgLojaOperadores", (cfg.operadores || []).join(", "));
  lucide.createIcons();
}

async function salvarConfigLoja() {
  const operadoresRaw = document.getElementById("cfgLojaOperadores").value;
  const operadores = operadoresRaw.split(",").map(s => s.trim()).filter(Boolean);
  const dados = {
    loja_nome: document.getElementById("cfgLojaNome").value.trim() || undefined,
    ambiente: document.getElementById("cfgLojaAmbiente").value,
    operadores,
  };
  const r = await apiFetch(`/api/v1/config?loja=${LOJA}`, { method: "PUT", body: JSON.stringify(dados) });
  r.ok ? showToast("Configurações da loja salvas.") : showToast("Erro ao salvar.", "error");
}

// ── Config Auditoria IA ───────────────────────────────────────────────────────
async function iniciarViewConfigAuditoria() {
  await _cfgCarregarPdvs();
  _cfgRenderTabs("cfgAuditTabs", iniciarViewConfigAuditoria);
  const r = await apiFetch(`/api/v1/config/${_cfgPdv}?loja=${LOJA}`);
  const cfg = r.ok ? await r.json() : {};
  _cfgSet("cfgAudEnabled", cfg.auditoria_enabled !== false);
  _cfgSet("cfgAudModoAuto", (cfg.auditoria_modo || "auto") === "auto");
  _cfgSet("cfgAudValorMin", cfg.auditoria_valor_minimo ?? "");
  _cfgSet("cfgAudMaxHora", cfg.auditoria_max_por_hora ?? "");
  _cfgSet("cfgAudProvedor", cfg.auditoria_provedor || "gemini");
  _cfgSet("cfgAudApiKey", cfg.auditoria_api_key || "");
  _cfgSet("cfgAudModelo", cfg.auditoria_modelo || "gemini-2.5-flash");
  _cfgSet("cfgAudConfianca", cfg.auditoria_confianca_minima ?? "");
  _cfgSet("cfgAudPrompt", cfg.auditoria_prompt || "");
  lucide.createIcons();
}

async function salvarConfigAuditoria() {
  const dados = {
    auditoria_enabled: document.getElementById("cfgAudEnabled").checked,
    auditoria_modo: document.getElementById("cfgAudModoAuto").checked ? "auto" : "manual",
    auditoria_provedor: document.getElementById("cfgAudProvedor").value,
    auditoria_modelo: document.getElementById("cfgAudModelo").value.trim() || undefined,
    auditoria_prompt: document.getElementById("cfgAudPrompt").value.trim() || undefined,
  };
  const apiKey = document.getElementById("cfgAudApiKey").value;
  if (apiKey && apiKey !== "***") dados.auditoria_api_key = apiKey;
  const conf = parseInt(document.getElementById("cfgAudConfianca").value);
  if (!isNaN(conf)) dados.auditoria_confianca_minima = conf;
  const valMin = parseFloat(document.getElementById("cfgAudValorMin").value);
  if (!isNaN(valMin)) dados.auditoria_valor_minimo = valMin;
  const maxH = parseInt(document.getElementById("cfgAudMaxHora").value);
  if (!isNaN(maxH)) dados.auditoria_max_por_hora = maxH;
  Object.keys(dados).forEach(k => dados[k] === undefined && delete dados[k]);
  const r = await apiFetch(`/api/v1/config/${_cfgPdv}?loja=${LOJA}`, { method: "PUT", body: JSON.stringify(dados) });
  r.ok ? showToast("Configurações de auditoria salvas.") : showToast("Erro ao salvar.", "error");
}

// ── Config Câmera ─────────────────────────────────────────────────────────────
async function iniciarViewConfigCamera() {
  await _cfgCarregarPdvs();
  _cfgRenderTabs("cfgCamTabs", iniciarViewConfigCamera);
  const r = await apiFetch(`/api/v1/config/${_cfgPdv}?loja=${LOJA}`);
  const cfg = r.ok ? await r.json() : {};
  _cfgSet("cfgCamHost", cfg.camera_host || "");
  _cfgSet("cfgCamUser", cfg.camera_user || "");
  _cfgSet("cfgCamPass", cfg.camera_pass || "");
  _cfgSet("cfgCamChannel", cfg.camera_channel ?? "");
  _cfgSet("cfgCamPhotoDelay", cfg.camera_photo_delay ?? "");
  _cfgSet("cfgCamClipDur", cfg.camera_clip_duration ?? "");
  lucide.createIcons();
}

async function salvarConfigCamera() {
  const dados = {
    camera_host: document.getElementById("cfgCamHost").value.trim() || undefined,
    camera_user: document.getElementById("cfgCamUser").value.trim() || undefined,
    camera_channel: parseInt(document.getElementById("cfgCamChannel").value) || undefined,
    camera_photo_delay: parseInt(document.getElementById("cfgCamPhotoDelay").value),
    camera_clip_duration: parseInt(document.getElementById("cfgCamClipDur").value) || undefined,
  };
  const pass = document.getElementById("cfgCamPass").value;
  if (pass && pass !== "***") dados.camera_pass = pass;
  Object.keys(dados).forEach(k => (dados[k] === undefined || isNaN(dados[k])) && delete dados[k]);
  const r = await apiFetch(`/api/v1/config/${_cfgPdv}?loja=${LOJA}`, { method: "PUT", body: JSON.stringify(dados) });
  r.ok ? showToast("Configurações de câmera salvas.") : showToast("Erro ao salvar.", "error");
}

// ── Config Notificações ───────────────────────────────────────────────────────
async function iniciarViewConfigNotificacoes() {
  const r = await apiFetch(`/api/v1/config?loja=${LOJA}`);
  const cfg = r.ok ? await r.json() : {};
  _cfgSet("cfgNotiToken", cfg.telegram_token || "");
  _cfgSet("cfgNotiChatId", cfg.telegram_chat_id || "");
  _cfgSet("cfgNotiEnabled", cfg.telegram_alertas !== false);
  _cfgSet("cfgNotiSev", cfg.telegram_severidade_minima || "warning");
  _cfgSet("cfgNotiSilStart", cfg.telegram_silencioso_inicio || "");
  _cfgSet("cfgNotiSilEnd", cfg.telegram_silencioso_fim || "");
  lucide.createIcons();
}

async function salvarConfigNotificacoes() {
  const dados = {
    telegram_chat_id: document.getElementById("cfgNotiChatId").value.trim() || undefined,
    telegram_alertas: document.getElementById("cfgNotiEnabled").checked,
    telegram_severidade_minima: document.getElementById("cfgNotiSev").value,
    telegram_silencioso_inicio: document.getElementById("cfgNotiSilStart").value || undefined,
    telegram_silencioso_fim: document.getElementById("cfgNotiSilEnd").value || undefined,
  };
  const token = document.getElementById("cfgNotiToken").value;
  if (token && token !== "***") dados.telegram_token = token;
  Object.keys(dados).forEach(k => dados[k] === undefined && delete dados[k]);
  const r = await apiFetch(`/api/v1/config?loja=${LOJA}`, { method: "PUT", body: JSON.stringify(dados) });
  r.ok ? showToast("Configurações de notificações salvas.") : showToast("Erro ao salvar.", "error");
}

// iOS Safari restaura scroll position ao reabrir aba — forçar topo em múltiplos momentos
if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
function _forcarTopo() {
  window.scrollTo(0, 0);
  document.documentElement.scrollTop = 0;
  document.body.scrollTop = 0;
}
_forcarTopo();
window.addEventListener('load', _forcarTopo);
setTimeout(_forcarTopo, 100);
setTimeout(_forcarTopo, 500);
setTimeout(_forcarTopo, 1000);
