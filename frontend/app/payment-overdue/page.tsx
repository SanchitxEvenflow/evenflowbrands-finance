"use client";

import { useEffect, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

interface Invoice {
  invoice_id: string;
  invoice_number: string;
  customer_id: string;
  customer_name: string;
  date: string;
  due_date: string | null;
  total: number;
  balance: number;
  status: string;
  currency_code: string;
  days_overdue: number | null;
}

interface KPI {
  count: number;
  amount: number;
}

interface OverdueSummary {
  invoices_checked: number;
  kpis: {
    past: KPI;
    this_week: KPI;
    future: KPI;
  };
  buckets: {
    past: Invoice[];
    this_week: Invoice[];
    future: Invoice[];
  };
}

type BucketType = "past" | "this_week" | "future";
type SortField = "due_date" | "balance" | null;
type SortDirection = "asc" | "desc";

export default function PaymentOverdue() {
  const [data, setData] = useState<OverdueSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedBucket, setSelectedBucket] = useState<BucketType>("past");
  const [sortField, setSortField] = useState<SortField>(null);
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const [searchQuery, setSearchQuery] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const fetchData = async (forceRefresh = false) => {
    try {
      forceRefresh ? setRefreshing(true) : setLoading(true);
      const res = await fetch(
        `${API_BASE}/payment-overdue/summary${forceRefresh ? "?force_refresh=true" : ""}`
      );
      if (!res.ok) {
        throw new Error("Failed to fetch payment overdue summary");
      }
      const json = await res.json();
      setData(json);
      setError(null);
    } catch (err: any) {
      setError(err.message || "An error occurred");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, []);

  const formatCurrency = (amount: number, currencyCode: string = "INR") => {
    return new Intl.NumberFormat("en-IN", {
      style: "currency",
      currency: currencyCode,
      maximumFractionDigits: 2,
    }).format(amount);
  };

  const getDaysOverdueLabel = (days: number | null) => {
    if (days === null) return "Unknown";
    if (days === 0) return "Due today";
    if (days > 0) return `Overdue by ${days} days`;
    return `Due in ${Math.abs(days)} days`;
  };

  const getDaysOverdueStyle = (days: number | null) => {
    if (days === null) return "text-slate-500";
    if (days > 0) return "text-red-600 font-medium";
    if (days === 0) return "text-amber-600 font-medium";
    return "text-emerald-600 font-medium";
  };

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortDirection("asc");
    }
  };

  const exportCSV = () => {
    if (!data) return;
    const currentList = data.buckets[selectedBucket] || [];
    if (currentList.length === 0) return;

    const headers = ["Customer", "Invoice Number", "Due Date", "Days Overdue", "Balance", "Currency"];
    const rows = currentList.map(inv => [
      `"${inv.customer_name?.replace(/"/g, '""') || ''}"`,
      inv.invoice_number,
      inv.due_date || "",
      inv.days_overdue ?? "",
      inv.balance,
      inv.currency_code
    ]);

    const csvContent = [headers.join(","), ...rows.map(r => r.join(","))].join("\n");
    const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.setAttribute("download", `payment_overdue_${selectedBucket}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  if (loading) {
    return (
      <div className="min-h-screen p-8 flex items-center justify-center">
        <div className="flex flex-col items-center gap-3 text-slate-500">
          <div className="spinner !border-slate-300 ![border-top-color:#6366f1] w-8 h-8" />
          <p>Loading payment data...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen p-8 flex items-center justify-center">
        <div className="bg-red-50 text-red-700 px-6 py-4 rounded-xl font-medium shadow-sm">
          Error: {error}
        </div>
      </div>
    );
  }

  if (!data) return null;

  const kpis = [
    {
      id: "past" as BucketType,
      title: "Past Due",
      data: data.kpis.past,
      color: "bg-red-50 border-red-200 text-red-900",
      activeColor: "ring-2 ring-red-500 ring-offset-2",
      iconColor: "text-red-500",
    },
    {
      id: "this_week" as BucketType,
      title: "Due This Week",
      data: data.kpis.this_week,
      color: "bg-amber-50 border-amber-200 text-amber-900",
      activeColor: "ring-2 ring-amber-500 ring-offset-2",
      iconColor: "text-amber-500",
    },
    {
      id: "future" as BucketType,
      title: "Due Future",
      data: data.kpis.future,
      color: "bg-emerald-50 border-emerald-200 text-emerald-900",
      activeColor: "ring-2 ring-emerald-500 ring-offset-2",
      iconColor: "text-emerald-500",
    },
  ];

  let currentList = data.buckets[selectedBucket] || [];

  if (searchQuery.trim()) {
    const q = searchQuery.toLowerCase();
    currentList = currentList.filter(inv => 
      inv.customer_name?.toLowerCase().includes(q) || 
      inv.invoice_number?.toLowerCase().includes(q)
    );
  }

  if (sortField) {
    currentList = [...currentList].sort((a, b) => {
      let aVal: any = a[sortField];
      let bVal: any = b[sortField];
      
      if (sortField === "due_date") {
        aVal = a.due_date ? new Date(a.due_date).getTime() : 0;
        bVal = b.due_date ? new Date(b.due_date).getTime() : 0;
      }

      if (aVal < bVal) return sortDirection === "asc" ? -1 : 1;
      if (aVal > bVal) return sortDirection === "asc" ? 1 : -1;
      return 0;
    });
  }

  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) {
      return (
        <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 inline ml-1 text-slate-300 group-hover:text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16V4m0 0L3 8m4-4l4 4m6 0v12m0 0l4-4m-4 4l-4-4" />
        </svg>
      );
    }
    return sortDirection === "asc" ? (
      <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 inline ml-1 text-indigo-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 15l7-7 7 7" />
      </svg>
    ) : (
      <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 inline ml-1 text-indigo-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
      </svg>
    );
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-indigo-50 flex flex-col items-center pb-16">
      <div className="w-full max-w-6xl px-4 py-8 mb-2 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-slate-800 tracking-tight">Payment Overdue</h1>
          <p className="text-slate-500 mt-2">
            Track outstanding payments across {data.invoices_checked.toLocaleString()} checked invoices.
          </p>
        </div>
        <button
          onClick={() => fetchData(true)}
          disabled={refreshing}
          className="flex items-center gap-2 px-4 py-2 mt-1 rounded-lg border border-slate-200 bg-white text-sm font-medium text-slate-600 hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed shrink-0 shadow-sm"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          {refreshing ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      <div className="w-full max-w-6xl px-4">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
          {kpis.map((kpi) => (
            <div
              key={kpi.id}
              onClick={() => { setSelectedBucket(kpi.id); setSortField(null); setSearchQuery(""); }}
              className={`cursor-pointer border rounded-2xl p-6 transition-all shadow-sm hover:shadow-md ${
                kpi.color
              } ${selectedBucket === kpi.id ? kpi.activeColor : "border-slate-200 bg-white"}`}
            >
              <div className="flex justify-between items-start mb-4">
                <h2 className="font-semibold text-lg">{kpi.title}</h2>
                <svg xmlns="http://www.w3.org/2000/svg" className={`h-6 w-6 ${kpi.iconColor}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
              <div className="mb-1 text-3xl font-bold">
                {formatCurrency(kpi.data.amount)}
              </div>
              <div className="text-sm opacity-80 font-medium">
                {kpi.data.count.toLocaleString()} invoices
              </div>
            </div>
          ))}
        </div>

        <div className="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden">
          <div className="px-6 py-4 border-b border-slate-100 bg-slate-50 flex items-center justify-between">
            <h2 className="font-semibold text-slate-800 text-lg flex items-center gap-2">
              {kpis.find((k) => k.id === selectedBucket)?.title} Invoices
              <span className="text-sm font-normal text-slate-500">
                ({currentList.length})
              </span>
            </h2>
            <div className="flex items-center gap-3">
              <input 
                type="text" 
                placeholder="Search customer..." 
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="px-4 py-2 border border-slate-200 rounded-lg text-sm outline-none focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 w-64"
              />
              <button 
                onClick={exportCSV}
                className="bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
              >
                Export CSV
              </button>
            </div>
          </div>
          
          {currentList.length === 0 ? (
            <div className="p-8 text-center text-slate-500">
              No invoices found.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm text-slate-600">
                <thead className="bg-slate-50 border-b border-slate-100 text-xs uppercase font-semibold text-slate-500">
                  <tr>
                    <th className="px-6 py-4 whitespace-nowrap">Customer</th>
                    <th className="px-6 py-4 whitespace-nowrap">Invoice Number</th>
                    <th 
                      className="px-6 py-4 whitespace-nowrap cursor-pointer hover:bg-slate-100 transition-colors group select-none"
                      onClick={() => handleSort("due_date")}
                    >
                      Due Date <SortIcon field="due_date" />
                    </th>
                    <th 
                      className="px-6 py-4 text-right whitespace-nowrap cursor-pointer hover:bg-slate-100 transition-colors group select-none"
                      onClick={() => handleSort("balance")}
                    >
                      Balance <SortIcon field="balance" />
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {currentList.map((inv) => (
                    <tr key={inv.invoice_id} className="hover:bg-slate-50 transition-colors">
                      <td className="px-6 py-4 font-medium text-slate-800">
                        {inv.customer_name}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        {inv.invoice_number}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        <div className={getDaysOverdueStyle(inv.days_overdue)}>
                          {getDaysOverdueLabel(inv.days_overdue)}
                        </div>
                        {inv.due_date && (
                          <div className="text-xs text-slate-400 mt-1">{inv.due_date}</div>
                        )}
                      </td>
                      <td className="px-6 py-4 text-right font-medium text-slate-800 whitespace-nowrap">
                        {formatCurrency(inv.balance, inv.currency_code)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
