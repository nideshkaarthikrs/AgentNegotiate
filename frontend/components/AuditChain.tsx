"use client";

import {
  Rocket,
  Heart,
  Flame,
  CheckCircle2,
  XCircle,
  Clock,
  CreditCard,
  PartyPopper,
  AlertTriangle,
  FileText,
  ShieldCheck,
  ShieldX,
} from "lucide-react";

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

interface AuditChainProps {
  entries: AuditEntry[];
  chainValid: boolean;
}

const eventIconMap: Record<string, React.ComponentType<{className?: string}>> = {
  negotiation_started: Rocket,
  buyer_offer: Heart,
  seller_offer: Flame,
  buyer_accept: CheckCircle2,
  seller_accept: CheckCircle2,
  buyer_reject: XCircle,
  seller_reject: XCircle,
  mandate_expired: Clock,
  payment_created: CreditCard,
  payment_success: PartyPopper,
  payment_failed: AlertTriangle,
};

export default function AuditChain({ entries, chainValid }: AuditChainProps) {
  return (
    <div>
      {/* Chain Status */}
      <div className={`mb-6 px-4 py-3 rounded-xl border flex items-center gap-3 ${
        chainValid
          ? "bg-[#06d6a0]/5 border-[#06d6a0]/20 text-[#06d6a0]"
          : "bg-[#ef233c]/5 border-[#ef233c]/20 text-[#ef233c]"
      }`}>
        {chainValid ? (
          <ShieldCheck className="w-5 h-5 shrink-0" />
        ) : (
          <ShieldX className="w-5 h-5 shrink-0" />
        )}
        <span className="text-sm font-medium">
          {chainValid ? "Audit Chain Verified — All hashes are valid" : "Chain Integrity Broken — Tampering Detected"}
        </span>
      </div>

      {/* Entries */}
      <div className="space-y-3">
        {entries.map((entry, i) => {
          const IconComponent = eventIconMap[entry.event_type] || FileText;
          return (
            <div
              key={entry.id}
              className={`glass rounded-xl p-4 animate-fade-up border ${
                entry.valid === false ? "border-[#ef233c]/30" : "border-white/5"
              }`}
              style={{ animationDelay: `${i * 0.05}s` }}
            >
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                  <IconComponent className="w-4 h-4 text-gray-400" />
                  <span className="text-sm font-medium text-white">
                    {entry.event_type.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  {entry.valid !== undefined && (
                    <span className={`text-xs px-2 py-0.5 rounded-full ${
                      entry.valid
                        ? "bg-[#06d6a0]/10 text-[#06d6a0]"
                        : "bg-[#ef233c]/10 text-[#ef233c]"
                    }`}>
                      {entry.valid ? "Valid" : "Invalid"}
                    </span>
                  )}
                  <span className="text-xs text-gray-600 font-[family-name:var(--font-mono)]">
                    {new Date(entry.timestamp).toLocaleTimeString()}
                  </span>
                </div>
              </div>

              {/* Hash Display */}
              <div className="mt-2 space-y-1">
                <div className="flex items-center gap-2">
                  <span className="text-[10px] text-gray-600 w-16 shrink-0 font-[family-name:var(--font-mono)] uppercase">Prev</span>
                  <code className="text-[10px] text-gray-600 font-[family-name:var(--font-mono)] truncate">
                    {entry.prev_hash === "GENESIS" ? "GENESIS" : entry.prev_hash.slice(0, 32) + "…"}
                  </code>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[10px] text-[#06d6a0] w-16 shrink-0 font-medium font-[family-name:var(--font-mono)] uppercase">Hash</span>
                  <code className="text-[10px] text-[#06d6a0]/80 font-[family-name:var(--font-mono)] truncate">
                    {entry.hash.slice(0, 32)}…
                  </code>
                </div>
              </div>

              {/* Chain link visual */}
              {i < entries.length - 1 && (
                <div className="flex justify-center mt-2">
                  <div className={`w-px h-4 ${entry.valid === false ? "bg-[#ef233c]/40" : "bg-[#06d6a0]/20"}`} />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
