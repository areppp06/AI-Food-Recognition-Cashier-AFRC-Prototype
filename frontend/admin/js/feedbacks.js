async function loadFeedbacks() {
  try {
    const res = await fetch("/api/admin/feedbacks");
    if (!res.ok) return;
    const data = await res.json();
    const feedbacks = data.feedbacks || [];
    const summary = data.summary || {};

    const avgEl = document.getElementById("fbAvgRating");
    const totalEl = document.getElementById("fbTotalCount");
    const posEl = document.getElementById("fbPositiveCount");
    const impEl = document.getElementById("fbImproveCount");

    const avg = Number(summary.avg_rating || 0);
    let ratingColor = "#94a3b8";
    if ((summary.total || 0) > 0) {
      if (avg >= 4.0) {
        ratingColor = "#10b981"; // 4-5 green
      } else if (avg >= 3.0) {
        ratingColor = "#eab308"; // 3 yellow
      } else {
        ratingColor = "#ef4444"; // 1-2 red
      }
    }

    if (avgEl) {
      avgEl.style.color = ratingColor;
      avgEl.innerHTML = `${avg.toFixed(1)} <span style="font-size:1.1rem; color:${ratingColor};">★</span>`;
    }
    if (totalEl) totalEl.textContent = `${summary.total || 0} total reviews`;
    if (posEl) posEl.textContent = summary.positive_count || 0;
    if (impEl) impEl.textContent = summary.improvement_count || 0;

    const tbody = document.getElementById("feedbacksBody");
    const mobileList = document.getElementById("feedbacksMobileList");

    if (!feedbacks.length) {
      if (tbody) tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color:#94a3b8; padding:2.5rem;">No customer feedbacks submitted yet.</td></tr>';
      if (mobileList) mobileList.innerHTML = '<div style="text-align:center; color:#94a3b8; padding:2rem;">No customer feedbacks submitted yet.</div>';
      return;
    }

    if (tbody) {
      tbody.innerHTML = feedbacks.map(fb => {
        let starsStr = "";
        for (let i = 1; i <= 5; i++) {
          starsStr += i <= fb.rating ? "★" : "☆";
        }
        const tagsHtml = fb.prefix_comment
          ? fb.prefix_comment.split(",").map(t => `<span class="fb-badge-pill">${esc(t.trim())}</span>`).join("")
          : '<span style="color:#94a3b8; font-style:italic;">—</span>';

        const photoHtml = fb.has_image && fb.image_url
          ? `<img src="${fb.image_url}" alt="Plate" class="feedback-photo-thumb" onclick="viewPhoto('${fb.image_url}')" title="Click to enlarge" loading="lazy">`
          : '<span style="font-size:0.75rem; color:#94a3b8; font-style:italic;">No photo</span>';

        return `
          <tr>
            <td style="font-size:0.82rem; color:#64748b; white-space:nowrap;">${esc(fb.created_at_local)}</td>
            <td><span class="badge" style="background:#f1f5f9; color:#0f172a; font-weight:700;">#${esc(fb.bill_number)}</span></td>
            <td style="font-size:0.84rem; font-weight:600; color:#1e293b;">${esc(fb.phone || "—")}</td>
            <td style="text-align:center;">${photoHtml}</td>
            <td><span class="feedback-stars-gold">${starsStr}</span> <span style="font-size:0.78rem; font-weight:700; color:#64748b;">(${fb.rating}.0)</span></td>
            <td>${tagsHtml}</td>
            <td style="font-size:0.84rem; color:#334155; max-width:260px; word-break:break-word; overflow-wrap:anywhere;">${fb.comment ? `“${esc(fb.comment)}”` : '<span style="color:#94a3b8; font-style:italic;">—</span>'}</td>
          </tr>
        `;
      }).join("");
    }

    if (mobileList) {
      mobileList.innerHTML = feedbacks.map(fb => {
        let starsStr = "";
        for (let i = 1; i <= 5; i++) {
          starsStr += i <= fb.rating ? "★" : "☆";
        }
        const tagsHtml = fb.prefix_comment
          ? fb.prefix_comment.split(",").map(t => `<span class="fb-badge-pill">${esc(t.trim())}</span>`).join("")
          : "";
        const photoHtml = fb.has_image && fb.image_url
          ? `<img src="${fb.image_url}" alt="Plate" class="feedback-photo-thumb" onclick="viewPhoto('${fb.image_url}')" title="Click to enlarge" loading="lazy">`
          : "";

        return `
          <div class="fb-mobile-card">
            <div style="display:flex; justify-content:space-between; align-items:flex-start;">
              <div>
                <span class="badge" style="background:#f1f5f9; color:#0f172a; font-weight:700;">#${esc(fb.bill_number)}</span>
                <span style="font-size:0.78rem; color:#64748b; margin-left:0.4rem;">${esc(fb.created_at_local)}</span>
              </div>
              ${photoHtml}
            </div>
            <div style="display:flex; align-items:center; justify-content:space-between;">
              <span class="feedback-stars-gold">${starsStr} (${fb.rating}.0)</span>
              <span style="font-size:0.8rem; font-weight:600; color:#334155;">${esc(fb.phone || "—")}</span>
            </div>
            ${tagsHtml ? `<div>${tagsHtml}</div>` : ""}
            ${fb.comment ? `<div class="fb-comment-bubble">“${esc(fb.comment)}”</div>` : ""}
          </div>
        `;
      }).join("");
    }
  } catch (err) {
    console.error("Failed to load feedbacks:", err);
  }
}

