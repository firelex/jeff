import { useMemo, useState } from 'react';

// A small line chart: one y axis, 2px lines, markers for sparse series, recessive grid, a crosshair with a tooltip,
// and a legend when there are two or more series. Colours come from the series (validated categorical slots).
export interface Series { name: string; color: string; points: { x: number; y: number }[]; markers?: boolean }

export const SERIES_COLORS = ['#7547fe', '#0e9f8e', '#d97706', '#2563eb']; // violet (the deck's --v), teal, amber, blue

export function LineChart({ series, xLabel, yLabel, xMax, yMin, yMax, format = (v) => v.toFixed(2), height = 180 }: {
  series: Series[]; xLabel: string; yLabel: string; xMax?: number; yMin?: number; yMax?: number;
  format?: (value: number) => string; height?: number;
}) {
  const width = 560;
  const pad = { left: 44, right: 12, top: 10, bottom: 28 };
  const all = series.flatMap((s) => s.points);
  const x1 = xMax ?? Math.max(1, ...all.map((p) => p.x));
  const ys = all.map((p) => p.y);
  const lo = yMin ?? (ys.length ? Math.min(...ys) : 0);
  const hi = yMax ?? (ys.length ? Math.max(...ys) : 1);
  const span = hi - lo || 1;
  const sx = (x: number) => pad.left + (x / x1) * (width - pad.left - pad.right);
  const sy = (y: number) => pad.top + (1 - (y - lo) / span) * (height - pad.top - pad.bottom);
  const ticks = useMemo(() => [0, 0.25, 0.5, 0.75, 1].map((f) => lo + f * span), [lo, span]);
  const [hover, setHover] = useState<number | null>(null);

  const nearest = (s: Series, x: number) => s.points.reduce<{ x: number; y: number } | null>(
    (best, p) => (best === null || Math.abs(p.x - x) < Math.abs(best.x - x) ? p : best), null);

  return (
    <div className="w-full">
      {series.length > 1 && (
        <div className="mb-2 flex flex-wrap gap-4 text-xs text-mute">
          {series.map((s) => (
            <span key={s.name} className="inline-flex items-center gap-1.5">
              <span className="inline-block h-0.5 w-4 rounded" style={{ background: s.color }} aria-hidden="true" />{s.name}
            </span>
          ))}
        </div>
      )}
      <div className="relative">
        <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label={`${yLabel} by ${xLabel}`}
          onMouseLeave={() => setHover(null)}
          onMouseMove={(event) => {
            const box = event.currentTarget.getBoundingClientRect();
            const x = ((event.clientX - box.left) / box.width * width - pad.left) / (width - pad.left - pad.right) * x1;
            setHover(Math.max(0, Math.min(x1, x)));
          }}>
          {ticks.map((tick) => (
            <g key={tick}>
              <line x1={pad.left} x2={width - pad.right} y1={sy(tick)} y2={sy(tick)} stroke="var(--line)" strokeWidth={1} />
              <text x={pad.left - 6} y={sy(tick) + 3} textAnchor="end" fontSize={10} fill="var(--mute)">{format(tick)}</text>
            </g>
          ))}
          <text x={width - pad.right} y={height - 6} textAnchor="end" fontSize={10} fill="var(--mute)">{xLabel} (of {x1})</text>
          <text x={pad.left} y={height - 6} fontSize={10} fill="var(--mute)">0</text>
          {series.map((s) => (
            <g key={s.name}>
              {s.points.length > 1 && (
                <polyline fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round"
                  points={s.points.map((p) => `${sx(p.x)},${sy(p.y)}`).join(' ')} />
              )}
              {(s.markers || s.points.length === 1) && s.points.map((p) => (
                <circle key={p.x} cx={sx(p.x)} cy={sy(p.y)} r={4} fill={s.color} stroke="var(--panel)" strokeWidth={2} />
              ))}
            </g>
          ))}
          {hover !== null && all.length > 0 && (
            <line x1={sx(hover)} x2={sx(hover)} y1={pad.top} y2={height - pad.bottom} stroke="var(--mute)" strokeDasharray="3 3" strokeWidth={1} />
          )}
        </svg>
        {hover !== null && all.length > 0 && (
          <div className="pointer-events-none absolute top-1 rounded-button border border-line bg-panel px-2.5 py-1.5 text-xs shadow-card"
            style={{ left: `${Math.min(70, (sx(hover) / width) * 100)}%` }}>
            {series.map((s) => {
              const p = nearest(s, hover);
              return p && (
                <div key={s.name} className="flex items-center gap-2 whitespace-nowrap">
                  <span className="inline-block size-2 rounded-full" style={{ background: s.color }} aria-hidden="true" />
                  <span className="text-mute">{s.name}</span>
                  <span className="font-mono text-text">{format(p.y)}</span>
                  <span className="text-mute">at {xLabel} {Math.round(p.x)}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
