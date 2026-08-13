"use client";

import { useEffect, useState } from "react";

export type RuntimeExtensions = {
  knowledge: boolean;
  analytics: boolean;
  headless_worker: boolean;
};

const SOURCE_CHECKOUT_DEFAULTS: RuntimeExtensions = {
  knowledge: true,
  analytics: true,
  headless_worker: true,
};

export function useRuntimeProfile() {
  const [extensions, setExtensions] = useState<RuntimeExtensions | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/runtime-profile")
      .then(async (response) => {
        if (!response.ok) throw new Error(`runtime profile returned ${response.status}`);
        return response.json() as Promise<{ extensions?: Partial<RuntimeExtensions> }>;
      })
      .then((payload) => {
        if (cancelled) return;
        setExtensions({ ...SOURCE_CHECKOUT_DEFAULTS, ...payload.extensions });
      })
      .catch(() => {
        // Source checkouts predating the runtime profile endpoint retain the
        // historical full settings surface. Managed runtimes always expose it.
        if (!cancelled) setExtensions(SOURCE_CHECKOUT_DEFAULTS);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return extensions;
}
