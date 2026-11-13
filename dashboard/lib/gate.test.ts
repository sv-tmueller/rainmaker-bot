import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { MAX_EDGE, MIN_EDGE, STATION_EDGE_DELTA, edgeFloor, gateStatus } from "./gate";

const configSrc = readFileSync(new URL("../../src/rainmaker/config.py", import.meta.url), "utf8");

describe("gate thresholds match src/rainmaker/config.py", () => {
  it("MIN_EDGE", () => {
    const m = configSrc.match(/^MIN_EDGE = (\S+)$/m);
    expect(m).not.toBeNull();
    expect(MIN_EDGE).toBe(Number(m![1]));
  });

  it("MAX_EDGE", () => {
    const m = configSrc.match(/^MAX_EDGE = (\S+)$/m);
    expect(m).not.toBeNull();
    expect(MAX_EDGE).toBe(Number(m![1]));
  });

  it("STATION_EDGE_DELTA has the same keys and values", () => {
    const block = configSrc.match(/^STATION_EDGE_DELTA[^=]*= \{([^}]*)\}/m);
    expect(block).not.toBeNull();
    const py: Record<string, number> = {};
    for (const e of block![1].matchAll(/\("(\w+)", "(\w+)"\):\s*([0-9.]+)/g)) {
      py[`${e[1]}|${e[2]}`] = Number(e[3]);
    }
    expect(STATION_EDGE_DELTA).toEqual(py);
  });
});

describe("edgeFloor", () => {
  it("is MIN_EDGE for a station with no delta", () => {
    expect(edgeFloor("KNYC", "TMAX")).toBe(MIN_EDGE);
  });

  it("adds the KSFO TMAX delta", () => {
    expect(edgeFloor("KSFO", "TMAX")).toBe(MIN_EDGE + 0.05);
  });

  it("does not add the KSFO TMAX delta to KSFO TMIN", () => {
    expect(edgeFloor("KSFO", "TMIN")).toBe(MIN_EDGE);
  });

  it("is MIN_EDGE when the station is unknown (precip)", () => {
    expect(edgeFloor(null, "PRCP")).toBe(MIN_EDGE);
  });
});

describe("gateStatus", () => {
  it("is edge gone at zero net edge", () => {
    expect(gateStatus(0.02, 0, "KNYC", "TMAX")).toBe("edge gone");
  });

  it("is edge gone at negative net edge", () => {
    expect(gateStatus(0.01, -0.02, "KNYC", "TMAX")).toBe("edge gone");
  });

  it("is below the gate between zero and the floor", () => {
    expect(gateStatus(0.15, 0.1, "KNYC", "TMAX")).toBe("below the gate");
  });

  it("clears exactly at the floor", () => {
    expect(gateStatus(0.15, MIN_EDGE, "KNYC", "TMAX")).toBe("clears the gate");
  });

  it("clears exactly at the cap on gross edge", () => {
    expect(gateStatus(MAX_EDGE, 0.2, "KNYC", "TMAX")).toBe("clears the gate");
  });

  it("is above the edge cap just over the cap on gross edge", () => {
    expect(gateStatus(MAX_EDGE + 0.001, 0.2, "KNYC", "TMAX")).toBe("above the edge cap");
  });

  it("uses the 0.17 floor for KSFO TMAX", () => {
    expect(gateStatus(0.2, 0.15, "KSFO", "TMAX")).toBe("below the gate");
    expect(gateStatus(0.2, edgeFloor("KSFO", "TMAX"), "KSFO", "TMAX")).toBe("clears the gate");
  });

  it("reports below the gate before above the cap when net misses the floor", () => {
    expect(gateStatus(0.3, 0.1, "KNYC", "TMAX")).toBe("below the gate");
  });
});
