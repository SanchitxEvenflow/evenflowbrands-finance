const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

let cachedPromise: Promise<any> | null = null;

// ponytail: module-level singleton, resets on full page reload; good enough for prefetch-on-nav
export function prefetchPaymentOverdue() {
  if (!cachedPromise) {
    cachedPromise = fetch(`${API_BASE}/payment-overdue/summary`).then((res) => {
      if (!res.ok) throw new Error("Failed to fetch payment overdue summary");
      return res.json();
    });
    cachedPromise.catch(() => {
      cachedPromise = null;
    });
  }
  return cachedPromise;
}
