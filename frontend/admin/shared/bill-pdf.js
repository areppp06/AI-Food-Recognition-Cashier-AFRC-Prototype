/* Shared Bill Receipt PDF Generator and Item Formatter (used by Bills & Archives). */

function formatItemsSummary(items) {
  if (!items || !items.length) return "-";
  return items.map(i => {
    const pcs = i.piece_count ? ` (${i.piece_count} pcs)` : "";
    return `${esc(i.name)} ×${esc(i.quantity)}${pcs}`;
  }).join(", ");
}

function generateBillPdfDoc(billData) {
  if (!billData || !window.jspdf) return null;
  const { jsPDF } = window.jspdf;
  const items = billData.items || [];
  const billNo = billData.bill_number || "AAA-0001";
  const total = Number(billData.total || 0).toFixed(2);
  const statusUpper = String(billData.status || "open").toUpperCase();
  const isVoid = ["VOID", "CANCELLED"].includes(statusUpper);
  const isPending = ["OPEN", "PENDING"].includes(statusUpper);
  const voidCause = billData.void_reason || (isVoid ? "Voided" : "");
  const dt = new Date(billData.paid_at || billData.updated_at || billData.created_at || Date.now());
  const dateStr = dt.toLocaleDateString("en-MY", { year: "numeric", month: "short", day: "2-digit" });
  const timeStr = dt.toLocaleTimeString("en-MY", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  const restName = billData.restaurant_name || "AFRC";
  const compName = billData.company_name || "";
  const compPhone = billData.company_phone || "";
  const compEmail = billData.company_email || "";
  const custPhone = billData.receipt_phone || "";
  const custEmail = billData.receipt_email || "";
  const hasCustomer = Boolean(custPhone || custEmail);
  const customerLines = (hasCustomer ? 1 : 0) + (custPhone ? 1 : 0) + (custEmail ? 1 : 0);

  const topInfoLines = (compName ? 1 : 0) + (compPhone ? 1 : 0) + (compEmail ? 1 : 0);
  const extraVoidHeight = (isVoid && voidCause) ? 5 : 0;
  const itemsHeight = items.length * 8.8;
  const pageHeight = Math.max(120, 98 + topInfoLines * 3.4 + customerLines * 3.6 + extraVoidHeight + itemsHeight);
  const doc = new jsPDF({ unit: "mm", format: [80, pageHeight] });

  let curY = 10;
  doc.setFont("helvetica", "bold");
  doc.setFontSize(11);
  doc.setTextColor(15, 23, 42);
  doc.text(restName, 40, curY, { align: "center" });

  if (compName) {
    curY += 3.6;
    doc.setFont("helvetica", "normal");
    doc.setFontSize(7.0);
    doc.setTextColor(71, 85, 105);
    doc.text(compName, 40, curY, { align: "center" });
  }
  if (compPhone) {
    curY += 3.0;
    doc.setFont("helvetica", "normal");
    doc.setFontSize(6.2);
    doc.setTextColor(100, 116, 139);
    doc.text("Tel: " + compPhone, 40, curY, { align: "center" });
  }
  if (compEmail) {
    curY += 3.0;
    doc.setFont("helvetica", "normal");
    doc.setFontSize(6.2);
    doc.setTextColor(100, 116, 139);
    doc.text(compEmail, 40, curY, { align: "center" });
  }

  curY += 3.8;
  doc.setFont("helvetica", "normal");
  doc.setFontSize(7.2);
  doc.setTextColor(100, 116, 139);
  doc.text(isVoid ? "VOIDED BILL RECEIPT" : (isPending ? "PENDING BILL RECEIPT" : "OFFICIAL PAYMENT RECEIPT"), 40, curY, { align: "center" });

  curY += 3.5;
  doc.setDrawColor(203, 213, 225);
  doc.setLineDashPattern([1, 1], 0);
  doc.line(6, curY, 74, curY);

  doc.setFontSize(8);
  doc.setTextColor(71, 85, 105);
  doc.text("BILL NO:", 6, curY + 5);
  doc.setFont("helvetica", "bold");
  doc.setTextColor(15, 23, 42);
  doc.text("#" + billNo, 74, curY + 5, { align: "right" });

  doc.setFont("helvetica", "normal");
  doc.setTextColor(71, 85, 105);
  doc.text("DATE:", 6, curY + 9.5);
  doc.setTextColor(15, 23, 42);
  doc.text(dateStr + " " + timeStr, 74, curY + 9.5, { align: "right" });

  doc.setTextColor(71, 85, 105);
  doc.text("STATUS:", 6, curY + 14);
  doc.setFont("helvetica", "bold");
  if (isVoid) {
    doc.setTextColor(185, 28, 28);
    doc.text("VOID", 74, curY + 14, { align: "right" });
  } else if (isPending) {
    doc.setTextColor(217, 119, 6);
    doc.text("PENDING", 74, curY + 14, { align: "right" });
  } else {
    doc.setTextColor(statusUpper === "PAID" ? 16 : 15, statusUpper === "PAID" ? 185 : 23, statusUpper === "PAID" ? 129 : 42);
    doc.text(statusUpper, 74, curY + 14, { align: "right" });
  }

  curY += 14;
  if (hasCustomer) {
    curY += 4.5;
    doc.setFont("helvetica", "bold");
    doc.setFontSize(7.0);
    doc.setTextColor(71, 85, 105);
    doc.text("Customer's Details:", 6, curY);

    doc.setFont("helvetica", "normal");
    doc.setFontSize(6.5);
    doc.setTextColor(51, 65, 85);
    if (custPhone) {
      curY += 3.4;
      doc.text(custPhone, 6, curY);
    }
    if (custEmail) {
      curY += 3.4;
      doc.text(custEmail, 6, curY);
    }
  }
  if (isVoid && voidCause) {
    curY += 4.5;
    doc.setFont("helvetica", "normal");
    doc.setTextColor(71, 85, 105);
    doc.text("VOID CAUSE:", 6, curY);
    doc.setFont("helvetica", "bold");
    doc.setTextColor(185, 28, 28);
    doc.text(String(voidCause).slice(0, 30), 74, curY, { align: "right" });
  }

  curY += 4.5;
  doc.line(6, curY, 74, curY);
  doc.setFontSize(7.5);
  doc.setTextColor(100, 116, 139);
  doc.text("ITEM / QTY", 6, curY + 4.5);
  doc.text("AMOUNT (RM)", 74, curY + 4.5, { align: "right" });
  doc.line(6, curY + 6.5, 74, curY + 6.5);

  let y = curY + 11.5;
  items.forEach((item) => {
    const pcs = item.piece_count ? ` (${item.piece_count} pcs)` : "";
    const label = `${item.name} x${item.quantity}${pcs}`;
    const sub = Number(item.subtotal || 0).toFixed(2);
    doc.setFont("helvetica", "bold");
    doc.setTextColor(15, 23, 42);
    doc.text(label.slice(0, 32), 6, y);
    doc.text(sub, 74, y, { align: "right" });
    if (item.unit_price !== undefined) {
      y += 3.5;
      doc.setFont("helvetica", "normal");
      doc.setFontSize(6.8);
      doc.setTextColor(100, 116, 139);
      doc.text(`@ RM ${Number(item.unit_price).toFixed(2)} / unit`, 6, y);
      doc.setFontSize(8);
    }
    y += 5.2;
  });

  doc.line(6, y - 1.5, 74, y - 1.5);
  y += 4;
  doc.setFillColor(isVoid ? 254 : (isPending ? 254 : 248), isVoid ? 242 : (isPending ? 243 : 250), isVoid ? 242 : (isPending ? 199 : 252));
  doc.setLineDashPattern([], 0);
  doc.roundedRect(6, y - 4, 68, 10, 2, 2, "F");
  doc.setFont("helvetica", "bold");
  doc.setFontSize(9.5);
  doc.setTextColor(isVoid ? 153 : (isPending ? 146 : 15), isVoid ? 27 : (isPending ? 64 : 23), isVoid ? 27 : (isPending ? 14 : 42));
  doc.text(isVoid ? "TOTAL (VOID)" : (isPending ? "TOTAL (PENDING)" : "TOTAL PAID"), 9, y + 2.2);
  doc.setTextColor(isVoid ? 185 : (isPending ? 217 : 255), isVoid ? 28 : (isPending ? 119 : 94), isVoid ? 28 : (isPending ? 6 : 54));
  doc.text("RM " + total, 71, y + 2.2, { align: "right" });

  y += 10;
  if (billData.curlec_payment_id) {
    doc.setFont("helvetica", "normal");
    doc.setFontSize(6.5);
    doc.setTextColor(100, 116, 139);
    doc.text("Payment Ref: " + billData.curlec_payment_id, 40, y, { align: "center" });
    y += 3.5;
  }
  const sid = billData.session_id || billData.id || "";
  if (sid) {
    doc.setFont("helvetica", "normal");
    doc.setFontSize(6.2);
    doc.setTextColor(148, 163, 184);
    doc.text("Session ID: " + sid.slice(0, 18), 40, y, { align: "center" });
    y += 4.5;
  } else {
    y += 1.5;
  }

  doc.setFont("helvetica", "bold");
  doc.setFontSize(7.5);
  doc.setTextColor(71, 85, 105);
  doc.text(isVoid ? "Bill record retained for reference." : (isPending ? "Pending payment — please pay at cashier." : "Thank you for dining with us!"), 40, y, { align: "center" });

  if (!isVoid) {
    y += 3.8;
    doc.setFont("helvetica", "normal");
    doc.setFontSize(6.0);
    doc.setTextColor(100, 116, 139);
    doc.text("If you have any issue regarding this payment, please head to cashier for support", 40, y, { align: "center", maxWidth: 66 });
  }

  return doc;
}

function downloadBillPdf(sessionId) {
  const billData = (typeof pageBills !== "undefined" && Array.isArray(pageBills)) ? pageBills.find(b => b.session_id === sessionId) : null;
  if (!billData) return;
  const doc = generateBillPdfDoc(billData);
  if (doc) {
    doc.save(`AFRC-Receipt-${billData.bill_number || "AAA-0001"}.pdf`);
  }
}

async function viewBillPdfPreview(billIdentifier) {
  if (!billIdentifier) {
    showToast("No bill reference associated with this scan.");
    return;
  }
  try {
    showToast("Preparing Bill PDF Preview...");
    let billData = null;
    if (typeof pageBills !== "undefined" && Array.isArray(pageBills)) {
      billData = pageBills.find(b => b.session_id === billIdentifier || b.bill_number === billIdentifier);
    }
    if (!billData) {
      const cleanNo = encodeURIComponent(String(billIdentifier).replace("#", "").trim());
      const res = await fetch(`/api/admin/bills/by-number/${cleanNo}`);
      if (res.ok) {
        billData = await res.json();
      } else {
        const sRes = await fetch(`/api/sessions/${encodeURIComponent(billIdentifier)}`);
        if (sRes.ok) {
          billData = await sRes.json();
        }
      }
    }
    if (!billData) {
      showToast("Bill details could not be found for PDF generation.");
      return;
    }
    const doc = generateBillPdfDoc(billData);
    if (!doc) {
      showToast("PDF generation engine not available.");
      return;
    }
    const blob = doc.output("blob");
    const blobUrl = URL.createObjectURL(blob);
    window.open(blobUrl, "_blank");
  } catch (err) {
    console.error("PDF Preview Error:", err);
    showToast("Error generating PDF preview.");
  }
}
