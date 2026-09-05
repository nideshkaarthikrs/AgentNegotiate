"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Cpu,
  BarChart3,
  CheckCircle2,
  XCircle,
  RefreshCw,
  CreditCard,
  ArrowRight,
  Activity,
} from "lucide-react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface Negotiation {
  negotiation_id: string;
  status: string;
  current_round: number;
  final_price: number | null;
  final_quantity: number | null;
  created_at: string;
  payment_status: string | null;
}

export default function MetricsPage() {
  const router = useRouter();
  const [negotiations, setNegotiations] = useState<Negotiation[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API_BASE}/api/negotiations`)
      .then((r) => r.json())
      .then((data) => {
        setNegotiations(data.negotiations || []);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  const total = negotiations.length;
  const accepted = negotiations.filter((n) => n.status === "accepted").length;
  const rejected = negotiations.filter((n) => n.status === "rejected").length;
  const active = negotiations.filter((n) => n.status === "active").length;
  const avgRounds =
    total > 0
      ? (negotiations.reduce((s, n) => s + n.current_round, 0) / total).toFixed(1)
      : "0";
  const paidCount = negotiations.filter((n) => n.payment_status === "captured").length;

  const statusColors: Record<string, string> = {
    active: "bg-[#06d6a0]/10 text-[#06d6a0] border-[#06d6a0]/20",
    accepted: "bg-[#06d6a0]/10 text-[#06d6a0] border-[#06d6a0]/20",
    rejected: "bg-[#ef233c]/10 text-[#ef233c] border-[#ef233c]/20",
    expired: "bg-[#ff6b35]/10 text-[#ff6b35] border-[#ff6b35]/20",
  };

  const metricCards = [
    { label: "Total", value: total, Icon: Activity, gradient: "from-[#06d6a0]/10 to-[#00b4d8]/10", accent: "text-[#06d6a0]" },
    { label: "Accepted", value: accepted, Icon: CheckCircle2, gradient: "from-[#06d6a0]/10 to-[#06d6a0]/5", accent: "text-[#06d6a0]" },
    { label: "Rejected", value: rejected, Icon: XCircle, gradient: "from-[#ef233c]/10 to-[#ef233c]/5", accent: "text-[#ef233c]" },
    { label: "Avg Rounds", value: avgRounds, Icon: RefreshCw, gradient: "from-[#00b4d8]/10 to-[#00b4d8]/5", accent: "text-[#00b4d8]" },
    { label: "Paid", value: paidCount, Icon: CreditCard, gradient: "from-[#ff6b35]/10 to-[#ff6b35]/5", accent: "text-[#ff6b35]" },
  ];

  return (
    <div className="min-h-screen mesh-gradient">
      {/* Header */}
      <header className="border-b border-white/5 glass sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-6 h-14 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button onClick={() => router.push("/")} className="flex items-center gap-3 hover:opacity-80 transition-opacity">
              <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-[#06d6a0] to-[#00b4d8] flex items-center justify-center">
                <Cpu className="w-4 h-4 text-[#060a10]" />
              </div>
              <span className="text-base font-semibold tracking-tight bg-gradient-to-r from-[#06d6a0] to-[#00b4d8] bg-clip-text text-transparent">
                AgentNegotiate
              </span>
            </button>
          </div>
          <nav className="flex gap-6 text-sm text-gray-500">
            <a href="/" className="hover:text-gray-300 transition-colors">Negotiate</a>
            <a href="/metrics" className="text-[#06d6a0] font-medium">Metrics</a>
          </nav>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-8">
        <div className="mb-8 animate-fade-up">
          <h1 className="text-2xl font-bold text-white flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-[#9b5de5]/10 border border-[#9b5de5]/20 flex items-center justify-center">
              <BarChart3 className="w-4.5 h-4.5 text-[#9b5de5]" />
            </div>
            Negotiation Metrics
          </h1>
          <p className="text-sm text-gray-500 mt-2">
            Overview of all negotiations processed by the system.
          </p>
        </div>

        {/* Metric Cards */}
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-8">
          {metricCards.map((m, i) => (
            <div
              key={i}
              className={`glass rounded-2xl p-5 animate-fade-up bg-gradient-to-br ${m.gradient}`}
              style={{ animationDelay: `${i * 0.1}s` }}
            >
              <div className={`w-8 h-8 rounded-lg ${m.accent} bg-white/[0.03] flex items-center justify-center mb-3`}>
                <m.Icon className="w-4 h-4" />
              </div>
              <div className="text-3xl font-bold text-white font-[family-name:var(--font-mono)]">{m.value}</div>
              <div className="text-xs text-gray-500 mt-1 font-[family-name:var(--font-mono)] uppercase tracking-wider">{m.label}</div>
            </div>
          ))}
        </div>

        {/* Negotiations Table */}
        <div className="glass rounded-2xl overflow-hidden animate-fade-up" style={{ animationDelay: "0.5s" }}>
          <div className="px-6 py-4 border-b border-white/5">
            <h2 className="text-sm font-semibold text-white">All Negotiations</h2>
          </div>

          {loading ? (
            <div className="p-12 text-center text-gray-600 animate-pulse">Loading…</div>
          ) : negotiations.length === 0 ? (
            <div className="p-12 text-center text-gray-600">
              No negotiations yet.{" "}
              <a href="/" className="text-[#06d6a0] hover:underline">
                Start one
              </a>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-gray-600 uppercase tracking-wider font-[family-name:var(--font-mono)]">
                    <th className="text-left px-6 py-3">ID</th>
                    <th className="text-left px-6 py-3">Status</th>
                    <th className="text-right px-6 py-3">Rounds</th>
                    <th className="text-right px-6 py-3">Final Price</th>
                    <th className="text-right px-6 py-3">Payment</th>
                    <th className="text-right px-6 py-3">Created</th>
                    <th className="text-right px-6 py-3"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/5">
                  {negotiations.map((n) => (
                    <tr key={n.negotiation_id} className="hover:bg-white/[0.02] transition-colors">
                      <td className="px-6 py-3 font-[family-name:var(--font-mono)] text-xs text-gray-500">
                        {n.negotiation_id.slice(0, 8)}…
                      </td>
                      <td className="px-6 py-3">
                        <span className={`text-xs px-2.5 py-1 rounded-full border ${statusColors[n.status] || "text-gray-500"}`}>
                          {n.status}
                        </span>
                      </td>
                      <td className="px-6 py-3 text-right text-gray-400 font-[family-name:var(--font-mono)]">{n.current_round}</td>
                      <td className="px-6 py-3 text-right text-white font-medium font-[family-name:var(--font-mono)]">
                        {n.final_price ? `₹${n.final_price.toFixed(2)}` : "—"}
                      </td>
                      <td className="px-6 py-3 text-right">
                        {n.payment_status === "captured" ? (
                          <span className="flex items-center gap-1 justify-end text-[#06d6a0] text-xs">
                            <CheckCircle2 className="w-3 h-3" />
                            Paid
                          </span>
                        ) : n.payment_status ? (
                          <span className="text-[#ff6b35] text-xs">{n.payment_status}</span>
                        ) : (
                          <span className="text-gray-600 text-xs">—</span>
                        )}
                      </td>
                      <td className="px-6 py-3 text-right text-xs text-gray-600 font-[family-name:var(--font-mono)]">
                        {new Date(n.created_at).toLocaleDateString()}
                      </td>
                      <td className="px-6 py-3 text-right">
                        <button
                          onClick={() => router.push(`/negotiate/${n.negotiation_id}`)}
                          className="flex items-center gap-1 text-xs text-[#06d6a0] hover:text-[#06d6a0]/80 transition-colors ml-auto"
                        >
                          View
                          <ArrowRight className="w-3 h-3" />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
