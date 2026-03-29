import { useState, useEffect } from 'react'
import { brokerAccounts as brokersApi } from '../lib/api'
import { useApi } from '../hooks/useApi'
import {
  PageSpinner, SectionHeader, Alert, EmptyState,
  ConfirmInline, Spinner, brokerLabel
} from '../components/ui'

const BROKERS = ['oanda', 'ibkr', 'tradovate', 'etrade']

const CREDENTIAL_PLACEHOLDERS = {
  oanda:     { api_key: 'Your Oanda API key', account_id: '101-001-XXXXXXX-001', base_url: 'https://api-fxtrade.oanda.com/v3' },
  ibkr:      { gateway_url: 'https://localhost:5000/v1/api', account_id: 'DU123456' },
  tradovate: { username: 'your@email.com', password: '••••••••', app_id: 'YourAppID', device_id: '', cid: '', sec: '' },
  etrade:    { consumer_key: '…', consumer_secret: '…', oauth_token: '…', oauth_token_secret: '…', account_id: 'XXXXXXXX', base_url: 'https://api.etrade.com' },
}

const TRADOVATE_URLS = {
  live: 'https://live.tradovateapi.com/v1',
  demo: 'https://demo.tradovateapi.com/v1',
}

export default function BrokerAccountsPage() {
  const { data, loading, error, refetch } = useApi(() => brokersApi.list())
  const [adding, setAdding]     = useState(false)
  const [selected, setSelected] = useState(null)  // account id being edited
  const [confirmDel, setConfirmDel] = useState(null)

  // Detect OAuth redirect params
  const [oauthData, setOauthData] = useState(null)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    if (params.get('oauth') === 'tradovate') {
      try {
        const accountsB64 = params.get('accounts') || ''
        const accounts = JSON.parse(atob(accountsB64))
        const token = params.get('token') || ''
        const env = params.get('env') || 'live'
        setOauthData({ accounts, token, env })
        setAdding(true)
      } catch (e) {
        console.error('Failed to parse OAuth redirect params', e)
      }
      // Clean URL
      window.history.replaceState({}, '', '/broker-accounts')
    }
    if (params.get('oauth_error')) {
      alert('Tradovate OAuth error: ' + params.get('oauth_error'))
      window.history.replaceState({}, '', '/broker-accounts')
    }
  }, [])

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
            setOauthData(null)
            refetch()
          }}
          onCancel={() => { setAdding(false); setOauthData(null); refetch() }}
          oauthData={oauthData}
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

function BrokerForm({ onSave, onCancel, initial, oauthData }) {
  const [broker, setBroker]   = useState(oauthData ? 'tradovate' : (initial?.broker || 'oanda'))
  const [alias, setAlias]     = useState(initial?.account_alias || 'primary')
  const [display, setDisplay] = useState(initial?.display_name || '')
  const [creds, setCreds]     = useState({})
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState(null)

  // Tradovate-specific state
  const [tvEnv, setTvEnv]       = useState(oauthData?.env || 'live')
  const [tvPropFirm, setTvPropFirm] = useState(false)
  const [tvShowAdvanced, setTvShowAdvanced] = useState(false)
  const [tvAccounts, setTvAccounts] = useState(oauthData?.accounts || null)
  const [tvSelected, setTvSelected] = useState({})
  const [tvFetching, setTvFetching] = useState(false)
  const [tvOAuthToken, setTvOAuthToken] = useState(oauthData?.token || null)

  // Pre-populate account selection when OAuth data arrives
  useEffect(() => {
    if (oauthData?.accounts && Object.keys(tvSelected).length === 0) {
      const sel = {}
      for (const a of oauthData.accounts) {
        const shortName = a.name.length > 6 ? a.name.slice(-3) : a.name
        sel[a.name] = { checked: true, alias: shortName, prop_firm: tvPropFirm }
      }
      setTvSelected(sel)
    }
  }, [oauthData])

  const isTradovate = broker === 'tradovate'
  const fields = CREDENTIAL_PLACEHOLDERS[broker] || {}
  const tvPrimaryFields = ['username', 'password', 'app_id']
  const tvAdvancedFields = ['device_id', 'cid', 'sec']

  const buildCreds = () => {
    const c = { ...creds }
    if (isTradovate) {
      c.base_url = TRADOVATE_URLS[tvEnv]
      c.app_version = c.app_version || '1.0'
      // Clear device binding fields if not explicitly set — avoids p-ticket penalties
      if (!c.device_id) c.device_id = ''
      if (!c.cid) c.cid = '0'
      if (!c.sec) c.sec = ''
    }
    return c
  }

  // Fetch account list from Tradovate
  const handleFetchAccounts = async () => {
    setTvFetching(true)
    setError(null)
    try {
      const accounts = await brokersApi.tradovateFetchAccounts(buildCreds())
      setTvAccounts(accounts)
      // Pre-select all, default alias = last segment of account name
      const sel = {}
      for (const a of accounts) {
        const shortName = a.name.length > 6 ? a.name.slice(-3) : a.name
        sel[a.name] = { checked: true, alias: shortName, prop_firm: tvPropFirm }
      }
      setTvSelected(sel)
    } catch (err) {
      setError(err.detail || 'Failed to fetch accounts')
    } finally {
      setTvFetching(false)
    }
  }

  // Start Tradovate OAuth flow
  const handleOAuthConnect = async () => {
    setError(null)
    try {
      const { url } = await brokersApi.tradovateOAuthUrl(tvEnv)
      window.location.href = url
    } catch (err) {
      setError(err.detail || 'Failed to start OAuth flow')
    }
  }

  // Bulk-save selected Tradovate accounts
  const handleBulkSave = async () => {
    setLoading(true)
    setError(null)
    try {
      const accounts = Object.entries(tvSelected)
        .filter(([, v]) => v.checked)
        .map(([name, v]) => ({
          name,
          alias: v.alias || name,
          display_name: v.alias || name,
          prop_firm: v.prop_firm,
        }))
      if (!accounts.length) {
        setError('Select at least one account')
        setLoading(false)
        return
      }
      // OAuth flow sends encrypted token; manual flow sends raw credentials
      const credsPayload = tvOAuthToken
        ? { _encrypted: tvOAuthToken }
        : buildCreds()
      await brokersApi.tradovateBulkCreate(credsPayload, accounts)
      onCancel()  // close form — parent will refetch
    } catch (err) {
      setError(err.detail || 'Failed to create accounts')
    } finally {
      setLoading(false)
    }
  }

  // Standard (non-Tradovate) save
  const handleSubmit = async (e) => {
    e.preventDefault()
    if (isTradovate && tvAccounts) {
      await handleBulkSave()
      return
    }
    setLoading(true)
    setError(null)
    try {
      const finalCreds = buildCreds()
      if (isTradovate && tvPropFirm) finalCreds.prop_firm = true
      await onSave({
        broker, account_alias: alias,
        display_name: display || null,
        auto_close_enabled: isTradovate && tvPropFirm,
        auto_close_time: isTradovate && tvPropFirm ? '16:50' : null,
        credentials: finalCreds,
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
        {/* Broker selector — only show if we haven't fetched Tradovate accounts yet */}
        {!(isTradovate && tvAccounts) && (
          <>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-base-300 mb-1.5">Broker</label>
                <select className="input" value={broker} onChange={e => { setBroker(e.target.value); setCreds({}); setTvAccounts(null) }}>
                  {BROKERS.map(b => <option key={b} value={b}>{brokerLabel(b)}</option>)}
                </select>
              </div>
              {!isTradovate && (
                <div>
                  <label className="block text-xs font-medium text-base-300 mb-1.5">Account alias</label>
                  <input className="input" value={alias} onChange={e => setAlias(e.target.value)} placeholder="primary" />
                </div>
              )}
            </div>

            {!isTradovate && (
              <div>
                <label className="block text-xs font-medium text-base-300 mb-1.5">Display name (optional)</label>
                <input className="input" value={display} onChange={e => setDisplay(e.target.value)} placeholder={`My ${brokerLabel(broker)} account`} />
              </div>
            )}
          </>
        )}

        {/* Tradovate environment + prop firm toggles */}
        {isTradovate && !tvAccounts && (
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-base-300 mb-1.5">Environment</label>
              <div className="flex bg-base-800 rounded-md p-0.5 gap-0.5">
                {['demo', 'live'].map(env => (
                  <button
                    key={env}
                    type="button"
                    onClick={() => setTvEnv(env)}
                    className={`flex-1 px-3 py-1.5 text-xs font-mono rounded transition-colors ${
                      tvEnv === env
                        ? env === 'live' ? 'bg-accent/20 text-accent' : 'bg-base-600 text-base-50'
                        : 'text-base-400 hover:text-base-200'
                    }`}
                  >
                    {env === 'demo' ? 'Demo' : 'Live'}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium text-base-300 mb-1.5">Account type</label>
              <div className="flex bg-base-800 rounded-md p-0.5 gap-0.5">
                {[false, true].map(isProp => (
                  <button
                    key={String(isProp)}
                    type="button"
                    onClick={() => setTvPropFirm(isProp)}
                    className={`flex-1 px-3 py-1.5 text-xs font-mono rounded transition-colors ${
                      tvPropFirm === isProp
                        ? 'bg-base-600 text-base-50'
                        : 'text-base-400 hover:text-base-200'
                    }`}
                  >
                    {isProp ? 'Prop firm' : 'Standard'}
                  </button>
                ))}
              </div>
              {tvPropFirm && (
                <p className="text-[10px] text-base-500 mt-1">
                  Auto-close at 4:50 PM ET enabled by default
                </p>
              )}
            </div>
          </div>
        )}

        {/* Credentials section — hidden after fetch/OAuth */}
        {!(isTradovate && tvAccounts) && (
          <div className="border-t border-base-700 pt-4">

            {isTradovate ? (
              <div>
                <button
                  type="button"
                  onClick={handleOAuthConnect}
                  className="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-md bg-orange-500/10 border border-orange-500/30 text-orange-400 hover:bg-orange-500/20 transition-colors text-sm font-medium"
                >
                  Connect with Tradovate
                </button>
                <p className="text-[10px] text-base-500 mt-1.5 text-center">
                  Log in via Tradovate — works with personal, demo, and prop firm accounts
                </p>
              </div>
            ) : (
              <>
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
              </>
            )}
          </div>
        )}

        {/* Tradovate account picker */}
        {isTradovate && tvAccounts && (
          <div className="border-t border-base-700 pt-4 space-y-3">
            <div className="flex items-center justify-between">
              <p className="text-xs font-medium text-base-300">
                {tvAccounts.length} account{tvAccounts.length !== 1 ? 's' : ''} found
              </p>
              <div className="flex gap-2">
                <button type="button" onClick={() => {
                  const sel = { ...tvSelected }
                  for (const k of Object.keys(sel)) sel[k] = { ...sel[k], checked: true }
                  setTvSelected(sel)
                }} className="text-[10px] text-base-400 hover:text-base-200">Select all</button>
                <button type="button" onClick={() => {
                  const sel = { ...tvSelected }
                  for (const k of Object.keys(sel)) sel[k] = { ...sel[k], checked: false }
                  setTvSelected(sel)
                }} className="text-[10px] text-base-400 hover:text-base-200">Select none</button>
              </div>
            </div>

            <div className="max-h-80 overflow-y-auto space-y-1 bg-base-950 rounded-md p-2">
              {tvAccounts.map(a => {
                const s = tvSelected[a.name] || { checked: false, alias: '', prop_firm: false }
                return (
                  <div key={a.name} className={`flex items-center gap-3 px-3 py-2 rounded transition-colors ${s.checked ? 'bg-base-800/60' : 'opacity-50'}`}>
                    <input
                      type="checkbox"
                      checked={s.checked}
                      onChange={e => setTvSelected(prev => ({
                        ...prev,
                        [a.name]: { ...prev[a.name], checked: e.target.checked }
                      }))}
                      className="accent-accent flex-shrink-0"
                    />
                    <div className="flex-1 min-w-0">
                      <div className="font-mono text-xs text-base-300 truncate" title={a.name}>{a.name}</div>
                      {a.nickname && <div className="text-[10px] text-base-500">{a.nickname}</div>}
                    </div>
                    <input
                      className="input py-1 text-xs font-mono w-28"
                      placeholder="Alias"
                      value={s.alias}
                      onChange={e => setTvSelected(prev => ({
                        ...prev,
                        [a.name]: { ...prev[a.name], alias: e.target.value }
                      }))}
                    />
                    <label className="flex items-center gap-1.5 cursor-pointer flex-shrink-0">
                      <span className="text-[10px] text-base-500">Prop</span>
                      <div
                        onClick={() => setTvSelected(prev => ({
                          ...prev,
                          [a.name]: { ...prev[a.name], prop_firm: !prev[a.name].prop_firm }
                        }))}
                        className={`w-6 h-3 rounded-full transition-colors relative cursor-pointer ${s.prop_firm ? 'bg-accent' : 'bg-base-600'}`}
                      >
                        <div className={`absolute top-0.5 w-2 h-2 rounded-full bg-white transition-all ${s.prop_firm ? 'left-3.5' : 'left-0.5'}`} />
                      </div>
                    </label>
                  </div>
                )
              })}
            </div>

            <button type="button" onClick={() => { setTvAccounts(null); setTvSelected({}) }}
              className="text-xs text-base-500 hover:text-base-300">
              ← Back to credentials
            </button>
          </div>
        )}

        <Alert type="error" message={error} />

        <div className="flex gap-3 pt-2">
          {isTradovate && !tvAccounts ? (
            <>
              <button type="button" onClick={handleFetchAccounts}
                className="btn-primary flex items-center gap-2" disabled={tvFetching}>
                {tvFetching && <Spinner size="sm" />}
                Fetch accounts
              </button>
              <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>
            </>
          ) : (
            <>
              <button type="submit" className="btn-primary flex items-center gap-2" disabled={loading}>
                {loading && <Spinner size="sm" />}
                {isTradovate && tvAccounts
                  ? `Add ${Object.values(tvSelected).filter(v => v.checked).length} account(s)`
                  : 'Save account'}
              </button>
              <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>
            </>
          )}
        </div>
      </form>
    </div>
  )
}

// ── Account card ───────────────────────────────────────────────────────────────

function AccountCard({ account, expanded, onToggle, onRefresh,
                        confirmDel, onDeleteClick, onDeleteCancel, onDeleteConfirm }) {
  const [editName, setEditName] = useState(account.display_name || '')
  const [nameSaving, setNameSaving] = useState(false)
  const [nameSaved, setNameSaved] = useState(false)

  useEffect(() => { setEditName(account.display_name || '') }, [account.display_name])

  const handleSaveName = async () => {
    setNameSaving(true)
    try {
      await brokersApi.updateDisplayName(account.id, editName || null)
      setNameSaved(true)
      setTimeout(() => setNameSaved(false), 2000)
      onRefresh()
    } catch {}
    finally { setNameSaving(false) }
  }

  return (
    <div className="panel overflow-hidden">
      <div
        className="px-5 py-4 flex items-center gap-4 cursor-pointer hover:bg-base-800/30 transition-colors"
        onClick={onToggle}
      >
        <BrokerBadge broker={account.broker} />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-base-100">
            {account.display_name || `${brokerLabel(account.broker)} / ${account.account_alias}`}
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
          {/* Display name */}
          <div>
            <label className="block text-xs font-medium text-base-400 mb-1.5">Display name</label>
            <div className="flex items-center gap-2">
              <input
                className="input py-1 text-xs flex-1"
                value={editName}
                onChange={e => setEditName(e.target.value)}
                placeholder={`${brokerLabel(account.broker)} / ${account.account_alias}`}
              />
              <button
                onClick={handleSaveName}
                disabled={nameSaving || editName === (account.display_name || '')}
                className="btn-primary text-xs py-1 px-3 disabled:opacity-30"
              >
                {nameSaving ? 'Saving…' : nameSaved ? '✓' : 'Save'}
              </button>
            </div>
          </div>

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

          {/* FIFO randomization — Oanda US only */}
          {account.broker === 'oanda' && (
            <FifoSettings account={account} onRefresh={onRefresh} />
          )}

          {/* Auto-close settings */}
          <AutoCloseSettings account={account} onRefresh={onRefresh} />

          {/* Import trade history — Tradovate only */}
          {account.broker === 'tradovate' && (
            <ImportHistory accountId={account.id} />
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

function FifoSettings({ account, onRefresh }) {
  const [enabled, setEnabled] = useState(account.fifo_randomize || false)
  const [saving, setSaving]   = useState(false)
  const [saved, setSaved]     = useState(false)

  // Sync local state when account prop updates after refetch
  useEffect(() => {
    setEnabled(account.fifo_randomize || false)
  }, [account.fifo_randomize])

  const handleSave = async () => {
    setSaving(true)
    try {
      await brokersApi.updateFifo(account.id, { fifo_randomize: enabled })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
      onRefresh()
    } catch {}
    finally { setSaving(false) }
  }

  return (
    <div className="border-t border-base-800 pt-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <p className="text-xs font-medium text-base-300">FIFO avoidance</p>
          <p className="text-xs text-base-500 mt-0.5">
            Adjusts each order size to be unique so Oanda can identify individual
            pyramid legs (required for US NFA FIFO compliance)
          </p>
        </div>
        <div
          onClick={() => setEnabled(v => !v)}
          className={`w-8 h-4 rounded-full transition-colors relative cursor-pointer flex-shrink-0 ${enabled ? 'bg-accent' : 'bg-base-600'}`}
        >
          <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white transition-all ${enabled ? 'left-4' : 'left-0.5'}`} />
        </div>
      </div>

      <button
        onClick={handleSave}
        disabled={saving}
        className="btn-primary text-xs py-1.5 px-3 mt-3"
      >
        {saving ? 'Saving…' : saved ? '✓ Saved' : 'Save FIFO settings'}
      </button>
    </div>
  )
}

function AutoCloseSettings({ account, onRefresh }) {
  const [enabled, setEnabled] = useState(account.auto_close_enabled || false)
  const [time, setTime]       = useState(account.auto_close_time || '16:50')
  const [saving, setSaving]   = useState(false)
  const [saved, setSaved]     = useState(false)

  useEffect(() => {
    setEnabled(account.auto_close_enabled || false)
    setTime(account.auto_close_time || '16:50')
  }, [account.auto_close_enabled, account.auto_close_time])

  const handleSave = async () => {
    setSaving(true)
    try {
      await brokersApi.updateAutoClose(account.id, { auto_close_enabled: enabled, auto_close_time: time })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
      onRefresh()
    } catch {}
    finally { setSaving(false) }
  }

  return (
    <div className="border-t border-base-800 pt-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <p className="text-xs font-medium text-base-300">Auto-close (prop firm compliance)</p>
          <p className="text-xs text-base-500 mt-0.5">
            Automatically close all open positions at a set time (ET) before session roll
          </p>
        </div>
        <label className="flex items-center gap-2 cursor-pointer">
          <span className="text-xs text-base-400">{enabled ? 'Enabled' : 'Disabled'}</span>
          <div
            onClick={() => setEnabled(v => !v)}
            className={`w-8 h-4 rounded-full transition-colors relative cursor-pointer ${enabled ? 'bg-accent' : 'bg-base-600'}`}
          >
            <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white transition-all ${enabled ? 'left-4' : 'left-0.5'}`} />
          </div>
        </label>
      </div>

      {enabled && (
        <div className="flex items-center gap-3 animate-fade-in">
          <div>
            <label className="block text-xs text-base-400 mb-1">Close time (ET)</label>
            <input
              type="time"
              className="input py-1 text-xs font-mono w-32"
              value={time}
              onChange={e => setTime(e.target.value)}
            />
          </div>
          <div className="text-xs text-base-500 pt-4">
            e.g. <span className="font-mono text-base-300">16:50</span> = 4:50 PM ET<br/>
            (10 min before 5 PM session roll)
          </div>
        </div>
      )}

      <button
        onClick={handleSave}
        disabled={saving}
        className="btn-primary text-xs py-1.5 px-3 mt-3"
      >
        {saving ? 'Saving…' : saved ? '✓ Saved' : 'Save auto-close settings'}
      </button>
    </div>
  )
}

function ImportHistory({ accountId }) {
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  const handleCsvUpload = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const data = await brokersApi.importCsv(accountId, file)
      setResult(data)
    } catch (err) {
      setError(err.detail || 'CSV import failed')
    } finally {
      setLoading(false)
      e.target.value = ''  // reset file input
    }
  }

  return (
    <div className="border-t border-base-800 pt-4">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs font-medium text-base-300">Import trade history</p>
          <p className="text-xs text-base-500 mt-0.5">
            Upload a Tradovate CSV export (Orders tab → Export)
          </p>
        </div>
        <label className={`btn-primary text-xs py-1.5 px-3 flex items-center gap-2 cursor-pointer ${loading ? 'opacity-50 pointer-events-none' : ''}`}>
          {loading && <Spinner size="sm" />}
          {loading ? 'Importing…' : 'Upload CSV'}
          <input type="file" accept=".csv" onChange={handleCsvUpload} className="hidden" />
        </label>
      </div>
      {result && (
        <div className="mt-2 text-xs font-mono text-accent">
          Imported {result.imported} fills ({result.skipped} duplicates skipped)
          {result.errors?.length > 0 && (
            <span className="text-loss ml-2">({result.errors.length} errors)</span>
          )}
        </div>
      )}
      {error && (
        <div className="mt-2 text-xs font-mono text-loss">{error}</div>
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
