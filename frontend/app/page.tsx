"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import {
  Zap,
  ShoppingCart,
  Building2,
  Shield,
  Link2,
  CreditCard,
  ChevronRight,
  Cpu,
} from "lucide-react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export default function HomePage() {
  const router = useRouter();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // Form state
  const [form, setForm] = useState({
    procurement_request: "We need 100 units of premium steel bolts for our manufacturing line. Looking for competitive pricing with 30-day payment terms.",
    buyer_target_price: 80,
    buyer_max_budget: 12000,
    buyer_quantity: 100,
    buyer_max_rounds: 3,
    seller_list_price: 120,
    seller_floor_price: 90,
    seller_quantity: 100,
    seller_max_rounds: 3,
  });

  const updateField = (field: string, value: string | number) => {
    setForm((f) => ({ ...f, [field]: value }));
  };

  const handleStart = async () => {
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${API_BASE}/api/negotiate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      router.push(`/negotiate/${data.negotiation_id}`);
    } catch (err: any) {
      setError(err.message || "Failed to start negotiation");
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen mesh-gradient">
      {/* Header */}
      <header className="border-b border-white/5 glass sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-6 h-14 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-[#06d6a0] to-[#00b4d8] flex items-center justify-center">
              <Cpu className="w-4 h-4 text-[#060a10]" />
            </div>
            <span className="text-base font-semibold tracking-tight bg-gradient-to-r from-[#06d6a0] to-[#00b4d8] bg-clip-text text-transparent">
              AgentNegotiate
            </span>
          </div>
          <nav className="flex gap-6 text-sm text-gray-500">
            <a href="/" className="text-[#06d6a0] font-medium">Negotiate</a>
            <a href="/metrics" className="hover:text-gray-300 transition-colors">Metrics</a>
          </nav>
        </div>
      </header>

      {/* Hero Section */}
      <section className="max-w-7xl mx-auto px-6 pt-16 pb-10">
        <div className="text-center animate-fade-up">
          <h1 className="text-5xl font-bold tracking-tight mb-4">
            <span className="bg-gradient-to-r from-white via-gray-200 to-gray-400 bg-clip-text text-transparent">
              Autonomous B2B
            </span>
            <br />
            <span className="bg-gradient-to-r from-[#06d6a0] to-[#00b4d8] bg-clip-text text-transparent">
              Procurement Agents
            </span>
          </h1>
          <p className="text-gray-500 text-base max-w-2xl mx-auto leading-relaxed">
            Two autonomous agents negotiate the best deal — with deterministic
            financial guardrails, real-time streaming, and integrated payments.
          </p>
        </div>
      </section>

      {/* Form */}
      <section className="max-w-5xl mx-auto px-6 pb-20">
        <div className="grid md:grid-cols-2 gap-6">
          {/* Buyer Card */}
          <div className="glass rounded-2xl p-6 animate-slide-left scanline-overlay" style={{ animationDelay: "0.1s" }}>
            <div className="relative z-10">
              <div className="flex items-center gap-3 mb-6">
                <div className="w-10 h-10 rounded-xl bg-[#00b4d8]/10 border border-[#00b4d8]/20 flex items-center justify-center">
                  <ShoppingCart className="w-5 h-5 text-[#00b4d8]" />
                </div>
                <div>
                  <h2 className="text-base font-semibold text-[#00b4d8]">Buyer Agent</h2>
                  <p className="text-xs text-gray-600">Configure the buyer&apos;s mandate</p>
                </div>
              </div>

              <div className="space-y-4">
                <div>
                  <label className="block text-xs text-gray-500 mb-1.5 font-[family-name:var(--font-mono)] uppercase tracking-wider">Target Unit Price (₹)</label>
                  <input
                    id="buyer-target-price"
                    type="number"
                    value={form.buyer_target_price}
                    onChange={(e) => updateField("buyer_target_price", e.target.value === "" ? "" : Number(e.target.value))}
                    className="w-full px-4 py-2.5 rounded-xl bg-white/[0.03] border border-white/[0.06] text-white placeholder-gray-600 focus:border-[#00b4d8]/40 focus:outline-none focus:ring-1 focus:ring-[#00b4d8]/20 transition-all font-[family-name:var(--font-mono)]"
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-500 mb-1.5 font-[family-name:var(--font-mono)] uppercase tracking-wider">Max Budget (₹)</label>
                  <input
                    id="buyer-max-budget"
                    type="number"
                    value={form.buyer_max_budget}
                    onChange={(e) => updateField("buyer_max_budget", e.target.value === "" ? "" : Number(e.target.value))}
                    className="w-full px-4 py-2.5 rounded-xl bg-white/[0.03] border border-white/[0.06] text-white placeholder-gray-600 focus:border-[#00b4d8]/40 focus:outline-none focus:ring-1 focus:ring-[#00b4d8]/20 transition-all font-[family-name:var(--font-mono)]"
                  />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-gray-500 mb-1.5 font-[family-name:var(--font-mono)] uppercase tracking-wider">Quantity</label>
                    <input
                      id="buyer-quantity"
                      type="number"
                      value={form.buyer_quantity}
                      onChange={(e) => updateField("buyer_quantity", e.target.value === "" ? "" : Number(e.target.value))}
                      className="w-full px-4 py-2.5 rounded-xl bg-white/[0.03] border border-white/[0.06] text-white focus:border-[#00b4d8]/40 focus:outline-none focus:ring-1 focus:ring-[#00b4d8]/20 transition-all font-[family-name:var(--font-mono)]"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-500 mb-1.5 font-[family-name:var(--font-mono)] uppercase tracking-wider">Max Rounds</label>
                    <input
                      id="buyer-max-rounds"
                      type="number"
                      min={1}
                      max={10}
                      value={form.buyer_max_rounds}
                      onChange={(e) => updateField("buyer_max_rounds", e.target.value === "" ? "" : Number(e.target.value))}
                      className="w-full px-4 py-2.5 rounded-xl bg-white/[0.03] border border-white/[0.06] text-white focus:border-[#00b4d8]/40 focus:outline-none focus:ring-1 focus:ring-[#00b4d8]/20 transition-all font-[family-name:var(--font-mono)]"
                    />
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Seller Card */}
          <div className="glass rounded-2xl p-6 animate-slide-right scanline-overlay" style={{ animationDelay: "0.1s" }}>
            <div className="relative z-10">
              <div className="flex items-center gap-3 mb-6">
                <div className="w-10 h-10 rounded-xl bg-[#ff6b35]/10 border border-[#ff6b35]/20 flex items-center justify-center">
                  <Building2 className="w-5 h-5 text-[#ff6b35]" />
                </div>
                <div>
                  <h2 className="text-base font-semibold text-[#ff6b35]">Seller Agent</h2>
                  <p className="text-xs text-gray-600">Configure the seller&apos;s policy</p>
                </div>
              </div>

              <div className="space-y-4">
                <div>
                  <label className="block text-xs text-gray-500 mb-1.5 font-[family-name:var(--font-mono)] uppercase tracking-wider">List Price (₹)</label>
                  <input
                    id="seller-list-price"
                    type="number"
                    value={form.seller_list_price}
                    onChange={(e) => updateField("seller_list_price", e.target.value === "" ? "" : Number(e.target.value))}
                    className="w-full px-4 py-2.5 rounded-xl bg-white/[0.03] border border-white/[0.06] text-white placeholder-gray-600 focus:border-[#ff6b35]/40 focus:outline-none focus:ring-1 focus:ring-[#ff6b35]/20 transition-all font-[family-name:var(--font-mono)]"
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-500 mb-1.5 font-[family-name:var(--font-mono)] uppercase tracking-wider">Floor Price (₹)</label>
                  <input
                    id="seller-floor-price"
                    type="number"
                    value={form.seller_floor_price}
                    onChange={(e) => updateField("seller_floor_price", e.target.value === "" ? "" : Number(e.target.value))}
                    className="w-full px-4 py-2.5 rounded-xl bg-white/[0.03] border border-white/[0.06] text-white placeholder-gray-600 focus:border-[#ff6b35]/40 focus:outline-none focus:ring-1 focus:ring-[#ff6b35]/20 transition-all font-[family-name:var(--font-mono)]"
                  />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-gray-500 mb-1.5 font-[family-name:var(--font-mono)] uppercase tracking-wider">Quantity</label>
                    <input
                      id="seller-quantity"
                      type="number"
                      value={form.seller_quantity}
                      onChange={(e) => updateField("seller_quantity", e.target.value === "" ? "" : Number(e.target.value))}
                      className="w-full px-4 py-2.5 rounded-xl bg-white/[0.03] border border-white/[0.06] text-white focus:border-[#ff6b35]/40 focus:outline-none focus:ring-1 focus:ring-[#ff6b35]/20 transition-all font-[family-name:var(--font-mono)]"
                    />
                  </div>
                  <div>
                    <label className="block text-xs text-gray-500 mb-1.5 font-[family-name:var(--font-mono)] uppercase tracking-wider">Max Rounds</label>
                    <input
                      id="seller-max-rounds"
                      type="number"
                      min={1}
                      max={10}
                      value={form.seller_max_rounds}
                      onChange={(e) => updateField("seller_max_rounds", e.target.value === "" ? "" : Number(e.target.value))}
                      className="w-full px-4 py-2.5 rounded-xl bg-white/[0.03] border border-white/[0.06] text-white focus:border-[#ff6b35]/40 focus:outline-none focus:ring-1 focus:ring-[#ff6b35]/20 transition-all font-[family-name:var(--font-mono)]"
                    />
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Procurement Request */}
        <div className="mt-6 glass rounded-2xl p-6 animate-fade-up" style={{ animationDelay: "0.2s" }}>
          <label className="block text-xs text-gray-500 mb-2 font-[family-name:var(--font-mono)] uppercase tracking-wider">Procurement Request (optional — parsed by AI)</label>
          <textarea
            id="procurement-request"
            rows={3}
            value={form.procurement_request}
            onChange={(e) => updateField("procurement_request", e.target.value)}
            className="w-full px-4 py-3 rounded-xl bg-white/[0.03] border border-white/[0.06] text-white placeholder-gray-600 focus:border-[#06d6a0]/40 focus:outline-none focus:ring-1 focus:ring-[#06d6a0]/20 transition-all resize-none"
            placeholder="Describe your procurement needs in natural language..."
          />
        </div>

        {/* Error */}
        {error && (
          <div className="mt-4 px-4 py-3 rounded-xl bg-[#ef233c]/10 border border-[#ef233c]/20 text-[#ef233c] text-sm flex items-center gap-2">
            <Shield className="w-4 h-4 shrink-0" />
            {error}
          </div>
        )}

        {/* Start Button */}
        <div className="mt-8 flex justify-center animate-fade-up" style={{ animationDelay: "0.3s" }}>
          <button
            id="start-negotiation-btn"
            onClick={handleStart}
            disabled={loading}
            className="group relative px-10 py-4 rounded-2xl bg-gradient-to-r from-[#06d6a0] to-[#00b4d8] text-[#060a10] font-bold text-base shadow-lg shadow-[#06d6a0]/15 hover:shadow-[#06d6a0]/30 hover:scale-[1.02] active:scale-[0.98] transition-all disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
          >
            <span className="relative z-10 flex items-center gap-3">
              {loading ? (
                <>
                  <svg className="w-5 h-5 animate-spin" viewBox="0 0 24 24" fill="none">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  Initializing Agents…
                </>
              ) : (
                <>
                  <Zap className="w-5 h-5" />
                  Deploy Negotiation
                </>
              )}
            </span>
          </button>
        </div>

        {/* Feature Cards */}
        <div className="mt-16 grid md:grid-cols-3 gap-4">
          {[
            {
              Icon: Shield,
              title: "Deterministic Guardrails",
              desc: "Budget & floor constraints enforced by arithmetic — never by the LLM.",
              accent: "text-[#06d6a0]",
              border: "hover:border-[#06d6a0]/20",
              bg: "bg-[#06d6a0]/5",
            },
            {
              Icon: Link2,
              title: "Hash-Chained Audit",
              desc: "Every decision is tamper-evident with SHA-256 chained logging.",
              accent: "text-[#00b4d8]",
              border: "hover:border-[#00b4d8]/20",
              bg: "bg-[#00b4d8]/5",
            },
            {
              Icon: CreditCard,
              title: "Razorpay Payments",
              desc: "Accepted deals generate instant payment links via Razorpay.",
              accent: "text-[#9b5de5]",
              border: "hover:border-[#9b5de5]/20",
              bg: "bg-[#9b5de5]/5",
            },
          ].map((f, i) => (
            <div
              key={i}
              className={`glass rounded-2xl p-5 ${f.border} transition-all animate-fade-up group`}
              style={{ animationDelay: `${0.4 + i * 0.1}s` }}
            >
              <div className={`w-9 h-9 rounded-lg ${f.bg} flex items-center justify-center mb-3`}>
                <f.Icon className={`w-4.5 h-4.5 ${f.accent}`} />
              </div>
              <h3 className="text-sm font-semibold text-white">{f.title}</h3>
              <p className="mt-1.5 text-xs text-gray-500 leading-relaxed">{f.desc}</p>
              <div className={`mt-3 flex items-center gap-1 text-xs ${f.accent} opacity-0 group-hover:opacity-100 transition-opacity`}>
                <span>Learn more</span>
                <ChevronRight className="w-3 h-3" />
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
