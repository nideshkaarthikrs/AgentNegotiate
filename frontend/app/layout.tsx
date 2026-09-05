import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "AgentNegotiate — Autonomous B2B Procurement",
  description:
    "Two-agent negotiation system with deterministic policy engine, LangGraph orchestration, and Razorpay payments.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${inter.variable} dark h-full antialiased`}>
      <body className="min-h-full flex flex-col bg-[#060a10] text-gray-100 font-sans">
        {children}
      </body>
    </html>
  );
}
