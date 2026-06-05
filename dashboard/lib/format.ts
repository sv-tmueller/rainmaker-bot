export function pct(x: number) {
  return `${(x * 100).toFixed(0)}%`;
}

export function degC(f: number) {
  return (((f - 32) * 5) / 9).toFixed(1);
}

// Mirrors parse_bucket_label in src/rainmaker/polymarket/markets.py.
export function withCelsius(label: string): string {
  const lowered = label.toLowerCase();
  if (lowered.includes("below") || lowered.includes("higher") || lowered.includes("above")) {
    const m = label.match(/-?\d+/);
    if (!m) return label;
    const op = lowered.includes("below") ? "<=" : ">=";
    return `${label} (${op} ${degC(+m[0])}°C)`;
  }
  const m = label.match(/(-?\d+)\s*-\s*(-?\d+)/);
  if (!m) return label;
  return `${label} (${degC(+m[1])}-${degC(+m[2])}°C)`;
}

export function signed(x: number, digits = 2) {
  return `${x >= 0 ? "+" : ""}${x.toFixed(digits)}`;
}
