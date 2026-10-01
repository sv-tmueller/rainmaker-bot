import type { CalibrationData } from "../lib/data";
import { pct } from "../lib/format";
import {
  buildReliabilitySeries,
  type ReliabilityChartData,
  type ReliabilityPoint,
} from "../lib/reliabilitySeries";

// ViewBox units equal CSS pixels: the svg is capped at W wide, so text keeps
// its nominal size instead of scaling with the panel.
const PLOT = 290;
const LEFT = 36;
const RIGHT = 14;
const TOP = 8;
const BOTTOM = 28;
const W = LEFT + PLOT + RIGHT;
const H = TOP + PLOT + BOTTOM;
const R_MIN = 2.5;
const R_MAX = 8;
const TICKS = [0, 0.25, 0.5, 0.75, 1];

const x = (p: number) => LEFT + p * PLOT;
const y = (p: number) => TOP + (1 - p) * PLOT;
const radius = (weight: number) => R_MIN + (R_MAX - R_MIN) * Math.sqrt(weight);

function tooltip(point: ReliabilityPoint) {
  return `${pct(point.lo)}-${pct(point.hi)}: predicted ${pct(point.predictedMean)}, observed ${pct(point.observedFreq)}, n ${point.count}`;
}

function VariableChart({ chart }: { chart: ReliabilityChartData }) {
  return (
    <figure className="m-0">
      <figcaption className="flex items-baseline gap-3 font-mono text-[11px]">
        <span className="font-sans text-[13px] text-fg">{chart.variable}</span>
        {chart.series.map((s) => (
          <span key={s.lead} className={s.colorClass}>
            ● <span className="text-muted">{s.lead}d</span>
          </span>
        ))}
      </figcaption>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width={W}
        className="mt-1.5 block h-auto w-full max-w-[340px]"
        role="img"
        aria-label={`Reliability diagram for ${chart.variable}`}
      >
        {TICKS.map((t) => (
          <g key={t}>
            <line
              x1={x(t)}
              y1={TOP}
              x2={x(t)}
              y2={TOP + PLOT}
              className="text-line"
              stroke="currentColor"
            />
            <line
              x1={LEFT}
              y1={y(t)}
              x2={LEFT + PLOT}
              y2={y(t)}
              className="text-line"
              stroke="currentColor"
            />
            <text
              x={x(t)}
              y={TOP + PLOT + 12}
              textAnchor="middle"
              className="fill-faint font-mono text-[9px]"
            >
              {pct(t)}
            </text>
            <text
              x={LEFT - 4}
              y={y(t) + 3}
              textAnchor="end"
              className="fill-faint font-mono text-[9px]"
            >
              {pct(t)}
            </text>
          </g>
        ))}
        <text
          x={LEFT + PLOT / 2}
          y={H - 3}
          textAnchor="middle"
          className="fill-muted font-mono text-[9px]"
        >
          predicted
        </text>
        <text
          transform={`translate(8 ${TOP + PLOT / 2}) rotate(-90)`}
          textAnchor="middle"
          className="fill-muted font-mono text-[9px]"
        >
          observed
        </text>
        <line
          x1={x(0)}
          y1={y(0)}
          x2={x(1)}
          y2={y(1)}
          className="text-muted"
          stroke="currentColor"
          strokeDasharray="3 4"
        />
        {chart.series.map((s) => (
          <g key={s.lead} className={s.colorClass}>
            <polyline
              points={s.points
                .map((p) => `${x(p.predictedMean).toFixed(1)},${y(p.observedFreq).toFixed(1)}`)
                .join(" ")}
              fill="none"
              stroke="currentColor"
              strokeWidth="1"
              opacity={0.6}
            />
            {s.points.map((p) => (
              <circle
                key={`${p.lo}|${p.hi}`}
                cx={x(p.predictedMean)}
                cy={y(p.observedFreq)}
                r={radius(p.weight)}
                fill="currentColor"
                fillOpacity={0.55}
                stroke="currentColor"
                strokeWidth="1"
              >
                <title>{`${chart.variable} ${s.lead}d ${tooltip(p)}`}</title>
              </circle>
            ))}
          </g>
        ))}
      </svg>
    </figure>
  );
}

export function ReliabilityChart({ calibration }: { calibration: CalibrationData }) {
  const charts = buildReliabilitySeries(calibration);
  return (
    <section className="rounded border border-line bg-panel px-4 py-4">
      <div className="text-[10px] uppercase tracking-[0.1em] text-muted">
        Reliability{" "}
        <span className="normal-case tracking-normal text-faint">
          · predicted probability vs observed frequency, by variable + lead
        </span>
      </div>
      {charts.length === 0 ? (
        <p className="mt-3 text-sm text-muted">No calibration data yet.</p>
      ) : (
        <>
          <div className="mt-2.5 flex flex-wrap gap-x-6 gap-y-5">
            {charts.map((chart) => (
              <VariableChart key={chart.variable} chart={chart} />
            ))}
          </div>
          <p className="mt-3 text-[11px] leading-relaxed text-faint">
            Above the dashed diagonal, the outcome won more often than predicted (we were too
            cautious). Below it, we were too confident. Bigger dots are buckets with more samples.
            Pooled across all cities.
          </p>
        </>
      )}
    </section>
  );
}
