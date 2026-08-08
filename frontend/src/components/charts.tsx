/**
 * Charts, drawn as plain SVG with no charting library.
 *
 * Two reasons, and the second is the one that matters. The first is that a
 * dependency-free chart is a few dozen lines here and keeps the client bundle
 * at zero — these render on the server like everything else in this app.
 *
 * The second is that every charting library wants a dense array of numbers, and
 * this dataset is not dense. Follower history has holes wherever collection
 * stopped, and impressions are simply absent for posts older than 30 days. The
 * usual fix — coalesce to zero, or interpolate across — produces exactly the
 * wrong picture: a flat, healthy-looking line across the period when the
 * collector was broken, or a smooth curve through data that was never observed.
 *
 * So `null` is a first-class value here. The line lifts its pen, the gap is
 * shaded and labelled, and a bar with no observation is drawn as an empty slot
 * rather than a bar of height zero.
 */

export type Point = {
  /** X-axis label. Rendered outside the SVG so it is never stretched. */
  label: string;
  /** `null` means "not observed" — never "zero". */
  value: number | null;
  /** Optional per-point annotation, surfaced as a tooltip. */
  note?: string;
};

const VIEW_WIDTH = 1000;

function extent(points: Point[]): { min: number; max: number } {
  const values = points.map((p) => p.value).filter((v): v is number => v !== null);
  if (values.length === 0) return { min: 0, max: 1 };
  const min = Math.min(...values);
  const max = Math.max(...values);
  // A flat series would otherwise divide by zero and collapse to the baseline.
  if (min === max) return { min: min - 1, max: max + 1 };
  return { min, max };
}

/** Contiguous runs of observed points, split wherever a null appears. */
function segments(points: Point[]): { index: number; value: number }[][] {
  const runs: { index: number; value: number }[][] = [];
  let current: { index: number; value: number }[] = [];

  points.forEach((point, index) => {
    if (point.value === null) {
      if (current.length > 0) runs.push(current);
      current = [];
      return;
    }
    current.push({ index, value: point.value });
  });
  if (current.length > 0) runs.push(current);
  return runs;
}

/** Runs of consecutive unobserved points, so a gap can be shaded. */
function holes(points: Point[]): { from: number; to: number }[] {
  const result: { from: number; to: number }[] = [];
  let start: number | null = null;

  points.forEach((point, index) => {
    if (point.value === null && start === null) start = index;
    if (point.value !== null && start !== null) {
      result.push({ from: start, to: index - 1 });
      start = null;
    }
  });
  if (start !== null) result.push({ from: start, to: points.length - 1 });
  return result;
}

function ChartShell({
  children,
  height,
  className = "",
}: {
  children: React.ReactNode;
  height: number;
  className?: string;
}) {
  return (
    <svg
      viewBox={`0 0 ${VIEW_WIDTH} ${height}`}
      // Stretches to the container width; `non-scaling-stroke` on every stroked
      // element keeps line weights even when it does.
      preserveAspectRatio="none"
      className={`w-full ${className}`}
      style={{ height }}
      role="img"
    >
      <defs>
        <pattern
          id="gap-hatch"
          width="6"
          height="6"
          patternUnits="userSpaceOnUse"
          patternTransform="rotate(45)"
        >
          <line
            x1="0"
            y1="0"
            x2="0"
            y2="6"
            stroke="var(--color-border)"
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
          />
        </pattern>
      </defs>
      {children}
    </svg>
  );
}

/**
 * A line chart that breaks across unobserved points.
 *
 * `fill` shades the area under the line, which reads well for a cumulative
 * series such as follower count.
 */
export function LineChart({
  points,
  height = 180,
  tone = "accent",
  fill = true,
  label,
}: {
  points: Point[];
  height?: number;
  tone?: "accent" | "inferred" | "positive";
  fill?: boolean;
  label?: string;
}) {
  const observed = points.filter((p) => p.value !== null);
  if (observed.length < 2) {
    return <EmptyChart height={height} message="Not enough observations to plot yet." />;
  }

  const { min, max } = extent(points);
  const padTop = 8;
  const padBottom = 8;
  const usable = height - padTop - padBottom;

  const x = (index: number) =>
    points.length === 1 ? VIEW_WIDTH / 2 : (index / (points.length - 1)) * VIEW_WIDTH;
  const y = (value: number) => padTop + usable - ((value - min) / (max - min)) * usable;

  const stroke = `var(--color-${tone})`;
  const runs = segments(points);

  return (
    <ChartShell height={height}>
      <title>{label ?? "Chart"}</title>

      {[0.25, 0.5, 0.75].map((fraction) => (
        <line
          key={fraction}
          x1={0}
          x2={VIEW_WIDTH}
          y1={padTop + usable * fraction}
          y2={padTop + usable * fraction}
          stroke="var(--color-border)"
          strokeWidth="1"
          strokeOpacity="0.5"
          vectorEffect="non-scaling-stroke"
        />
      ))}

      {/* Gaps are shaded rather than merely empty: an unexplained blank in a
          chart reads as a rendering fault, which is not the message. */}
      {holes(points).map((hole) => (
        <rect
          key={`${hole.from}-${hole.to}`}
          x={x(Math.max(0, hole.from - 0.5))}
          width={Math.max(4, x(hole.to + 0.5) - x(Math.max(0, hole.from - 0.5)))}
          y={padTop}
          height={usable}
          fill="url(#gap-hatch)"
          opacity="0.5"
        >
          <title>Not observed — collection was interrupted, so nothing was recorded.</title>
        </rect>
      ))}

      {runs.map((run) => {
        const line = run.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.index)},${y(p.value)}`).join(" ");
        const area =
          run.length > 1
            ? `${line} L${x(run[run.length - 1].index)},${height - padBottom} L${x(run[0].index)},${height - padBottom} Z`
            : "";
        return (
          <g key={`${run[0].index}-${run[run.length - 1].index}`}>
            {fill && area && <path d={area} fill={stroke} opacity="0.12" />}
            <path
              d={line}
              fill="none"
              stroke={stroke}
              strokeWidth="2"
              strokeLinejoin="round"
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
            />
          </g>
        );
      })}
    </ChartShell>
  );
}

/**
 * A bar series around a zero baseline, for signed values such as daily change.
 *
 * An unobserved day is drawn hatched at full height — visible, and impossible
 * to read as a day of no change.
 */
export function BarSeries({
  points,
  height = 140,
  formatValue = (v: number) => v.toFixed(0),
  label,
}: {
  points: Point[];
  height?: number;
  formatValue?: (value: number) => string;
  label?: string;
}) {
  const observed = points.filter((p) => p.value !== null);
  if (observed.length === 0) {
    return <EmptyChart height={height} message="Nothing observed in this period yet." />;
  }

  const values = observed.map((p) => p.value as number);
  const magnitude = Math.max(Math.abs(Math.min(...values)), Math.abs(Math.max(...values)), 1);
  const pad = 6;
  const usable = height - pad * 2;
  const zero = pad + usable / 2;
  const slot = VIEW_WIDTH / points.length;
  const barWidth = Math.max(1, slot * 0.7);

  return (
    <ChartShell height={height}>
      <title>{label ?? "Chart"}</title>

      {points.map((point, index) => {
        const left = index * slot + (slot - barWidth) / 2;

        if (point.value === null) {
          return (
            <rect
              key={point.label}
              x={left}
              width={barWidth}
              y={pad}
              height={usable}
              fill="url(#gap-hatch)"
              opacity="0.45"
            >
              <title>{`${point.label}: not observed`}</title>
            </rect>
          );
        }

        const magnitudeFraction = Math.abs(point.value) / magnitude;
        const barHeight = Math.max(1, (magnitudeFraction * usable) / 2);
        const positive = point.value >= 0;

        return (
          <rect
            key={point.label}
            x={left}
            width={barWidth}
            y={positive ? zero - barHeight : zero}
            height={barHeight}
            fill={positive ? "var(--color-positive)" : "var(--color-negative)"}
            opacity="0.85"
          >
            <title>
              {`${point.label}: ${formatValue(point.value)}${point.note ? ` — ${point.note}` : ""}`}
            </title>
          </rect>
        );
      })}

      <line
        x1={0}
        x2={VIEW_WIDTH}
        y1={zero}
        y2={zero}
        stroke="var(--color-border)"
        strokeWidth="1"
        vectorEffect="non-scaling-stroke"
      />
    </ChartShell>
  );
}

function EmptyChart({ height, message }: { height: number; message: string }) {
  return (
    <div
      className="flex items-center justify-center rounded-lg border border-dashed border-border text-xs text-text-muted"
      style={{ height }}
    >
      {message}
    </div>
  );
}

/**
 * A framed chart: title, value readout, the plot, and end labels.
 *
 * Axis labels live in HTML rather than inside the SVG because the SVG is
 * stretched horizontally to fill its container, which would distort text.
 */
export function ChartCard({
  title,
  subtitle,
  readout,
  children,
  startLabel,
  endLabel,
  footer,
}: {
  title: string;
  subtitle?: string;
  readout?: React.ReactNode;
  children: React.ReactNode;
  startLabel?: string;
  endLabel?: string;
  footer?: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-border bg-surface p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="text-sm font-medium">{title}</h2>
          {subtitle && <p className="mt-0.5 text-xs text-text-muted">{subtitle}</p>}
        </div>
        {readout}
      </div>

      <div className="mt-4">{children}</div>

      {(startLabel || endLabel) && (
        <div className="mt-1.5 flex justify-between text-[10px] text-text-muted">
          <span>{startLabel}</span>
          <span>{endLabel}</span>
        </div>
      )}

      {footer && <div className="mt-3 text-xs leading-relaxed text-text-muted">{footer}</div>}
    </section>
  );
}

/** Legend explaining the hatched band, shown wherever a chart contains one. */
export function GapLegend() {
  return (
    <p className="flex items-center gap-2 text-xs text-text-muted">
      <span
        aria-hidden
        className="inline-block h-3 w-6 rounded-sm border border-border"
        style={{
          backgroundImage:
            "repeating-linear-gradient(45deg, var(--color-border) 0 2px, transparent 2px 5px)",
        }}
      />
      Hatched means not observed. Collection stopped, so nothing was recorded — this is
      not a period of zero growth.
    </p>
  );
}
