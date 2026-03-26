import { useState } from 'react'
import { pnl as pnlApi } from '../lib/api'
import { useApi } from '../hooks/useApi'
import { PageSpinner, SectionHeader, EmptyState } from '../components/ui'
import {
  ComposedChart, Bar, Line, XAxis, YAxis, Tooltip,
  ResponsiveContainer, ReferenceLine, Cell, Legend,
} from 'recharts'
import { clsx } from 'clsx'

const PERIODS = [
  { key: '15min', label: '15min' },
  { key: 'daily', label: 'Daily' },
  { key: 'weekly', label: 'Weekly' },
]

const VIEWS = [
  { key: 'period', label: 'Period' },
  { key: 'cumulative', label: 'Cumulative' },
]

export default function PnlCharts() {
  const [period, setPeriod] = useState('daily')
  const [view,   setView]   = useState('period')

  const { data, loading, refetch } = useApi(
    () => pnlApi.summary(period),
    [period]
  )

  return (
    <div className="space-y-4">
      {/* Controls */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {/* Period toggle */}
          <div className="flex bg-base-800 rounded-md p-0.5 gap-0.5">
            {PERIODS.map(p => (
              <button
                key={p.key}
                onClick={() => setPeriod(p.key)}
                className={clsx(
                  'px-3 py-1 text-xs font-mono rounded transition-colors',
                  period === p.key
                    ? 'bg-base-600 text-base-50'
                    : 'text-base-400 hover:text-base-200'
                )}
              >
                {p.label}
              </button>
            ))}
          </div>
          {/* View toggle */}
          <div className="flex bg-base-800 rounded-md p-0.5 gap-0.5">
            {VIEWS.map(v => (
              <button
                key={v.key}
                onClick={() => setView(v.key)}
                className={clsx(
                  'px-3 py-1 text-xs font-mono rounded transition-colors',
                  view === v.key
                    ? 'bg-base-600 text-base-50'
                    : 'text-base-400 hover:text-base-200'
                )}
              >
                {v.label}
              </button>
            ))}
          </div>
        </div>
        <button
          onClick={refetch}
          className="text-xs text-base-400 hover:text-base-200"
        >
          ↻ Refresh
        </button>
      </div>

      {/* Charts grid */}
      {loading ? (
        <div className="flex justify-center py-10"><PageSpinner /></div>
      ) : !data?.length ? (
        <EmptyState
          icon="📊"
          title="No broker accounts"
          description="Add a broker account to see P&L charts."
        />
      ) : data.every(a => a.bars.length === 0) ? (
        <EmptyState
          icon="📊"
          title="No trade history yet"
          description="P&L charts will appear once orders start filling."
        />
      ) : (
        <div className={clsx(
          'grid gap-4',
          data.length === 1 ? 'grid-cols-1' :
          data.length === 2 ? 'grid-cols-2' :
          'grid-cols-2 xl:grid-cols-3'
        )}>
          {data.map(account => (
            <AccountPnlCard
              key={`${account.broker}-${account.account}`}
              account={account}
              period={period}
              view={view}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function AccountPnlCard({ account, period, view }) {
  const hasData   = account.bars.length > 0
  const totalReal = account.bars.reduce((s, b) => s + b.realized_pnl, 0)
  const totalUnreal = account.bars.length > 0
    ? account.bars[account.bars.length - 1].unrealized_pnl
    : 0
  const totalPnl  = totalReal + totalUnreal
  const isPositive = totalPnl >= 0

  return (
    <div className="panel overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-base-800">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm font-medium text-base-100">
              {account.display_name || `${account.broker} / ${account.account}`}
            </div>
            <div className="text-xs text-base-500 font-mono mt-0.5">
              {account.broker} · {account.account}
            </div>
          </div>
          <div className="text-right">
            <div className={clsx(
              'text-sm font-mono font-semibold',
              isPositive ? 'text-accent' : 'text-loss'
            )}>
              {isPositive ? '+' : ''}{totalPnl.toFixed(2)}
            </div>
            <div className="text-[10px] text-base-500 mt-0.5">
              {totalReal.toFixed(2)} real · {totalUnreal.toFixed(2)} unreal
            </div>
          </div>
        </div>
      </div>

      {/* Chart */}
      {!hasData ? (
        <div className="flex items-center justify-center h-32 text-xs text-base-600">
          No fills in this period
        </div>
      ) : (
        <div className="px-2 py-3">
          <ResponsiveContainer width="100%" height={160}>
            <ComposedChart
              data={account.bars}
              margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
            >
              <XAxis
                dataKey="period_start"
                tickFormatter={d => fmtLabel(d, period)}
                tick={{ fill: '#7e7e90', fontSize: 10, fontFamily: 'JetBrains Mono' }}
                axisLine={false}
                tickLine={false}
                interval="preserveStartEnd"
              />
              <YAxis
                tick={{ fill: '#7e7e90', fontSize: 10, fontFamily: 'JetBrains Mono' }}
                axisLine={false}
                tickLine={false}
                tickFormatter={v => v >= 0 ? `+${v.toFixed(0)}` : v.toFixed(0)}
                width={45}
              />
              <Tooltip content={<CustomTooltip period={period} />} />
              <ReferenceLine y={0} stroke="#2e2e36" strokeWidth={1} />

              {view === 'period' ? (
                <>
                  {/* Realized bars */}
                  <Bar dataKey="realized_pnl" name="Realized" radius={[2, 2, 0, 0]}>
                    {account.bars.map((b, i) => (
                      <Cell
                        key={i}
                        fill={b.realized_pnl >= 0 ? '#00e5a0' : '#ff4d4d'}
                        fillOpacity={0.85}
                      />
                    ))}
                  </Bar>
                  {/* Unrealized overlay */}
                  <Bar dataKey="unrealized_pnl" name="Unrealized" radius={[2, 2, 0, 0]}>
                    {account.bars.map((b, i) => (
                      <Cell
                        key={i}
                        fill={b.unrealized_pnl >= 0 ? '#00e5a0' : '#ff4d4d'}
                        fillOpacity={0.35}
                      />
                    ))}
                  </Bar>
                </>
              ) : (
                <>
                  {/* Cumulative realized line */}
                  <Line
                    type="monotone"
                    dataKey="cumulative_realized"
                    name="Cum. Realized"
                    stroke="#00e5a0"
                    strokeWidth={1.5}
                    dot={false}
                    activeDot={{ r: 3, fill: '#00e5a0' }}
                  />
                  {/* Cumulative total line (includes unrealized) */}
                  <Line
                    type="monotone"
                    dataKey="cumulative_total"
                    name="Cum. Total"
                    stroke="#7eb8ff"
                    strokeWidth={1.5}
                    strokeDasharray="4 2"
                    dot={false}
                    activeDot={{ r: 3, fill: '#7eb8ff' }}
                  />
                </>
              )}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  )
}

function CustomTooltip({ active, payload, label, period }) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-base-900 border border-base-700 rounded-md px-3 py-2 text-xs font-mono shadow-lg">
      <div className="text-base-400 mb-1.5">{fmtLabel(label, period)}</div>
      {payload.map((p, i) => (
        <div key={i} className="flex items-center justify-between gap-4">
          <span style={{ color: p.color }}>{p.name}</span>
          <span className={p.value >= 0 ? 'text-accent' : 'text-loss'}>
            {p.value >= 0 ? '+' : ''}{Number(p.value).toFixed(2)}
          </span>
        </div>
      ))}
    </div>
  )
}

function fmtLabel(isoString, period) {
  const d = new Date(isoString)
  if (period === '15min') {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
  if (period === 'daily') {
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  }
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}
