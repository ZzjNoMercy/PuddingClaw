"use client";

import { AppProvider } from "@/lib/store";

export default function Providers({ children }: { children: React.ReactNode }) {
  return <AppProvider>{children}</AppProvider>;
}
