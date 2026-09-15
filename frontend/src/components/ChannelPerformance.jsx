import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { conversions, money, moneyCompact, roas } from '../format';
import { channelLabel } from '../theme';
import { Card, EmptyState, InlineError, Skeleton } from './primitives';

function ChannelTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  return (
    <div className="chart-tooltip">
      <div className="chart-tooltip-title">{channelLabel(row.channel)}</div>
      <div className="chart-tooltip-row">
        <span>Attributed</span>
        <strong>{money(row.attributed_revenue)}</strong>
      </div>
      <div className="chart-tooltip-row">
        <span>Spend</span>
        <strong>{money(row.spend)}</strong>
      </div>
      <div className="chart-tooltip-row">
        <span>ROAS</span>
        <strong>{roas(row.roas)}</strong>
      </div>
      <div className="chart-tooltip-row">
        <span>Conversions</span>
        <strong>{conversions(row.attributed_conversions)}</strong>
      </div>
    </div>
  );
}

export default function ChannelPerformance({ state, model, colors, onRetry }) {
  const { data, error, isLoading, isRefreshing } = state;
  const rows = data?.data ?? [];

  // Already sorted by attributed revenue descending server-side; bars
  // re-order as the model changes while each channel keeps its colour.
  const chartRows = rows.map((row) => ({
    ...row,
    attributed_revenue: Number(row.attributed_revenue) || 0,
  }));

  // Size the container to include the axis band so the card never gets a
  // nested scrollbar.
  const chartHeight = Math.max(200, chartRows.length * 38 + 34);

  return (
    <section className="section" aria-labelledby="channels-heading">
      <div className="section-head">
        <h2 id="channels-heading" className="section-title">
          Channel performance
        </h2>
        <p className="section-note">
          Attributed revenue by channel under {model}. Switch the model above
          and the bars re-order — each channel keeps its colour, so only the
          ranking moves.
        </p>
      </div>

      <Card>
        {error && !rows.length ? (
          <InlineError error={error} onRetry={onRetry} />
        ) : isLoading ? (
          <>
            <Skeleton height={232} />
            <Skeleton height={120} style={{ marginTop: 14 }} />
          </>
        ) : !rows.length ? (
          <EmptyState>
            No channel results yet. Run the pipeline to populate the dashboard.
          </EmptyState>
        ) : (
          <div className={isRefreshing ? 'is-refreshing' : undefined}>
            <div className="chart-frame" style={{ height: chartHeight }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={chartRows}
                  layout="vertical"
                  margin={{ top: 4, right: 24, bottom: 4, left: 6 }}
                  barCategoryGap="22%"
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
                    content={<ChannelTooltip />}
                    cursor={{ fill: colors.grid, fillOpacity: 0.7 }}
                  />
                  <Bar
                    dataKey="attributed_revenue"
                    radius={[0, 3, 3, 0]}
                    isAnimationActive
                    animationDuration={620}
                    animationEasing="ease-out"
                    maxBarSize={18}
                  >
                    {chartRows.map((row) => (
                      <Cell
                        key={row.channel}
                        fill={colors.channel[row.channel] || colors.fallback}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>

            <div className="table-wrap">
              <table className="data">
                <caption>
                  Table view of the chart above — the WCAG-clean equivalent.
                  ROAS shows an em dash where there was no spend.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Channel</th>
                    <th scope="col">Attributed revenue</th>
                    <th scope="col">Spend</th>
                    <th scope="col">ROAS</th>
                    <th scope="col">Conversions</th>
                    <th scope="col">Touchpoints</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.channel}>
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
                      <td className="num">{money(row.attributed_revenue)}</td>
                      <td className="num">{money(row.spend)}</td>
                      <td className="num">{roas(row.roas)}</td>
                      <td className="num">
                        {conversions(row.attributed_conversions)}
                      </td>
                      <td className="num">{row.touchpoint_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </Card>
    </section>
  );
}
