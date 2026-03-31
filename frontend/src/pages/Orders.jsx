import { useState, useEffect } from 'react'
import { orders as ordersApi } from '../lib/api'
import { useApi } from '../hooks/useApi'
import {
  PageSpinner, SectionHeader, StatusBadge, Mono, EmptyState, brokerLabel, BrokerIcon
} from '../components/ui'

const BROKERS  = ['', 'oanda', 'ibkr', 'tradovate', 'etrade']
const STATUSES = ['', 'filled', 'open', 'submitted', 'cancelled', 'rejected', 'error']

export default function OrdersPage() {
  const [broker, setBroker]   = useState('')
  const [status, setStatus]   = useState('')
  const [symbol, setSymbol]   = useState('')

  const params = {}
  if (broker) params.broker = broker
  if (status) params.status = status
  if (symbol) params.symbol = symbol.toUpperCase()
  params.limit = 100

  const { data, loading, refetch } = useApi(
    () => ordersApi.list(params),
    [broker, status, symbol]
  )

  return (
    <div className="space-y-6 animate-fade-in">
      <SectionHeader title="Orders" description="Complete execution history" />

      {/* Filters */}
      <div className="flex flex-wrap gap-3 panel p-4">
        <input
          className="input w-36 py-1.5"
          placeholder="Symbol…"
          value={symbol}
          onChange={e => setSymbol(e.target.value)}
        />
        <select
          className="input w-36 py-1.5"
          value={broker}
          onChange={e => setBroker(e.target.value)}
        >
          <option value="">All brokers</option>
          {BROKERS.filter(Boolean).map(b => (
            <option key={b} value={b}>{brokerLabel(b)}</option>
          ))}
        </select>
        <select
          className="input w-40 py-1.5"
          value={status}
          onChange={e => setStatus(e.target.value)}
        >
          <option value="">All statuses</option>
          {STATUSES.filter(Boolean).map(s => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <button onClick={refetch} className="btn-ghost py-1.5 px-3 text-xs">↻ Refresh</button>
      </div>

      {/* Table */}
      <div className="panel overflow-hidden">
        {loading ? (
          <div className="flex justify-center py-16"><PageSpinner /></div>
        ) : !data?.length ? (
          <EmptyState icon="📋" title="No orders found" description="Try adjusting the filters." />
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Symbol</th>
                  <th>Action</th>
                  <th>Type</th>
                  <th>Qty</th>
                  <th>Broker Qty</th>
                  <th>Price</th>
                  <th>Fill price</th>
                  <th>Broker / Account</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {data.map(order => <OrderRow key={order.id} order={order} />)}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {data && (
        <p className="text-xs text-base-500 text-right">
          Showing {data.length} order{data.length !== 1 ? 's' : ''}
        </p>
      )}
    </div>
  )
}

function OrderRow({ order }) {
  const [expanded, setExpanded] = useState(false)
  const isBuy   = order.action === 'buy'
  const isClose = order.action === 'close'
  const actionColor = isBuy ? 'text-accent' : isClose ? 'text-warn' : 'text-loss'
  const hasDetail = order.error_message || order.broker_request || order.broker_response

  const fmtJson = (str) => {
    try { return JSON.stringify(JSON.parse(str), null, 2) }
    catch { return str }
  }

  return (
    <>
      <tr
        onClick={() => hasDetail && setExpanded(v => !v)}
        className={hasDetail ? 'cursor-pointer hover:bg-base-800/50' : ''}
      >
        <td>
          <span className="font-mono text-xs text-base-400">
            {new Date(order.created_at).toLocaleString([], {
              month: 'short', day: 'numeric',
              hour: '2-digit', minute: '2-digit'
            })}
          </span>
        </td>
        <td>
          <span className="font-mono font-medium text-base-100">{order.symbol}</span>
          {order.instrument_type !== 'forex' && (
            <span className="ml-1 text-[10px] text-base-500">{order.instrument_type}</span>
          )}
        </td>
        <td>
          <span className={`font-mono font-semibold uppercase text-xs ${actionColor}`}>
            {order.action}
          </span>
        </td>
        <td><span className="text-base-400 text-xs font-mono">{order.order_type}</span></td>
        <td><Mono>{order.quantity.toLocaleString()}</Mono></td>
        <td>
          {order.broker_quantity != null && order.broker_quantity !== order.quantity ? (
            <div className="text-right">
              <Mono className="text-warn">{order.broker_quantity.toLocaleString()}</Mono>
              <div className="text-[10px] text-base-600 font-mono">randomized</div>
            </div>
          ) : (
            <Mono className="text-base-500">—</Mono>
          )}
        </td>
        <td>
          <Mono className="text-base-400">
            {order.price ? order.price.toFixed(5) : '—'}
          </Mono>
        </td>
        <td>
          <Mono>
            {order.avg_fill_price ? order.avg_fill_price.toFixed(5) : '—'}
          </Mono>
        </td>
        <td>
          <span className="flex items-center gap-1 text-xs text-base-400">
            <BrokerIcon broker={order.broker} size={12} />
            {brokerLabel(order.broker)}
          </span>
          <div className="text-[10px] font-mono text-base-600">{order.account}</div>
        </td>
        <td><StatusBadge status={order.status} /></td>
      </tr>

      {expanded && hasDetail && (
        <tr>
          <td colSpan={10} className="p-0">
            <div className="px-4 py-3 bg-base-900 border-t border-base-800 space-y-3">
              {order.error_message && (
                <div>
                  <div className="text-[10px] font-mono text-base-500 uppercase tracking-wider mb-1.5">
                    Error message
                  </div>
                  <pre className="bg-base-950 border border-loss/20 rounded-md p-3 text-xs font-mono text-loss whitespace-pre-wrap">
                    {order.error_message}
                  </pre>
                </div>
              )}
              {(order.broker_request || order.broker_response) && (
                <div className="grid grid-cols-2 gap-3">
                  {order.broker_request && (
                    <div>
                      <div className="text-[10px] font-mono text-base-500 uppercase tracking-wider mb-1.5">
                        Request sent to broker
                      </div>
                      <pre className="bg-base-950 border border-base-700 rounded-md p-3 text-xs font-mono text-base-300 whitespace-pre-wrap overflow-x-auto h-48 overflow-y-auto">
                        {fmtJson(order.broker_request)}
                      </pre>
                    </div>
                  )}
                  {order.broker_response && (
                    <div>
                      <div className="text-[10px] font-mono text-base-500 uppercase tracking-wider mb-1.5">
                        Response from broker
                      </div>
                      <pre className="bg-base-950 border border-base-700 rounded-md p-3 text-xs font-mono text-base-300 whitespace-pre-wrap overflow-x-auto h-48 overflow-y-auto">
                        {fmtJson(order.broker_response)}
                      </pre>
                    </div>
                  )}
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}
