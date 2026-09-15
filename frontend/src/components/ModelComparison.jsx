import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { money, moneyCompact } from '../format';
import { MODEL_ORDER, channelLabel, modelLabel } from '../theme';
import { comparisonChartRows } from '../selectors';
import { Card, EmptyState, InlineError, Skeleton } from './primitives';

function ComparisonTooltip({ active, payload, label, colors }) {
  if (!active || !payload?.length) return null;
  const sorted = [...payload].sort((a, b) => b.value - a.value);
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip-title">{channelLabel(label)}</div>
      {sorted.map((entry) => (
        <div className="chart-tooltip-row" key={entry.dataKey}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <span
              className="swatch"
              style={{ background: colors.model[entry.dataKey] }}
              aria-hidden="true"
            />
            {modelLabel(entry.dataKey)}
          </span>
          <strong>{money(entry.value)}</strong>
        </div>
      ))}
    </div>
  );
}

export default function ModelComparison({ state, colors, onRetry }) {
  const { data, error, isLoading, isRefreshing } = state;
  const channels = data?.channels ?? [];
  const chartRows = comparisonChartRows(data, MODEL_ORDER);
  const biggestSwing = channels.length ? channels[0].swing : null;

  // Grouped bars: 5 per channel, so the container needs room for the group
  // plus the axis band underneath.
  const chartHeight = Math.max(280, chartRows.length * 68 + 46);

  return (
    <section className="section" aria-labelledby="comparison-heading">
      <div className="section-head">
        <h2 id="comparison-heading" className="section-title">
          Section 3 · Model comparison
        </h2>
        <p className="section-note">
          Each channel&rsquo;s bar height depends entirely on which attribution
          model you believe. A taller spread within a group means more contested
          revenue.
        </p>
      </div>

      <Card
        title="Attributed revenue by channel, under all five models"
        subtitle="Five bars per channel — one per model. The total across channels is identical for every model; only the split differs."
      >
        {error && !channels.length ? (
          <InlineError error={error} onRetry={onRetry} />
        ) : isLoading ? (
          <>
            <Skeleton height={300} />
            <Skeleton height={140} style={{ marginTop: 14 }} />
          </>
        ) : !channels.length ? (
          <EmptyState>
            No comparison data yet. Run the pipeline to populate the dashboard.
          </EmptyState>
        ) : (
          <div className={isRefreshing ? 'is-refreshing' : undefined}>
            <div className="chart-frame" style={{ height: chartHeight }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={chartRows}
                  layout="vertical"
                  margin={{ top: 4, right: 20, bottom: 4, left: 6 }}
                  barCategoryGap="26%"
                  barGap={2}
                >
                  <CartesianGrid
                    horizontal={false}
                    stroke={colors.grid}
                    strokeWidth={1}
                  />
                  <XAxis
                    type="number"
                    tickFormatter={moneyCompact}
                    tick={{ fill: colors.axis, fontSize: 11 }}
                    axisLine={{ stroke: colors.grid }}
                    tickLine={false}
                  />
                  <YAxis
                    type="category"
                    dataKey="channel"
                    width={92}
                    tick={{ fill: colors.axis, fontSize: 11.5 }}
                    tickFormatter={channelLabel}
                    axisLine={false}
                    tickLine={false}
                  />
                  <Tooltip
                    content={<ComparisonTooltip colors={colors} />}
                    cursor={{ fill: colors.grid, fillOpacity: 0.4 }}
                  />
                  {MODEL_ORDER.map((modelName) => (
                    <Bar
                      key={modelName}
                      dataKey={modelName}
                      name={modelLabel(modelName)}
                      fill={colors.model[modelName]}
                      radius={[0, 3, 3, 0]}
                      maxBarSize={11}
                      isAnimationActive
                      animationDuration={520}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            </div>

            {/* A legend is always present for >= 2 series, so identity is
                never carried by colour alone. */}
            <ul className="legend">
              {MODEL_ORDER.map((modelName) => (
                <li className="legend-item" key={modelName}>
                  <span
                    className="swatch"
                    style={{ background: colors.model[modelName] }}
                    aria-hidden="true"
                  />
                  {modelLabel(modelName)}
                </li>
              ))}
            </ul>

            <div className="table-wrap">
              <table className="data">
                <caption>
                  Sorted by swing, descending. The largest disagreement is
                  highlighted.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Channel</th>
                    {MODEL_ORDER.map((modelName) => (
                      <th scope="col" key={modelName}>
                        {modelLabel(modelName)}
                      </th>
                    ))}
                    <th scope="col">Swing</th>
                  </tr>
                </thead>
                <tbody>
                  {channels.map((row) => (
                    <tr
                      key={row.channel}
                      className={
                        row.swing === biggestSwing ? 'is-highlight' : undefined
                      }
                    >
                      <td>
                        <span className="cell-channel">
                          <span
                            className="swatch"
                            style={{
                              background:
                                colors.channel[row.channel] || colors.fallback,
                            }}
                            aria-hidden="true"
                          />
                          {channelLabel(row.channel)}
                        </span>
                      </td>
                      {MODEL_ORDER.map((modelName) => {
                        const value = row.by_model?.[modelName];
                        const isMax = value === row.max_revenue;
                        const isMin = value === row.min_revenue;
                        return (
                          <td
                            className="num"
                            key={modelName}
                            style={
                              isMax
                                ? { fontWeight: 660, color: 'var(--text-1)' }
                                : isMin
                                  ? { color: 'var(--text-3)' }
                                  : undefined
                            }
                          >
                            {money(value)}
                          </td>
                        );
                      })}
                      <td className="num" style={{ fontWeight: 660 }}>
                        {money(row.swing)}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <th scope="row">Total</th>
                    {MODEL_ORDER.map((modelName) => (
                      <td className="num" key={modelName}>
                        {money(data?.totals_by_model?.[modelName])}
                      </td>
                    ))}
                    <td className="num" style={{ fontWeight: 660 }}>
                      {money(data?.total_swing)}
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>

            <p className="section-note" style={{ marginTop: 12 }}>
              Every model column totals exactly the same revenue — the models
              never disagree about how much was earned, only about which
              channel earned it. {money(data?.total_swing)} of revenue is
              contested across all channels.
            </p>
          </div>
        )}
      </Card>
    </section>
  );
}
