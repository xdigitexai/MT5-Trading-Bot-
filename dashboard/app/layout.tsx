import type { ReactNode } from "react";

export const metadata = {
  title: "MT5 Forex Bot",
  description: "Fail-closed MT5 trading control plane (demo by default)",
};

/** The App Router requires a root layout; without it `next build` aborts. */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body style={{ margin: 0, fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif" }}>
        {children}
      </body>
    </html>
  );
}
