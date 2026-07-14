"use client";

import { useState, useEffect } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

interface LogEntry {
  grn_code: string;
  bill_number: string;
  bill_id: string;
  created_at: string;
}

interface BillGroup {
  bill_number: string;
  bill_id: string;
  created_at: string;
  grns: LogEntry[];
}

const Spinner = () => (
  <svg className="animate-spin h-4 w-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
  </svg>
);

function groupByBill(entries: LogEntry[]): BillGroup[] {
  const map = new Map<string, BillGroup>();
  for (const e of entries) {
    if (!map.has(e.bill_number)) {
      map.set(e.bill_number, {
        bill_number: e.bill_number,
        bill_id: e.bill_id,
        created_at: e.created_at,
        grns: [],
      });
    }
    map.get(e.bill_number)!.grns.push(e);
  }
  return Array.from(map.values()).sort((a, b) => b.created_at.localeCompare(a.created_at));
}

export default function HistoryPage() {
  const [bills, setBills] = useState<BillGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  useEffect(() => {
    fetch(`${API_BASE}/grn-push/log`)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: { entries: LogEntry[] }) => {
        setBills(groupByBill(data.entries));
        setLoading(false);
      })
      .catch(err => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  const toggle = (bill_number: string) => {
    setExpanded(prev => {
      const next = new Set(prev);
      next.has(bill_number) ? next.delete(bill_number) : next.add(bill_number);
      return next;
    });
  };

  return (
    <div className="w-full max-w-5xl mx-auto py-8 px-6">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-slate-800 tracking-tight">History</h1>
        <p className="text-slate-500 mt-2">All bills and GRNs logged under GRN push, grouped by bill number</p>
      </div>

      {loading && (
        <div className="flex items-center gap-3 text-slate-500 py-12 justify-center">
          <Spinner /> Loading log…
        </div>
      )}

      {error && (
        <div className="p-4 bg-red-50 border border-red-200 rounded-xl text-red-700 text-sm font-medium">
          Failed to load log: {error}
        </div>
      )}

      {!loading && !error && bills.length === 0 && (
        <div className="text-center py-12 text-slate-400">No entries in log yet. Run a GRN push first.</div>
      )}

      {!loading && !error && bills.length > 0 && (
        <div className="space-y-3">
          {/* Summary */}
          <div className="flex items-center gap-4 mb-4 text-sm text-slate-600">
            <span className="font-semibold">{bills.length} bills</span>
            <span>{bills.reduce((s, b) => s + b.grns.length, 0)} GRNs total</span>
          </div>

          {bills.map(group => {
            const isOpen = expanded.has(group.bill_number);

            return (
              <div key={group.bill_number} className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
                {/* Bill header row */}
                <button
                  onClick={() => toggle(group.bill_number)}
                  className="w-full flex items-center gap-4 px-5 py-4 text-left hover:bg-slate-50 transition"
                >
                  <span className="font-mono font-semibold text-slate-800 flex-1">{group.bill_number}</span>

                  <span className="text-xs text-slate-500">{group.grns.length} GRN{group.grns.length !== 1 ? "s" : ""}</span>

                  <span className="text-xs text-slate-400 font-mono">{group.created_at}</span>

                  {/* Chevron */}
                  <svg
                    className={`w-4 h-4 text-slate-400 transition-transform ${isOpen ? "rotate-180" : ""}`}
                    fill="none" stroke="currentColor" viewBox="0 0 24 24"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>

                {/* Expanded GRN rows */}
                {isOpen && (
                  <div className="border-t border-slate-100">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="bg-slate-50 text-left">
                          <th className="px-5 py-2 font-semibold text-slate-500 text-xs">GRN Code</th>
                          <th className="px-5 py-2 font-semibold text-slate-500 text-xs">Date</th>
                          <th className="px-5 py-2 font-semibold text-slate-500 text-xs">Bill ID</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-50">
                        {group.grns.map(entry => (
                          <tr key={entry.grn_code} className="hover:bg-slate-50">
                            <td className="px-5 py-2.5 font-mono text-slate-700">{entry.grn_code}</td>
                            <td className="px-5 py-2.5 text-xs text-slate-400 font-mono">{entry.created_at}</td>
                            <td className="px-5 py-2.5 text-xs text-slate-400 font-mono truncate max-w-xs">{entry.bill_id || "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
