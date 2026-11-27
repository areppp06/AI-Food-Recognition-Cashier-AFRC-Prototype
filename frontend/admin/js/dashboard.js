let latestSummary = null;
let latestAnalytics = null;
let analyticsPeriod = "7d";
let analyticsChannel = "all";
let analyticsMonth = "";
let analyticsQuarter = "";
let analyticsReqSeq = 0;
let resizeChartTimer = null;
let billsTableDirty = true;
const salesSeriesVisible = { void: true, paid: true, revenue: true, scans: true };

function toggleSalesSeries(key) {
  if (!(key in salesSeriesVisible)) return;
  salesSeriesVisible[key] = !salesSeriesVisible[key];
  const btn = document.querySelector(`.svg-legend-toggle[data-series="${key}"]`);
  if (btn) {
    const isOn = salesSeriesVisible[key];
    btn.classList.toggle("hidden-series", !isOn);
    btn.setAttribute("aria-pressed", isOn ? "true" : "false");
  }
  if (latestAnalytics) {
    renderSalesSvgChart(latestAnalytics.series || []);
  }
}

function applySummaryCards(sum) {
  document.getElementById("statTodayRev").textContent = "RM " + Number(sum.today_revenue || 0).toFixed(2);
  let todaySub = `${sum.today_paid_count || 0} paid bills today`;
  if ((sum.channel || "all") === "all" && sum.channel_breakdown) {
    const aRev = Number(sum.channel_breakdown.afrc_today_revenue || 0).toFixed(2);
    const sRev = Number(sum.channel_breakdown.selfservice_today_revenue || 0).toFixed(2);
    todaySub += ` · AFRC RM ${aRev} · SC RM ${sRev}`;
  }
  document.getElementById("statTodayPaidSub").textContent = todaySub;

  document.getElementById("statWeeklyRev").textContent = "RM " + Number(sum.weekly_revenue || 0).toFixed(2);
  document.getElementById("statWeeklyPaidSub").textContent = `Last 7 days · ${sum.weekly_paid_count || 0} paid bills`;

  document.getElementById("statTodayBills").textContent = String(sum.today_bills || 0);
  document.getElementById("statTodayBillsSub").textContent = `Logged today · ${sum.total_bills || 0} all-time`;

  const todayVoid = sum.today_void_count || 0;
  const todayPending = sum.today_pending_count || 0;
  document.getElementById("statAttention").textContent = String(todayVoid);
  document.getElementById("statAttentionSub").textContent = `${todayVoid} voided today · ${todayPending} pending today`;
}

function syncPeriodSelectors(analytics) {
  const monthSel = document.getElementById("monthSelect");
  const quarterSel = document.getElementById("quarterSelect");
  if (analytics && Array.isArray(analytics.available_months)) {
    const curVal = analyticsMonth || "";
    monthSel.innerHTML = `<option value="">Last 12 Months (All)</option>` +
      analytics.available_months.map(m => `<option value="${esc(m.value)}">${esc(m.label)}</option>`).join("");
    monthSel.value = curVal;
  }
  if (analytics && Array.isArray(analytics.available_quarters)) {
    const curVal = analyticsQuarter || "";
    quarterSel.innerHTML = `<option value="">Last 4 Quarters (All)</option>` +
      analytics.available_quarters.map(q => `<option value="${esc(q.value)}">${esc(q.label)}</option>`).join("");
    quarterSel.value = curVal;
  }
  monthSel.style.display = analyticsPeriod === "monthly" ? "inline-block" : "none";
  quarterSel.style.display = analyticsPeriod === "quarterly" ? "inline-block" : "none";
}

async function fetchAnalyticsUpdate() {
  const mySeq = ++analyticsReqSeq;
  const params = new URLSearchParams({
    period: analyticsPeriod,
    channel: analyticsChannel,
    month: analyticsPeriod === "monthly" ? analyticsMonth : "",
    quarter: analyticsPeriod === "quarterly" ? analyticsQuarter : "",
    _t: String(Date.now()),
  });
  const res = await fetch("/api/admin/analytics?" + params.toString(), { cache: "no-store" });
  if (!res.ok) {
    showToast("Failed to load chart data.");
    return;
  }
  const data = await res.json();
  if (mySeq !== analyticsReqSeq) return;
  if (data.summary) {
    latestSummary = data.summary;
    applySummaryCards(data.summary);
  }
  if (data.analytics) {
    latestAnalytics = data.analytics;
    renderAnalytics(data.analytics);
  }
}

async function setAnalyticsChannel(ch, btn) {
  analyticsChannel = String(ch || "all");
  document.querySelectorAll("#channelFilterPills .range-pill").forEach(b => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  await fetchAnalyticsUpdate();
}

async function setAnalyticsRange(period, btn) {
  analyticsPeriod = String(period || "7d");
  if (analyticsPeriod !== "monthly") analyticsMonth = "";
  if (analyticsPeriod !== "quarterly") analyticsQuarter = "";
  document.querySelectorAll(".overview-toolbar .range-pill").forEach(b => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  syncPeriodSelectors(latestAnalytics);
  await fetchAnalyticsUpdate();
}

async function onMonthSelectChange() {
  analyticsMonth = document.getElementById("monthSelect").value || "";
  analyticsPeriod = "monthly";
  await fetchAnalyticsUpdate();
}

async function onQuarterSelectChange() {
  analyticsQuarter = document.getElementById("quarterSelect").value || "";
  analyticsPeriod = "quarterly";
  await fetchAnalyticsUpdate();
}

function niceMoneyMax(rawMax, steps = 5) {
  if (!rawMax || rawMax <= 0) return steps * 10;
  const rough = rawMax / steps;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const norm = rough / mag;
  const s = norm <= 1 ? 1 : norm <= 1.5 ? 1.5 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return s * mag * steps;
}

function niceIntMax(rawMax, steps = 5) {
  if (!rawMax || rawMax <= 0) return steps;
  const rawStep = Math.ceil(rawMax / steps);
  if (rawStep <= 5) return rawStep * steps;
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const norm = rawStep / mag;
  const s = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return Math.ceil(s * mag) * steps;
}

function renderSalesSvgChart(series) {
  const stage = document.getElementById("salesChartStage");
  const svg = document.getElementById("salesChartSvg");
  const tip = document.getElementById("salesChartTooltip");
  if (!stage || !svg || !tip) return;
  tip.style.display = "none";

  const items = Array.isArray(series) ? series : [];
  const showRev = Boolean(salesSeriesVisible.revenue);
  const showPaid = Boolean(salesSeriesVisible.paid);
  const showVoid = Boolean(salesSeriesVisible.void);
  const showScans = Boolean(salesSeriesVisible.scans);

  if (!items.length) {
    svg.innerHTML = `<text x="50%" y="50%" text-anchor="middle" fill="#94a3b8" font-size="13" font-family="Plus Jakarta Sans, sans-serif">No sales activity in this period.</text>`;
    svg.onmousemove = null;
    svg.onmouseleave = null;
    return;
  }

  if (!showRev && !showPaid && !showVoid && !showScans) {
    svg.innerHTML = `<text x="50%" y="50%" text-anchor="middle" fill="#94a3b8" font-size="13" font-family="Plus Jakarta Sans, sans-serif">All series hidden — click a legend button above to show.</text>`;
    svg.onmousemove = null;
    svg.onmouseleave = null;
    return;
  }

  const W = Math.max(480, stage.clientWidth || 720);
  const H = 275;
  const padL = 64;
  const padR = 52;
  const padT = 12;
  const padB = 34;
  const plotW = Math.max(120, W - padL - padR);
  const plotH = Math.max(100, H - padT - padB);
  const baseY = padT + plotH;

  const maxRev = showRev ? niceMoneyMax(Math.max(...items.map(s => Number(s.revenue || 0)), 0), 5) : 50;
  const maxCntRaw = Math.max(
    ...items.map(s => Math.max(
      showPaid ? Number(s.paid_count || 0) : 0,
      showVoid ? Number(s.void_count || 0) : 0,
      showScans ? Number(s.scan_count || 0) : 0
    )),
    0
  );
  const hasCountAxis = showPaid || showVoid || showScans;
  const maxCnt = hasCountAxis ? niceIntMax(maxCntRaw, 5) : 5;

  const steps = 5;
  let gridSvg = "";
  for (let i = 0; i <= steps; i++) {
    const y = padT + plotH - (i / steps) * plotH;
    const revTick = Math.round((maxRev * i) / steps);
    const cntTick = Math.round((maxCnt * i) / steps);
    gridSvg += `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${padL + plotW}" y2="${y.toFixed(1)}" stroke="${i === 0 ? "#cbd5e1" : "#f1f5f9"}" stroke-width="1"/>`;
    if (showRev) {
      gridSvg += `<text x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#64748b" font-size="11" font-family="Plus Jakarta Sans, sans-serif">RM ${revTick}</text>`;
    }
    if (hasCountAxis) {
      gridSvg += `<text x="${padL + plotW + 8}" y="${(y + 4).toFixed(1)}" text-anchor="start" fill="#64748b" font-size="11" font-family="Plus Jakarta Sans, sans-serif">${cntTick}</text>`;
    }
  }

  if (showRev) {
    gridSvg += `<text transform="rotate(-90 14 ${padT + plotH / 2})" x="14" y="${padT + plotH / 2}" text-anchor="middle" fill="#64748b" font-size="11" font-weight="600" font-family="Plus Jakarta Sans, sans-serif">Revenue (RM)</text>`;
  }
  if (hasCountAxis) {
    gridSvg += `<text transform="rotate(90 ${W - 12} ${padT + plotH / 2})" x="${W - 12}" y="${padT + plotH / 2}" text-anchor="middle" fill="#64748b" font-size="11" font-weight="600" font-family="Plus Jakarta Sans, sans-serif">Bills & Scans</text>`;
  }

  const n = items.length;
  const slotW = plotW / n;
  const barW = Math.max(4, Math.min(48, slotW * 0.62));
  const labelStride = Math.max(1, Math.ceil(n / Math.max(4, Math.floor(plotW / 68))));

  let barsSvg = "";
  let xLabelsSvg = "";
  const paidPts = [];
  const voidPts = [];
  const scanPts = [];
  const cxList = [];

  items.forEach((s, idx) => {
    const cx = padL + idx * slotW + slotW / 2;
    cxList.push(cx);
    const rev = Number(s.revenue || 0);
    const paid = Number(s.paid_count || 0);
    const voidCnt = Number(s.void_count || 0);
    const scans = Number(s.scan_count || 0);

    if (showRev) {
      const barH = maxRev > 0 ? Math.max(0, (rev / maxRev) * plotH) : 0;
      const bx = cx - barW / 2;
      const by = baseY - barH;
      if (barH > 0.5) {
        const r = Math.min(5, barW / 2, barH);
        const pathD = `M ${bx.toFixed(1)},${(by + r).toFixed(1)} Q ${bx.toFixed(1)},${by.toFixed(1)} ${(bx + r).toFixed(1)},${by.toFixed(1)} L ${(bx + barW - r).toFixed(1)},${by.toFixed(1)} Q ${(bx + barW).toFixed(1)},${by.toFixed(1)} ${(bx + barW).toFixed(1)},${(by + r).toFixed(1)} L ${(bx + barW).toFixed(1)},${baseY.toFixed(1)} L ${bx.toFixed(1)},${baseY.toFixed(1)} Z`;
        barsSvg += `<path d="${pathD}" fill="#facc15" stroke="#eab308" stroke-width="1"/>`;
      }
    }

    const py = baseY - (paid / maxCnt) * plotH;
    const vy = baseY - (voidCnt / maxCnt) * plotH;
    const sy = baseY - (scans / maxCnt) * plotH;
    paidPts.push({ x: cx, y: py });
    voidPts.push({ x: cx, y: vy });
    scanPts.push({ x: cx, y: sy });

    if (idx % labelStride === 0) {
      const xLbl = s.label.length > 7 && s.label.includes(" ") ? s.label.split(" ").slice(0, 2).join(" ") : s.label;
      xLabelsSvg += `<text x="${cx.toFixed(1)}" y="${H - 10}" text-anchor="middle" fill="#64748b" font-size="11" font-family="Plus Jakarta Sans, sans-serif">${esc(xLbl)}</text>`;
    }
  });

  const paidLinePts = paidPts.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const voidLinePts = voidPts.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const scanLinePts = scanPts.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");

  let linesSvg = "";
  if (showScans) {
    linesSvg += `<polyline fill="none" stroke="#3b82f6" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" points="${scanLinePts}"/>`;
    scanPts.forEach(p => {
      linesSvg += `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3.2" fill="#3b82f6" stroke="#ffffff" stroke-width="1"/>`;
    });
  }
  if (showPaid) {
    linesSvg += `<polyline fill="none" stroke="#10b981" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" points="${paidLinePts}"/>`;
    paidPts.forEach(p => {
      linesSvg += `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3.2" fill="#10b981" stroke="#ffffff" stroke-width="1"/>`;
    });
  }
  if (showVoid) {
    linesSvg += `<polyline fill="none" stroke="#ef4444" stroke-width="2" stroke-dasharray="5 4" stroke-linejoin="round" stroke-linecap="round" points="${voidLinePts}"/>`;
    voidPts.forEach(p => {
      linesSvg += `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="2.8" fill="#ef4444" stroke="#ffffff" stroke-width="1"/>`;
    });
  }

  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = `
    <g>${gridSvg}</g>
    <rect id="svgHoverBand" x="0" y="${padT}" width="${slotW.toFixed(1)}" height="${plotH}" fill="rgba(15, 23, 42, 0.05)" rx="4" opacity="0" style="pointer-events:none;"/>
    <g>${barsSvg}</g>
    <g>${linesSvg}</g>
    <g>${xLabelsSvg}</g>
    <circle id="svgHoverScanDot" cx="0" cy="0" r="5" fill="#3b82f6" stroke="#ffffff" stroke-width="2" opacity="0" style="pointer-events:none;"/>
    <circle id="svgHoverPaidDot" cx="0" cy="0" r="5" fill="#10b981" stroke="#ffffff" stroke-width="2" opacity="0" style="pointer-events:none;"/>
    <circle id="svgHoverVoidDot" cx="0" cy="0" r="4.5" fill="#ef4444" stroke="#ffffff" stroke-width="2" opacity="0" style="pointer-events:none;"/>
    <rect x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" fill="transparent" style="cursor:crosshair;"/>
  `;

  const band = document.getElementById("svgHoverBand");
  const sDot = document.getElementById("svgHoverScanDot");
  const pDot = document.getElementById("svgHoverPaidDot");
  const vDot = document.getElementById("svgHoverVoidDot");

  const showAtIndex = (idx, clientY) => {
    const item = items[idx];
    if (!item) return;
    band.setAttribute("x", (padL + idx * slotW).toFixed(1));
    band.setAttribute("opacity", "1");
    if (showScans) {
      sDot.setAttribute("cx", scanPts[idx].x.toFixed(1));
      sDot.setAttribute("cy", scanPts[idx].y.toFixed(1));
      sDot.setAttribute("opacity", "1");
    } else {
      sDot.setAttribute("opacity", "0");
    }
    if (showPaid) {
      pDot.setAttribute("cx", paidPts[idx].x.toFixed(1));
      pDot.setAttribute("cy", paidPts[idx].y.toFixed(1));
      pDot.setAttribute("opacity", "1");
    } else {
      pDot.setAttribute("opacity", "0");
    }
    if (showVoid) {
      vDot.setAttribute("cx", voidPts[idx].x.toFixed(1));
      vDot.setAttribute("cy", voidPts[idx].y.toFixed(1));
      vDot.setAttribute("opacity", "1");
    } else {
      vDot.setAttribute("opacity", "0");
    }

    let tipRows = "";
    if (showRev) {
      tipRows += `
        <div class="chart-tooltip-row">
          <span class="chart-tooltip-lbl"><span class="chart-tooltip-dot" style="background:#facc15;border:1px solid #eab308;"></span>Paid Revenue (RM)</span>
          <strong>RM ${Number(item.revenue || 0).toFixed(2)}</strong>
        </div>`;
    }
    if (showPaid) {
      tipRows += `
        <div class="chart-tooltip-row">
          <span class="chart-tooltip-lbl"><span class="chart-tooltip-dot" style="background:#10b981;"></span>Paid Bills</span>
          <strong>${Number(item.paid_count || 0)}</strong>
        </div>`;
    }
    if (showVoid) {
      tipRows += `
        <div class="chart-tooltip-row">
          <span class="chart-tooltip-lbl"><span class="chart-tooltip-dot" style="background:#ef4444;"></span>Voided Bills</span>
          <strong>${Number(item.void_count || 0)}</strong>
        </div>`;
    }
    if (showScans) {
      tipRows += `
        <div class="chart-tooltip-row">
          <span class="chart-tooltip-lbl"><span class="chart-tooltip-dot" style="background:#3b82f6;"></span>Scanned Images</span>
          <strong>${Number(item.scan_count || 0)}</strong>
        </div>`;
    }
    tip.innerHTML = `<div class="chart-tooltip-title">${esc(item.label)}</div>${tipRows}`;
    tip.style.display = "block";
    const stageRect = stage.getBoundingClientRect();
    const relX = (cxList[idx] / W) * stageRect.width;
    const tipW = tip.offsetWidth || 185;
    const leftPx = relX + 14 + tipW > stageRect.width
      ? Math.max(4, relX - tipW - 14)
      : Math.max(4, relX + 14);
    const relY = clientY ? Math.max(6, Math.min(stageRect.height - 95, clientY - stageRect.top - 40)) : 16;
    tip.style.left = `${Math.round(leftPx)}px`;
    tip.style.top = `${Math.round(relY)}px`;
  };

  svg.onmousemove = (ev) => {
    const rect = svg.getBoundingClientRect();
    const scaleX = W / (rect.width || W);
    const svgX = (ev.clientX - rect.left) * scaleX;
    if (svgX < padL || svgX > padL + plotW) {
      band.setAttribute("opacity", "0");
      sDot.setAttribute("opacity", "0");
      pDot.setAttribute("opacity", "0");
      vDot.setAttribute("opacity", "0");
      tip.style.display = "none";
      return;
    }
    const idx = Math.max(0, Math.min(n - 1, Math.floor((svgX - padL) / slotW)));
    showAtIndex(idx, ev.clientY);
  };

  svg.onmouseleave = () => {
    band.setAttribute("opacity", "0");
    sDot.setAttribute("opacity", "0");
    pDot.setAttribute("opacity", "0");
    vDot.setAttribute("opacity", "0");
    tip.style.display = "none";
  };
}

function buildSvgDonut({ total, centerText, centerSub, slices, size = 160, radius = 56, strokeWidth = 22, ariaLabel = "Donut Chart", interactivePrefix = "" }) {
  const half = size / 2;
  const circ = 2 * Math.PI * radius;
  let arcs = "";
  if (!total || total <= 0) {
    arcs = `<circle cx="${half}" cy="${half}" r="${radius}" fill="none" stroke="#e2e8f0" stroke-width="${strokeWidth}"/>`;
  } else {
    let offset = 0;
    slices.forEach((part, idx) => {
      if (!part.val || part.val <= 0) return;
      const len = (part.val / total) * circ;
      const evAttrs = interactivePrefix
        ? ` data-void-slice="${idx}" style="cursor:pointer; transition:opacity 0.12s ease;" onmouseenter="highlightVoidCat(${idx})" onmouseleave="clearVoidCatHighlight()"`
        : "";
      const tipTitle = part.label ? `<title>${esc(part.label)}: ${esc(part.val)}</title>` : "";
      arcs += `<circle cx="${half}" cy="${half}" r="${radius}" fill="none" stroke="${part.color}" stroke-width="${strokeWidth}" stroke-dasharray="${len.toFixed(2)} ${(circ - len).toFixed(2)}" stroke-dashoffset="${(-offset).toFixed(2)}" transform="rotate(-90 ${half} ${half})"${evAttrs}>${tipTitle}</circle>`;
      offset += len;
    });
  }
  return `
    <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" aria-label="${esc(ariaLabel)}">
      ${arcs}
      <text x="${half}" y="${half - 4}" text-anchor="middle" fill="#0f172a" font-size="22" font-weight="800" font-family="Plus Jakarta Sans, sans-serif">${esc(centerText)}</text>
      <text x="${half}" y="${half + 15}" text-anchor="middle" fill="#64748b" font-size="11" font-weight="600" font-family="Plus Jakarta Sans, sans-serif">${esc(centerSub)}</text>
    </svg>
  `;
}

function voidCategoryColor(reason, idx) {
  const r = String(reason || "").toLowerCase();
  if (r.includes("timed out") || r.includes("timeout")) return "#ef4444";
  if (r.includes("cashier")) return "#f59e0b";
  if (r.includes("cancel")) return "#8b5cf6";
  if (r.includes("retri") || r.includes("retry") || r.includes("limit")) return "#ec4899";
  const fallback = ["#0ea5e9", "#6366f1", "#14b8a6", "#64748b"];
  return fallback[idx % fallback.length];
}

function highlightVoidCat(idx) {
  document.querySelectorAll("[data-void-slice]").forEach(el => {
    el.style.opacity = Number(el.dataset.voidSlice) === idx ? "1" : "0.35";
  });
  document.querySelectorAll("[data-void-row]").forEach(el => {
    el.classList.toggle("is-highlighted", Number(el.dataset.voidRow) === idx);
  });
}

function clearVoidCatHighlight() {
  document.querySelectorAll("[data-void-slice]").forEach(el => {
    el.style.opacity = "1";
  });
  document.querySelectorAll("[data-void-row]").forEach(el => {
    el.classList.remove("is-highlighted");
  });
}

function renderOutcomeSvgChart(pt) {
  const host = document.getElementById("outcomeChartHost");
  if (!host) return;
  const paidV = Number(pt.paid_count || 0);
  const voidV = Number(pt.void_count || 0);
  const openV = Number(pt.open_count || 0);
  const total = paidV + voidV + openV;
  const donutSvg = buildSvgDonut({
    total,
    centerText: String(total),
    centerSub: "bills",
    ariaLabel: "Bill Outcomes Donut",
    slices: [
      { label: "Paid", val: paidV, color: "#10b981" },
      { label: "Voided", val: voidV, color: "#ef4444" },
      { label: "Pending", val: openV, color: "#f59e0b" },
    ],
  });

  host.innerHTML = `
    ${donutSvg}
    <div class="outcome-legend">
      <span class="outcome-legend-item"><span class="swatch-line green"></span>Paid (${paidV})</span>
      <span class="outcome-legend-item"><span class="swatch-line" style="background:#ef4444;"></span>Voided (${voidV})</span>
      <span class="outcome-legend-item"><span class="swatch-line" style="background:#f59e0b;"></span>Pending (${openV})</span>
    </div>
  `;
}

window.addEventListener("resize", () => {
  clearTimeout(resizeChartTimer);
  resizeChartTimer = setTimeout(() => {
    if (latestAnalytics) renderSalesSvgChart(latestAnalytics.series || []);
  }, 120);
});

function renderAnalytics(analytics) {
  if (!analytics) return;
  syncPeriodSelectors(analytics);
  const pt = analytics.period_totals || {};
  document.getElementById("salesChartSubtitle").textContent =
    `${analytics.period_label} — Paid revenue (RM) vs bills & scanned images`;
  document.getElementById("periodRevenueBadge").textContent =
    `Period Revenue: RM ${Number(pt.revenue || 0).toFixed(2)} · ${pt.scan_count || 0} Images Scanned`;
  document.getElementById("outcomeChartSub").textContent =
    `${pt.total_bills || 0} bills in ${analytics.period_label.toLowerCase()}`;

  const series = analytics.series || [];
  renderSalesSvgChart(series);
  renderOutcomeSvgChart(pt);

  // Top Dishes
  const topEl = document.getElementById("topDishesList");
  const topDishes = analytics.top_dishes || [];
  if (!topDishes.length) {
    topEl.innerHTML = '<div class="empty-state-sm">No paid dish sales in this period yet.</div>';
  } else {
    const sortedDishes = [...topDishes].sort((a, b) =>
      (Number(b.quantity || 0) - Number(a.quantity || 0)) ||
      (Number(b.revenue || 0) - Number(a.revenue || 0))
    );
    const maxQty = Math.max(...sortedDishes.map(d => Number(d.quantity || 0)), 1);
    topEl.innerHTML = `<div style="font-size:0.75rem;color:var(--muted);margin-bottom:0.65rem;">Note: Orange bars show quantities sold.</div><div class="breakdown-list">${sortedDishes.slice(0, 6).map(d => {
      const qty = Number(d.quantity || 0);
      const pct = qty > 0 ? Math.max(6, Math.round((qty / maxQty) * 100)) : 0;
      return `
      <div>
        <div class="breakdown-item-head">
          <span class="breakdown-name">${esc(d.name)}</span>
          <span class="breakdown-meta">${esc(d.quantity)} sold · RM ${Number(d.revenue).toFixed(2)}</span>
        </div>
        <div class="breakdown-track">
          <div class="breakdown-fill" style="width:${pct}%"></div>
        </div>
      </div>
    `;
    }).join("")}</div>`;
  }

  // Void Causes
  const voidEl = document.getElementById("voidCausesList");
  const voidCauses = analytics.void_causes || [];
  if (!voidCauses.length) {
    voidEl.innerHTML = '<div class="empty-state-sm">No voided bills in this period.</div>';
  } else {
    const totalVoidCnt = voidCauses.reduce((sum, v) => sum + Number(v.count || 0), 0);
    const totalVoidVal = voidCauses.reduce((sum, v) => sum + Number(v.lost_value || 0), 0);
    const slices = voidCauses.map((v, idx) => ({
      label: v.reason,
      val: Number(v.count || 0),
      color: voidCategoryColor(v.reason, idx),
    }));
    const voidDonutSvg = buildSvgDonut({
      total: totalVoidCnt,
      centerText: String(totalVoidCnt),
      centerSub: "voided",
      size: 152,
      radius: 52,
      strokeWidth: 22,
      ariaLabel: "Void Causes Donut Chart",
      slices,
      interactivePrefix: "void",
    });

    const listHtml = voidCauses.map((v, idx) => {
      const cnt = Number(v.count || 0);
      const pct = totalVoidCnt > 0 ? Math.round((cnt / totalVoidCnt) * 100) : 0;
      const color = slices[idx].color;
      return `
        <div class="void-cat-row" data-void-row="${idx}" onmouseenter="highlightVoidCat(${idx})" onmouseleave="clearVoidCatHighlight()">
          <div class="breakdown-item-head">
            <span class="breakdown-name" style="display:inline-flex;align-items:center;"><span class="void-cat-dot" style="background:${color};"></span>${esc(v.reason)}</span>
            <span class="breakdown-meta">${pct}% · ${esc(cnt)} · RM ${Number(v.lost_value || 0).toFixed(2)}</span>
          </div>
          <div class="breakdown-track">
            <div class="breakdown-fill" style="width:${Math.max(6, pct)}%; background:${color};"></div>
          </div>
        </div>
      `;
    }).join("");

    voidEl.innerHTML = `
      <div class="void-visual-wrap">
        <div class="void-donut-box">
          ${voidDonutSvg}
          <div class="void-donut-sub">RM ${totalVoidVal.toFixed(2)} voided</div>
        </div>
        <div class="breakdown-list">${listHtml}</div>
      </div>
    `;
  }
}

function exportOverviewCsv() {
  const params = new URLSearchParams({
    period: analyticsPeriod,
    month: analyticsPeriod === "monthly" ? analyticsMonth : "",
    quarter: analyticsPeriod === "quarterly" ? analyticsQuarter : "",
  });
  window.location.href = "/api/admin/export?" + params.toString();
}

function exportOverviewPdf() {
  if (!window.jspdf || !latestSummary || !latestAnalytics) {
    showToast("Dashboard data is still loading.");
    return;
  }
  const { jsPDF } = window.jspdf;
  const doc = new jsPDF({ unit: "mm", format: "a4" });
  const sum = latestSummary;
  const an = latestAnalytics;
  const pt = an.period_totals || {};

  const restName = (an.restaurant_name || (latestSummary && latestSummary.restaurant_name) || "AFRC").trim();
  const compName = (an.company_name || (latestSummary && latestSummary.company_name) || "").trim();
  const domainName = (window.location.hostname || "e-cashier").trim();

  let y = 16;
  const checkPage = (needed = 12) => {
    if (y + needed > 282) {
      doc.addPage();
      y = 16;
    }
  };

  doc.setFillColor(255, 94, 54);
  doc.rect(0, 0, 210, 3.5, "F");

  // Restaurant Name
  doc.setFont("helvetica", "bold");
  doc.setFontSize(17);
  doc.setTextColor(15, 23, 42);
  doc.text(restName, 14, y);
  y += 6.5;

  // Company Name
  if (compName) {
    doc.setFont("helvetica", "normal");
    doc.setFontSize(10);
    doc.setTextColor(71, 85, 105);
    doc.text(compName, 14, y);
    y += 7;
  } else {
    y += 2;
  }

  // [domainname] comprehensive data reports
  doc.setFont("helvetica", "bold");
  doc.setFontSize(11);
  doc.setTextColor(15, 23, 42);
  doc.text(`${domainName} comprehensive data reports`, 14, y);
  y += 6;

  // Period & Generation timestamp
  doc.setFont("helvetica", "normal");
  doc.setFontSize(9);
  doc.setTextColor(100, 116, 139);
  doc.text(`Period: ${an.period_label}   |   Generated: ${an.generated_at_local}`, 14, y);
  y += 9;

  // Period Performance Summary Box (Replaces daily/weekly KPI grid)
  doc.setDrawColor(226, 232, 240);
  doc.setFillColor(248, 250, 252);
  doc.roundedRect(14, y, 182, 24, 2, 2, "FD");

  doc.setFont("helvetica", "bold");
  doc.setFontSize(10);
  doc.setTextColor(15, 23, 42);
  doc.text(`Period Performance Summary (${an.period_label})`, 18, y + 7.5);

  doc.setFont("helvetica", "normal");
  doc.setFontSize(9);
  doc.setTextColor(51, 65, 85);
  doc.text(
    `Paid Revenue: RM ${Number(pt.revenue || 0).toFixed(2)}   |   Paid: ${pt.paid_count || 0}   |   Voided: ${pt.void_count || 0}   |   Scanned: ${pt.scan_count || 0}`,
    18, y + 16.5
  );
  y += 32;

  // Timeline Table
  doc.setFont("helvetica", "bold");
  doc.setFontSize(11);
  doc.setTextColor(15, 23, 42);
  doc.text("Sales Timeline Breakdown", 14, y);
  y += 6;
  doc.setFontSize(8.5);
  doc.setTextColor(100, 116, 139);
  doc.text("Period", 14, y);
  doc.text("Paid Rev (RM)", 68, y, { align: "right" });
  doc.text("Paid", 96, y, { align: "right" });
  doc.text("Pending", 122, y, { align: "right" });
  doc.text("Void", 146, y, { align: "right" });
  doc.text("Total", 170, y, { align: "right" });
  doc.text("Scans", 194, y, { align: "right" });
  y += 2.5;
  doc.line(14, y, 196, y);
  y += 5;

  doc.setFont("helvetica", "normal");
  doc.setTextColor(15, 23, 42);
  (an.series || []).forEach(row => {
    checkPage(6);
    doc.text(String(row.label), 14, y);
    doc.text(Number(row.revenue || 0).toFixed(2), 68, y, { align: "right" });
    doc.text(String(row.paid_count || 0), 96, y, { align: "right" });
    doc.text(String(row.open_count || 0), 122, y, { align: "right" });
    doc.text(String(row.void_count || 0), 146, y, { align: "right" });
    doc.text(String(row.total_count || 0), 170, y, { align: "right" });
    doc.text(String(row.scan_count || 0), 194, y, { align: "right" });
    y += 5.2;
  });
  y += 5;

  // Top Dishes
  checkPage(18);
  doc.setFont("helvetica", "bold");
  doc.setFontSize(11);
  doc.text("Top Selling Dishes (Paid Bills)", 14, y);
  y += 6;
  doc.setFont("helvetica", "normal");
  doc.setFontSize(9);
  if (!(an.top_dishes || []).length) {
    doc.text("No dish sales recorded in this period.", 14, y);
    y += 6;
  } else {
    an.top_dishes.forEach((d, idx) => {
      checkPage(6);
      doc.text(`${idx + 1}. ${d.name}`, 14, y);
      doc.text(`${d.quantity} sold  ·  RM ${Number(d.revenue).toFixed(2)}`, 192, y, { align: "right" });
      y += 5.5;
    });
  }
  y += 5;

  // Void Causes
  checkPage(18);
  doc.setFont("helvetica", "bold");
  doc.setFontSize(11);
  doc.text("Void Causes Breakdown", 14, y);
  y += 6;
  doc.setFont("helvetica", "normal");
  doc.setFontSize(9);
  if (!(an.void_causes || []).length) {
    doc.text("No voided bills recorded in this period.", 14, y);
  } else {
    an.void_causes.forEach(v => {
      checkPage(6);
      doc.text(String(v.reason), 14, y);
      doc.text(`${v.count} bills  ·  RM ${Number(v.lost_value).toFixed(2)}`, 192, y, { align: "right" });
      y += 5.5;
    });
  }

  const filePrefix = (restName || "AFRC").replace(/[^a-zA-Z0-9_-]/g, "_");
  doc.save(`${filePrefix}-Sales-Overview-${an.file_tag || "report"}-${new Date().toISOString().slice(0, 10)}.pdf`);
}
