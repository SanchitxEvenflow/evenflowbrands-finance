"use client";

import { useState, useRef, useEffect } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

const ENTITIES = [
  { name: "Evenflow Brands Tech Private Limited", orgId: "60009401567" },
  { name: "Everlong Brands Private Limited", orgId: "60014528875" },
  { name: "Fourth Second Private Limited", orgId: "60014753441" },
  { name: "Pepmart Brands International Private Limited", orgId: "60014871731" },
];

interface SectionRow {
  tds_section: string;
  tds_section_description: string;
  section_code: string;
  tds_bcyamount: number;
}

interface EntityResult {
  organization_id: string;
  entity_name: string;
  sections: SectionRow[];
  total_tds: number;
}

interface SummaryResponse {
  entities: EntityResult[];
  consolidated: { sections: SectionRow[]; total_tds: number };
}

const toISODate = (date: Date) => date.toISOString().slice(0, 10);

const formatINR = (n: number) =>
  `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

export default function TdsFetcherPage() {
  const [selectedOrgIds, setSelectedOrgIds] = useState<string[]>(ENTITIES.map((e) => e.orgId));
  const [startDate, setStartDate] = useState(toISODate(new Date(Date.now() - 30 * 24 * 60 * 60 * 1000)));
  const [endDate, setEndDate] = useState(toISODate(new Date()));

  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [result, setResult] = useState<SummaryResponse | null>(null);

  const [dropdownOpen, setDropdownOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const allSelected = selectedOrgIds.length === ENTITIES.length;

  const toggleEntity = (orgId: string) => {
    setSelectedOrgIds((prev) =>
      prev.includes(orgId) ? prev.filter((id) => id !== orgId) : [...prev, orgId]
    );
  };

  const toggleAll = () => {
    setSelectedOrgIds(allSelected ? [] : ENTITIES.map((e) => e.orgId));
  };

  const buildBody = () => ({
    organization_ids: selectedOrgIds,
    from_date: startDate,
    to_date: endDate,
  });

  const fetchSummary = async () => {
    if (selectedOrgIds.length === 0) {
      setErrorMsg("Select at least one entity");
      return;
    }
    setLoading(true);
    setErrorMsg(null);
    setResult(null);
    try {
      const res = await fetch(`${API_BASE}/tds/summary`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildBody()),
      });
      if (!res.ok) throw new Error(`Fetch failed: ${res.statusText}`);
      const data: SummaryResponse = await res.json();
      setResult(data);
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : "An unexpected error occurred");
    } finally {
      setLoading(false);
    }
  };

  const downloadCsv = async () => {
    if (selectedOrgIds.length === 0) {
      setErrorMsg("Select at least one entity");
      return;
    }
    setDownloading(true);
    setErrorMsg(null);
    try {
      const res = await fetch(`${API_BASE}/tds/csv`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildBody()),
      });
      if (!res.ok) throw new Error(`Download failed: ${res.statusText}`);
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `tds_summary_${startDate}_to_${endDate}.csv`;
      a.click();
      window.URL.revokeObjectURL(url);
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : "An unexpected error occurred");
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="w-full max-w-5xl mx-auto py-8 px-6">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-slate-800 tracking-tight">TDS Fetcher</h1>
        <p className="text-slate-500 mt-2">Consolidated TDS payable summary across entities</p>
      </div>

      <div className="bg-white rounded-2xl shadow-sm border border-slate-200 mb-8">
        <div className="p-6 border-b border-slate-100 bg-slate-50 rounded-t-2xl">
          <div className="flex flex-col lg:flex-row lg:items-start justify-between gap-4 relative z-10">
            <div className="flex items-end gap-4 flex-wrap">
              <div className="relative" ref={dropdownRef}>
                <label className="block text-sm font-semibold text-slate-700 mb-2">Entities</label>
                <button
                  type="button"
                  onClick={() => setDropdownOpen(!dropdownOpen)}
                  disabled={loading || downloading}
                  className="flex items-center justify-between w-72 px-4 py-2 bg-white border border-slate-300 rounded-xl outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50 text-slate-700 shadow-sm transition-all text-left"
                >
                  <span className="truncate pr-4 text-sm">
                    {selectedOrgIds.length === ENTITIES.length
                      ? "All Entities"
                      : selectedOrgIds.length === 0
                      ? "Select Entities"
                      : selectedOrgIds.length === 1
                      ? ENTITIES.find((e) => e.orgId === selectedOrgIds[0])?.name
                      : `${selectedOrgIds.length} Entities Selected`}
                  </span>
                  <svg className={`w-4 h-4 text-slate-400 transition-transform ${dropdownOpen ? "rotate-180" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>

                {dropdownOpen && (
                  <div className="absolute top-full left-0 mt-2 w-72 bg-white border border-slate-200 rounded-xl shadow-lg py-2 z-50 max-h-64 overflow-y-auto">
                    <label className="flex items-center px-4 py-2 hover:bg-slate-50 cursor-pointer transition-colors border-b border-slate-100">
                      <input
                        type="checkbox"
                        checked={allSelected}
                        onChange={toggleAll}
                        className="rounded text-indigo-600 focus:ring-indigo-500 mr-3 h-4 w-4"
                      />
                      <span className="text-sm font-semibold text-slate-700">Select All</span>
                    </label>
                    {ENTITIES.map((entity) => (
                      <label key={entity.orgId} className="flex items-center px-4 py-2 hover:bg-slate-50 cursor-pointer transition-colors">
                        <input
                          type="checkbox"
                          checked={selectedOrgIds.includes(entity.orgId)}
                          onChange={() => toggleEntity(entity.orgId)}
                          className="rounded text-indigo-600 focus:ring-indigo-500 mr-3 h-4 w-4"
                        />
                        <span className="text-sm text-slate-700">{entity.name}</span>
                      </label>
                    ))}
                  </div>
                )}
              </div>

              <div>
                <label className="block text-sm font-semibold text-slate-700 mb-2">Start Date</label>
                <input
                  type="date"
                  value={startDate}
                  onChange={(e) => setStartDate(e.target.value)}
                  disabled={loading || downloading}
                  className="px-4 py-2 border border-slate-300 rounded-xl outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50 text-slate-700 w-44 text-center font-mono tracking-wider"
                />
              </div>
              <div>
                <label className="block text-sm font-semibold text-slate-700 mb-2">End Date</label>
                <input
                  type="date"
                  value={endDate}
                  onChange={(e) => setEndDate(e.target.value)}
                  disabled={loading || downloading}
                  className="px-4 py-2 border border-slate-300 rounded-xl outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50 text-slate-700 w-44 text-center font-mono tracking-wider"
                />
              </div>
            </div>

            <div className="flex flex-col gap-3 lg:items-end">
              <button
                onClick={downloadCsv}
                disabled={loading || downloading}
                className="px-6 py-2.5 bg-slate-800 hover:bg-slate-900 text-white font-semibold rounded-xl transition-all disabled:opacity-50 shadow-md hover:shadow-lg whitespace-nowrap lg:mt-7"
              >
                {downloading ? "Downloading..." : "⬇ Download CSV"}
              </button>
              <button
                onClick={fetchSummary}
                disabled={loading || downloading}
                className="px-6 py-2.5 bg-indigo-600 hover:bg-indigo-700 text-white font-semibold rounded-xl transition-all disabled:opacity-50 shadow-md hover:shadow-lg whitespace-nowrap"
              >
                {loading ? "Fetching..." : "Fetch Summary"}
              </button>
            </div>
          </div>
        </div>

        {errorMsg && (
          <div className="mx-6 mt-6 p-4 bg-red-50 text-red-700 rounded-xl border border-red-100 flex items-center gap-3">
            <span className="text-xl">⚠️</span>
            <span className="font-medium">{errorMsg}</span>
          </div>
        )}

        {result && (
          <div className="p-6 flex flex-col gap-8">
            <div>
              <h2 className="text-lg font-bold text-slate-800 mb-3">Consolidated Summary</h2>
              <div className="overflow-x-auto rounded-xl border border-slate-200 shadow-sm">
                <table className="w-full text-sm text-left">
                  <thead className="bg-slate-50 text-slate-600 font-semibold uppercase text-xs">
                    <tr>
                      <th className="px-4 py-3 border-b border-slate-200">TDS Section</th>
                      <th className="px-4 py-3 text-right border-b border-slate-200">TDS Amount</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 text-slate-800">
                    {result.consolidated.sections.map((s) => (
                      <tr key={s.tds_section}>
                        <td className="px-4 py-3">
                          <div className="font-semibold text-indigo-700">{s.section_code}</div>
                          <div className="text-xs text-slate-500">{s.tds_section_description}</div>
                        </td>
                        <td className="px-4 py-3 text-right font-medium">{formatINR(s.tds_bcyamount)}</td>
                      </tr>
                    ))}
                  </tbody>
                  <tfoot>
                    <tr className="bg-slate-900 text-white font-semibold">
                      <td className="px-4 py-3">Total</td>
                      <td className="px-4 py-3 text-right">{formatINR(result.consolidated.total_tds)}</td>
                    </tr>
                  </tfoot>
                </table>
              </div>
            </div>

            <div>
              <h2 className="text-lg font-bold text-slate-800 mb-3">Entity-wise Summary</h2>
              <div className="flex flex-col gap-4">
                {result.entities.map((entity) => (
                  <div key={entity.organization_id} className="overflow-x-auto rounded-xl border border-slate-200 shadow-sm">
                    <div className="px-4 py-2 bg-indigo-50 text-indigo-800 font-semibold text-sm border-b border-indigo-100">
                      {entity.entity_name}
                    </div>
                    <table className="w-full text-sm text-left">
                      <thead className="bg-slate-50 text-slate-600 font-semibold uppercase text-xs">
                        <tr>
                          <th className="px-4 py-3 border-b border-slate-200">TDS Section</th>
                          <th className="px-4 py-3 text-right border-b border-slate-200">TDS Amount</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-100 text-slate-800">
                        {entity.sections.map((s) => (
                          <tr key={s.tds_section}>
                            <td className="px-4 py-3">
                          <div className="font-semibold text-indigo-700">{s.section_code}</div>
                          <div className="text-xs text-slate-500">{s.tds_section_description}</div>
                        </td>
                            <td className="px-4 py-3 text-right font-medium">{formatINR(s.tds_bcyamount)}</td>
                          </tr>
                        ))}
                      </tbody>
                      <tfoot>
                        <tr className="bg-slate-100 text-slate-800 font-semibold">
                          <td className="px-4 py-3">Subtotal</td>
                          <td className="px-4 py-3 text-right">{formatINR(entity.total_tds)}</td>
                        </tr>
                      </tfoot>
                    </table>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
