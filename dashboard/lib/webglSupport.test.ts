import { describe, expect, test } from "vitest";
import { canUseWebGL } from "./webglSupport";

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
