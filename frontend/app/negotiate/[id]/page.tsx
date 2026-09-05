"use client";

import { useEffect, useRef, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import OfferCard from "@/components/OfferCard";
import {
  Cpu,
  ShoppingCart,
  Building2,
  FileText,
  Loader2,
  CheckCircle2,
  XCircle,
  Clock,
  WifiOff,
  MessageSquare,
  CreditCard,
  CircleDollarSign,
} from "lucide-react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const WS_BASE = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000";

interface NegEvent {
  event_type: string;
  round_number: number;
  role: string;
  decision: string;
  price?: number | null;
  quantity?: number | null;
  explanation: string;
  status: string;
}

export default function NegotiatePage() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const id = params.id as string;

  const [events, setEvents] = useState<NegEvent[]>([]);
  const [status, setStatus] = useState<string>("connecting");
  const [finalPrice, setFinalPrice] = useState<number | null>(null);
  const [paymentLink, setPaymentLink] = useState<string>("");
  const [paymentLoading, setPaymentLoading] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  // `onclose` is created once and would otherwise read the `status` from the
  // render it was defined in — always "connecting".
  const statusRef = useRef(status);
  statusRef.current = status;
  // Guards against double-rendering an event that arrives over both the
  // WebSocket replay buffer and the REST hydration.
  const seenRef = useRef<Set<string>>(new Set());
  // Guards against sending the same razorpay_* redirect params twice (e.g.
  // React 18 strict-mode's double-invoked effects in dev).
  const verifiedRef = useRef(false);

  const eventKey = (e: NegEvent) =>
    `${e.event_type}|${e.round_number}|${e.role}|${e.decision}|${e.price ?? ""}`;

  const addEvent = (e: NegEvent) => {
    const key = eventKey(e);
    if (seenRef.current.has(key)) return;
    seenRef.current.add(key);
    setEvents((prev) => [...prev, e]);
  };

  // Connect WebSocket
  useEffect(() => {
    const ws = new WebSocket(`${WS_BASE}/ws/negotiate/${id}`);
    wsRef.current = ws;

    ws.onopen = () => setStatus("active");

    ws.onmessage = (event) => {
      try {
        const data: NegEvent = JSON.parse(event.data);
        addEvent(data);

        if (data.status && data.status !== "active") {
          setStatus(data.status);
        }
        if (data.decision === "accepted" && data.price != null) {
          setFinalPrice(data.price);
        }
        if (data.event_type === "payment_success") {
          setPaymentLink("paid");
        }
      } catch {
        // ignore
      }
    };

    ws.onclose = () => {
      if (statusRef.current === "connecting" || statusRef.current === "active") {
        setStatus((current) =>
          current === "connecting" ? "disconnected" : current
        );
      }
    };

    // Keepalive ping
    const pingInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send("ping");
    }, 30000);

    return () => {
      clearInterval(pingInterval);
      ws.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // Also load existing data via REST, so a reload (or a WS connection opened
  // after the run finished) replays the whole transcript instead of showing an
  // empty pane. `rounds` was already fetched here and thrown away.
  useEffect(() => {
    fetch(`${API_BASE}/api/negotiations/${id}`)
      .then((r) => r.json())
      .then((data) => {
        const neg = data?.negotiation;
        if (!neg) return;

        for (const round of data.rounds ?? []) {
          for (const side of ["buyer", "seller"] as const) {
            const decision = round[`${side}_decision`];
            if (!decision) continue;
            const offer = round[`${side}_offer`];
            addEvent({
              event_type: `${side}_${decision.decision}`,
              round_number: round.round_number,
              role: side,
              decision: decision.decision,
              price: offer?.price ?? null,
              quantity: offer?.quantity ?? null,
              explanation: round[`${side}_explanation`] || "",
              status: neg.status,
            });
          }
        }

        if (neg.status !== "active") {
          setStatus(neg.status);
          addEvent({
            event_type: "negotiation_completed",
            round_number: neg.current_round ?? 0,
            role: "system",
            decision: neg.status,
            price: neg.final_price ?? null,
            quantity: neg.final_quantity ?? null,
            explanation:
              neg.status === "accepted"
                ? "Deal agreed by both agents!"
                : neg.status === "expired"
                ? "The buyer's mandate expired before the negotiation could start."
                : "No deal reached.",
            status: neg.status,
          });
        }
        if (neg.final_price != null) setFinalPrice(neg.final_price);
        if (neg.payment_status === "captured") {
          setPaymentLink("paid");
        } else if (neg.razorpay_payment_link) {
          setPaymentLink(neg.razorpay_payment_link);
        }
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // Razorpay redirects the browser back to this same page with
  // razorpay_* query params after a Payment Link checkout. The webhook
  // (the only other source of "captured") requires Razorpay's servers to
  // reach ours over the public internet, which they can't in local dev
  // (localhost) — so without this, the page never learns the payment
  // happened. This signature arrives via the browser itself, so it works
  // regardless of webhook reachability.
  useEffect(() => {
    const paymentId = searchParams.get("razorpay_payment_id");
    const signature = searchParams.get("razorpay_signature");
    const linkId = searchParams.get("razorpay_payment_link_id");
    const linkStatus = searchParams.get("razorpay_payment_link_status");
    if (!paymentId || !signature || !linkId || !linkStatus) return;
    if (verifiedRef.current) return;
    verifiedRef.current = true;

    fetch(`${API_BASE}/api/negotiations/${id}/verify-payment`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        razorpay_payment_id: paymentId,
        razorpay_payment_link_id: linkId,
        razorpay_payment_link_reference_id:
          searchParams.get("razorpay_payment_link_reference_id") || "",
        razorpay_payment_link_status: linkStatus,
        razorpay_signature: signature,
      }),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data?.payment_status === "captured") setPaymentLink("paid");
      })
      .catch(() => {})
      .finally(() => {
        // Strip the razorpay_* params so a refresh doesn't re-send them and
        // so the payment id/signature don't linger in the address bar.
        router.replace(`/negotiate/${id}`);
      });
  }, [id, router, searchParams]);

  // Auto-scroll
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [events]);

  const handleCreatePayment = async () => {
    setPaymentLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/negotiations/${id}/pay`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ callback_url: window.location.href }),
      });
      const data = await res.json();
      if (data.payment_link) {
        setPaymentLink(data.payment_link);
      }
    } catch {
      // ignore
    }
    setPaymentLoading(false);
  };

  const statusConfig: Record<string, { label: string; color: string; Icon: React.ComponentType<{className?: string}> }> = {
    connecting: { label: "Connecting…", color: "text-gray-500", Icon: Loader2 },
    active: { label: "Negotiating", color: "text-[#06d6a0]", Icon: MessageSquare },
    accepted: { label: "Deal Accepted", color: "text-[#06d6a0]", Icon: CheckCircle2 },
    rejected: { label: "No Deal", color: "text-[#ef233c]", Icon: XCircle },
    expired: { label: "Expired", color: "text-[#ff6b35]", Icon: Clock },
    disconnected: { label: "Disconnected", color: "text-gray-500", Icon: WifiOff },
  };
  const st = statusConfig[status] || statusConfig.connecting;

  // Separate events by role for split view
  const buyerEvents = events.filter((e) => e.role === "buyer");
  const sellerEvents = events.filter((e) => e.role === "seller");
  const systemEvents = events.filter((e) => e.role === "system");

  return (
    <div className="min-h-screen mesh-gradient flex flex-col">
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

          <div className="flex items-center gap-4">
            <div className={`flex items-center gap-2 text-sm ${st.color}`}>
              <st.Icon className={`w-4 h-4 ${status === "connecting" ? "animate-spin" : ""} ${status === "active" ? "animate-status-blink" : ""}`} />
              <span className="font-medium font-[family-name:var(--font-mono)] text-xs uppercase tracking-wider">{st.label}</span>
              {status === "active" && (
                <span className="w-1.5 h-1.5 rounded-full bg-[#06d6a0] animate-status-blink" />
              )}
            </div>
            <a
              href={`/negotiate/${id}/audit`}
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg glass hover:border-[#06d6a0]/20 transition-all text-gray-400 hover:text-white"
            >
              <FileText className="w-3.5 h-3.5" />
              Audit Log
            </a>
          </div>
        </div>
      </header>

      {/* Negotiation ID */}
      <div className="max-w-7xl mx-auto px-6 py-3">
        <p className="text-xs text-gray-600 font-[family-name:var(--font-mono)]">
          SESSION: {id}
        </p>
      </div>

      {/* Main Content — Split Pane */}
      <div className="flex-1 max-w-7xl mx-auto px-6 pb-8 w-full">
        <div className="grid md:grid-cols-2 gap-6 h-full">
          {/* Buyer Side */}
          <div className="flex flex-col">
            <div className="flex items-center gap-2 mb-4">
              <div className="w-8 h-8 rounded-lg bg-[#00b4d8]/10 border border-[#00b4d8]/20 flex items-center justify-center">
                <ShoppingCart className="w-4 h-4 text-[#00b4d8]" />
              </div>
              <h2 className="text-sm font-semibold text-[#00b4d8]">Buyer Agent</h2>
              <span className="text-xs text-gray-600 ml-auto font-[family-name:var(--font-mono)]">{buyerEvents.length} actions</span>
            </div>
            <div className="flex-1 space-y-3 overflow-y-auto max-h-[60vh] pr-2" ref={scrollRef}>
              {buyerEvents.map((e, i) => {
                const { role: _role, ...rest } = e;
                return <OfferCard key={i} role="buyer" index={i} {...rest} roundNumber={e.round_number} />;
              })}
              {buyerEvents.length === 0 && status === "active" && (
                <div className="glass rounded-xl p-8 text-center text-gray-600 text-sm">
                  <div className="flex flex-col items-center gap-2">
                    <Loader2 className="w-5 h-5 animate-spin text-[#00b4d8]/50" />
                    <span>Awaiting buyer agent…</span>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Seller Side */}
          <div className="flex flex-col">
            <div className="flex items-center gap-2 mb-4">
              <div className="w-8 h-8 rounded-lg bg-[#ff6b35]/10 border border-[#ff6b35]/20 flex items-center justify-center">
                <Building2 className="w-4 h-4 text-[#ff6b35]" />
              </div>
              <h2 className="text-sm font-semibold text-[#ff6b35]">Seller Agent</h2>
              <span className="text-xs text-gray-600 ml-auto font-[family-name:var(--font-mono)]">{sellerEvents.length} actions</span>
            </div>
            <div className="flex-1 space-y-3 overflow-y-auto max-h-[60vh] pr-2">
              {sellerEvents.map((e, i) => {
                const { role: _role, ...rest } = e;
                return <OfferCard key={i} role="seller" index={i} {...rest} roundNumber={e.round_number} />;
              })}
              {sellerEvents.length === 0 && status === "active" && (
                <div className="glass rounded-xl p-8 text-center text-gray-600 text-sm">
                  <div className="flex flex-col items-center gap-2">
                    <Loader2 className="w-5 h-5 animate-spin text-[#ff6b35]/50" />
                    <span>Awaiting seller agent…</span>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* System Events (deal results) */}
        {systemEvents.filter((e) => e.event_type === "negotiation_completed").length > 0 && (
          <div className="mt-6 space-y-3">
            {systemEvents
              .filter((e) => e.event_type === "negotiation_completed")
              .map((e, i) => {
                const { role: _role, ...rest } = e;
                return <OfferCard key={i} role="system" index={i} {...rest} roundNumber={e.round_number} />;
              })}
          </div>
        )}

        {/* Payment Section */}
        {status === "accepted" && (
          <div className="mt-6 glass rounded-2xl p-6 gradient-border animate-fade-up animate-neon-pulse">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-xl bg-[#06d6a0]/10 border border-[#06d6a0]/20 flex items-center justify-center">
                  <CheckCircle2 className="w-5 h-5 text-[#06d6a0]" />
                </div>
                <div>
                  <h3 className="text-lg font-semibold text-[#06d6a0]">Deal Accepted</h3>
                  {finalPrice && (
                    <p className="text-sm text-gray-400 mt-0.5">
                      Agreed price: <span className="text-white font-bold font-[family-name:var(--font-mono)]">₹{finalPrice.toFixed(2)}</span> per unit
                    </p>
                  )}
                </div>
              </div>
              <div>
                {paymentLink === "paid" ? (
                  <span className="flex items-center gap-2 px-4 py-2 rounded-xl bg-[#06d6a0]/10 border border-[#06d6a0]/20 text-[#06d6a0] text-sm">
                    <CheckCircle2 className="w-4 h-4" />
                    Payment Received
                  </span>
                ) : paymentLink ? (
                  <a
                    href={paymentLink}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-2 px-6 py-2.5 rounded-xl bg-gradient-to-r from-[#06d6a0] to-[#00b4d8] text-[#060a10] font-bold text-sm hover:shadow-lg hover:shadow-[#06d6a0]/20 transition-all"
                  >
                    <CreditCard className="w-4 h-4" />
                    Pay Now via Razorpay
                  </a>
                ) : (
                  <button
                    onClick={handleCreatePayment}
                    disabled={paymentLoading}
                    className="flex items-center gap-2 px-6 py-2.5 rounded-xl bg-gradient-to-r from-[#06d6a0] to-[#00b4d8] text-[#060a10] font-bold text-sm hover:shadow-lg hover:shadow-[#06d6a0]/20 transition-all disabled:opacity-50 cursor-pointer"
                  >
                    <CircleDollarSign className="w-4 h-4" />
                    {paymentLoading ? "Creating…" : "Create Payment Link"}
                  </button>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
