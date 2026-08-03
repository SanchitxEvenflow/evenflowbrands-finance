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

export default function SwiggyCreditNote() {
  const [rawInput, setRawInput] = useState("");
  const [parsedNumbers, setParsedNumbers] = useState<string[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [results, setResults] = useState<ProcessResult[]>([]);
  
  const [toast, setToast] = useState<{ type: ToastType; msg: string } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = (type: ToastType, msg: string) => {
    setToast({ type, msg });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 4000);
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
    const updatedResults = [...initialResults];

    for (let i = 0; i < numbers.length; i++) {
      const po = numbers[i];
      updatedResults[i].status = "processing";
      setResults([...updatedResults]);

      try {
        const resp = await fetch(`${API_BASE}/instamart/process-po`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ po_number: po }),
        });
        
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
        <h1 className="text-3xl font-bold text-slate-800 tracking-tight">Create Credit Note - Swiggy Instamart</h1>
        <p className="text-slate-500 mt-2">Generate credit notes from Swiggy Instamart discrepancy notes in Zoho</p>
      </div>

      <div className="w-full max-w-4xl px-4">
        <div className="bg-white rounded-2xl shadow-sm border border-slate-200 overflow-hidden mb-6">
          
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
                placeholder={"KOCPO115501\nKOCPO115502\n...or paste a comma-separated list"}
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
