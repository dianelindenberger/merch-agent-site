const state = {
  page: "home",
  homePeriod: "yesterday",
  period: "7D",
  adsTab: "Overview",
  adsPeriod: "today",
  adsCustomStart: "",
  adsCustomEnd: "",
  homeData: null,
  homeError: "",
  homeLoading: false,
  royaltyTier: null,
  royaltyTierError: "",
  adsData: null,
  adsError: "",
  campaignsData: null,
  campaignsError: "",
  campaignSearch: "",
  selectedCampaign: "",
  selectedAdGroup: "",
  campaignDetail: null,
  campaignDetailError: "",
  analyticsData: null,
  analyticsError: "",
  analyticsShowSales: true,
  analyticsShowRoyalties: true,
  analyticsCustomStart: "",
  analyticsCustomEnd: "",
  dailyAudit: null,
  dailyAuditError: "",
  dailyAuditLoading: false,
  aiMessages: [
    { role: "ai", text: "Ask me to explain the daily audit, a bid recommendation, a search term, or a sales opportunity. I can also prepare a campaign-change log, but I will not apply Amazon Ads changes.", evidence: [] },
  ],
  aiLoading: false,
  aiError: "",
  aiScrollToBottom: false,
  aiMode: "audit",
  changeOptions: null,
  changeOptionsError: "",
  logCampaign: "",
  logDate: new Date().toLocaleDateString("en-CA"),
  logDescription: "",
  logMessages: [
    { role: "ai", text: "Select the campaign and date, then describe what changed in your own words.", evidence: [] },
  ],
  logLoading: false,
  logError: "",
  logScrollToBottom: false,
  salesUploadLoading: false,
  salesUploadMessage: "",
  salesUploadError: "",
};

const sampleProducts = [
  { title: "Hold Your Horses Funny Meme", market: "USA", price: "$21.99", royalty: "$5.14", age: "2h ago", thumb: "H", color: "brown" },
  { title: "Bleghssed Death Metal Shirt", market: "USA", price: "$21.99", royalty: "$4.92", age: "3h ago", thumb: "B", color: "black" },
  { title: "Watermelon Cat", market: "UK", price: "$21.99", royalty: "$6.84", age: "4h ago", thumb: "W", color: "green" },
];

const markets = [
  ["USA", 21, "$111.79", "dot-us"],
  ["UK", 2, "$10.01", "dot-uk"],
  ["DE", 2, "$6.01", "dot-de"],
  ["FR", 0, "$0.00", "dot-fr"],
  ["JP", 0, "$0.00", "dot-jp"],
  ["ES", 0, "$0.00", "dot-es"],
];

const campaigns = [
  { name: "Watermelon Cat - Auto", spend: "$34.71", roas: "11.17", fill: 84 },
  { name: "Side Eye Horse Meme - Manual", spend: "$18.23", roas: "4.21", fill: 58 },
  { name: "Eepy Cat - Auto", spend: "$15.08", roas: "3.22", fill: 42 },
];

const pageMeta = {
  home: ["Dashboard", "Today at a glance"],
  ads: ["Ads", "Amazon Ads snapshot"],
  analytics: ["Analytics", "Trends and breakdowns"],
  ai: ["AI Assistant", "Daily audit and business analysis"],
  more: ["More", "Tools and settings"],
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatMoney(value) {
  return `$${Number(value || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatCurrency(value, currency = "USD") {
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: currency || "USD",
      minimumFractionDigits: currency === "JPY" ? 0 : 2,
      maximumFractionDigits: currency === "JPY" ? 0 : 2,
    }).format(Number(value || 0));
  } catch (error) {
    return formatMoney(value);
  }
}

function formatCurrencyBreakdown(items, fallbackValue = 0, fallbackCurrency = "USD") {
  const values = (items || []).filter((item) => Number(item.amount || 0) !== 0);
  if (!values.length) return formatCurrency(fallbackValue, fallbackCurrency);
  return values.map((item) => formatCurrency(item.amount, item.currency)).join(" + ");
}

function campaignChangeSummary(change) {
  if (change?.summary) return change.summary;
  const campaign = change?.campaignName || "Campaign";
  const dateText = change?.effectiveDate || "date not provided";
  if (change?.changeType === "Status change" && change?.newValue) {
    return `${campaign} ${String(change.newValue).toLowerCase()} on ${dateText}`;
  }
  return `${campaign}: ${change?.changeType || "campaign change"} on ${dateText}`;
}

function marketFlag(market) {
  const flags = {
    ".com": "🇺🇸",
    ".co.uk": "🇬🇧",
    ".de": "🇩🇪",
    ".fr": "🇫🇷",
    ".it": "🇮🇹",
    ".es": "🇪🇸",
    ".co.jp": "🇯🇵",
  };
  return flags[String(market?.code || "").toLowerCase()] || "🌐";
}

function displayMoney(value) {
  if (typeof value === "string" && value.trim().startsWith("$")) {
    return value;
  }

  return formatMoney(value);
}

function productColor(index) {
  return ["teal", "sky", "coral", "gold", "indigo"][index % 5];
}

function fallbackHomeData() {
  return {
    source: "unavailable",
    period: "unavailable",
    reportDate: "Data unavailable",
    latestImport: "",
    sales: 0,
    royalties: 0,
    revenue: 0,
    returns: 0,
    markets: [],
    products: [],
    summary: ["Live sales data is unavailable."],
  };
}

async function loadHomeData() {
  state.homeLoading = true;
  try {
    const response = await fetch(`/api/home?period=${encodeURIComponent(state.homePeriod)}`, { cache: "no-store" });

    if (!response.ok) {
      throw new Error(`API returned ${response.status}`);
    }

    state.homeData = await response.json();
    state.homeError = "";
  } catch (error) {
    state.homeData = fallbackHomeData();
    state.homeError = "Live sales data could not be loaded. Keep the Merch Agent server running and try again.";
  } finally {
    state.homeLoading = false;
  }

  render();
}

async function loadRoyaltyTier() {
  try {
    const response = await fetch("/api/royalty-tier", { cache: "no-store" });

    if (!response.ok) {
      throw new Error(`API returned ${response.status}`);
    }

    state.royaltyTier = await response.json();
    state.royaltyTierError = "";
  } catch (error) {
    state.royaltyTier = null;
    state.royaltyTierError = "Royalty tier data needs the v2 data server.";
  }

  render();
}

async function loadAdsData() {
  try {
    const params = new URLSearchParams({ period: state.adsPeriod });
    if (state.adsPeriod === "custom") {
      params.set("start", state.adsCustomStart);
      params.set("end", state.adsCustomEnd);
    }
    const response = await fetch(`/api/ads?${params.toString()}`, { cache: "no-store" });

    if (!response.ok) {
      throw new Error(`API returned ${response.status}`);
    }

    state.adsData = await response.json();
    state.adsError = "";
  } catch (error) {
    state.adsData = null;
    state.adsError = "Showing sample ad metrics. Import an advertised product report to use live data.";
  }

  render();
}

async function loadDailyAudit() {
  state.dailyAuditLoading = true;
  try {
    const response = await fetch("/api/daily-audit", { cache: "no-store" });
    if (!response.ok) throw new Error(`API returned ${response.status}`);
    state.dailyAudit = await response.json();
    state.dailyAuditError = "";
  } catch (error) {
    state.dailyAudit = null;
    state.dailyAuditError = "The daily audit could not be loaded from the data server.";
  } finally {
    state.dailyAuditLoading = false;
  }
  render();
}

async function uploadSalesReport(file) {
  if (!file || state.salesUploadLoading) return;
  state.salesUploadLoading = true;
  state.salesUploadMessage = "";
  state.salesUploadError = "";
  render();
  try {
    const dataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("The file could not be read."));
      reader.readAsDataURL(file);
    });
    const encoded = String(dataUrl || "").split(",", 2)[1] || "";
    const response = await fetch("/api/sales-upload", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fileName: file.name, data: encoded }),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || `Upload failed (${response.status})`);
    state.salesUploadMessage = result.message || "Sales report synchronized.";
    await Promise.all([loadHomeData(), loadAnalyticsData(), loadDailyAudit()]);
  } catch (error) {
    state.salesUploadError = error.message || "The sales report could not be uploaded.";
  } finally {
    state.salesUploadLoading = false;
    render();
  }
}

async function loadCampaignsData() {
  try {
    const response = await fetch("/api/campaigns?period=last30", { cache: "no-store" });

    if (!response.ok) {
      throw new Error(`API returned ${response.status}`);
    }

    state.campaignsData = await response.json();
    state.campaignsError = "";
  } catch (error) {
    state.campaignsData = { campaigns: [] };
    state.campaignsError = "Campaign data could not be loaded from the local database.";
  }

  render();
}

async function askAssistant(question) {
  const cleanQuestion = String(question || "").trim();

  if (!cleanQuestion || state.aiLoading) {
    return;
  }

  state.aiMessages.push({ role: "user", text: cleanQuestion, evidence: [] });
  state.aiLoading = true;
  state.aiError = "";
  state.aiScrollToBottom = true;
  render();

  try {
    const response = await fetch("/api/assistant", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: cleanQuestion,
        period: state.homePeriod,
        history: state.aiMessages.slice(-8).map(({ role, text }) => ({ role, text })),
      }),
    });

    if (!response.ok) {
      throw new Error(`API returned ${response.status}`);
    }

    const data = await response.json();
    state.aiMessages.push({ role: "ai", text: data.answer, evidence: data.evidence || [], pendingLog: data.pendingLog || null, saved: false });
    if (data.action?.type === "navigate" && data.action.page) {
      state.page = data.action.page;
      if (data.action.adsTab) state.adsTab = data.action.adsTab;
    }
  } catch (error) {
    state.aiError = "The assistant could not reach the local data server. Please try again.";
  } finally {
    state.aiLoading = false;
    state.aiScrollToBottom = true;
    render();
  }
}

async function loadChangeOptions() {
  try {
    const response = await fetch("/api/change-options", { cache: "no-store" });
    if (!response.ok) throw new Error(`API returned ${response.status}`);
    state.changeOptions = await response.json();
    state.changeOptionsError = "";
    if (!state.logCampaign && state.changeOptions.campaigns?.length) {
      state.logCampaign = state.changeOptions.campaigns[0].name;
    }
  } catch (error) {
    state.changeOptionsError = "Campaigns and recent changes could not be loaded.";
  }
  if (state.page === "ai" && state.aiMode === "log") render();
}

async function previewLoggedChange() {
  const details = state.logDescription.trim();
  if (!state.logCampaign || !state.logDate || !details || state.logLoading) {
    state.logError = "Choose a campaign and date, then describe what changed.";
    render();
    return;
  }

  state.logMessages.push({ role: "user", text: details, evidence: [] });
  state.logLoading = true;
  state.logError = "";
  state.logScrollToBottom = true;
  render();

  try {
    const response = await fetch("/api/change-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        campaignName: state.logCampaign,
        effectiveDate: state.logDate,
        details,
      }),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || `API returned ${response.status}`);
    state.logMessages.push({
      role: "ai",
      text: data.answer,
      evidence: ["Nothing has been saved yet."],
      pendingLog: data.pendingLog,
      saved: false,
    });
    state.logDescription = "";
  } catch (error) {
    state.logError = error.message || "The change could not be previewed.";
  } finally {
    state.logLoading = false;
    state.logScrollToBottom = true;
    render();
  }
}

async function loadCampaignDetail(campaignName) {
  state.selectedCampaign = campaignName;
  state.selectedAdGroup = "";
  state.campaignDetail = null;
  state.campaignDetailError = "";
  render();

  try {
    const response = await fetch(`/api/campaign-detail?period=last30&name=${encodeURIComponent(campaignName)}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`API returned ${response.status}`);
    state.campaignDetail = await response.json();
  } catch (error) {
    state.campaignDetailError = "Campaign targets could not be loaded from the local database.";
  }

  render();
}

async function saveCampaignChange(messageIndex, source = "ai") {
  const messages = source === "logger" ? state.logMessages : state.aiMessages;
  const message = messages[messageIndex];
  if (!message?.pendingLog || message.saved) return;

  try {
    const response = await fetch("/api/campaign-change", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(message.pendingLog),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || `API returned ${response.status}`);
    message.saved = true;
    message.pendingLog.summary = data.summary || campaignChangeSummary(message.pendingLog);
    message.evidence = [`Saved ${data.loggedAt}`];
    messages.push({
      role: "ai",
      text: `Logged: ${data.summary || campaignChangeSummary(message.pendingLog)}.`,
      evidence: ["This entry will appear in the campaign's Change History."],
    });
    if (source === "logger") await loadChangeOptions();
  } catch (error) {
    if (source === "logger") state.logError = error.message || "The campaign change could not be saved.";
    else state.aiError = error.message || "The campaign change could not be saved.";
  }

  if (source === "logger") state.logScrollToBottom = true;
  else state.aiScrollToBottom = true;
  render();
}

async function loadAnalyticsData() {
  try {
    const params = new URLSearchParams({ period: state.period });
    if (state.period === "Custom") {
      params.set("start", state.analyticsCustomStart);
      params.set("end", state.analyticsCustomEnd);
    }
    const response = await fetch(`/api/analytics?${params.toString()}`, { cache: "no-store" });

    if (!response.ok) {
      throw new Error(`API returned ${response.status}`);
    }

    state.analyticsData = await response.json();
    state.analyticsError = state.analyticsData.error || "";
  } catch (error) {
    state.analyticsData = null;
    state.analyticsError = "Analytics needs the v2 data server and daily sales imports.";
  }

  render();
}

function moneyCard(label, value, delta) {
  return `
    <div class="metric">
      <div class="label">${label}</div>
      <div class="value">${value}</div>
      ${delta ? `<div class="sub positive">${delta}</div>` : ""}
    </div>
  `;
}

function renderMarketBreakdown(homeData) {
  const dataMarkets = homeData.markets || [];

  return `
    <div class="hero-market-section">
      <div class="row">
        <h2 class="section-title">Sales and royalties by market</h2>
        <span class="label positive">${homeData.source === "sqlite" ? "Live data" : "Sample"}</span>
      </div>
      <div class="market-grid">
        ${dataMarkets.length ? dataMarkets.slice(0, 7).map((market) => `
          <div class="market">
            <div class="market-heading">
              <span class="market-flag" role="img" aria-label="${escapeHtml(market.name)} flag">${marketFlag(market)}</span>
              <span class="label">${escapeHtml(market.name)}</span>
            </div>
            <div class="value">${market.units || 0} sold</div>
            <div class="sub">${formatCurrency(market.royalties ?? market.revenue, market.currency || "USD")} ${escapeHtml(market.currency || "USD")} royalties</div>
          </div>
        `).join("") : `<div class="sub empty-state">No marketplace data is available for this period.</div>`}
      </div>
    </div>
  `;
}

function renderProducts(homeData) {
  const dataProducts = homeData.products || [];

  return `
    <div class="card">
      <div class="row">
        <h2 class="section-title">Latest Sales</h2>
        <span class="label positive">${dataProducts.length}</span>
      </div>
      ${dataProducts.slice(0, 12).map((item, index) => `
        <div class="product-row">
          <div class="thumb ${productColor(index)}">${escapeHtml((item.title || "M").slice(0, 1).toUpperCase())}</div>
          <div>
            <div class="title">${escapeHtml(item.title)}</div>
            <div class="sub">${escapeHtml(item.market || "Marketplace pending")} | ${item.units || 0} sold</div>
          </div>
          <div>
            <div class="amount">${formatCurrency(item.royalty, item.currency || "USD")}</div>
            <div class="sub positive">${escapeHtml(item.time || "")}</div>
          </div>
        </div>
      `).join("") || `<div class="sub">No product sales are available for this period.</div>`}
    </div>
  `;
}

function renderHome() {
  const homeData = state.homeData || fallbackHomeData();
  const summary = homeData.summary?.length ? homeData.summary : fallbackHomeData().summary;
  const briefing = homeData.businessBriefing;
  const reportDate = homeData.reportDate || homeData.latestImport || "Latest import";
  const sourceNote = state.homeError
    ? `<section class="card"><div class="sub">${escapeHtml(state.homeError)}</div></section>`
    : "";

  return `
    <div class="stack">
      <div class="chip-row" aria-label="Home date range">
        ${[
          ["yesterday", "Yesterday"],
          ["last7", "7 Days"],
          ["last30", "30 Days"],
        ].map(([value, label]) => `
          <button type="button" class="chip ${value === state.homePeriod ? "active" : ""}" data-home-period="${value}">${label}</button>
        `).join("")}
      </div>
      ${state.homeLoading ? `<section class="card"><div class="sub">Loading ${state.homePeriod === "last30" ? "30-day" : state.homePeriod === "last7" ? "7-day" : "yesterday's"} analysis...</div></section>` : ""}
      ${sourceNote}
      <section class="hero-card">
        <div class="row">
          <div>
            <div class="label">All Markets</div>
            <div class="sub">${escapeHtml(reportDate)}</div>
          </div>
          <span class="chip active">${homeData.period === "yesterday" ? "Yesterday" : homeData.period === "last7" ? "7 Days" : homeData.period === "last30" ? "30 Days" : "Latest"}</span>
        </div>
        <div class="big-sales">${homeData.sales || 0}</div>
        <div class="metric-grid">
          ${moneyCard("Returns", String(homeData.returns || 0))}
          ${moneyCard("Markets with sales", String((homeData.markets || []).filter((market) => Number(market.units || 0) > 0).length))}
        </div>
        ${renderMarketBreakdown(homeData)}
      </section>
      ${briefing ? `
        <section class="card weekly-briefing">
          <div class="row">
            <div>
              <h2 class="section-title">Weekly Business Briefing</h2>
              <div class="sub">${escapeHtml(briefing.periodStart)} through ${escapeHtml(briefing.periodEnd)}</div>
            </div>
            <span class="trend-badge ${escapeHtml(briefing.sales.direction)}">Sales ${escapeHtml(briefing.sales.direction)}</span>
          </div>
          <div class="briefing-grid">
            <div class="briefing-block">
              <small>Sales behavior</small>
              <strong>${briefing.sales.units} units</strong>
              <p>${escapeHtml(briefing.sales.summary)}</p>
            </div>
            <div class="briefing-block">
              <small>Advertising impact</small>
              <strong>${formatMoney(briefing.ads.spend)} spend | ${Number(briefing.ads.acos || 0).toFixed(1)}% ACOS</strong>
              <p>${escapeHtml(briefing.ads.summary)} ${escapeHtml(briefing.impactSummary)}</p>
            </div>
          </div>
          <div class="opportunity-list">
            <h3>Opportunities for the next day and week</h3>
            ${briefing.opportunities.map((item, index) => `<div><span>${index + 1}</span><p>${escapeHtml(item)}</p></div>`).join("")}
          </div>
        </section>
      ` : `
        <section class="card soft-card">
          <h2 class="section-title">Latest Summary</h2>
          <div class="sub">${summary.map(escapeHtml).join(" ")}</div>
        </section>
      `}
      ${renderProducts(homeData)}
    </div>
  `;
}

function bars(values) {
  return `<div class="bar-chart">${values.map((value) => `<div class="bar" style="height:${value}%"></div>`).join("")}</div>`;
}

function formatShortDate(value) {
  const date = new Date(`${value}T00:00:00`);

  if (Number.isNaN(date.getTime())) {
    return value || "";
  }

  return `${date.getMonth() + 1}/${date.getDate()}`;
}

function sampleAnalyticsData() {
  return {
    period: state.period,
    startDate: "",
    endDate: "",
    dayCount: 0,
    totals: { sales: 0, royalties: 0 },
    points: [],
  };
}

function renderComboChart(data) {
  const points = data.points || [];
  const royaltyCurrency = data.royaltyChartCurrency || "USD";

  if (!points.length) {
    return `<div class="sub">No daily sales points are available for this period yet.</div>`;
  }

  const width = 360;
  const height = 240;
  const top = 22;
  const right = 20;
  const bottom = 44;
  const left = 34;
  const chartWidth = width - left - right;
  const chartHeight = height - top - bottom;
  const maxSales = Math.max(1, ...points.map((point) => point.sales || 0));
  const maxRoyalties = Math.max(1, ...points.map((point) => point.royalties || 0));
  const gap = 5;
  const slot = chartWidth / points.length;
  const barWidth = Math.max(5, Math.min(15, slot - gap));

  const yForSales = (value) => top + chartHeight - ((value || 0) / maxSales) * chartHeight;
  const yForRoyalties = (value) => top + chartHeight - ((value || 0) / maxRoyalties) * chartHeight;
  const xForIndex = (index) => left + slot * index + slot / 2;

  const grid = [0, 0.25, 0.5, 0.75, 1].map((ratio) => {
    const y = top + chartHeight - ratio * chartHeight;
    return `<line x1="${left}" y1="${y}" x2="${width - right}" y2="${y}" stroke="#e8eef3" stroke-width="1"/>`;
  }).join("");

  const barsSvg = state.analyticsShowSales
    ? points.map((point, index) => {
      const x = xForIndex(index) - barWidth / 2;
      const y = yForSales(point.sales);
      const barHeight = top + chartHeight - y;
      return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${barHeight.toFixed(2)}" rx="4" fill="#d9ecff" stroke="#68a2e8" stroke-width="1.4"/>`;
    }).join("")
    : "";

  const linePoints = points.map((point, index) => `${xForIndex(index).toFixed(2)},${yForRoyalties(point.royalties).toFixed(2)}`).join(" ");
  const royaltiesSvg = state.analyticsShowRoyalties
    ? `<polyline points="${linePoints}" fill="none" stroke="#e9788f" stroke-width="2.4"/>
       ${points.map((point, index) => `<circle cx="${xForIndex(index).toFixed(2)}" cy="${yForRoyalties(point.royalties).toFixed(2)}" r="3.5" fill="#ffffff" stroke="#e9788f" stroke-width="2"/>`).join("")}`
    : "";

  const labelStep = Math.max(1, Math.ceil((points.length - 1) / 4));
  const labelIndexes = [];
  for (let index = 0; index < points.length; index += labelStep) {
    labelIndexes.push(index);
  }
  if (labelIndexes[labelIndexes.length - 1] !== points.length - 1) {
    labelIndexes.push(points.length - 1);
  }

  return `
    <div class="chart-scale-labels"><span>Units sold</span><span>${escapeHtml(royaltyCurrency)} royalties</span></div>
    <svg class="combo-chart" viewBox="0 0 ${width} ${height - 28}" role="img" aria-label="Sales and ${escapeHtml(royaltyCurrency)} royalties analytics chart">
      ${grid}
      <line x1="${left}" y1="${top + chartHeight}" x2="${width - right}" y2="${top + chartHeight}" stroke="#d8e1e8" stroke-width="1"/>
      ${barsSvg}
      ${royaltiesSvg}
    </svg>
    <div class="chart-axis-labels" style="grid-template-columns: repeat(${labelIndexes.length}, minmax(0, 1fr));">
      ${labelIndexes.map((index) => `<span>${escapeHtml(formatShortDate(points[index].date))}</span>`).join("")}
    </div>
    <div class="chart-legend">
      ${state.analyticsShowSales ? "<span><i class='legend-dot legend-sales'></i>Sales</span>" : ""}
      ${state.analyticsShowRoyalties ? `<span><i class='legend-dot legend-royalties'></i>${escapeHtml(royaltyCurrency)} Royalties</span>` : ""}
    </div>
  `;
}

function renderAnalytics() {
  const data = state.analyticsData || sampleAnalyticsData();
  const points = data.points || [];
  const totals = data.totals || {};
  const royaltyBreakdown = data.royaltyByCurrency || [];
  const royaltySummary = royaltyBreakdown.length
    ? royaltyBreakdown.map((item) => formatCurrency(item.amount, item.currency)).join(" + ")
    : formatCurrency(totals.royalties, data.royaltyChartCurrency || "USD");
  const errorNote = state.analyticsError
    ? `<section class="card"><div class="sub">${escapeHtml(state.analyticsError)}</div></section>`
    : "";

  return `
    <div class="stack">
      <div class="analytics-heading">
        <h2>Analytics</h2>
        <div class="sub">${escapeHtml(data.startDate || "")} - ${escapeHtml(data.endDate || "")}</div>
        <div class="sub">${data.dayCount || points.length}-day window | ${data.availableDayCount ?? points.length} days available</div>
      </div>
      <div class="chip-row">
        ${["7D", "30D", "90D", "1Y", "Custom"].map((label) => `<button class="chip ${label === state.period ? "active" : ""}" data-period="${label}">${label}</button>`).join("")}
      </div>
      ${state.period === "Custom" ? `
        <div class="custom-range">
          <label>Start<input type="date" data-analytics-custom-start value="${escapeHtml(state.analyticsCustomStart)}"></label>
          <label>End<input type="date" data-analytics-custom-end value="${escapeHtml(state.analyticsCustomEnd)}"></label>
          <button type="button" class="primary-button" data-analytics-custom-apply>Apply</button>
        </div>
      ` : ""}
      ${errorNote}
      <section class="card analytics-chart-card">
        <div class="analytics-controls">
          <div>
            <div class="analytics-control-label">Toggle Data</div>
            <div class="analytics-toggle-row">
              <button class="chip ${state.analyticsShowSales ? "active" : ""}" data-analytics-toggle="sales">Sales</button>
              <button class="chip ${state.analyticsShowRoyalties ? "active" : ""}" data-analytics-toggle="royalties">USD Royalties</button>
            </div>
          </div>
        </div>
        ${renderComboChart(data)}
      </section>
      <section class="metric-grid">
        ${moneyCard("Sales", Number(totals.sales || 0).toLocaleString(), `${points.length} daily points`)}
        ${moneyCard("Reported royalties", royaltySummary, "Kept in original currencies")}
      </section>
    </div>
  `;
}

function percent(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function tierClass(tierName) {
  const name = String(tierName || "").toLowerCase();
  if (name === "premium") return "premium";
  if (name === "plus") return "plus";
  return "";
}

function renderRoyaltyTierTracker() {
  const data = state.royaltyTier;

  if (state.royaltyTierError) {
    return `<section class="card"><div class="sub">${escapeHtml(state.royaltyTierError)}</div></section>`;
  }

  if (!data?.primary) {
    const requirements = data?.requirements || {};
    return `
      <section class="card tier-card">
        <div class="row">
          <div>
            <h2 class="section-title">Royalty Tier Tracker</h2>
            <div class="sub">Trailing 60 Days</div>
          </div>
          <span class="status-pill review">Incomplete</span>
        </div>
        <div class="sub">${escapeHtml(data?.summary || "Import matching sales and campaign reports to calculate royalty tier progress.")}</div>
        <div class="tier-requirements">
          <div><span class="requirement-dot ${requirements.sales ? "ready" : ""}"></span><strong>60-day Merch sales</strong><span>${requirements.sales ? "Ready" : "Needed"}</span></div>
          <div><span class="requirement-dot ${requirements.ads ? "ready" : ""}"></span><strong>60-day Amazon Ads</strong><span>${requirements.ads ? "Ready" : "Needed"}</span></div>
        </div>
      </section>
    `;
  }

  const primary = data.primary;
  const premiumProgress = Math.min(100, Math.round((primary.nonOrganicRatio / 0.35) * 100));
  const tierName = primary.tier?.name || "Creator";
  const multiplier = primary.tier?.multiplier || "1x";
  const premiumGap = data.progress?.premiumGap || 0;
  const cards = data.cards || [];
  const cappedNote = primary.isCapped
    ? "<div class='sub'>Ad orders were higher than imported units for this period, so the tracker capped non-organic share at 100%.</div>"
    : "";

  return `
    <section class="card tier-card ${tierClass(tierName)}">
      <div class="row">
        <div>
          <h2 class="section-title">Royalty Tier Tracker</h2>
          <div class="sub">${escapeHtml(primary.label)}${primary.reportDate ? ` | ${escapeHtml(primary.reportDate)}` : ""}</div>
        </div>
        <div class="tier-badge">
          <strong>${escapeHtml(tierName)}</strong>
          <span class="sub">${escapeHtml(multiplier)} royalty</span>
        </div>
      </div>
      <div class="tier-split">
        <div class="tier-split-box">
          <div class="label">Non-Organic</div>
          <div class="tier-percent non-organic">${percent(primary.nonOrganicRatio)}</div>
          <div class="sub">${primary.adOrders || 0} ad orders</div>
        </div>
        <div class="tier-split-box">
          <div class="label">Organic</div>
          <div class="tier-percent organic">${percent(primary.organicRatio)}</div>
          <div class="sub">${primary.organicUnits || 0} organic units</div>
        </div>
      </div>
      <div class="tier-progress"><span style="width:${premiumProgress}%"></span></div>
      <div class="sub">${premiumGap > 0 ? `${percent(premiumGap)} needed for Premium` : "Premium threshold reached"}</div>
      ${cappedNote}
    </section>
    <section class="card">
      <h2 class="section-title">Reference Windows</h2>
      <div class="sub">Only Trailing 60 Days is used for the tier estimate. Shorter windows are shown for context.</div>
      <div class="tier-mini-grid">
        ${cards.slice(0, 3).map((card) => `
          <div class="tier-mini">
            <div class="row">
              <div>
                <div class="title">${escapeHtml(card.label)}</div>
                <div class="sub">${card.units || 0} units | ${card.adOrders || 0} ad orders</div>
              </div>
              <div class="amount">${percent(card.nonOrganicRatio)}</div>
            </div>
            <div class="tier-progress"><span style="width:${Math.min(100, Math.round((card.nonOrganicRatio / 0.35) * 100))}%"></span></div>
            <div class="sub">${escapeHtml(card.tier?.name || "Creator")} estimate</div>
          </div>
        `).join("")}
      </div>
    </section>
  `;
}

function renderCampaignList() {
  const allCampaigns = state.campaignsData?.campaigns || [];
  const search = state.campaignSearch.trim().toLowerCase();
  const campaignsList = allCampaigns.filter((item) => !search || item.name.toLowerCase().includes(search));
  const errorNote = state.campaignsError ? `<section class="card"><div class="sub">${escapeHtml(state.campaignsError)}</div></section>` : "";

  return `
    <input class="search" data-campaign-search value="${escapeHtml(state.campaignSearch)}" placeholder="Search ${state.campaignsData?.count || allCampaigns.length} campaigns">
    ${errorNote}
    <section class="card">
      <div class="row">
        <h2 class="section-title">Campaign Performance</h2>
        <span class="label positive">Last 30 days</span>
      </div>
      ${campaignsList.length ? campaignsList.map((item) => `
        <button type="button" class="campaign-detail-row campaign-open-button" data-campaign-name="${escapeHtml(item.name)}">
          <div class="campaign-detail-heading">
            <div>
              <div class="title">${escapeHtml(item.name)}</div>
              <div class="sub">${escapeHtml(item.country || "Marketplace")} | ${item.clicks} clicks | ${item.orders} orders</div>
            </div>
            <span class="status-pill ${item.status}">${item.status === "performing" ? "Performing" : item.status === "review" ? "Review" : "Watch"}</span>
          </div>
          <div class="campaign-metrics">
            <span><small>Spend</small>${formatCurrency(item.spend, item.currency || "USD")}</span>
            <span><small>Sales</small>${formatCurrency(item.sales, item.currency || "USD")}</span>
            <span><small>ROAS</small>${Number(item.roas || 0).toFixed(2)}</span>
            <span><small>ACOS</small>${Number(item.acos || 0).toFixed(1)}%</span>
          </div>
          <div class="campaign-open-label">View bids and targets <span aria-hidden="true">&#8250;</span></div>
        </button>
      `).join("") : `<div class="sub">No campaigns match this search.</div>`}
    </section>
  `;
}

function renderCampaignDetail() {
  if (state.campaignDetailError) {
    return `<section class="card"><div class="negative sub">${escapeHtml(state.campaignDetailError)}</div></section>`;
  }

  if (!state.campaignDetail) {
    return `<section class="card"><div class="sub">Loading campaign bids and targets...</div></section>`;
  }

  const data = state.campaignDetail;
  const campaign = data.campaign || { name: state.selectedCampaign };
  const adGroups = data.adGroups || [];
  const allTargets = data.targets || [];
  const targets = state.selectedAdGroup
    ? allTargets.filter((item) => item.adGroupName === state.selectedAdGroup)
    : allTargets;
  const changes = data.changes || [];

  return `
    <button type="button" class="back-button" data-campaign-back>&#8249; All campaigns</button>
    <section class="card campaign-detail-header">
      <div class="title">${escapeHtml(campaign.name)}</div>
      <div class="sub">Campaign report ending ${escapeHtml(campaign.reportDate || "latest")}</div>
      <div class="campaign-metrics detail-summary">
        <span><small>Spend</small>${formatCurrency(campaign.spend || 0, campaign.currency || "USD")}</span>
        <span><small>Sales</small>${formatCurrency(campaign.sales || 0, campaign.currency || "USD")}</span>
        <span><small>Orders</small>${campaign.orders || 0}</span>
        <span><small>ROAS</small>${Number(campaign.roas || 0).toFixed(2)}</span>
      </div>
    </section>
    <section class="card">
      <div class="row">
        <h2 class="section-title">Ad Groups</h2>
        <span class="label">${adGroups.length} total</span>
      </div>
      ${adGroups.length ? `
        <button type="button" class="ad-group-row ${state.selectedAdGroup ? "" : "active"}" data-ad-group="">
          <span><strong>All ad groups</strong><small>${allTargets.length} targets</small></span>
          <span class="ad-group-action">Show all</span>
        </button>
        ${adGroups.map((group) => `
          <button type="button" class="ad-group-row ${state.selectedAdGroup === group.name ? "active" : ""}" data-ad-group="${escapeHtml(group.name)}">
            <span><strong>${escapeHtml(group.name)}</strong><small>${group.targetCount} targets | ${group.clicks} clicks | ${group.orders} orders</small></span>
            <span class="ad-group-summary"><strong>${formatCurrency(group.spend, campaign.currency || "USD")}</strong><small>${Number(group.roas || 0).toFixed(2)} ROAS</small></span>
          </button>
        `).join("")}
      ` : `<div class="sub">No ad-group names are available in the latest targeting report.</div>`}
    </section>
    <section class="card">
      <div class="row">
        <h2 class="section-title">${state.selectedAdGroup ? `${escapeHtml(state.selectedAdGroup)} Bids` : "Bids and Targets"}</h2>
        <span class="label">Report ending ${escapeHtml(data.targetReportDate || "not available")}</span>
      </div>
      ${targets.length ? targets.map((item) => `
        <div class="target-row">
          <div class="target-heading">
            <div>
              <div class="title">${escapeHtml(item.target)}</div>
              <div class="sub">${escapeHtml(item.matchType || "Automatic target")}</div>
            </div>
            <div class="bid-value"><small>Current bid</small>${formatCurrency(item.bid, campaign.currency || "USD")}</div>
          </div>
          <div class="target-metrics">
            <span><small>Clicks</small>${item.clicks}</span>
            <span><small>Spend</small>${formatCurrency(item.spend, campaign.currency || "USD")}</span>
            <span><small>Orders</small>${item.orders}</span>
            <span><small>Sales</small>${formatCurrency(item.sales, campaign.currency || "USD")}</span>
            <span><small>ROAS</small>${Number(item.roas || 0).toFixed(2)}</span>
          </div>
        </div>
      `).join("") : `<div class="sub">No target rows are available for this campaign in the latest 30-day targeting report.</div>`}
    </section>
    <section class="card">
      <div class="row">
        <h2 class="section-title">Change History</h2>
        <span class="label">${changes.length} logged</span>
      </div>
      ${changes.length ? changes.map((item) => `
        <div class="change-row">
          <div class="title">${escapeHtml(item.summary || item.changeType)}</div>
          <div class="sub">${escapeHtml(item.details)}</div>
          <div class="sub">${item.effectiveDate ? `Effective ${escapeHtml(item.effectiveDate)} | ` : ""}Logged ${escapeHtml(item.loggedAt)}</div>
        </div>
      `).join("") : `<div class="sub">No changes have been logged for this campaign yet. Tell the AI Assistant what you changed to add the first entry.</div>`}
    </section>
  `;
}

function adsPeriodLabel(period) {
  return {
    today: "Today",
    yesterday: "Yesterday",
    last7: "7 Days",
    last30: "30 Days",
    last60: "60 Days",
    custom: "Custom",
  }[period] || "Today";
}

function advertisedProductStatus(item) {
  const spend = Number(item?.spend || 0);
  const sales = Number(item?.sales || 0);
  if (spend <= 0) return { tone: "inactive", label: "No spend" };
  if (sales <= 0 && spend < 5) return { tone: "gathering", label: "Gathering data" };
  if (sales <= 0) return { tone: "review", label: "No sales yet" };
  const acos = spend / sales * 100;
  return acos <= 20
    ? { tone: "performing", label: "At target" }
    : { tone: "review", label: `ACOS ${acos.toFixed(1)}%` };
}

function renderAdsPerformance(data) {
  const daily = data?.daily || [];
  if (!daily.length) {
    return `<section class="card"><h2 class="section-title">Performance</h2><div class="sub">No daily Ads performance is available for this range yet.</div></section>`;
  }

  const chartWidth = Math.max(420, daily.length * 24);
  const chartHeight = 142;
  const maxImpressions = Math.max(1, ...daily.map((day) => Number(day.impressions || 0)));
  const maxOrders = Math.max(1, ...daily.map((day) => Number(day.orders || 0)));
  const maxAcos = Math.max(1, ...daily.map((day) => Number(day.acos || 0)));
  const step = chartWidth / daily.length;
  const point = (day, index, field, maximum) => {
    const x = Math.round(index * step + step / 2);
    const y = Math.round(chartHeight - 18 - (Number(day[field] || 0) / maximum) * (chartHeight - 42));
    return `${x},${y}`;
  };
  const orderPoints = daily.map((day, index) => point(day, index, "orders", maxOrders)).join(" ");
  const acosPoints = daily.map((day, index) => point(day, index, "acos", maxAcos)).join(" ");
  const total = daily.reduce((sum, day) => ({
    spend: sum.spend + Number(day.spend || 0),
    impressions: sum.impressions + Number(day.impressions || 0),
    orders: sum.orders + Number(day.orders || 0),
    sales: sum.sales + Number(day.sales || 0),
  }), { spend: 0, impressions: 0, orders: 0, sales: 0 });
  const totalAcos = total.sales ? (total.spend / total.sales) * 100 : 0;
  const showEvery = daily.length > 35 ? 7 : daily.length > 16 ? 4 : daily.length > 9 ? 2 : 1;

  return `
    <section class="card ads-performance">
      <div class="row">
        <div>
          <h2 class="section-title">Performance</h2>
          <div class="sub">${escapeHtml(data.rangeStart || "")} to ${escapeHtml(data.rangeEnd || data.reportDate || "")}${data.partial ? " | Today is partial" : ""}</div>
        </div>
      </div>
      <div class="performance-legend">
        <div><span class="legend-swatch spend"></span><small>Total cost</small><strong>${formatMoney(total.spend)}</strong></div>
        <div><span class="legend-swatch impressions"></span><small>Impressions</small><strong>${total.impressions.toLocaleString()}</strong></div>
        <div><span class="legend-swatch acos"></span><small>ACOS</small><strong>${totalAcos.toFixed(1)}%</strong></div>
        <div><span class="legend-swatch orders"></span><small>Purchases</small><strong>${total.orders.toLocaleString()}</strong></div>
      </div>
      <div class="performance-scroll">
        <div class="performance-plot">
          <div class="impression-bars" aria-hidden="true">
            ${daily.map((day) => `<span style="height:${Math.max(2, Math.round((Number(day.impressions || 0) / maxImpressions) * 82))}%"></span>`).join("")}
          </div>
          <svg viewBox="0 0 ${chartWidth} ${chartHeight}" preserveAspectRatio="none" aria-label="Daily ACOS and purchases trend">
            <polyline class="chart-line acos" points="${acosPoints}"></polyline>
            <polyline class="chart-line orders" points="${orderPoints}"></polyline>
          </svg>
          <div class="performance-dates">
            ${daily.map((day, index) => `<span>${index % showEvery === 0 || index === daily.length - 1 ? escapeHtml(String(day.date || "").slice(5)) : ""}</span>`).join("")}
          </div>
        </div>
      </div>
    </section>
  `;
}

function renderAds() {
  const adsTabs = ["Overview", "Campaigns", "Royalty"];
  const adsData = state.adsData;
  const metrics = adsData?.metrics;
  const topProducts = adsData?.topProducts || [];
  const adsSourceNote = state.adsError
    ? `<section class="card"><div class="sub">${escapeHtml(state.adsError)}</div></section>`
    : "";
  const periodControls = `
    <div class="chip-row" aria-label="Ads date range">
      ${[
        ["today", "Today"],
        ["yesterday", "Yesterday"],
        ["last7", "7 Days"],
        ["last30", "30 Days"],
        ["last60", "60 Days"],
        ["custom", "Custom"],
      ].map(([value, label]) => `<button type="button" class="chip ${state.adsPeriod === value ? "active" : ""}" data-ads-period="${value}">${label}</button>`).join("")}
    </div>
    ${state.adsPeriod === "custom" ? `
      <div class="custom-range">
        <label>Start<input type="date" data-ads-custom-start value="${escapeHtml(state.adsCustomStart)}"></label>
        <label>End<input type="date" data-ads-custom-end value="${escapeHtml(state.adsCustomEnd)}"></label>
        <button type="button" class="primary-button" data-ads-custom-apply>Apply</button>
      </div>
    ` : ""}
  `;

  if (state.adsTab === "Royalty") {
    return `
      <div class="stack">
        <div class="chip-row">
          ${adsTabs.map((label) => `<button class="chip ${label === state.adsTab ? "active" : ""}" data-ads-tab="${label}">${label}</button>`).join("")}
        </div>
        ${renderRoyaltyTierTracker()}
      </div>
    `;
  }

  if (state.adsTab === "Campaigns") {
    return `
      <div class="stack">
        <div class="chip-row">
          ${adsTabs.map((label) => `<button type="button" class="chip ${label === state.adsTab ? "active" : ""}" data-ads-tab="${label}">${label}</button>`).join("")}
        </div>
        ${state.selectedCampaign ? renderCampaignDetail() : renderCampaignList()}
      </div>
    `;
  }

  return `
    <div class="stack">
      <div class="chip-row">
        ${adsTabs.map((label) => `<button class="chip ${label === state.adsTab ? "active" : ""}" data-ads-tab="${label}">${label}</button>`).join("")}
      </div>
      ${periodControls}
      ${adsSourceNote}
      <div class="metric-grid">
        ${moneyCard("Ad Spend", metrics ? formatMoney(metrics.spend) : "$0.00", metrics ? `${adsPeriodLabel(state.adsPeriod)}${adsData?.partial ? " | partial" : ""}` : "No data")}
        ${moneyCard("Ad Sales", metrics ? formatMoney(metrics.sales) : "$0.00", metrics ? `${metrics.orders} orders` : "No data")}
        ${moneyCard("ACOS", metrics ? `${metrics.acos}%` : "-", metrics ? `${metrics.units} units` : "No data")}
        ${moneyCard("ROAS", metrics ? String(metrics.roas) : "-", metrics ? `${metrics.asins} ASINs` : "No data")}
        ${moneyCard("Clicks", metrics ? Number(metrics.clicks || 0).toLocaleString() : "0", metrics ? `${metrics.ctr}% CTR` : "No data")}
        ${moneyCard("Impressions", metrics ? Number(metrics.impressions || 0).toLocaleString() : "0", metrics ? `${formatMoney(metrics.cpc)} CPC` : "No data")}
      </div>
      ${renderAdsPerformance(adsData)}
      <section class="card">
        <div class="row">
          <h2 class="section-title">Top Advertised Products</h2>
          <span class="label">Report ending ${escapeHtml(adsData?.reportDate || "latest")}</span>
        </div>
        ${topProducts.length ? topProducts.map((item) => {
          const status = advertisedProductStatus(item);
          return `
          <div class="ad-product-row">
            <div class="ad-product-heading">
              <div>
              <div class="title">${escapeHtml(item.campaignName || item.name)}</div>
                <div class="sub">${escapeHtml(item.asin || "Campaign summary")}</div>
              </div>
              <span class="status-pill ${status.tone}">${status.label}</span>
            </div>
            <div class="ad-product-metrics">
              <span><small>Spend</small>${formatCurrency(item.spend || 0, item.currency || "USD")}</span>
              <span><small>Ad Sales</small>${formatCurrency(item.sales || 0, item.currency || "USD")}</span>
              <span><small>Orders</small>${Number(item.orders || 0).toLocaleString()}</span>
              <span><small>ROAS</small>${Number(item.roas || 0).toFixed(2)}</span>
              <span><small>ACOS</small>${Number(item.sales || 0) ? ((Number(item.spend || 0) / Number(item.sales)) * 100).toFixed(1) + "%" : "-"}</span>
            </div>
          </div>
        `}).join("") : `<div class="sub">No advertised product data is available for this period.</div>`}
      </section>
    </div>
  `;
}

function renderAssistantMessage(message, index, source) {
  return `
    <div class="bubble ${message.role} ${message.pendingLog ? "has-pending-log" : ""}">
      <div>${escapeHtml(message.text)}</div>
      ${message.evidence?.length ? `
        <div class="evidence-list">
          <strong>${message.saved && message.pendingLog ? "Log receipt" : "Data used"}</strong>
          ${message.evidence.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
        </div>
      ` : ""}
      ${message.pendingLog ? `
        <div class="change-confirmation">
          <strong>${message.saved ? "Saved" : "Confirm campaign change"}</strong>
          <span>${escapeHtml(campaignChangeSummary(message.pendingLog))}</span>
          <button type="button" class="log-change-button ${message.saved ? "saved" : ""}" data-log-change="${index}" data-log-source="${source}" ${message.saved ? "disabled" : ""}>${message.saved ? "Change logged" : "Confirm and log change"}</button>
        </div>
      ` : ""}
    </div>
  `;
}

function renderAskAssistant() {
  const suggestions = [
    "Summarize today's daily audit",
    "Which bids should I change?",
    "Which search terms need attention?",
    "What sales opportunities should I review?",
  ];

  return `
    <div class="stack assistant-workspace">
      <div class="chip-row assistant-suggestions">
        ${suggestions.map((label) => `<button type="button" class="chip" data-ai-suggestion="${escapeHtml(label)}">${escapeHtml(label)}</button>`).join("")}
      </div>
      <section class="assistant-thread" aria-live="polite">
        ${state.aiMessages.map((message, index) => renderAssistantMessage(message, index, "ai")).join("")}
        ${state.aiLoading ? `<div class="bubble ai"><span class="typing-dots">Checking your data...</span></div>` : ""}
        ${state.aiError ? `<div class="card"><div class="negative sub">${escapeHtml(state.aiError)}</div></div>` : ""}
      </section>
      <div class="prompt-bar">
        <input data-ai-input aria-label="Ask the assistant" placeholder="Ask a question or give a command">
        <button type="button" class="send-button" data-ai-send aria-label="Send question">&#8593;</button>
      </div>
    </div>
  `;
}

function renderChangeLogger() {
  const campaigns = state.changeOptions?.campaigns || [];
  const recentChanges = state.changeOptions?.recentChanges || [];
  return `
    <div class="stack assistant-workspace">
      <section class="card change-entry-card">
        <div>
          <h2 class="section-title">Log a campaign change</h2>
          <div class="sub">Choose the exact campaign and effective date before describing the change.</div>
        </div>
        <label class="field-label">
          <span>Campaign</span>
          <select data-change-campaign aria-label="Campaign">
            ${campaigns.map((item) => `<option value="${escapeHtml(item.name)}" ${item.name === state.logCampaign ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}
          </select>
        </label>
        <label class="field-label">
          <span>Date change was made</span>
          <input type="date" data-change-date aria-label="Date change was made" value="${escapeHtml(state.logDate)}">
        </label>
        <label class="field-label">
          <span>What changed?</span>
          <textarea data-change-description aria-label="What changed" rows="3" placeholder="Example: Lowered substitutes bid from .13 to .12">${escapeHtml(state.logDescription)}</textarea>
        </label>
        <button type="button" class="preview-change-button" data-change-preview>Preview change</button>
        ${state.changeOptionsError ? `<div class="negative sub">${escapeHtml(state.changeOptionsError)}</div>` : ""}
      </section>
      <section class="assistant-thread change-thread" aria-live="polite">
        ${state.logMessages.map((message, index) => renderAssistantMessage(message, index, "logger")).join("")}
        ${state.logLoading ? `<div class="bubble ai"><span class="typing-dots">Preparing the receipt...</span></div>` : ""}
        ${state.logError ? `<div class="card"><div class="negative sub">${escapeHtml(state.logError)}</div></div>` : ""}
      </section>
      <section class="card recent-changes-card">
        <div class="row">
          <h2 class="section-title">Recent changes</h2>
          <span class="label">${recentChanges.length} shown</span>
        </div>
        ${recentChanges.length ? recentChanges.slice(0, 8).map((item) => `
          <div class="recent-change-row">
            <strong>${escapeHtml(item.summary || item.details)}</strong>
            <span>${escapeHtml(item.loggedAt)}</span>
          </div>
        `).join("") : `<div class="sub">No campaign changes have been logged yet.</div>`}
      </section>
    </div>
  `;
}

function auditActionClass(action) {
  if (action === "Lower bid") return "lower";
  if (action === "Test small increase") return "increase";
  return "hold";
}

function renderDailyAudit() {
  if (state.dailyAuditLoading && !state.dailyAudit) {
    return `<section class="card"><div class="sub">Loading the latest daily audit...</div></section>`;
  }
  if (state.dailyAuditError) {
    return `<section class="card"><div class="negative sub">${escapeHtml(state.dailyAuditError)}</div></section>`;
  }

  const audit = state.dailyAudit || {};
  const recommendations = audit.bidRecommendations || [];
  const changes = recommendations.filter((item) => item.action !== "Hold");
  const holds = recommendations.filter((item) => item.action === "Hold");
  const searchTerms = audit.searchTermFindings || [];
  const salesOpportunities = audit.salesPatternOpportunities || [];
  const bidHistory = audit.inferredBidChanges || [];

  return `
    <div class="stack audit-workspace">
      <section class="card audit-summary">
        <div class="row">
          <div>
            <h2 class="section-title">Daily campaign audit</h2>
            <div class="sub">Target: ${Number(audit.targetRoas || 5).toFixed(2)} ROAS / ${Number(audit.targetAcos || 20).toFixed(0)}% ACOS</div>
          </div>
          <span class="status-pill performing">Read only</span>
        </div>
        <div class="audit-counts">
          <div><strong>${changes.length}</strong><span>Bid actions</span></div>
          <div><strong>${holds.length}</strong><span>Hold steady</span></div>
          <div><strong>${searchTerms.length}</strong><span>Search terms</span></div>
        </div>
        <div class="audit-source sub">Targets: ${escapeHtml(audit.targetSnapshot || "not loaded")} · Search terms: ${escapeHtml(audit.searchTermSnapshot || "pending first import")}${audit.searchTermPeriod ? ` (${escapeHtml(audit.searchTermPeriod === "last7" ? "7-day" : "30-day")})` : ""}</div>
        ${audit.targetDataStale ? `<div class="audit-stale-warning">Target data is ${audit.targetReportDate ? `only current through ${escapeHtml(audit.targetReportDate)}` : "missing a report date"}. Refresh before acting on these bid suggestions.</div>` : ""}
      </section>

      <section class="card audit-section">
        <div class="row">
          <h2 class="section-title">Recommended bid actions</h2>
          <span class="label">${changes.length}</span>
        </div>
        ${changes.length ? changes.slice(0, 20).map((item) => `
          <article class="audit-action ${auditActionClass(item.action)}">
            <div class="audit-action-head">
              <span class="audit-action-label">${escapeHtml(item.action)}</span>
              <span class="confidence ${escapeHtml(item.confidence)}">${escapeHtml(item.confidence)} confidence</span>
            </div>
            <strong>${escapeHtml(item.campaignName)}</strong>
            <div class="audit-target">${escapeHtml(item.target)}${item.matchType ? ` · ${escapeHtml(item.matchType)}` : ""}</div>
            <div class="bid-change"><span>${formatMoney(item.currentBid)}</span><b>→</b><strong>${formatMoney(item.suggestedBid)}</strong><em>${Number(item.changePercent || 0) > 0 ? "+" : ""}${Number(item.changePercent || 0).toFixed(1)}%</em></div>
            <div class="audit-metrics">${item.clicks} clicks · ${formatMoney(item.spend)} spend · ${item.orders} orders · ${Number(item.roas || 0).toFixed(2)} ROAS</div>
            <p>${escapeHtml(item.reason)}</p>
            <button type="button" class="ask-audit-button" data-audit-question="${escapeHtml(`Explain the bid recommendation for ${item.campaignName} target ${item.target}`)}">Ask AI about this</button>
          </article>
        `).join("") : `<div class="audit-empty">No bid changes meet the evidence thresholds. Holding steady is a valid decision.</div>`}
        ${changes.length > 20 ? `<div class="sub">Showing the 20 highest-priority actions of ${changes.length}.</div>` : ""}
      </section>

      <section class="card audit-section">
        <div class="row">
          <h2 class="section-title">Search-term findings</h2>
          <span class="label">${searchTerms.length}</span>
        </div>
        ${searchTerms.length ? searchTerms.slice(0, 15).map((item) => `
          <article class="search-finding">
            <div class="audit-action-head"><strong>${escapeHtml(item.searchTerm)}</strong><span class="confidence ${escapeHtml(item.confidence)}">${escapeHtml(item.confidence)}</span></div>
            <div class="sub">${escapeHtml(item.campaignName)}</div>
            <div class="audit-metrics">${item.clicks} clicks · ${formatMoney(item.spend)} spend · ${item.orders} orders · ${Number(item.roas || 0).toFixed(2)} ROAS</div>
            <div class="search-action">${escapeHtml(item.action)}</div>
            <button type="button" class="ask-audit-button" data-audit-question="${escapeHtml(`Explain search term ${item.searchTerm} in ${item.campaignName}`)}">Ask AI about this</button>
          </article>
        `).join("") : `<div class="audit-empty">Waiting for the first Amazon search-term report to complete. The checkpointed report will resume during the next refresh.</div>`}
      </section>

      <section class="card audit-section">
        <div class="row">
          <h2 class="section-title">Automatically detected bid history</h2>
          <span class="label">${bidHistory.length}</span>
        </div>
        ${bidHistory.length ? bidHistory.slice(0, 12).map((item) => `
          <div class="detected-change">
            <strong>${escapeHtml(item.campaignName)} · ${escapeHtml(item.target)}</strong>
            <span>${formatMoney(item.previousBid)} → ${formatMoney(item.newBid)} (${Number(item.changePercent || 0) > 0 ? "+" : ""}${Number(item.changePercent || 0).toFixed(1)}%)</span>
          </div>
        `).join("") : `<div class="audit-empty">No bid differences have been detected between comparable target snapshots yet.</div>`}
      </section>

      <section class="card audit-section">
        <div class="row">
          <div>
            <h2 class="section-title">Sales-pattern opportunities</h2>
            <div class="sub">Through ${escapeHtml(audit.salesDataThrough || "not loaded")}</div>
          </div>
          <span class="label">${salesOpportunities.length}</span>
        </div>
        ${salesOpportunities.length ? salesOpportunities.slice(0, 15).map((item) => `
          <article class="sales-opportunity">
            <div class="audit-action-head"><strong>${escapeHtml(item.title)}</strong><span class="confidence ${escapeHtml(item.priority)}">${escapeHtml(item.priority)}</span></div>
            <div class="search-action">${escapeHtml(item.pattern)}</div>
            <div class="audit-metrics">${item.recentUnits} units in the latest 7 days · ${item.previousUnits} in the prior 7 · ${formatMoney(item.recentRoyalties)} reported royalties</div>
            <p>${escapeHtml(item.nextStep)}</p>
            <div class="sub">${item.hasObviousCampaign ? "An existing campaign appears to match this title." : "No obvious campaign-name match was found; confirm manually before creating one."}</div>
            <button type="button" class="ask-audit-button" data-audit-question="${escapeHtml(`Explain the sales opportunity for ${item.title}`)}">Ask AI about this</button>
          </article>
        `).join("") : `<div class="audit-empty">No new or accelerating sales pattern currently meets the review threshold.</div>`}
      </section>

      <section class="card soft-card"><div class="sub">${escapeHtml(audit.caveat || "Recommendations are read-only and require review before changes are made in Amazon Ads.")}</div></section>
    </div>
  `;
}

function renderAI() {
  return `
    <div class="stack">
      <section class="card assistant-intro">
        <div class="row">
          <div>
            <h2 class="section-title">Business Assistant</h2>
            <div class="sub">Analysis and change history use separate workspaces</div>
          </div>
          <span class="status-pill performing">Live</span>
        </div>
      </section>
      <div class="assistant-mode-control three" role="tablist" aria-label="Assistant mode">
        <button type="button" role="tab" data-ai-mode="audit" class="${state.aiMode === "audit" ? "active" : ""}" aria-selected="${state.aiMode === "audit"}">Daily Audit</button>
        <button type="button" role="tab" data-ai-mode="ask" class="${state.aiMode === "ask" ? "active" : ""}" aria-selected="${state.aiMode === "ask"}">Ask Assistant</button>
        <button type="button" role="tab" data-ai-mode="log" class="${state.aiMode === "log" ? "active" : ""}" aria-selected="${state.aiMode === "log"}">Log Changes</button>
      </div>
      ${state.aiMode === "audit" ? renderDailyAudit() : state.aiMode === "log" ? renderChangeLogger() : renderAskAssistant()}
    </div>
  `;
}

function renderMore() {
  const statusRows = [
    ["Merch sales", state.homeData?.reportDate || "Not loaded", "Complete through"],
    ["Daily analytics", state.analyticsData?.dataThrough || state.analyticsData?.endDate || "Not loaded", "Available through"],
    ["Amazon Ads", state.adsData?.reportDate || "Not loaded", state.adsData?.partial ? "Includes partial data" : "Report through"],
    ["Campaign performance", state.campaignsData?.reportDate || "Not loaded", "Report through"],
  ];
  return `
    <div class="stack">
      <section class="card">
        <div class="row">
          <div>
            <div class="title">Diane Lindenberger</div>
            <div class="sub">View profile</div>
          </div>
          <div class="profile-mark" aria-hidden="true">DL</div>
        </div>
      </section>
      <section class="card">
        <div class="row">
          <div>
            <h2 class="section-title">Data Status</h2>
            <div class="sub">Dates shown throughout the app come from these local imports.</div>
          </div>
          <span class="status-pill performing">Connected</span>
        </div>
        <div class="status-list">
          ${statusRows.map(([label, value, context]) => `
            <div class="status-row">
              <div><div class="title">${escapeHtml(label)}</div><div class="sub">${escapeHtml(context)}</div></div>
              <strong>${escapeHtml(value)}</strong>
            </div>
          `).join("")}
        </div>
      </section>
      <button type="button" class="primary-button wide-button" data-refresh-all>Refresh displayed data</button>
      <section class="card sales-upload-card">
        <div>
          <h2 class="section-title">Upload Merch sales report</h2>
          <div class="sub">For online use, select the downloaded CSV or Excel report here. It will refresh the sales periods and daily audit.</div>
        </div>
        <input type="file" data-sales-upload accept=".csv,.xlsx,.xls" aria-label="Choose Merch sales report">
        <button type="button" class="primary-button wide-button" data-sales-upload-button ${state.salesUploadLoading ? "disabled" : ""}>${state.salesUploadLoading ? "Uploading and refreshing..." : "Upload sales report"}</button>
        ${state.salesUploadMessage ? `<div class="positive sub">${escapeHtml(state.salesUploadMessage)}</div>` : ""}
        ${state.salesUploadError ? `<div class="negative sub">${escapeHtml(state.salesUploadError)}</div>` : ""}
      </section>
      <section class="card soft-card">
        <h2 class="section-title">Private Local App</h2>
        <div class="sub">Merch sales update when a new report is imported. Amazon Ads data updates through the approved API sync. Refreshing this screen reloads the newest data already stored on this computer.</div>
      </section>
    </div>
  `;
}

function render() {
  const [title, kicker] = pageMeta[state.page];
  document.querySelector(".phone").dataset.page = state.page;
  document.querySelector("#page-title").textContent = title;
  document.querySelector("#page-kicker").textContent = kicker;
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.page === state.page);
  });

  const content = document.querySelector("#app-content");
  const pages = { home: renderHome, ads: renderAds, analytics: renderAnalytics, ai: renderAI, more: renderMore };
  content.innerHTML = pages[state.page]();
  if (state.page === "ai" && state.aiMode === "log" && state.logScrollToBottom) {
    const pendingConfirmation = content.querySelector('[data-log-source="logger"]:not([disabled])');
    (pendingConfirmation || content.querySelector(".change-thread"))?.scrollIntoView({ block: "center" });
    state.logScrollToBottom = false;
  } else if (state.page === "ai" && state.aiScrollToBottom) {
    content.scrollTop = content.scrollHeight;
    state.aiScrollToBottom = false;
  } else {
    content.scrollTop = 0;
  }

  document.querySelectorAll("[data-page]").forEach((button) => {
    button.addEventListener("click", () => {
      state.page = button.dataset.page;
      render();
    });
  });

  document.querySelectorAll("[data-refresh-all]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      await Promise.all([
        loadHomeData(),
        loadRoyaltyTier(),
        loadAdsData(),
        loadCampaignsData(),
        loadAnalyticsData(),
        loadDailyAudit(),
        loadChangeOptions(),
      ]);
    });
  });

  document.querySelectorAll("[data-period]").forEach((button) => {
    button.addEventListener("click", () => {
      state.period = button.dataset.period;
      if (state.period === "Custom" && !state.analyticsCustomStart) {
        const availableEnd = state.analyticsData?.dataThrough || new Date(Date.now() - 86400000).toLocaleDateString("en-CA");
        const end = new Date(`${availableEnd}T00:00:00`);
        const start = new Date(end);
        start.setDate(start.getDate() - 29);
        state.analyticsCustomStart = start.toLocaleDateString("en-CA");
        state.analyticsCustomEnd = availableEnd;
      }
      loadAnalyticsData();
      render();
    });
  });

  const analyticsCustomApply = document.querySelector("[data-analytics-custom-apply]");
  if (analyticsCustomApply) {
    analyticsCustomApply.addEventListener("click", () => {
      state.analyticsCustomStart = document.querySelector("[data-analytics-custom-start]")?.value || "";
      state.analyticsCustomEnd = document.querySelector("[data-analytics-custom-end]")?.value || "";
      loadAnalyticsData();
    });
  }

  document.querySelectorAll("[data-home-period]").forEach((button) => {
    button.addEventListener("click", () => {
      state.homePeriod = button.dataset.homePeriod;
      state.homeData = null;
      state.homeLoading = true;
      render();
      loadHomeData();
    });
  });

  const campaignSearch = document.querySelector("[data-campaign-search]");
  if (campaignSearch) {
    campaignSearch.addEventListener("input", (event) => {
      state.campaignSearch = event.target.value;
      render();
      const nextSearch = document.querySelector("[data-campaign-search]");
      nextSearch?.focus();
      nextSearch?.setSelectionRange(state.campaignSearch.length, state.campaignSearch.length);
    });
  }

  document.querySelectorAll("[data-campaign-name]").forEach((button) => {
    button.addEventListener("click", () => loadCampaignDetail(button.dataset.campaignName));
  });

  const campaignBack = document.querySelector("[data-campaign-back]");
  if (campaignBack) {
    campaignBack.addEventListener("click", () => {
      state.selectedCampaign = "";
      state.selectedAdGroup = "";
      state.campaignDetail = null;
      render();
    });
  }

  document.querySelectorAll("[data-ad-group]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedAdGroup = button.dataset.adGroup || "";
      render();
    });
  });

  document.querySelectorAll("[data-ads-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      state.adsTab = button.dataset.adsTab;
      render();
    });
  });

  document.querySelectorAll("[data-ads-period]").forEach((button) => {
    button.addEventListener("click", () => {
      state.adsPeriod = button.dataset.adsPeriod;
      if (state.adsPeriod === "custom" && !state.adsCustomStart) {
        const end = new Date();
        const start = new Date();
        start.setDate(start.getDate() - 6);
        state.adsCustomStart = start.toLocaleDateString("en-CA");
        state.adsCustomEnd = end.toLocaleDateString("en-CA");
      }
      state.adsData = null;
      render();
      if (state.adsPeriod !== "custom") loadAdsData();
    });
  });

  const customApply = document.querySelector("[data-ads-custom-apply]");
  if (customApply) {
    customApply.addEventListener("click", () => {
      state.adsCustomStart = document.querySelector("[data-ads-custom-start]")?.value || "";
      state.adsCustomEnd = document.querySelector("[data-ads-custom-end]")?.value || "";
      loadAdsData();
    });
  }

  document.querySelectorAll("[data-analytics-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.analyticsToggle === "sales") {
        state.analyticsShowSales = !state.analyticsShowSales;
      }

      if (button.dataset.analyticsToggle === "royalties") {
        state.analyticsShowRoyalties = !state.analyticsShowRoyalties;
      }

      if (!state.analyticsShowSales && !state.analyticsShowRoyalties) {
        state.analyticsShowSales = true;
      }

      render();
    });
  });

  document.querySelectorAll("[data-ai-suggestion]").forEach((button) => {
    button.addEventListener("click", () => askAssistant(button.dataset.aiSuggestion));
  });

  document.querySelectorAll("[data-audit-question]").forEach((button) => {
    button.addEventListener("click", () => {
      const question = button.dataset.auditQuestion;
      state.aiMode = "ask";
      render();
      askAssistant(question);
    });
  });

  document.querySelectorAll("[data-ai-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      state.aiMode = button.dataset.aiMode;
      state.logError = "";
      render();
      if (state.aiMode === "log" && !state.changeOptions) loadChangeOptions();
    });
  });

  document.querySelectorAll("[data-log-change]").forEach((button) => {
    button.addEventListener("click", () => saveCampaignChange(Number(button.dataset.logChange), button.dataset.logSource || "ai"));
  });

  const changeCampaign = document.querySelector("[data-change-campaign]");
  if (changeCampaign) {
    changeCampaign.addEventListener("change", (event) => {
      state.logCampaign = event.target.value;
    });
  }

  const changeDate = document.querySelector("[data-change-date]");
  if (changeDate) {
    changeDate.addEventListener("change", (event) => {
      state.logDate = event.target.value;
    });
  }

  const changeDescription = document.querySelector("[data-change-description]");
  if (changeDescription) {
    changeDescription.addEventListener("input", (event) => {
      state.logDescription = event.target.value;
    });
  }

  const changePreview = document.querySelector("[data-change-preview]");
  if (changePreview) changePreview.addEventListener("click", previewLoggedChange);

  const salesUploadButton = document.querySelector("[data-sales-upload-button]");
  if (salesUploadButton) {
    salesUploadButton.addEventListener("click", () => {
      const file = document.querySelector("[data-sales-upload]")?.files?.[0];
      if (!file) {
        state.salesUploadError = "Choose a Merch sales CSV or Excel report first.";
        render();
        return;
      }
      uploadSalesReport(file);
    });
  }

  const aiInput = document.querySelector("[data-ai-input]");
  const aiSend = document.querySelector("[data-ai-send]");
  if (aiInput && aiSend) {
    const submitQuestion = () => {
      const question = aiInput.value;
      aiInput.value = "";
      askAssistant(question);
    };
    aiSend.addEventListener("click", submitQuestion);
    aiInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submitQuestion();
      }
    });
  }
}

render();
loadHomeData();
loadRoyaltyTier();
loadAdsData();
loadCampaignsData();
loadAnalyticsData();
loadDailyAudit();
loadChangeOptions();
