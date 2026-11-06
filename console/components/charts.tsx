"use client";

/**
 * 极简图表组件（手写 SVG，不引图表库）。
 *
 * 为什么不引 recharts/echarts：这个控制台只需要「趋势 + 排行」两种图，加起来不到 200 行；
 * 引一个库要多 200KB bundle、多一套主题覆盖，还会在评审时把注意力从"数据对不对"移到
 * "配色好不好"。真要的是多序列叠加、时间缩放、交互缩放时再换——那时候换的成本也不会更高。
 *
 * 三个刻意的细节：
 * 1. 用 viewBox 而不是像素宽度：容器怎么变都不糊，也不需要在 resize 里重算。
 * 2. 每个点都有 `<title>`：鼠标停上去能看到确切数值，不靠猜高度。
 * 3. 全零序列不画成"平地"压到底：给一条贴底的灰线 + 一句"窗口内没有流量"，
 *    否则空数据看起来像"服务挂了但图很好看"。
 */

type Point = { label: string; value: number; hint?: string };

export function AreaChart({
  data,
  height = 150,
  stroke = "var(--accent)",
  fill = "rgba(88, 166, 255, 0.16)",
  format = (v: number) => String(v),
}: {
  data: Point[];
  height?: number;
  stroke?: string;
  fill?: string;
  format?: (v: number) => string;
}) {
  if (data.length === 0) return <div className="empty">窗口内没有数据</div>;

  const W = 1000;
  const H = 300;
  const pad = { l: 8, r: 8, t: 14, b: 26 };
  const innerW = W - pad.l - pad.r;
  const innerH = H - pad.t - pad.b;
  const max = Math.max(1, ...data.map((d) => d.value));
  const step = data.length > 1 ? innerW / (data.length - 1) : 0;

  const xy = data.map((d, i) => ({
    x: pad.l + (data.length === 1 ? innerW / 2 : i * step),
    y: pad.t + innerH - (d.value / max) * innerH,
    d,
  }));

  const line = xy.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const area = `${line} L${xy[xy.length - 1].x.toFixed(1)},${pad.t + innerH} L${xy[0].x.toFixed(1)},${pad.t + innerH} Z`;
  const flat = data.every((d) => d.value === 0);

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      style={{ width: "100%", height, display: "block" }}
      role="img"
      aria-label="趋势图"
    >
      {/* 三条参考线：顶部 / 中位 / 底部，读图时不用量像素 */}
      {[0, 0.5, 1].map((r) => (
        <line
          key={r}
          x1={pad.l}
          x2={W - pad.r}
          y1={pad.t + innerH * r}
          y2={pad.t + innerH * r}
          stroke="var(--border)"
          strokeWidth={1}
          strokeDasharray={r === 1 ? "0" : "3 5"}
        />
      ))}

      {flat ? (
        <line
          x1={pad.l}
          x2={W - pad.r}
          y1={pad.t + innerH}
          y2={pad.t + innerH}
          stroke="var(--muted-foreground)"
          strokeWidth={2}
          opacity={0.5}
        />
      ) : (
        <>
          <path d={area} fill={fill} stroke="none" />
          <path d={line} fill="none" stroke={stroke} strokeWidth={2.5} strokeLinejoin="round" />
        </>
      )}

      {xy.map((p) => (
        <g key={p.d.label}>
          <circle cx={p.x} cy={p.y} r={4} fill="var(--card)" stroke={stroke} strokeWidth={2}>
            <title>{`${p.d.label} · ${format(p.d.value)}`}</title>
          </circle>
          {/* 命中区域比圆点大：鼠标不用瞄准 */}
          <rect x={p.x - step / 2 || 0} y={pad.t} width={Math.max(step, 24)} height={innerH} fill="transparent">
            <title>{`${p.d.label} · ${format(p.d.value)}`}</title>
          </rect>
        </g>
      ))}

      {xy.map((p, i) => {
        // 标签太密会互相压住：>8 个点时只显示首尾与中位
        const show = data.length <= 8 || i === 0 || i === data.length - 1 || i === Math.floor(data.length / 2);
        if (!show) return null;
        return (
          <text
            key={`t-${p.d.label}`}
            x={p.x}
            y={H - 6}
            textAnchor={i === 0 ? "start" : i === data.length - 1 ? "end" : "middle"}
            fill="var(--muted-foreground)"
            fontSize={16}
          >
            {p.d.label}
          </text>
        );
      })}
    </svg>
  );
}

export function BarList({
  items,
  format = (v: number) => String(v),
  empty = "暂无数据",
}: {
  items: { label: string; value: number; hint?: string; tone?: string }[];
  format?: (v: number) => string;
  empty?: string;
}) {
  if (items.length === 0) return <div className="empty">{empty}</div>;
  const max = Math.max(1, ...items.map((i) => i.value));

  return (
    <div className="barlist">
      {items.map((item) => (
        <div className="barline" key={item.label}>
          <div className="barline-label mono" title={item.label}>
            {item.label}
          </div>
          <div className="barline-track">
            <div
              className="barline-fill"
              style={{
                width: `${Math.max(2, (item.value / max) * 100)}%`,
                background: item.tone ?? "var(--accent)",
              }}
            />
          </div>
          <div className="barline-value mono">
            {format(item.value)}
            {item.hint && <span className="sub"> {item.hint}</span>}
          </div>
        </div>
      ))}
    </div>
  );
}

/** 卡片里的小趋势线：只要形状，不要坐标轴。 */
export function Spark({ values, height = 28 }: { values: number[]; height?: number }) {
  if (values.length < 2) return <div style={{ height }} />;
  const W = 120;
  const H = 32;
  const max = Math.max(1, ...values);
  const step = W / (values.length - 1);
  const d = values
    .map((v, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${(H - (v / max) * H).toFixed(1)}`)
    .join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height, display: "block" }} aria-hidden>
      <path d={d} fill="none" stroke="var(--accent)" strokeWidth={2} opacity={0.75} />
    </svg>
  );
}