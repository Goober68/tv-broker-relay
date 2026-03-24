import { useState } from 'react'
import { brokerAccounts as brokersApi } from '../lib/api'
import { useApi } from '../hooks/useApi'
import {
  PageSpinner, SectionHeader, Alert, EmptyState,
  ConfirmInline, Spinner
} from '../components/ui'

const BROKERS = ['oanda', 'ibkr', 'tradovate', 'etrade']

const CREDENTIAL_PLACEHOLDERS = {
  oanda:     { api_key: 'Your Oanda API key', account_id: '101-001-XXXXXXX-001', base_url: 'https://api-fxtrade.oanda.com/v3' },
  ibkr:      { gateway_url: 'https://localhost:5000/v1/api', account_id: 'DU123456' },
  tradovate: { username: 'your@email.com', password: '••••••••', app_id: 'YourAppID', app_version: '1.0', base_url: 'https://live.tradovateapi.com/v1' },
  etrade:    { consumer_key: '…', consumer_secret: '…', oauth_token: '…', oauth_token_secret: '…', account_id: 'XXXXXXXX', base_url: 'https://api.etrade.com' },
}

export default function BrokerAccountsPage() {
  const { data, loading, error, refetch } = useApi(() => brokersApi.list())
  const [adding, setAdding]     = useState(false)
  const [selected, setSelected] = useState(null)  // account id being edited
  const [confirmDel, setConfirmDel] = useState(null)

  const handleDelete = async (id) => {
    try {
      await brokersApi.delete(id)
      setConfirmDel(null)
      refetch()
    } catch {}
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <SectionHeader
        title="Broker Accounts"
        description="Connect your trading accounts. Credentials are encrypted at rest."
        action={
          !adding && (
            <button onClick={() => setAdding(true)} className="btn-primary">
              + Add broker
            </button>
          )
        }
      />

      {/* Add form */}
      {adding && (
        <BrokerForm
          onSave={async (body) => {
            await brokersApi.create(body)
            setAdding(false)
            refetch()
          }}
          onCancel={() => setAdding(false)}
        />
      )}

      {/* Account list */}
      {loading ? (
        <div className="flex justify-center py-16"><PageSpinner /></div>
      ) : error ? (
        <Alert type="error" message={error} />
      ) : !data?.length ? (
        <div className="panel">
          <EmptyState
            icon="🔌"
            title="No broker accounts"
            description="Add a broker account to start routing TradingView alerts."
            action={<button onClick={() => setAdding(true)} className="btn-primary">Connect a broker</button>}
          />
        </div>
      ) : (
        <div className="space-y-3">
          {data.map(account => (
            <AccountCard
              key={account.id}
              account={account}
              expanded={selected === account.id}
              onToggle={() => setSelected(selected === account.id ? null : account.id)}
              onRefresh={refetch}
              confirmDel={confirmDel === account.id}
              onDeleteClick={() => setConfirmDel(account.id)}
              onDeleteCancel={() => setConfirmDel(null)}
              onDeleteConfirm={() => handleDelete(account.id)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// ── Add/Edit form ──────────────────────────────────────────────────────────────

function BrokerForm({ onSave, onCancel, initial }) {
  const [broker, setBroker]   = useState(initial?.broker || 'oanda')
  const [alias, setAlias]     = useState(initial?.account_alias || 'primary')
  const [display, setDisplay] = useState(initial?.display_name || '')
  const [creds, setCreds]     = useState({})
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState(null)

  const fields = CREDENTIAL_PLACEHOLDERS[broker] || {}

  const handleSubmit = async (e) => {
    e.preventDefault()
    setLoading(true)
    setError(null)
    try {
      await onSave({
        broker, account_alias: alias,
        display_name: display || null,
        credentials: creds,
      })
    } catch (err) {
      setError(err.detail || 'Failed to save')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="panel p-6 animate-slide-up">
      <h3 className="font-display font-semibold text-base-100 mb-5">Connect a broker</h3>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-medium text-base-300 mb-1.5">Broker</label>
            <select className="input" value={broker} onChange={e => { setBroker(e.target.value); setCreds({}) }}>
              {BROKERS.map(b => <option key={b} value={b}>{b}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-base-300 mb-1.5">Account alias</label>
            <input className="input" value={alias} onChange={e => setAlias(e.target.value)} placeholder="primary" />
          </div>
        </div>

        <div>
          <label className="block text-xs font-medium text-base-300 mb-1.5">Display name (optional)</label>
          <input className="input" value={display} onChange={e => setDisplay(e.target.value)} placeholder={`My ${broker} account`} />
        </div>

        <div className="border-t border-base-700 pt-4">
          <p className="text-xs text-base-400 mb-3 flex items-center gap-1">
            <span>🔒</span> Credentials are AES-256 encrypted before storage
          </p>
          {Object.entries(fields).map(([field, placeholder]) => (
            <div key={field} className="mb-3">
              <label className="block text-xs font-medium text-base-300 mb-1.5 font-mono">{field}</label>
              <input
                className="input"
                type={field.toLowerCase().includes('password') || field.toLowerCase().includes('secret') ? 'password' : 'text'}
                placeholder={placeholder}
                value={creds[field] || ''}
                onChange={e => setCreds(prev => ({ ...prev, [field]: e.target.value }))}
              />
            </div>
          ))}
        </div>

        <Alert type="error" message={error} />

        <div className="flex gap-3 pt-2">
          <button type="submit" className="btn-primary flex items-center gap-2" disabled={loading}>
            {loading && <Spinner size="sm" />}
            Save account
          </button>
          <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>
        </div>
      </form>
    </div>
  )
}

// ── Account card ───────────────────────────────────────────────────────────────

function AccountCard({ account, expanded, onToggle, onRefresh,
                        confirmDel, onDeleteClick, onDeleteCancel, onDeleteConfirm }) {
  return (
    <div className="panel overflow-hidden">
      <div
        className="px-5 py-4 flex items-center gap-4 cursor-pointer hover:bg-base-800/30 transition-colors"
        onClick={onToggle}
      >
        <BrokerBadge broker={account.broker} />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-base-100">
            {account.display_name || `${account.broker} / ${account.account_alias}`}
          </div>
          <div className="text-xs text-base-400 font-mono mt-0.5">
            alias: <span className="text-base-300">{account.account_alias}</span>
          </div>
        </div>
        <span className={`badge ${account.is_active ? 'badge-green' : 'badge-neutral'}`}>
          {account.is_active ? 'active' : 'inactive'}
        </span>
        <span className="text-base-500 text-xs">{expanded ? '▲' : '▼'}</span>
      </div>

      {expanded && (
        <div className="border-t border-base-800 px-5 py-4 space-y-4 animate-fade-in">
          {/* Credential summary */}
          <div>
            <p className="text-xs font-medium text-base-400 mb-2">Stored credentials (redacted)</p>
            <div className="bg-base-950 rounded-md p-3 space-y-1">
              {Object.entries(account.credential_summary || {}).map(([k, v]) => (
                <div key={k} className="flex items-center gap-3 text-xs">
                  <span className="font-mono text-base-500 w-28 flex-shrink-0">{k}</span>
                  <span className="font-mono text-base-300">{v}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Instrument map for IBKR/Tradovate */}
          {['ibkr', 'tradovate'].includes(account.broker) && (
            <InstrumentMap accountId={account.id} />
          )}

          {/* Delete */}
          <div className="pt-2">
            {confirmDel ? (
              <ConfirmInline
                message="Delete this broker account? This cannot be undone."
                onConfirm={onDeleteConfirm}
                onCancel={onDeleteCancel}
                dangerous
              />
            ) : (
              <button onClick={onDeleteClick} className="btn-danger text-xs py-1.5 px-3">
                Delete account
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function BrokerBadge({ broker }) {
  const colors = {
    oanda:     'bg-blue-500/10 text-blue-400',
    ibkr:      'bg-purple-500/10 text-purple-400',
    tradovate: 'bg-orange-500/10 text-orange-400',
    etrade:    'bg-green-500/10 text-green-400',
  }
  return (
    <div className={`text-xs font-mono font-bold px-2 py-1 rounded uppercase ${colors[broker] || 'bg-base-700 text-base-300'}`}>
      {broker}
    </div>
  )
}

function InstrumentMap({ accountId }) {
  const { data, loading, refetch } = useApi(() => brokersApi.instruments(accountId))
  const [adding, setAdding]     = useState(false)
  const [symbol, setSymbol]     = useState('')
  const [conid, setConid]       = useState('')
  const [secType, setSecType]   = useState('STK')
  const [exchange, setExchange] = useState('')
  const [multiplier, setMult]   = useState('')

  const handleAdd = async (e) => {
    e.preventDefault()
    const entry = {}
    if (conid)      entry.conid      = parseInt(conid)
    if (secType)    entry.sec_type   = secType
    if (exchange)   entry.exchange   = exchange
    if (multiplier) entry.multiplier = parseFloat(multiplier)
    await brokersApi.upsertInstrument(accountId, symbol.toUpperCase(), entry)
    setAdding(false)
    setSymbol(''); setConid(''); setExchange(''); setMult('')
    refetch()
  }

  const handleDelete = async (sym) => {
    await brokersApi.deleteInstrument(accountId, sym)
    refetch()
  }

  const instruments = data ? Object.entries(data) : []

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <p className="text-xs font-medium text-base-400">Instrument map</p>
        <button onClick={() => setAdding(v => !v)} className="text-xs text-accent hover:text-accent-dim">
          {adding ? 'Cancel' : '+ Add'}
        </button>
      </div>

      {adding && (
        <form onSubmit={handleAdd} className="bg-base-950 rounded-md p-3 mb-3 space-y-2 animate-fade-in">
          <div className="grid grid-cols-2 gap-2">
            <input className="input py-1 text-xs" placeholder="Symbol (e.g. ES)" value={symbol} onChange={e => setSymbol(e.target.value)} required />
            <input className="input py-1 text-xs" placeholder="ConID (IBKR only)" value={conid} onChange={e => setConid(e.target.value)} />
            <select className="input py-1 text-xs" value={secType} onChange={e => setSecType(e.target.value)}>
              <option value="STK">STK — Equity</option>
              <option value="FUT">FUT — Future</option>
              <option value="OPT">OPT — Option</option>
              <option value="CASH">CASH — Forex</option>
            </select>
            <input className="input py-1 text-xs" placeholder="Exchange (e.g. CME)" value={exchange} onChange={e => setExchange(e.target.value)} />
            <input className="input py-1 text-xs" placeholder="Multiplier (e.g. 50)" value={multiplier} onChange={e => setMult(e.target.value)} />
          </div>
          <button type="submit" className="btn-primary text-xs py-1 px-3">Save instrument</button>
        </form>
      )}

      {loading ? (
        <div className="text-xs text-base-500 py-2">Loading…</div>
      ) : instruments.length === 0 ? (
        <p className="text-xs text-base-500 italic">No instruments configured.</p>
      ) : (
        <div className="bg-base-950 rounded-md overflow-hidden">
          {instruments.map(([sym, cfg]) => (
            <div key={sym} className="flex items-center gap-3 px-3 py-2 border-b border-base-800 last:border-0 text-xs">
              <span className="font-mono font-bold text-base-100 w-12">{sym}</span>
              <span className="text-base-500 font-mono flex-1">{JSON.stringify(cfg)}</span>
              <button onClick={() => handleDelete(sym)} className="text-base-600 hover:text-loss transition-colors">✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
