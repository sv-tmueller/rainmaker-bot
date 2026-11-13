// Edge thresholds of the recommendation gate. Keep equal to MIN_EDGE, MAX_EDGE
// and STATION_EDGE_DELTA in src/rainmaker/config.py; gate.test.ts fails if they
// drift apart. Only the price-dependent checks are copied: win probability and
// source count are stored and do not change with the price.
export const MIN_EDGE = 0.12;
export const MAX_EDGE = 0.25;
// Keyed "ICAO|VARIABLE".
export const STATION_EDGE_DELTA: Record<string, number> = {
  "KSFO|TMAX": 0.05,
};

export type GateStatus =
  | "edge gone"
  | "below the gate"
  | "above the edge cap"
  | "clears the gate";

// Precip markets have no station, so no delta, as in the Python precip path.
export function edgeFloor(station: string | null, variable: string): number {
  const delta = station === null ? 0 : (STATION_EDGE_DELTA[`${station}|${variable}`] ?? 0);
  return MIN_EDGE + delta;
}

// Same boundaries as edge.py: net edge at least the floor, gross edge at most
// the cap. First match wins.
export function gateStatus(
  grossEdge: number,
  netEdge: number,
  station: string | null,
  variable: string,
): GateStatus {
  if (netEdge <= 0) return "edge gone";
  if (netEdge < edgeFloor(station, variable)) return "below the gate";
  if (grossEdge > MAX_EDGE) return "above the edge cap";
  return "clears the gate";
}
