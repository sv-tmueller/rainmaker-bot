/**
 * Pure WebGL-support decision. Takes a probe function instead of touching the
 * DOM directly, so the decision logic is unit-testable without a real
 * canvas: the caller passes something like
 * `(type) => canvas.getContext(type)`.
 *
 * Some browsers throw instead of returning null when WebGL is disabled
 * (for example a locked-down GPU sandbox), so both cases fold to false.
 * Only WebGL2 counts: three.js dropped WebGL1 in r163.
 */
export function canUseWebGL(probe: (type: string) => unknown): boolean {
  try {
    return probe("webgl2") != null;
  } catch {
    return false;
  }
}

/**
 * Wraps a canvas's `getContext` so a probe built from it releases the context
 * it creates (via `WEBGL_lose_context`) instead of leaking it. Without this,
 * every probe (each time the "3D view" disclosure opens) creates a live
 * WebGL context that is never freed, building toward the browser's cap on
 * live contexts.
 */
export function releasingProbe(
  canvas: Pick<HTMLCanvasElement, "getContext">,
): (type: string) => WebGLRenderingContext | null {
  return (type: string) => {
    const ctx = canvas.getContext(type) as WebGLRenderingContext | null;
    ctx?.getExtension("WEBGL_lose_context")?.loseContext();
    return ctx;
  };
}

/**
 * A `webglcontextlost` event on a canvas already detached from the document
 * is disposal noise, not a real, recoverable context loss: r3f's teardown
 * (`gl.forceContextLoss()`) queues the browser's loss event for after the
 * canvas is removed, so a listener that doesn't check this fires on every
 * unmount, not just on an actual driver-level context loss.
 */
export function isGenuineContextLoss(domElement: Pick<Node, "isConnected">): boolean {
  return domElement.isConnected;
}
