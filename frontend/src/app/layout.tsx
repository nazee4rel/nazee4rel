import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "X Account Intelligence",
  description: "Agentic monitoring and analysis for your X/Twitter account.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-canvas text-text">{children}</body>
    </html>
  );
}
