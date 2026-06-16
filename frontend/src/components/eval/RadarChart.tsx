interface RadarChartProps {
  dimensions: Array<{ name: string; score: number | null }>;
  dimensionsB?: Array<{ name: string; score: number | null }>;
  size?: number;
}

/**
 * Compute a point on a regular pentagon.
 * Starts at 12 o'clock (top), goes clockwise.
 * @param index  vertex index 0-4
 * @param radius distance from center
 * @param cx     center X
 * @param cy     center Y
 */
function pentagonPoint(
  index: number,
  radius: number,
  cx: number,
  cy: number
): [number, number] {
  const angle = (index * 2 * Math.PI) / 5 - Math.PI / 2;
  return [cx + radius * Math.cos(angle), cy + radius * Math.sin(angle)];
}

function pointsToPath(pts: [number, number][]): string {
  return pts.map(([x, y], i) => `${i === 0 ? "M" : "L"} ${x} ${y}`).join(" ") + " Z";
}

export function RadarChart({ dimensions, dimensionsB, size = 200 }: RadarChartProps) {
  const cx = size / 2;
  const cy = size / 2;
  const maxRadius = size / 2 - 24;
  const levels = 5;
  const hasCompare = dimensionsB && dimensionsB.length === 5;

  const bgLevels = Array.from({ length: levels }, (_, lvl) => {
    const r = (maxRadius * (lvl + 1)) / levels;
    const pts = Array.from({ length: 5 }, (__, i) => pentagonPoint(i, r, cx, cy));
    return { r, pts };
  });

  // Data A polygon (green)
  const dataPtsA: [number, number][] = dimensions.map((d, i) => {
    const score = d.score ?? 0;
    const r = (maxRadius * Math.max(0, Math.min(5, score))) / levels;
    return pentagonPoint(i, r, cx, cy);
  });

  // Data B polygon (blue) — only when comparing
  const dataPtsB: [number, number][] | null = hasCompare
    ? dimensionsB.map((d, i) => {
        const score = d.score ?? 0;
        const r = (maxRadius * Math.max(0, Math.min(5, score))) / levels;
        return pentagonPoint(i, r, cx, cy);
      })
    : null;

  const labelOffset = maxRadius + 14;
  const scoreOffset = maxRadius + 4;

  return (
    <svg width={size} height={size} className="overflow-visible">
      {/* Background grid */}
      {bgLevels.map(({ pts }, lvl) => (
        <polygon
          key={lvl}
          points={pts.map(([x, y]) => `${x},${y}`).join(" ")}
          fill="none"
          className="stroke-gray-200"
          strokeWidth={1}
        />
      ))}

      {/* Axis spokes */}
      {Array.from({ length: 5 }, (_, i) => {
        const [ax, ay] = pentagonPoint(i, maxRadius, cx, cy);
        return (
          <line key={i} x1={cx} y1={cy} x2={ax} y2={ay} className="stroke-gray-200" strokeWidth={1} />
        );
      })}

      {/* Data B polygon (blue, drawn first so A overlays) */}
      {dataPtsB && (
        <>
          <path
            d={pointsToPath(dataPtsB)}
            className="fill-blue-400/20 stroke-blue-500"
            strokeWidth={1.5}
            strokeLinejoin="round"
            strokeDasharray="4 2"
          />
          {dataPtsB.map(([x, y], i) => (
            <circle key={`b-${i}`} cx={x} cy={y} r={2} className="fill-blue-500" />
          ))}
        </>
      )}

      {/* Data A polygon (green) */}
      <path
        d={pointsToPath(dataPtsA)}
        className="fill-emerald-400/20 stroke-emerald-500"
        strokeWidth={1.5}
        strokeLinejoin="round"
      />
      {dataPtsA.map(([x, y], i) => (
        <circle key={`a-${i}`} cx={x} cy={y} r={2.5} className="fill-emerald-500" />
      ))}

      {/* Labels and scores */}
      {dimensions.map((d, i) => {
        const [lx, ly] = pentagonPoint(i, labelOffset, cx, cy);
        const [sx, sy] = pentagonPoint(i, scoreOffset, cx, cy);
        const dx = lx - cx;
        const anchor = dx > 8 ? "start" : dx < -8 ? "end" : "middle";

        const scoreA = d.score;
        const scoreB = hasCompare ? dimensionsB[i].score : null;

        return (
          <g key={i}>
            <text x={lx} y={ly} textAnchor={anchor} dominantBaseline="middle" fontSize={10} className="fill-gray-500">
              {d.name}
            </text>
            {hasCompare ? (
              <text x={sx} y={sy} textAnchor="middle" dominantBaseline="middle" fontSize={9} className="fill-gray-600">
                <tspan className="fill-emerald-600" fontWeight={600}>{scoreA ?? "—"}</tspan>
                <tspan className="fill-gray-400">{" → "}</tspan>
                <tspan className="fill-blue-600" fontWeight={600}>{scoreB ?? "—"}</tspan>
              </text>
            ) : (
              <text x={sx} y={sy} textAnchor="middle" dominantBaseline="middle" fontSize={10} fontWeight={600} className="fill-gray-700">
                {scoreA !== null ? scoreA : "—"}
              </text>
            )}
          </g>
        );
      })}

      {/* Legend (only in compare mode) */}
      {hasCompare && (
        <g>
          <rect x={size - 60} y={4} width={8} height={8} rx={1} className="fill-emerald-500" />
          <text x={size - 48} y={10} fontSize={9} dominantBaseline="middle" className="fill-gray-500">版本A</text>
          <rect x={size - 60} y={16} width={8} height={8} rx={1} className="fill-blue-500" />
          <text x={size - 48} y={22} fontSize={9} dominantBaseline="middle" className="fill-gray-500">版本B</text>
        </g>
      )}
    </svg>
  );
}
