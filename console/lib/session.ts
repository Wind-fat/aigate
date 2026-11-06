"use client";

import { useEffect, useState } from "react";

/**
 * Who am I, and may I write?
 *
 * The role is fetched once per page load and cached at module scope: every page needs it,
 * and re-fetching on each navigation adds a round trip to a value that only changes when
 * someone edits their own account. `canWrite` mirrors the server rule (viewer = read only,
 * everything else may write); self-service endpoints are the one exception, handled by the
 * server, not here.
 */

type Me = { name: string; role: string };

let cached: Me | null = null;
let inflight: Promise<Me | null> | null = null;

function load(): Promise<Me | null> {
  if (cached) return Promise.resolve(cached);
  if (!inflight) {
    inflight = fetch("/api/console/me", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: Me | null) => {
        cached = d;
        return d;
      })
      .catch(() => null)
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

export function useSession() {
  const [me, setMe] = useState<Me | null>(cached);
  const [ready, setReady] = useState(cached !== null);

  useEffect(() => {
    let alive = true;
    load().then((d) => {
      if (!alive) return;
      setMe(d);
      setReady(true);
    });
    return () => {
      alive = false;
    };
  }, []);

  return {
    me,
    ready,
    role: me?.role ?? null,
    canWrite: ready && me !== null && me.role !== "viewer",
  };
}