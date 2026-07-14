"use client";

import { useState, useEffect, ChangeEvent } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

interface LineItem {
  name: string;
  description: string;
  quantity: number;
  rate: number;
  sku: string;
}

interface Bill {
  bill_number: string;
  po_code: string;
  grn_codes: string[];
  facilities: string[];
  vendor_code: string;
  vendor_name: string;
  vendor_gst: string;
  date: string | null;
  invoice_date: string | null;
  line_items: LineItem[];
  notes: string;
  status: "pending" | "sku_flagged" | "vendor_flagged" | "fetch_incomplete";
  issues: string[];
  updated_at: string;
}

interface FetchError {
  code: string;
  error: string;
}

interface AttachmentFailure {
  billId: string;
  billNumber: string;
  error: string;
  file: File | null;
}

type Queue = Record<string, Bill>;

const Spinner = ({ size = 16 }: { size?: number }) => (
  <svg className="animate-spin" style={{ width: size, height: size }} xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
  </svg>
);

const formatDateYYYYMMDD = (date: Date) => {
  const y = date.getFullYear();
  const m = (date.getMonth() + 1).toString().padStart(2, "0");
  const d = date.getDate().toString().padStart(2, "0");
  return `${y}-${m}-${d}`;
};

const statusColor = (status: string) => {
  switch (status) {
    case "sku_flagged":
    case "fetch_incomplete":
      return "text-red-600 border-red-300 bg-red-50";
    case "vendor_flagged":
      return "text-amber-600 border-amber-300 bg-amber-50";
    default:
      return "text-slate-500 border-slate-300 bg-slate-50";
  }
};

export default function GrnPushPage() {
  const [startDate, setStartDate] = useState(formatDateYYYYMMDD(new Date(Date.now() - 7 * 24 * 60 * 60 * 1000)));
  const [endDate, setEndDate] = useState(formatDateYYYYMMDD(new Date()));

  const [isPulling, setIsPulling] = useState(false);
  const [pulledCodes, setPulledCodes] = useState<string[] | null>(null);
  const [isFetchingDetails, setIsFetchingDetails] = useState(false);
  const [failedCodes, setFailedCodes] = useState<FetchError[]>([]);
  const [isRetrying, setIsRetrying] = useState(false);

  const [queue, setQueue] = useState<Queue>({});
  const [isLoadingQueue, setIsLoadingQueue] = useState(false);

  const [attachmentFailures, setAttachmentFailures] = useState<AttachmentFailure[]>([]);
  const [retryingAttachmentFor, setRetryingAttachmentFor] = useState<string | null>(null);

  const [globalError, setGlobalError] = useState<string | null>(null);
  const [globalSuccess, setGlobalSuccess] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const showToast = (message: string) => {
    setToast(message);
    setTimeout(() => setToast(null), 4000);
  };

  const fetchQueue = async () => {
    setIsLoadingQueue(true);
    setGlobalError(null);
    try {
      const res = await fetch(`${API_BASE}/grn-push/queue`);
      if (!res.ok) throw new Error("Failed to fetch queue");
      setQueue(await res.json());
    } catch (err: unknown) {
      setGlobalError(err instanceof Error ? err.message : "Error fetching queue");
    } finally {
      setIsLoadingQueue(false);
    }
  };

  useEffect(() => {
    fetchQueue();
  }, []);

  const handlePullGRNs = async () => {
    setIsPulling(true);
    setGlobalError(null);
    setGlobalSuccess(null);
    setPulledCodes(null);
    try {
      const res = await fetch(`${API_BASE}/grn-push/receipts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start: startDate, end: endDate }),
      });
      if (!res.ok) throw new Error("Failed to pull receipts");
      const data = await res.json();
      setPulledCodes(data.codes || []);
    } catch (err: unknown) {
      setGlobalError(err instanceof Error ? err.message : "Error pulling GRNs");
    } finally {
      setIsPulling(false);
    }
  };

  const postFetchDetails = async (codes: string[]) => {
    const res = await fetch(`${API_BASE}/grn-push/fetch-details`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ codes }),
    });
    if (!res.ok) throw new Error("Failed to fetch GRN details");
    return res.json();
  };

  const handleFetchDetails = async () => {
    if (!pulledCodes || pulledCodes.length === 0) return;
    setIsFetchingDetails(true);
    setGlobalError(null);
    setGlobalSuccess(null);
    try {
      const data = await postFetchDetails(pulledCodes);
      setGlobalSuccess(`Queued ${data.queued?.length || 0} bill(s).${data.skipped?.length ? ` Skipped ${data.skipped.length} (kitting/dekitting/return).` : ""}`);
      setFailedCodes(data.errors || []);
      setPulledCodes(null);
      await fetchQueue();
    } catch (err: unknown) {
      setGlobalError(err instanceof Error ? err.message : "Error fetching details");
    } finally {
      setIsFetchingDetails(false);
    }
  };

  const handleRetryFailed = async () => {
    if (failedCodes.length === 0) return;
    setIsRetrying(true);
    setGlobalError(null);
    setGlobalSuccess(null);
    try {
      const data = await postFetchDetails(failedCodes.map((f) => f.code));
      setGlobalSuccess(`Retry queued ${data.queued?.length || 0} bill(s).`);
      setFailedCodes(data.errors || []);
      await fetchQueue();
    } catch (err: unknown) {
      setGlobalError(err instanceof Error ? err.message : "Error retrying failed GRNs");
    } finally {
      setIsRetrying(false);
    }
  };

  const handleAttachmentFileChange = (billId: string, file: File | null) => {
    setAttachmentFailures((prev) => prev.map((a) => (a.billId === billId ? { ...a, file } : a)));
  };

  const handleRetryAttachment = async (billId: string) => {
    const entry = attachmentFailures.find((a) => a.billId === billId);
    if (!entry || !entry.file) return;
    setRetryingAttachmentFor(billId);
    try {
      const formData = new FormData();
      formData.append("invoice_pdf", entry.file);
      const res = await fetch(`${API_BASE}/grn-push/bills/${billId}/attachment`, { method: "POST", body: formData });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Attachment retry failed");
      setAttachmentFailures((prev) => prev.filter((a) => a.billId !== billId));
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Attachment retry failed";
      setAttachmentFailures((prev) => prev.map((a) => (a.billId === billId ? { ...a, error: message } : a)));
    } finally {
      setRetryingAttachmentFor(null);
    }
  };

  return (
    <div className="w-full max-w-5xl mx-auto py-8 px-6">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-slate-800 tracking-tight">GRN Push</h1>
        <p className="text-slate-500 mt-2">Pull GRNs from Unicommerce (virtual warehouse), review, and push as Bills to Zoho Books.</p>
      </div>

      {globalError && (
        <div className="mb-6 p-4 bg-red-50 text-red-700 rounded-xl border border-red-100 flex items-center gap-3">
          <span className="text-xl">⚠️</span>
          <span className="font-medium">{globalError}</span>
        </div>
      )}

      {globalSuccess && (
        <div className="mb-6 p-4 bg-emerald-50 text-emerald-700 rounded-xl border border-emerald-100 flex items-center gap-3">
          <span className="text-xl">✓</span>
          <span className="font-medium">{globalSuccess}</span>
        </div>
      )}

      {/* STEP 1: PULL GRNS */}
      <div className="bg-white rounded-2xl shadow-sm border border-slate-200 p-6 mb-6">
        <h2 className="text-lg font-bold text-slate-800 mb-4">1. Pull GRNs by Date</h2>
        <div className="flex items-end gap-4 flex-wrap">
          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-2">Start Date</label>
            <input
              type="date"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              disabled={isPulling}
              className="px-4 py-2 border border-slate-300 rounded-xl outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50 text-slate-700 w-44 text-center font-mono"
            />
          </div>
          <div>
            <label className="block text-sm font-semibold text-slate-700 mb-2">End Date</label>
            <input
              type="date"
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              disabled={isPulling}
              className="px-4 py-2 border border-slate-300 rounded-xl outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50 text-slate-700 w-44 text-center font-mono"
            />
          </div>
          <button
            onClick={handlePullGRNs}
            disabled={isPulling}
            className="px-6 py-2.5 bg-indigo-600 hover:bg-indigo-700 text-white font-semibold rounded-xl transition-all disabled:opacity-50 flex items-center gap-2 shadow-md"
          >
            {isPulling ? <Spinner /> : null}
            Find GRNs
          </button>
        </div>

        {pulledCodes !== null && (
          <div className="mt-5 p-4 bg-slate-50 rounded-xl border border-slate-200">
            <p className="mb-3 text-slate-700">
              Found <strong>{pulledCodes.length}</strong> GRN(s) in this date range.
            </p>
            {pulledCodes.length > 0 && (
              <button
                onClick={handleFetchDetails}
                disabled={isFetchingDetails}
                className="px-5 py-2 bg-emerald-600 hover:bg-emerald-700 text-white font-semibold rounded-xl transition-all disabled:opacity-50 flex items-center gap-2"
              >
                {isFetchingDetails ? <Spinner /> : null}
                Fetch Details &amp; Add to Queue
              </button>
            )}
          </div>
        )}
      </div>

      {failedCodes.length > 0 && (
        <div className="bg-white rounded-2xl shadow-sm border border-red-200 p-6 mb-6">
          <div className="flex justify-between items-center flex-wrap gap-4">
            <div>
              <h3 className="text-red-600 font-bold mb-2">⚠️ {failedCodes.length} GRN(s) failed to fetch</h3>
              <ul className="list-disc pl-5 text-sm text-slate-600">
                {failedCodes.map((f) => (
                  <li key={f.code}>
                    <strong>{f.code}</strong>: {f.error}
                  </li>
                ))}
              </ul>
            </div>
            <button
              onClick={handleRetryFailed}
              disabled={isRetrying}
              className="px-5 py-2 bg-red-600 hover:bg-red-700 text-white font-semibold rounded-xl transition-all disabled:opacity-50 flex items-center gap-2"
            >
              {isRetrying ? <Spinner /> : null}
              Retry Failed
            </button>
          </div>
        </div>
      )}

      {attachmentFailures.length > 0 && (
        <div className="bg-white rounded-2xl shadow-sm border border-red-200 p-6 mb-6">
          <h3 className="text-red-600 font-bold mb-4">⚠️ PDF attachment failed — bill was still created</h3>
          <div className="flex flex-col gap-3">
            {attachmentFailures.map((a) => (
              <div key={a.billId} className="flex justify-between items-center flex-wrap gap-4 p-3 bg-slate-50 rounded-xl">
                <div className="text-sm">
                  <strong>{a.billNumber}</strong> (Bill ID: {a.billId})
                  <div className="text-red-600">{a.error}</div>
                </div>
                <div className="flex items-center gap-3">
                  <input type="file" accept="application/pdf" onChange={(e) => handleAttachmentFileChange(a.billId, e.target.files?.[0] || null)} />
                  <button
                    onClick={() => handleRetryAttachment(a.billId)}
                    disabled={!a.file || retryingAttachmentFor === a.billId}
                    className="px-4 py-1.5 bg-red-600 hover:bg-red-700 text-white font-semibold rounded-lg text-sm transition-all disabled:opacity-50"
                  >
                    {retryingAttachmentFor === a.billId ? <Spinner size={14} /> : "Retry Attach"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* STEP 2: REVIEW & PUSH */}
      <div className="bg-white rounded-2xl shadow-sm border border-slate-200 p-6">
        <div className="flex justify-between items-center mb-5">
          <h2 className="text-lg font-bold text-slate-800">2. Review &amp; Push to Zoho</h2>
          <button
            onClick={fetchQueue}
            disabled={isLoadingQueue}
            className="px-4 py-1.5 bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold rounded-lg text-sm transition-all disabled:opacity-50 flex items-center gap-2"
          >
            {isLoadingQueue ? <Spinner size={14} /> : null}
            Refresh Queue
          </button>
        </div>

        {isLoadingQueue && Object.keys(queue).length === 0 ? (
          <div className="flex justify-center py-12 text-slate-400">
            <Spinner size={32} />
          </div>
        ) : Object.keys(queue).length === 0 ? (
          <div className="text-center py-12 text-slate-400 bg-slate-50 rounded-xl">No pending bills in the queue. Pull GRNs above to get started.</div>
        ) : (
          <div className="flex flex-col gap-6">
            {Object.values(queue).map((bill) => (
              <BillCard
                key={bill.bill_number}
                bill={bill}
                onRefresh={fetchQueue}
                onPushed={(message, billNumber) => {
                  showToast(message);
                  setQueue((prev) => {
                    const nextQ = { ...prev };
                    delete nextQ[billNumber];
                    return nextQ;
                  });
                }}
                onAttachmentFailure={(billId, error) => setAttachmentFailures((prev) => [...prev, { billId, billNumber: bill.bill_number, error, file: null }])}
              />
            ))}
          </div>
        )}
      </div>

      {/* Toast Notification */}
      {toast && (
        <div className="fixed bottom-6 right-6 bg-emerald-600 text-white px-5 py-3 rounded-xl shadow-2xl flex items-center gap-3 z-50 transition-all duration-300">
          <span className="text-xl">✓</span>
          <span className="font-medium">{toast}</span>
        </div>
      )}
    </div>
  );
}

function BillCard({
  bill,
  onRefresh,
  onPushed,
  onAttachmentFailure,
}: {
  bill: Bill;
  onRefresh: () => void;
  onPushed: (message: string, billNumber: string) => void;
  onAttachmentFailure: (billId: string, error: string) => void;
}) {
  const [isEditing, setIsEditing] = useState(false);
  const [editedLineItems, setEditedLineItems] = useState<LineItem[]>(bill.line_items || []);
  const [file, setFile] = useState<File | null>(null);
  const [isPushing, setIsPushing] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [localSuccess, setLocalSuccess] = useState<string | null>(null);

  useEffect(() => {
    setEditedLineItems(bill.line_items || []);
    setIsEditing(false);
    setLocalError(null);
    setLocalSuccess(null);
  }, [bill]);

  const handleLineItemChange = (index: number, field: keyof LineItem, value: string | number) => {
    const newItems = [...editedLineItems];
    newItems[index] = { ...newItems[index], [field]: value };
    setEditedLineItems(newItems);
  };

  const handleSave = async () => {
    setIsSaving(true);
    setLocalError(null);
    try {
      const res = await fetch(`${API_BASE}/grn-push/queue/${bill.bill_number}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ line_items: editedLineItems }),
      });
      if (!res.ok) throw new Error("Failed to update bill");
      setLocalSuccess("Saved successfully");
      setIsEditing(false);
      onRefresh();
    } catch (err: unknown) {
      setLocalError(err instanceof Error ? err.message : "Error saving bill");
    } finally {
      setIsSaving(false);
    }
  };

  const handlePush = async () => {
    setIsPushing(true);
    setLocalError(null);
    setLocalSuccess(null);

    const formData = new FormData();
    if (file) formData.append("invoice_pdf", file);

    try {
      const res = await fetch(`${API_BASE}/grn-push/queue/${bill.bill_number}/push`, { method: "POST", body: formData });
      const data = await res.json();

      if (!res.ok) {
        if (res.status === 422) onRefresh();
        throw new Error(data.detail || "Failed to push to Zoho");
      }

      const message =
        data.vendor_match_method === "existing"
          ? `Bill ${bill.bill_number} already exists in Zoho (Bill ID: ${data.bill_id}) — removed from queue.`
          : `Pushed Bill ${bill.bill_number} to Zoho successfully! Bill ID: ${data.bill_id}. Vendor match: ${data.vendor_match_method}`;
      if (data.attachment_error) onAttachmentFailure(data.bill_id, data.attachment_error);
      onPushed(message, bill.bill_number);
    } catch (err: unknown) {
      setLocalError(err instanceof Error ? err.message : "Error pushing bill");
    } finally {
      setIsPushing(false);
    }
  };

  const handleDelete = async () => {
    if (!window.confirm(`Discard Bill ${bill.bill_number}?`)) return;
    setIsDeleting(true);
    setLocalError(null);
    try {
      const res = await fetch(`${API_BASE}/grn-push/queue/${bill.bill_number}`, { method: "DELETE" });
      if (!res.ok) throw new Error("Failed to discard bill");
      onRefresh();
    } catch (err: unknown) {
      setLocalError(err instanceof Error ? err.message : "Error discarding bill");
      setIsDeleting(false);
    }
  };

  const handleFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) setFile(e.target.files[0]);
  };

  return (
    <div className="border border-slate-200 rounded-xl overflow-hidden">
      <div className="p-5 border-b border-slate-100 bg-slate-50 flex justify-between flex-wrap gap-4">
        <div>
          <div className="flex items-center gap-3 mb-2 flex-wrap">
            <h4 className="text-lg font-semibold text-slate-800">{bill.vendor_name}</h4>
            <span className="text-xs px-2 py-0.5 rounded-full bg-slate-200 text-slate-600">{bill.vendor_gst}</span>
            <span className={`text-xs px-2 py-0.5 rounded-full border font-semibold uppercase ${statusColor(bill.status)}`}>{bill.status.replace("_", " ")}</span>
          </div>
          <div className="text-sm text-slate-500 flex gap-4 flex-wrap">
            <span>
              <strong>Invoice:</strong> {bill.bill_number}
            </span>
            <span>
              <strong>PO:</strong> {bill.po_code}
            </span>
            <span>
              <strong>Date:</strong> {bill.invoice_date}
            </span>
          </div>
        </div>
        <button onClick={handleDelete} disabled={isDeleting} className="p-2 text-red-500 hover:bg-red-50 rounded-lg border border-red-200" title="Discard Bill">
          {isDeleting ? <Spinner /> : "Discard"}
        </button>
      </div>

      {bill.issues && bill.issues.length > 0 && (
        <div className="p-4 bg-red-50 text-red-700 border-b border-slate-100">
          <div className="font-semibold mb-1">⚠️ Push Blocked Issues:</div>
          <ul className="list-disc pl-5 text-sm">
            {bill.issues.map((issue, i) => (
              <li key={i}>{issue}</li>
            ))}
          </ul>
        </div>
      )}

      {localError && <div className="p-4 text-red-600 border-b border-slate-100 text-sm font-medium">{localError}</div>}
      {localSuccess && <div className="p-4 text-emerald-600 border-b border-slate-100 text-sm font-medium">{localSuccess}</div>}

      <div className="p-5">
        <div className="flex justify-between items-center mb-3">
          <h5 className="font-semibold text-slate-700">Line Items ({editedLineItems.length})</h5>
          {!isEditing ? (
            <button onClick={() => setIsEditing(true)} className="px-3 py-1 text-sm bg-slate-100 hover:bg-slate-200 rounded-lg">
              Edit Line Items
            </button>
          ) : (
            <div className="flex gap-2">
              <button
                onClick={() => {
                  setIsEditing(false);
                  setEditedLineItems(bill.line_items || []);
                }}
                className="px-3 py-1 text-sm bg-slate-100 hover:bg-slate-200 rounded-lg"
              >
                Cancel
              </button>
              <button onClick={handleSave} disabled={isSaving} className="px-3 py-1 text-sm bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg disabled:opacity-50 flex items-center gap-1.5">
                {isSaving ? <Spinner size={14} /> : null} Save
              </button>
            </div>
          )}
        </div>

        <div className="overflow-x-auto rounded-lg border border-slate-200">
          <table className="w-full text-sm text-left">
            <thead className="bg-slate-50 text-slate-600 font-semibold uppercase text-xs">
              <tr>
                <th className="px-3 py-2">SKU</th>
                <th className="px-3 py-2">Name</th>
                <th className="px-3 py-2">Qty</th>
                <th className="px-3 py-2">Rate</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {editedLineItems.map((item, idx) => (
                <tr key={idx}>
                  <td className="px-3 py-2 font-mono">
                    {isEditing ? (
                      <input type="text" value={item.sku} onChange={(e) => handleLineItemChange(idx, "sku", e.target.value)} className="border border-slate-300 rounded px-2 py-1 w-24" />
                    ) : (
                      item.sku
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {isEditing ? (
                      <input type="text" value={item.name} onChange={(e) => handleLineItemChange(idx, "name", e.target.value)} className="border border-slate-300 rounded px-2 py-1 w-full" />
                    ) : (
                      item.name
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {isEditing ? (
                      <input
                        type="number"
                        value={item.quantity}
                        onChange={(e) => handleLineItemChange(idx, "quantity", parseFloat(e.target.value))}
                        className="border border-slate-300 rounded px-2 py-1 w-20"
                      />
                    ) : (
                      item.quantity
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {isEditing ? (
                      <input
                        type="number"
                        step="0.01"
                        value={item.rate}
                        onChange={(e) => handleLineItemChange(idx, "rate", parseFloat(e.target.value))}
                        className="border border-slate-300 rounded px-2 py-1 w-24"
                      />
                    ) : (
                      item.rate.toFixed(2)
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {bill.notes && (
          <div className="mt-3 text-sm text-slate-500">
            <strong>Notes:</strong> {bill.notes}
          </div>
        )}
      </div>

      <div className="p-5 bg-slate-50 border-t border-slate-100 flex justify-between items-center flex-wrap gap-4">
        <div className="flex items-center gap-3">
          <label htmlFor={`pdf-${bill.bill_number}`} className={`cursor-pointer flex items-center gap-2 text-sm font-medium ${file ? "text-emerald-600" : "text-indigo-600"}`}>
            📎 {file ? file.name : "Attach PDF (Optional)"}
          </label>
          <input type="file" id={`pdf-${bill.bill_number}`} accept="application/pdf" className="hidden" onChange={handleFileChange} />
          {file && (
            <button onClick={() => setFile(null)} className="text-xs text-slate-500 hover:text-slate-700">
              Clear
            </button>
          )}
        </div>

        <button
          onClick={handlePush}
          disabled={isPushing || isEditing}
          className="px-5 py-2 bg-indigo-600 hover:bg-indigo-700 text-white font-semibold rounded-xl transition-all disabled:opacity-50 flex items-center gap-2"
        >
          {isPushing ? <Spinner /> : null}
          Push to Zoho
        </button>
      </div>
    </div>
  );
}
