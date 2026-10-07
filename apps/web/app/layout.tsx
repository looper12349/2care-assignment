import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Carepath · Appointment assistant",
  description: "A local synthetic clinic demonstration with careful scheduling and evidence-based improvement.",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
