"use client";

import {
  ArrowLeftRight,
  CheckCircle2,
  XCircle,
  Rocket,
  Trophy,
  Ban,
  MessageSquare,
} from "lucide-react";

interface OfferCardProps {
  role: "buyer" | "seller" | "system";
  decision: string;
  price?: number | null;
  quantity?: number | null;
  explanation: string;
  roundNumber: number;
  index: number;
}

const decisionIconMap: Record<string, React.ComponentType<{className?: string}>> = {
  counter: ArrowLeftRight,
  accept: CheckCircle2,
  reject: XCircle,
  started: Rocket,
  accepted: Trophy,
  rejected: Ban,
};

export default function OfferCard({
  role,
  decision,
  price,
  quantity,
  explanation,
  roundNumber,
  index,
}: OfferCardProps) {
  const isBuyer = role === "buyer";
  const isSystem = role === "system";

  const colorMap = {
    buyer: {
      bg: "bg-[#00b4d8]/[0.04]",
      border: "border-[#00b4d8]/15",
      accent: "text-[#00b4d8]",
      badge: "bg-[#00b4d8]/10 text-[#00b4d8] border-[#00b4d8]/20",
      dot: "bg-[#00b4d8]",
    },
    seller: {
      bg: "bg-[#ff6b35]/[0.04]",
      border: "border-[#ff6b35]/15",
      accent: "text-[#ff6b35]",
      badge: "bg-[#ff6b35]/10 text-[#ff6b35] border-[#ff6b35]/20",
      dot: "bg-[#ff6b35]",
    },
    system: {
      bg: "bg-[#9b5de5]/[0.04]",
      border: "border-[#9b5de5]/15",
      accent: "text-[#9b5de5]",
      badge: "bg-[#9b5de5]/10 text-[#9b5de5] border-[#9b5de5]/20",
      dot: "bg-[#9b5de5]",
    },
  };

  const colors = colorMap[role];
  const animClass = isBuyer ? "animate-slide-left" : "animate-slide-right";

  const DecisionIcon = decisionIconMap[decision] || MessageSquare;

  const isInitialOffer = decision === "counter" && roundNumber === 1 && isBuyer;

  const decisionLabel = {
    counter: isInitialOffer ? "Initial Offer" : "Counter Offer",
    accept: "Accepted",
    reject: "Rejected",
    started: "Started",
    accepted: "Deal Closed",
    rejected: "No Deal",
  }[decision] || decision;

  return (
    <div
      className={`${colors.bg} border ${colors.border} rounded-2xl p-5 ${animClass}`}
      style={{ animationDelay: `${index * 0.15}s` }}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className={`w-1.5 h-1.5 rounded-full ${colors.dot}`} />
          <span className={`text-xs font-medium uppercase tracking-wider ${colors.accent} font-[family-name:var(--font-mono)]`}>
            {isSystem ? "System" : isBuyer ? "Buyer" : "Seller"}
          </span>
          {roundNumber > 0 && (
            <span className="text-xs text-gray-600 font-[family-name:var(--font-mono)]">R{roundNumber}</span>
          )}
        </div>
        <span className={`flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border ${colors.badge}`}>
          <DecisionIcon className="w-3 h-3" />
          {decisionLabel}
        </span>
      </div>

      {/* Render ₹0 too — a poisoned or zero opening bid must be visible, not
          silently blank. Hiding it is part of why the ₹0.01 bug went unnoticed. */}
      {price != null && (
        <div className="mb-3">
          <span className="text-2xl font-bold text-white font-[family-name:var(--font-mono)]">₹{price.toFixed(2)}</span>
          {quantity != null && quantity > 0 && (
            <span className="text-sm text-gray-500 ml-2">× {quantity} units</span>
          )}
          {quantity != null && quantity > 0 && (
            <span className="text-xs text-gray-600 ml-2 font-[family-name:var(--font-mono)]">
              = ₹{(price * quantity).toLocaleString("en-IN", { maximumFractionDigits: 2 })}
            </span>
          )}
        </div>
      )}

      <p className="text-sm text-gray-400 leading-relaxed">{explanation}</p>
    </div>
  );
}
