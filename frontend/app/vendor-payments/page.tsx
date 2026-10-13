"use client";

import { useState } from "react";
import * as XLSX from "xlsx";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

interface Bank {
  account_id: string;
  bank_name: string;
  account_number: string;
  ifsc: string;
  beneficiary_name: string;
  is_primary: boolean;
}

interface Row {
  vendor_name: string;
  amount: string;
  bank_account_id: string;
  banks?: Bank[]; // undefined until verified
  verifyError?: string;
}

interface Result {
  vendor_name: string;
  amount: number;
  status: "ok" | "failed" | "unknown";
  payment_number?: string;
  reference_number?: string;
  error?: string;
}

const emptyRow = (): Row => ({ vendor_name: "", amount: "", bank_account_id: "" });

const TEMPLATE_HEADER = ["Vendor Name", "Amount"];

const downloadTemplate = () => {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([`${TEMPLATE_HEADER.join(",")}\n`], { type: "text/csv" }));
  a.download = "vendor_payments_template.csv";
  a.click();
  URL.revokeObjectURL(a.href);
};

// Strict: header must be exactly the template's, every row a vendor name and a plain amount (e.g. 12500 or 12500.50).
// Any problem rejects the whole file, so a half-read CSV never reaches the payment table.
function parseVendorCsv(text: string): { rows: Row[]; errors: string[] } {
  const book = XLSX.read(text, { type: "string", raw: true });
  const lines: string[][] = XLSX.utils.sheet_to_json(book.Sheets[book.SheetNames[0]], { header: 1, raw: false, defval: "", blankrows: false });
  const head = (lines[0] ?? []).map((c) => String(c).trim());
  // Blank cells past the template columns are padding from a longer data row, not a header problem.
  if (TEMPLATE_HEADER.some((c, i) => head[i] !== c) || head.slice(TEMPLATE_HEADER.length).some(Boolean))
    return { rows: [], errors: [`Header must be exactly "${TEMPLATE_HEADER.join(",")}" — download the template`] };
  const errors: string[] = [];
  const rows = lines.slice(1).map((l, i) => {
    const [name = "", rawAmount = "", ...extra] = l.map((c) => String(c).trim());
    const amount = rawAmount.replace(/,/g, "");
    const at = `Row ${i + 2}`;
    if (extra.some(Boolean)) errors.push(`${at}: extra columns (amount with commas must be in quotes, e.g. "12,500")`);
    if (!name) errors.push(`${at}: vendor name missing`);
    // Commas allowed as digit grouping (12,500 or 1,25,000), stripped before use.
    if (!/^\d+(,\d+)*(\.\d{1,2})?$/.test(rawAmount) || Number(amount) <= 0) errors.push(`${at}: amount "${rawAmount}" is not a positive number`);
    return { vendor_name: name, amount, bank_account_id: "" };
  });
  if (!rows.length) errors.push("No vendor rows found in CSV");
  return { rows, errors };
}

const vendorKey = (name: string) => name.trim().toLowerCase();

const formatINR = (n: number) =>
  `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

const inputCls =
  "px-3 py-2 border border-slate-300 rounded-xl outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50 text-slate-700 text-sm";

export default function VendorPaymentsPage() {
  const [rows, setRows] = useState<Row[]>([emptyRow()]);

  const [submitting, setSubmitting] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [pin, setPin] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [results, setResults] = useState<Result[] | null>(null);

  const busy = submitting || verifying;

  // Changing the vendor name throws away its verified bank account.
  const updateRow = (i: number, key: "vendor_name" | "amount" | "bank_account_id", value: string) =>
    setRows((prev) => prev.map((r, idx) => (idx !== i ? r
      : key === "vendor_name" ? { ...r, vendor_name: value, bank_account_id: "", banks: undefined, verifyError: undefined }
      : { ...r, [key]: value })));

  const filled = rows.filter((r) => r.vendor_name.trim() && Number(r.amount) > 0);
  const total = filled.reduce((sum, r) => sum + Number(r.amount), 0);
  // Same vendor twice in one batch is almost always a mistake: flag the rows and block sending until merged/removed.
  const counts = new Map<string, number>();
  rows.forEach((r) => r.vendor_name.trim() && counts.set(vendorKey(r.vendor_name), (counts.get(vendorKey(r.vendor_name)) ?? 0) + 1));
  const isDup = (r: Row) => (counts.get(vendorKey(r.vendor_name)) ?? 0) > 1;
  const hasDups = rows.some(isDup);
  const allVerified = filled.length > 0 && !hasDups && filled.every((r) => r.bank_account_id);

  const verify = async () => {
    if (filled.length === 0) return setErrorMsg("Add at least one vendor with an amount");
    if (hasDups) return setErrorMsg("Same vendor appears more than once — merge or remove the flagged rows");
    setVerifying(true);
    setErrorMsg(null);
    try {
      const res = await fetch(`${API_BASE}/vendor-payments/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ names: filled.map((r) => r.vendor_name) }),
      });
      const data: { vendor_name: string; banks: Bank[]; error?: string }[] = await res.json();
      if (!res.ok) throw new Error((data as unknown as { detail?: string }).detail ?? res.statusText);
      const byName = new Map(data.map((d) => [d.vendor_name, d]));
      setRows((prev) => prev.map((r) => {
        const d = byName.get(r.vendor_name.trim());
        if (!d) return r;
        // Pre-select only when there's no doubt: a single account, or the vendor's primary one.
        const pick = d.banks.length === 1 ? d.banks[0] : d.banks.find((b) => b.is_primary);
        return { ...r, banks: d.banks, bank_account_id: pick?.account_id ?? "",
          verifyError: d.error ?? (d.banks.length ? undefined : "No active bank account in Zoho") };
      }));
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : "An unexpected error occurred");
    } finally {
      setVerifying(false);
    }
  };

  const loadCsv = async (file: File | undefined) => {
    if (!file) return;
    const { rows: parsed, errors } = parseVendorCsv(await file.text());
    setResults(null);
    if (errors.length) return setErrorMsg(`CSV rejected — ${errors.join("; ")}`);
    setErrorMsg(null);
    setRows(parsed);
  };

  const submit = async () => {
    if (hasDups) return setErrorMsg("Same vendor appears more than once — merge or remove the flagged rows");
    if (!allVerified) return setErrorMsg("Verify vendors and pick a bank account for every row");
    if (!/^\d{6}$/.test(pin)) return setErrorMsg("Enter your 6-digit Zoho HSBC Secret PIN");
    if (!confirm(`SEND ${filled.length} payment(s) totalling ${formatINR(total)} from HSBC to the bank accounts shown? Money will move and this can't be undone.`)) return;
    const sent = filled;

    setSubmitting(true);
    setErrorMsg(null);
    setResults(null);
    try {
      const res = await fetch(`${API_BASE}/vendor-payments/bulk`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          pin,
          rows: sent.map((r) => ({ vendor_name: r.vendor_name, amount: Number(r.amount), bank_account_id: r.bank_account_id })),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : res.statusText);
      setResults(data.results);
      // Keep only rows Zoho definitely didn't take, so a re-run can never pay anyone twice.
      // "unknown" rows are left out on purpose: check Zoho/HSBC by hand first.
      const failed = sent.filter((_, i) => data.results[i].status === "failed");
      setRows(failed.length ? failed : [emptyRow()]);
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : "An unexpected error occurred");
    } finally {
      setPin(""); // never keep the PIN around after a send attempt
      setSubmitting(false);
    }
  };

  return (
    <div className="w-full max-w-6xl mx-auto py-8 px-6">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-slate-800 tracking-tight">Vendor Payments</h1>
        <p className="text-slate-500 mt-2">
          Pay multiple vendors from HSBC Bank Account via Zoho — money moves to each vendor&apos;s bank account, dated today, booked as vendor advance
        </p>
      </div>

      <div className="bg-white rounded-2xl shadow-sm border border-slate-200 mb-8">
        <div className="p-6">
          <table className="w-full text-sm text-left">
            <thead className="text-slate-600 font-semibold uppercase text-xs">
              <tr>
                <th className="pb-2">Vendor Name (exact, as in Zoho)</th>
                <th className="pb-2 w-40">Amount</th>
                <th className="pb-2 w-72">Transfer To</th>
                <th className="pb-2 w-10" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i} className={isDup(row) ? "bg-red-50" : ""}>
                  <td className="pr-2 py-1">
                    <input value={row.vendor_name} onChange={(e) => updateRow(i, "vendor_name", e.target.value)} disabled={busy} className={`${inputCls} w-full`} />
                    {isDup(row) && <span className="text-red-600 text-xs">Duplicate vendor in this batch</span>}
                  </td>
                  <td className="pr-2 py-1">
                    <input type="number" min="0" step="0.01" value={row.amount} onChange={(e) => updateRow(i, "amount", e.target.value)} disabled={busy} className={`${inputCls} w-full text-right`} />
                  </td>
                  <td className="pr-2 py-1">
                    {row.verifyError ? (
                      <span className="text-red-600 text-xs">{row.verifyError}</span>
                    ) : row.banks ? (
                      <select value={row.bank_account_id} onChange={(e) => updateRow(i, "bank_account_id", e.target.value)} disabled={busy} className={`${inputCls} w-full`}>
                        <option value="">Select bank account</option>
                        {row.banks.map((b) => (
                          <option key={b.account_id} value={b.account_id}>
                            {b.bank_name} · {b.account_number} · {b.ifsc} · {b.beneficiary_name}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <span className="text-slate-400 text-xs">Not verified</span>
                    )}
                  </td>
                  <td className="py-1">
                    <button onClick={() => setRows((prev) => (prev.length > 1 ? prev.filter((_, idx) => idx !== i) : [emptyRow()]))} disabled={busy} className="text-slate-400 hover:text-red-600 px-2">
                      ✕
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="flex items-center justify-between mt-4 flex-wrap gap-4">
            <div className="flex items-center gap-6">
            <button onClick={() => setRows((prev) => [...prev, emptyRow()])} disabled={busy} className="text-indigo-600 font-semibold text-sm">
              + Add vendor
            </button>
            <label className="text-indigo-600 font-semibold text-sm cursor-pointer">
              ⬆ Upload CSV
              <input type="file" accept=".csv,text/csv" className="hidden" disabled={busy}
                onChange={(e) => { loadCsv(e.target.files?.[0]); e.target.value = ""; }} />
            </label>
            <button onClick={downloadTemplate} className="text-indigo-600 font-semibold text-sm">
              ⬇ CSV Template
            </button>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-slate-600 text-sm">
                {filled.length} payment(s) · <span className="font-semibold text-slate-800">{formatINR(total)}</span>
              </span>
              <button onClick={verify} disabled={busy} className="px-5 py-2.5 border border-indigo-600 text-indigo-600 font-semibold rounded-xl transition-all disabled:opacity-50">
                {verifying ? "Verifying..." : "Verify Vendors"}
              </button>
              <input type="password" inputMode="numeric" autoComplete="off" maxLength={6} placeholder="Secret PIN"
                value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))} disabled={busy} className={`${inputCls} w-32`} />
              <button onClick={submit} disabled={busy || !allVerified} className="px-6 py-2.5 bg-indigo-600 hover:bg-indigo-700 text-white font-semibold rounded-xl transition-all disabled:opacity-50 shadow-md">
                {submitting ? "Sending..." : "Send Payments"}
              </button>
            </div>
          </div>
        </div>

        {errorMsg && (
          <div className="mx-6 mb-6 p-4 bg-red-50 text-red-700 rounded-xl border border-red-100 flex items-center gap-3">
            <span className="text-xl">⚠️</span>
            <span className="font-medium">{errorMsg}</span>
          </div>
        )}

        {results && (
          <div className="px-6 pb-6">
            <h2 className="text-lg font-bold text-slate-800 mb-3">
              Results — {results.filter((r) => r.status === "ok").length} initiated, {results.filter((r) => r.status === "failed").length} failed
              {results.some((r) => r.status === "unknown") && `, ${results.filter((r) => r.status === "unknown").length} unknown`}
            </h2>
            <div className="overflow-x-auto rounded-xl border border-slate-200">
              <table className="w-full text-sm text-left">
                <tbody className="divide-y divide-slate-100 text-slate-800">
                  {results.map((r, i) => (
                    <tr key={i} className={{ ok: "", failed: "bg-red-50", unknown: "bg-amber-50" }[r.status]}>
                      <td className="px-4 py-2">{r.vendor_name}</td>
                      <td className="px-4 py-2 text-right">{formatINR(r.amount)}</td>
                      <td className="px-4 py-2">
                        {r.status === "ok" ? `✅ ${r.payment_number} · ${r.reference_number ?? ""}`
                          : r.status === "failed" ? `❌ ${r.error}`
                          : `⚠️ Unknown — check Zoho/HSBC before retrying (${r.error})`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {results.some((r) => r.status === "failed") && (
              <p className="text-slate-500 text-sm mt-2">Failed rows kept in the form above — nothing was sent for them; fix and re-run.</p>
            )}
            {results.some((r) => r.status === "unknown") && (
              <p className="text-amber-700 text-sm mt-2">Unknown rows were NOT kept: Zoho didn&apos;t answer, so the money may have gone. Check Zoho/HSBC before paying them again.</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
