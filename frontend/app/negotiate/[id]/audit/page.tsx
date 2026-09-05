"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import AuditChain from "@/components/AuditChain";
import { Cpu, ArrowLeft, Link2 } from "lucide-react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface AuditEntry {
  id: number;
  negotiation_id: string;
  event_type: string;
  data: string;
  timestamp: string;
  prev_hash: string;
  hash: string;
  valid?: boolean;
}

export default function AuditPage() {
  const params = useParams();
  const router = useRouter();
  const id = params.id as string;

  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [chainValid, setChainValid] = useState(true);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API_BASE}/api/negotiations/${id}/audit`)
      .then((r) => r.json())
      .then((data) => {
        setEntries(data.entries || []);
        setChainValid(data.chain_valid ?? true);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [id]);

  return (
    <div className="min-h-screen mesh-gradient">
      {/* Header */}
      <header className="border-b border-white/5 glass sticky top-0 z-50">
        <div className="max-w-4xl mx-auto px-6 h-14 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button onClick={() => router.push(`/negotiate/${id}`)} className="flex items-center gap-3 hover:opacity-80 transition-opacity">
              <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-[#06d6a0] to-[#00b4d8] flex items-center justify-center">
                <Cpu className="w-4 h-4 text-[#060a10]" />
              </div>
              <span className="text-base font-semibold tracking-tight bg-gradient-to-r from-[#06d6a0] to-[#00b4d8] bg-clip-text text-transparent">
                AgentNegotiate
              </span>
            </button>
          </div>
          <button
            onClick={() => router.push(`/negotiate/${id}`)}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg glass hover:border-[#06d6a0]/20 transition-all text-gray-400 hover:text-white"
          >
            <ArrowLeft className="w-3.5 h-3.5" />
            Back to Negotiation
          </button>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-6 py-8">
        <div className="mb-8 animate-fade-up">
          <h1 className="text-2xl font-bold text-white flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-[#00b4d8]/10 border border-[#00b4d8]/20 flex items-center justify-center">
              <Link2 className="w-4.5 h-4.5 text-[#00b4d8]" />
            </div>
            Audit Trail
          </h1>
          <p className="text-sm text-gray-600 mt-2 font-[family-name:var(--font-mono)]">
            {id}
          </p>
          <p className="text-xs text-gray-500 mt-2">
            Every offer, decision, and payment event is cryptographically chained with SHA-256
            to ensure tamper-evidence.
          </p>
        </div>

        {loading ? (
          <div className="glass rounded-xl p-12 text-center text-gray-600 animate-pulse">
            Loading audit trail…
          </div>
        ) : entries.length === 0 ? (
          <div className="glass rounded-xl p-12 text-center text-gray-600">
            No audit entries yet. The negotiation may still be in progress.
          </div>
        ) : (
          <AuditChain entries={entries} chainValid={chainValid} />
        )}
      </main>
    </div>
  );
}
