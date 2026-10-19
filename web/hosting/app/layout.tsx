import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Warsaw Deal Hunter",
  description: "Explore Warsaw apartment asking prices, model estimates and original listing links.",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
