/**
 * Pure WebGL-support decision. Takes a probe function instead of touching the
 * DOM directly, so the decision logic is unit-testable without a real
 * canvas: the caller passes something like
 * `(type) => canvas.getContext(type)`.
 *
 * Some browsers throw instead of returning null when WebGL is disabled
 * (for example a locked-down GPU sandbox), so both cases fold to false.
 */
export function canUseWebGL(probe: (type: string) => unknown): boolean {
  try {
    return probe("webgl2") != null || probe("webgl") != null;
  } catch {
    return false;
  }
}
