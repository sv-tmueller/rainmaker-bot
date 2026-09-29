import { describe, expect, test, vi } from "vitest";
import { canUseWebGL, isGenuineContextLoss, releasingProbe } from "./webglSupport";

describe("canUseWebGL", () => {
  test("true when the probe returns a context", () => {
    expect(canUseWebGL(() => ({}))).toBe(true);
  });

  test("false when the probe returns null (context creation failed)", () => {
    expect(canUseWebGL(() => null)).toBe(false);
  });

  test("false when the probe throws (blocked or unsupported)", () => {
    expect(
      canUseWebGL(() => {
        throw new Error("WebGL context creation is disallowed");
      }),
    ).toBe(false);
  });
});

describe("releasingProbe", () => {
  test("releases the context it creates via WEBGL_lose_context", () => {
    const loseContext = vi.fn();
    const getExtension = vi.fn(() => ({ loseContext }));
    const ctx = { getExtension };
    const canvas = { getContext: vi.fn(() => ctx) };

    const result = releasingProbe(canvas)("webgl2");

    expect(result).toBe(ctx);
    expect(getExtension).toHaveBeenCalledWith("WEBGL_lose_context");
    expect(loseContext).toHaveBeenCalledOnce();
  });

  test("returns null without throwing when getContext yields no context", () => {
    const canvas = { getContext: vi.fn(() => null) };

    expect(releasingProbe(canvas)("webgl")).toBeNull();
  });
});

describe("isGenuineContextLoss", () => {
  test("true when the canvas is still attached to the document", () => {
    expect(isGenuineContextLoss({ isConnected: true })).toBe(true);
  });

  test("false when the canvas has already been detached (r3f teardown noise)", () => {
    expect(isGenuineContextLoss({ isConnected: false })).toBe(false);
  });
});
