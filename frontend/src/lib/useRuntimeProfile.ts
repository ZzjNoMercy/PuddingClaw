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

// The sidebar is currently mounted by several top-level route layouts. Keep
// the runtime profile outside individual component instances so navigating
// between those routes does not briefly reset capability-gated items.
let cachedRuntimeExtensions: RuntimeExtensions = SOURCE_CHECKOUT_DEFAULTS;
let runtimeProfileRequest: Promise<RuntimeExtensions> | null = null;

function loadRuntimeProfile(): Promise<RuntimeExtensions> {
  if (runtimeProfileRequest) return runtimeProfileRequest;
  runtimeProfileRequest = fetch("/api/runtime-profile")
    .then(async (response) => {
      if (!response.ok) throw new Error(`runtime profile returned ${response.status}`);
      return response.json() as Promise<{ extensions?: Partial<RuntimeExtensions> }>;
    })
    .then((payload) => {
      cachedRuntimeExtensions = { ...SOURCE_CHECKOUT_DEFAULTS, ...payload.extensions };
      return cachedRuntimeExtensions;
    })
    .catch(() => cachedRuntimeExtensions);
  return runtimeProfileRequest;
}

export function useRuntimeProfile() {
  const [extensions, setExtensions] = useState<RuntimeExtensions>(cachedRuntimeExtensions);

  useEffect(() => {
    let cancelled = false;
    void loadRuntimeProfile().then((next) => {
      if (!cancelled) setExtensions(next);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return extensions;
}
