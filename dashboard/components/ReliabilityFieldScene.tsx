"use client";

import { Canvas, type ThreeEvent } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import { useMemo, useState } from "react";
import type { CalibrationData } from "../lib/data";
import { buildReliabilityField, type ReliabilityBar } from "../lib/reliabilityField";
import { isGenuineContextLoss } from "../lib/webglSupport";
import { coverageClass } from "./CalibrationPanel";
import { pct } from "../lib/format";

const BAR_SPACING = 1.4;
const BAR_SIZE = 0.7;
const HEIGHT_SCALE = 4;
const REFERENCE_THICKNESS = 0.06;

type ColorTokens = {
  pos: string;
  warm: string;
  fg: string;
  fg2: string;
};

// Read the dashboard's own CSS custom properties instead of hardcoding a
// separate 3D palette, so the scene matches the rest of the (dark-only) page.
function readColorTokens(): ColorTokens {
  const style = getComputedStyle(document.documentElement);
  const read = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback;
  return {
    pos: read("--pos", "#3fb950"),
    warm: read("--warm", "#e8b05a"),
    fg: read("--fg", "#e8eae6"),
    fg2: read("--fg-2", "#bdb8af"),
  };
}

// Same under/over-confidence thresholds the 2D calibration panel uses, so a
// bar's color means the same thing here as the "cov" cells there. The
// middling ("") band maps to fg2, not accent: --accent equals --warm in this
// dashboard's palette, which would make "slightly off" and "badly off" bars
// indistinguishable.
export function barColor(
  observedFreq: number,
  predictedMean: number,
  tokens: ColorTokens,
): string {
  const cls = coverageClass(observedFreq, predictedMean);
  return cls === "text-pos" ? tokens.pos : cls === "text-warm" ? tokens.warm : tokens.fg2;
}

function Bar({
  bar,
  tokens,
  onHover,
}: {
  bar: ReliabilityBar;
  tokens: ColorTokens;
  onHover: (bar: ReliabilityBar | null) => void;
}) {
  const height = Math.max(bar.observedFreq * HEIGHT_SCALE, 0.02);
  const refHeight = Math.max(bar.predictedMean * HEIGHT_SCALE, 0.02);
  // Thin bins (low weight) render narrower and more translucent, so a
  // near-empty bin reads as thin rather than as a full-size, misleading bar.
  const width = BAR_SIZE * (0.35 + 0.65 * bar.weight);
  const opacity = 0.35 + 0.65 * bar.weight;
  const color = barColor(bar.observedFreq, bar.predictedMean, tokens);

  return (
    <group position={[bar.leadIndex * BAR_SPACING, 0, bar.binIndex * BAR_SPACING]}>
      <mesh
        position={[0, height / 2, 0]}
        onPointerOver={(event: ThreeEvent<PointerEvent>) => {
          event.stopPropagation();
          onHover(bar);
        }}
        onPointerOut={(event: ThreeEvent<PointerEvent>) => {
          event.stopPropagation();
          onHover(null);
        }}
      >
        <boxGeometry args={[width, height, width]} />
        <meshStandardMaterial color={color} transparent opacity={opacity} />
      </mesh>
      {/* Translucent reference at the predicted mean: a perfectly calibrated
          bin's bar top sits flush with this plane. */}
      <mesh position={[0, refHeight, 0]}>
        <boxGeometry args={[BAR_SIZE * 1.15, REFERENCE_THICKNESS, BAR_SIZE * 1.15]} />
        <meshStandardMaterial color={tokens.fg} transparent opacity={0.3} depthWrite={false} />
      </mesh>
    </group>
  );
}

export function ReliabilityFieldScene({
  calibration,
  onContextLost,
}: {
  calibration: CalibrationData;
  onContextLost: () => void;
}) {
  const [variable, setVariable] = useState(calibration.variables[0] ?? "");
  const field = useMemo(
    () => buildReliabilityField(calibration, variable),
    [calibration, variable],
  );
  const [hovered, setHovered] = useState<ReliabilityBar | null>(null);
  const tokens = useMemo(readColorTokens, []);

  const centerX = (Math.max(field.leads.length - 1, 0) * BAR_SPACING) / 2;
  const centerZ = (Math.max(field.bins.length - 1, 0) * BAR_SPACING) / 2;

  return (
    <div>
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="Variable">
        {calibration.variables.map((v) => (
          <button
            key={v}
            type="button"
            onClick={() => setVariable(v)}
            aria-pressed={v === variable}
            className={
              "rounded border px-2 py-0.5 text-[11px] transition-colors " +
              (v === variable
                ? "border-accent-border bg-panel text-fg"
                : "border-line text-muted hover:text-faint")
            }
          >
            {v}
          </button>
        ))}
      </div>

      <div className="relative mt-2.5 h-[320px] overflow-hidden rounded border border-line bg-panel">
        {field.bars.length === 0 ? (
          <p className="p-4 text-sm text-muted">No calibration data for {variable}.</p>
        ) : (
          // frameloop="demand": nothing renders unless the user (or a data
          // change) invalidates the frame, so the scene never animates on its
          // own, satisfying prefers-reduced-motion without extra branching.
          <Canvas
            frameloop="demand"
            camera={{ position: [centerX + 3, HEIGHT_SCALE + 1.5, centerZ + 5.5], fov: 45 }}
            onCreated={({ gl }) => {
              gl.domElement.addEventListener("webglcontextlost", (event) => {
                // r3f's teardown (switching to a variable with no bars unmounts
                // <Canvas>) calls forceContextLoss(), which queues this event
                // for after the canvas is already detached. Only a loss on a
                // still-mounted canvas is a real, recoverable context loss.
                if (!isGenuineContextLoss(gl.domElement)) return;
                event.preventDefault();
                onContextLost();
              });
            }}
          >
            <ambientLight intensity={0.9} />
            <directionalLight position={[4, 6, 4]} intensity={0.6} />
            <group position={[-centerX, 0, -centerZ]}>
              {field.bars.map((bar) => (
                <Bar
                  key={`${bar.leadIndex}-${bar.binIndex}`}
                  bar={bar}
                  tokens={tokens}
                  onHover={setHovered}
                />
              ))}
            </group>
            <OrbitControls makeDefault enableDamping={false} target={[0, HEIGHT_SCALE / 4, 0]} />
          </Canvas>
        )}
      </div>

      <div aria-live="polite" className="mt-2 min-h-[1.5rem] font-mono text-[11px] text-faint">
        {hovered ? (
          <span>
            {hovered.lead}d · bin {pct(hovered.lo)}-{pct(hovered.hi)} · predicted{" "}
            {pct(hovered.predictedMean)} · observed {pct(hovered.observedFreq)} · n{hovered.count}
          </span>
        ) : (
          <span>Hover or focus a bar for its bin range, predicted and observed value, and n.</span>
        )}
      </div>

      <p className="mt-2 text-[11px] leading-relaxed text-faint">
        Starting view: lead time in days runs left to right, predicted-probability bins run back to
        front (lowest at the back). Bar height is observed frequency. The translucent slab marks the
        bin&apos;s predicted mean, where a perfectly calibrated bar stops. Bar color is the gap
        between the two: green within 5 points, light gray 5 to 10 points, amber more than 10.
        Thinner, fainter bars have fewer samples. Drag to rotate, scroll to zoom.
      </p>

      {/* Keyboard-accessible equivalent of hovering a bar: screen-reader-only
          controls so "focus" (not just mouse hover) surfaces the same detail,
          since a 3D mesh in a canvas is not itself a focusable DOM element. */}
      <ul className="sr-only">
        {field.bars.map((bar) => (
          <li key={`focus-${bar.leadIndex}-${bar.binIndex}`}>
            <button
              type="button"
              onFocus={() => setHovered(bar)}
              onBlur={() => setHovered(null)}
            >
              {`${bar.lead}d, bin ${pct(bar.lo)}-${pct(bar.hi)}`}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
