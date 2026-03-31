import { clsx } from 'clsx'

// ── Broker display names ──────────────────────────────────────────────────────

const BROKER_DISPLAY = {
  oanda: 'Oanda', ibkr: 'IBKR', tradovate: 'Tradovate',
  etrade: 'E*Trade', rithmic: 'Rithmic', tradestation: 'TradeStation',
  alpaca: 'Alpaca', tastytrade: 'Tastytrade',
}

export function brokerLabel(broker) {
  return BROKER_DISPLAY[broker] || broker
}

const BROKER_ICONS = {
  oanda: '/brokers/oanda-icon.png?v=1',
  tradovate: '/brokers/tradovate-icon.png?v=1',
}

const BROKER_LOGOS = {
  oanda: '/brokers/oanda.svg?v=1',
  tradovate: '/brokers/tradovate-dark.png?v=1',
  'tradovate-prop': '/brokers/tradovate-prop.svg?v=1',
}

export function BrokerIcon({ broker, size = 16, className }) {
  const src = BROKER_ICONS[broker]
  if (!src) return null
  return (
    <img
      src={src}
      alt={brokerLabel(broker)}
      width={size}
      height={size}
      className={clsx('inline-block flex-shrink-0', className)}
    />
  )
}

export function BrokerLogo({ broker, accountType, height = 18, className }) {
  const isProp = accountType && accountType.startsWith('prop')
  const key = (broker === 'tradovate' && isProp) ? 'tradovate-prop' : broker
  const src = BROKER_LOGOS[key]
  if (!src) return null
  return (
    <img
      src={src}
      alt={brokerLabel(broker)}
      style={{ height }}
      className={clsx('inline-block flex-shrink-0 w-auto', className)}
    />
  )
}

// ── Spinner ────────────────────────────────────────────────────────────────────

export function Spinner({ size = 'md', className }) {
  const sizes = { sm: 'w-4 h-4', md: 'w-6 h-6', lg: 'w-10 h-10' }
  return (
    <div className={clsx('animate-spin rounded-full border-2 border-base-700 border-t-accent', sizes[size], className)} />
  )
}

export function PageSpinner() {
  return (
    <div className="flex items-center justify-center min-h-[200px]">
      <Spinner size="lg" />
    </div>
  )
}

// ── Alert ──────────────────────────────────────────────────────────────────────

export function Alert({ type = 'error', message, className }) {
  if (!message) return null
  const styles = {
    error:   'bg-loss/10 border-loss/30 text-loss',
    success: 'bg-accent/10 border-accent/30 text-accent',
    warn:    'bg-warn/10 border-warn/30 text-warn',
    info:    'bg-base-700 border-base-600 text-base-200',
  }
  return (
    <div className={clsx('border rounded-md px-4 py-3 text-sm animate-fade-in', styles[type], className)}>
      {message}
    </div>
  )
}

// ── Empty State ────────────────────────────────────────────────────────────────

export function EmptyState({ icon, title, description, action }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      {icon && <div className="text-base-500 text-4xl mb-4">{icon}</div>}
      <div className="text-base-200 font-medium mb-1">{title}</div>
      {description && <div className="text-base-400 text-sm max-w-xs">{description}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

// ── Order / Delivery Status Badge ──────────────────────────────────────────────

const STATUS_STYLES = {
  filled:    'badge-green',
  open:      'badge-amber',
  submitted: 'badge-amber',
  pending:   'badge-neutral',
  cancelled: 'badge-neutral',
  rejected:  'badge-red',
  error:     'badge-red',
  partial:   'badge-amber',
  active:    'badge-green',
  past_due:  'badge-red',
  canceled:  'badge-neutral',
  trialing:  'badge-amber',
}

export function StatusBadge({ status }) {
  const style = STATUS_STYLES[status] || 'badge-neutral'
  return (
    <span className={clsx('badge', style)}>
      {status}
    </span>
  )
}

// ── P&L value ─────────────────────────────────────────────────────────────────

export function PnlValue({ value, decimals = 2, prefix = '' }) {
  if (value == null) return <span className="text-base-400 font-mono">—</span>
  const cls = value > 0 ? 'pnl-positive' : value < 0 ? 'pnl-negative' : 'pnl-zero'
  const sign = value > 0 ? '+' : ''
  return (
    <span className={cls}>
      {prefix}{sign}{Number(value).toFixed(decimals)}
    </span>
  )
}

// ── Mono value (prices, quantities) ───────────────────────────────────────────

export function Mono({ children, className }) {
  return <span className={clsx('font-mono tabular text-sm', className)}>{children}</span>
}

// ── Section header ─────────────────────────────────────────────────────────────

export function SectionHeader({ title, description, action }) {
  return (
    <div className="flex items-start justify-between mb-6">
      <div>
        <h1 className="font-display text-xl font-bold text-base-50">{title}</h1>
        {description && <p className="text-base-400 text-sm mt-0.5">{description}</p>}
      </div>
      {action && <div>{action}</div>}
    </div>
  )
}

// ── Stat card ─────────────────────────────────────────────────────────────────

export function StatCard({ label, value, sub, accent }) {
  return (
    <div className="panel p-5">
      <div className="text-xs text-base-400 uppercase tracking-wider mb-2">{label}</div>
      <div className={clsx('text-2xl font-mono font-semibold tabular', accent ? 'text-accent' : 'text-base-50')}>
        {value}
      </div>
      {sub && <div className="text-xs text-base-500 mt-1">{sub}</div>}
    </div>
  )
}

// ── Confirm dialog (inline, not modal) ────────────────────────────────────────

export function ConfirmInline({ message, onConfirm, onCancel, dangerous }) {
  return (
    <div className="flex items-center gap-3 bg-base-800 border border-base-600 rounded-md px-4 py-3 animate-fade-in">
      <span className="text-sm text-base-300 flex-1">{message}</span>
      <button
        onClick={onCancel}
        className="btn-ghost py-1 px-3 text-xs"
      >
        Cancel
      </button>
      <button
        onClick={onConfirm}
        className={dangerous ? 'btn-danger py-1 px-3 text-xs' : 'btn-primary py-1 px-3 text-xs'}
      >
        Confirm
      </button>
    </div>
  )
}

// ── Copy button ───────────────────────────────────────────────────────────────

import { useState } from 'react'

export function CopyButton({ value, label = 'Copy' }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard.writeText(value).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }
  return (
    <button
      onClick={copy}
      className={clsx(
        'text-xs px-2 py-1 rounded font-mono transition-all',
        copied
          ? 'bg-accent/20 text-accent border border-accent/30'
          : 'bg-base-700 text-base-300 border border-base-600 hover:border-base-500'
      )}
    >
      {copied ? '✓ Copied' : label}
    </button>
  )
}
