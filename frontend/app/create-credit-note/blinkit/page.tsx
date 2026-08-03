"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

function escHtml(str: string) {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

type ToastType = "success" | "error";

interface ProcessResult {
  poNumber: string;
  status: "pending" | "processing" | "success" | "error";
  message?: string;
  creditNotes?: { creditnote_id: string; creditnote_number: string }[];
}

export default function BlinkitCreditNote() {
  const [authStatus, setAuthStatus] = useState<"loading" | "unauthorized" | "authorized">("loading");
  const [otpRequested, setOtpRequested] = useState(false);
  const [otp, setOtp] = useState("");
  const [authBusy, setAuthBusy] = useState(false);

  const [rawInput, setRawInput] = useState("");
  const [parsedNumbers, setParsedNumbers] = useState<string[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [results, setResults] = useState<ProcessResult[]>([]);
  
  const [toast, setToast] = useState<{ type: ToastType; msg: string } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    checkStatus();
  }, []);

  const checkStatus = async () => {
    try {
      const resp = await fetch(`${API_BASE}/blinkit/status`);
      if (resp.ok) {
        const data = await resp.json();
        setAuthStatus(data.logged_in ? "authorized" : "unauthorized");
      } else {
        setAuthStatus("unauthorized");
      }
    } catch (err) {
      setAuthStatus("unauthorized");
      console.error(err);
    }
  };

  const showToast = (type: ToastType, msg: string) => {
    setToast({ type, msg });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 4000);
  };

  const handleSendOtp = async () => {
    setAuthBusy(true);
    try {
      const resp = await fetch(`${API_BASE}/blinkit/send-otp`, { method: "POST" });
      if (!resp.ok) {
        throw new Error(`Failed to send OTP (${resp.status})`);
      }
      setOtpRequested(true);
      showToast("success", "OTP sent to configured email!");
    } catch (err: any) {
      showToast("error", err.message || "Failed to send OTP");
    } finally {
      setAuthBusy(false);
    }
  };

  const handleVerifyOtp = async () => {
    if (!otp.trim()) return;
    setAuthBusy(true);
    try {
      const resp = await fetch(`${API_BASE}/blinkit/verify-otp`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ otp: otp.trim() }),
      });
      if (!resp.ok) {
        let detail = "Failed to verify OTP";
        try {
          detail = (await resp.json()).detail ?? detail;
        } catch {}
        throw new Error(detail);
      }
      const data = await resp.json();
      if (data.logged_in) {
        setAuthStatus("authorized");
        showToast("success", "Logged in successfully!");
      }
    } catch (err: any) {
      showToast("error", err.message || "Failed to verify OTP");
    } finally {
      setAuthBusy(false);
    }
  };

  const parseNumbers = useCallback((raw: string) => {
    const parts = raw
      .split(/[\n,;]+/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    const unique = [...new Map(parts.map((s) => [s.toUpperCase(), s])).values()];
    setParsedNumbers(unique);
  }, []);

  useEffect(() => {
    parseNumbers(rawInput);
  }, [rawInput, parseNumbers]);

  const removeChip = (idx: number) => {
    const updated = parsedNumbers.filter((_, i) => i !== idx);
    setParsedNumbers(updated);
    setRawInput(updated.join("\n"));
  };

  const clearAll = () => {
    setRawInput("");
    setParsedNumbers([]);
    setResults([]);
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(true);
  };

  const handleDragLeave = () => setIsDragOver(false);

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
    const file = e.dataTransfer.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      setRawInput(ev.target?.result as string);
    };
    reader.readAsText(file);
  };

  const processPOs = async () => {
    const numbers = parsedNumbers.slice(0, 100); // limit to 100 per batch
    if (!numbers.length) return;
    
    setBusy(true);
    setProgress(0);
    
    const initialResults: ProcessResult[] = numbers.map(num => ({
      poNumber: num,
      status: "pending"
    }));
    setResults(initialResults);

    let completed = 0;
    let nextIndex = 0;
    let stopped = false;
    const updatedResults = [...initialResults];

    // 2 POs in flight at a time — halves wall-clock time vs. one-at-a-time, while the
    // shared blinkit_client still paces every actual Blinkit call server-side.
    const CONCURRENCY = 2;

    const worker = async () => {
      while (!stopped) {
        const i = nextIndex++;
        if (i >= numbers.length) return;
        const po = numbers[i];
        updatedResults[i].status = "processing";
        setResults([...updatedResults]);

        try {
          const resp = await fetch(`${API_BASE}/blinkit/process-po`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ po_number: po }),
          });

          if (resp.status === 401) {
            stopped = true;
            setAuthStatus("unauthorized");
            setOtpRequested(false);
            setOtp("");
            showToast("error", "Session expired. Please log in again.");
            return;
          }

          if (!resp.ok) {
            let detail = `Error ${resp.status}`;
            try {
              detail = (await resp.json()).detail ?? detail;
            } catch {}
            throw new Error(detail);
          }

          const data = await resp.json();
          updatedResults[i].status = "success";
          updatedResults[i].creditNotes = data.credit_notes || [];
          updatedResults[i].message = `Invoice: ${data.invoice_number || 'N/A'}`;
        } catch (err: any) {
          updatedResults[i].status = "error";
          updatedResults[i].message = err.message || "Unknown error";
        }

        completed++;
        setProgress((completed / numbers.length) * 100);
        setResults([...updatedResults]);
      }
    };

    await Promise.all(Array.from({ length: CONCURRENCY }, worker));

    setBusy(false);
    setTimeout(() => setProgress(0), 1000);
  };

  const hasNotes = parsedNumbers.length > 0 && parsedNumbers.length <= 100;

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-indigo-50 flex flex-col items-center pb-16">
      {/* Toast */}
      {toast && (
        <div
          className={`fixed top-6 right-6 z-50 flex items-center gap-3 px-5 py-3 rounded-xl shadow-lg text-sm font-medium max-w-sm ${
            toast.type === "success" ? "bg-green-600 text-white" : "bg-red-600 text-white"
          }`}
        >
          <span className="text-lg">{toast.type === "success" ? "✓" : "✕"}</span>
          <span>{toast.msg}</span>
        </div>
      )}

      {/* Page Header */}
      <div className="w-full max-w-4xl px-4 py-8 mb-2">
        <h1 className="text-3xl font-bold text-slate-800 tracking-tight">Create Credit Note - Blinkit</h1>
        <p className="text-slate-500 mt-2">Generate credit notes from Blinkit discrepancy notes in Zoho</p>
      </div>

      <div className="w-full max-w-4xl px-4">
        <div className="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden mb-6">
          {authStatus === "loading" ? (
            <div className="p-8 flex items-center justify-center text-slate-500">
              <div className="spinner !border-slate-300 ![border-top-color:#6366f1] mr-3" />
              Checking authentication status...
            </div>
          ) : authStatus === "unauthorized" ? (
            <div className="p-8 flex flex-col items-center justify-center text-center">
              <div className="w-16 h-16 bg-indigo-50 rounded-full flex items-center justify-center mb-4 text-indigo-600">
                <svg xmlns="http://www.w3.org/2000/svg" className="h-8 w-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
                </svg>
              </div>
              <h2 className="text-xl font-semibold text-slate-800 mb-2">Blinkit Authentication Required</h2>
              <p className="text-slate-500 mb-6 max-w-md">
                You need to log in to the Blinkit Partner Portal to fetch discrepancy notes.
              </p>
              
              {!otpRequested ? (
                <button
                  onClick={handleSendOtp}
                  disabled={authBusy}
                  className="bg-indigo-600 hover:bg-indigo-700 disabled:bg-slate-300 text-white font-semibold py-3 px-6 rounded-xl transition-colors text-sm flex items-center gap-2"
                >
                  {authBusy ? <div className="spinner shrink-0 ![border-top-color:#fff]" /> : null}
                  Send Login OTP
                </button>
              ) : (
                <div className="flex flex-col gap-3 w-full max-w-xs">
                  <input
                    type="text"
                    placeholder="Enter OTP"
                    value={otp}
                    onChange={(e) => setOtp(e.target.value)}
                    className="w-full px-4 py-3 text-center tracking-widest text-lg font-mono border border-slate-200 rounded-xl outline-none focus:ring-2 focus:ring-indigo-300 focus:border-indigo-400 transition"
                  />
                  <button
                    onClick={handleVerifyOtp}
                    disabled={authBusy || !otp.trim()}
                    className="bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold py-3 px-6 rounded-xl transition-colors text-sm flex items-center justify-center gap-2"
                  >
                    {authBusy ? <div className="spinner shrink-0 ![border-top-color:#fff]" /> : null}
                    Verify OTP
                  </button>
                </div>
              )}
            </div>
          ) : (
            <>
              {/* Input section */}
              <div className="p-6 border-b border-slate-100">
                <label className="block text-sm font-semibold text-slate-700 mb-2">
                  PO Numbers{" "}
                  <span className="ml-1 font-normal text-slate-400">(one per line, or comma-separated)</span>
                </label>
                <div
                  className={`drop-zone rounded-xl border-2 border-dashed border-slate-200 bg-slate-50 transition-all${isDragOver ? " drag-over" : ""}`}
                  onDragOver={handleDragOver}
                  onDragLeave={handleDragLeave}
                  onDrop={handleDrop}
                >
                  <textarea
                    rows={8}
                    value={rawInput}
                    onChange={(e) => setRawInput(e.target.value)}
                    placeholder={"1723710043291\n1723710043292\n...or paste a comma-separated list"}
                    className="w-full bg-transparent px-4 py-3 text-sm text-slate-700 placeholder-slate-400 resize-none outline-none font-mono"
                  />
                  <div className="flex items-center justify-between px-4 py-2 border-t border-dashed border-slate-200 text-xs text-slate-400">
                    <span>Drag &amp; drop a <code>.txt</code> file here</span>
                    <button onClick={clearAll} className="text-slate-400 hover:text-red-400 transition-colors">Clear all</button>
                  </div>
                </div>
              </div>

              {/* Chips */}
              {parsedNumbers.length > 0 && (
                <div className="px-6 py-4 border-b border-slate-100 bg-slate-50">
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Parsed</span>
                    <span className="text-xs font-semibold text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded-full">
                      {parsedNumbers.length}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {parsedNumbers.slice(0, 100).map((num, idx) => (
                      <span key={idx} className="chip">
                        <span dangerouslySetInnerHTML={{ __html: escHtml(num) }} />
                        <button onClick={() => removeChip(idx)} title="Remove" disabled={busy}>×</button>
                      </span>
                    ))}
                    {parsedNumbers.length > 100 && (
                      <span className="text-xs text-red-400 font-medium self-center">
                        +{parsedNumbers.length - 100} ignored
                      </span>
                    )}
                  </div>
                  {parsedNumbers.length > 100 && (
                    <p className="mt-2 text-xs text-red-500 font-medium">Maximum 100 POs allowed per batch.</p>
                  )}
                </div>
              )}

              {/* Action */}
              <div className="px-6 py-5">
                <button
                  onClick={processPOs}
                  disabled={busy || !hasNotes}
                  className="w-full flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-slate-300 disabled:cursor-not-allowed text-white font-semibold py-3 px-4 rounded-xl transition-colors text-sm"
                >
                  {busy ? (
                    <div className="spinner shrink-0 ![border-top-color:#fff]" />
                  ) : (
                    <svg xmlns="http://www.w3.org/2000/svg" className="w-5 h-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                    </svg>
                  )}
                  <span>{busy ? `Processing ${parsedNumbers.slice(0, 100).length} POs…` : "Process POs"}</span>
                </button>
              </div>

              {/* Progress */}
              {busy && (
                <div className="px-6 pb-5">
                  <div className="w-full bg-slate-100 rounded-full h-2 overflow-hidden">
                    <div
                      className="bg-indigo-500 h-2 rounded-full transition-all duration-300"
                      style={{ width: `${progress}%` }}
                    />
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        {/* Results Table */}
        {results.length > 0 && (
          <div className="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden">
            <div className="px-6 py-4 border-b border-slate-100 bg-slate-50">
              <h3 className="font-semibold text-slate-800">Processing Results</h3>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="border-b border-slate-100 text-xs text-slate-500 uppercase tracking-wide">
                    <th className="px-6 py-3 font-semibold">PO Number</th>
                    <th className="px-6 py-3 font-semibold">Status</th>
                    <th className="px-6 py-3 font-semibold">Message</th>
                    <th className="px-6 py-3 font-semibold">Credit Notes</th>
                  </tr>
                </thead>
                <tbody className="text-sm">
                  {results.map((res, i) => (
                    <tr key={i} className="border-b border-slate-50 hover:bg-slate-50 transition-colors">
                      <td className="px-6 py-3 font-mono text-slate-700">{res.poNumber}</td>
                      <td className="px-6 py-3">
                        {res.status === "pending" && <span className="inline-block px-2 py-1 bg-slate-100 text-slate-600 rounded text-xs font-medium">Pending</span>}
                        {res.status === "processing" && <span className="inline-block px-2 py-1 bg-indigo-100 text-indigo-600 rounded text-xs font-medium flex items-center gap-1 w-max"><div className="w-2 h-2 rounded-full bg-indigo-500 animate-pulse"/> Processing</span>}
                        {res.status === "success" && <span className="inline-block px-2 py-1 bg-green-100 text-green-700 rounded text-xs font-medium">Success</span>}
                        {res.status === "error" && <span className="inline-block px-2 py-1 bg-red-100 text-red-700 rounded text-xs font-medium">Error</span>}
                      </td>
                      <td className="px-6 py-3 text-slate-600">
                        {res.message || "-"}
                      </td>
                      <td className="px-6 py-3 text-slate-600">
                        {res.creditNotes && res.creditNotes.length > 0 ? (
                          <div className="flex flex-col gap-1">
                            {res.creditNotes.map(cn => (
                              <span key={cn.creditnote_id} className="font-mono text-xs bg-slate-100 px-1.5 py-0.5 rounded">
                                {cn.creditnote_number}
                              </span>
                            ))}
                          </div>
                        ) : "-"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
