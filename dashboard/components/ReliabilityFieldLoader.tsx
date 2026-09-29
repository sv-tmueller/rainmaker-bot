"use client";

import { Component, useEffect, useState, type ReactNode } from "react";
import dynamic from "next/dynamic";
import type { CalibrationData } from "../lib/data";
import { canUseWebGL } from "../lib/webglSupport";

// The heavy r3f scene (three.js + fiber + drei) loads only when this island
// mounts, and never on the server: `ssr: false` has to live in a "use client"
// module (this one), not in page.tsx, or Next.js 16 rejects it at build time.
const ReliabilityFieldScene = dynamic(
  () => import("./ReliabilityFieldScene").then((mod) => mod.ReliabilityFieldScene),
  {
    ssr: false,
    loading: () => <p className="text-sm text-muted">Loading 3D view…</p>,
  },
);

const FALLBACK_MESSAGE = "3D view unavailable in this browser (no WebGL, or a render error).";

// Catches render-time errors thrown by the r3f tree (e.g. a driver quirk
// three.js can't recover from) and swaps to the plain-text fallback instead
// of taking the rest of the page down with it.
class SceneErrorBoundary extends Component<
  { children: ReactNode; onError: () => void },
  { hasError: boolean }
> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch() {
    this.props.onError();
  }

  render() {
    if (this.state.hasError) return null;
    return this.props.children;
  }
}

function probeWebGL(): boolean {
  try {
    const canvas = document.createElement("canvas");
    return canUseWebGL((type) => canvas.getContext(type));
  } catch {
    return false;
  }
}

export function ReliabilityFieldLoader({ calibration }: { calibration: CalibrationData }) {
  // null = probe not run yet (avoids a flash of the wrong state before the
  // effect fires on the client).
  const [webglOk, setWebglOk] = useState<boolean | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setWebglOk(probeWebGL());
  }, []);

  if (calibration.rows.length === 0) {
    return <p className="text-sm text-muted">No calibration data yet.</p>;
  }
  if (webglOk === null) return null;
  if (!webglOk || failed) {
    return <p className="text-sm text-muted">{FALLBACK_MESSAGE}</p>;
  }

  return (
    <SceneErrorBoundary onError={() => setFailed(true)}>
      <ReliabilityFieldScene calibration={calibration} onContextLost={() => setFailed(true)} />
    </SceneErrorBoundary>
  );
}
