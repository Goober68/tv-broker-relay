import { useState, useEffect } from 'react'
import { brokerAccounts as brokersApi } from '../lib/api'
import { useApi } from '../hooks/useApi'
import {
  PageSpinner, SectionHeader, Alert, EmptyState,
  ConfirmInline, Spinner, brokerLabel, BrokerIcon, BrokerLogo
} from '../components/ui'
import AccountWizard from '../components/AccountWizard'

const BROKERS = ['oanda', 'tradovate']

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
  const [reauthMsg, setReauthMsg] = useState(null)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const oauthMode = params.get('oauth')
    if (oauthMode === 'reauth') {
      // Reconnect flow — update existing accounts with fresh token
      const token = params.get('token') || ''
      window.history.replaceState({}, '', '/broker-accounts')
      if (token) {
        brokersApi.tradovateReauth(token).then(res => {
          setReauthMsg(`Re-authorized ${res.count} account(s): ${res.updated.join(', ')}`)
          refetch()
          setTimeout(() => setReauthMsg(null), 6000)
        }).catch(err => {
          alert('Reauth failed: ' + (err.detail || err.message))
        })
      }
    } else if (oauthMode === 'tradovate') {
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
              + Account
            </button>
          )
        }
      />

      {reauthMsg && (
        <Alert type="success" message={reauthMsg} />
      )}

      {/* Add wizard */}
      {adding && (
        <AccountWizard
          onDone={() => { setAdding(false); setOauthData(null); refetch() }}
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
  const [broker, setBroker]   = useState(oauthData ? 'tradovate' : (initial?.broker || null))
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
      <h3 className="font-display font-semibold text-base-100 mb-5">Connect a broker account</h3>
      <form onSubmit={handleSubmit} className="space-y-4">

        {/* Step 1: Broker selector — always visible unless OAuth accounts loaded */}
        {!(isTradovate && tvAccounts) && (
          <div>
            <label className="block text-xs font-medium text-base-300 mb-2">Select broker</label>
            <div className="flex gap-2">
              {BROKERS.map(b => (
                <button
                  key={b}
                  type="button"
                  onClick={() => { setBroker(b); setCreds({}); setTvAccounts(null) }}
                  className={`flex-1 py-2 px-3 text-xs font-mono rounded-md border transition-colors ${
                    broker === b
                      ? 'bg-base-700 border-accent/40 text-base-100'
                      : 'bg-base-900 border-base-700 text-base-500 hover:text-base-300 hover:border-base-600'
                  }`}
                >
                  {brokerLabel(b)}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Step 2: Broker-specific connection — only after broker selected */}
        {broker && !isTradovate && !(isTradovate && tvAccounts) && (
          <>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-base-300 mb-1.5">Account ID</label>
                <input className="input" value={alias} onChange={e => setAlias(e.target.value)} placeholder="primary" />
              </div>
              <div>
                <label className="block text-xs font-medium text-base-300 mb-1.5">Display name (optional)</label>
                <input className="input" value={display} onChange={e => setDisplay(e.target.value)} placeholder={`My ${brokerLabel(broker)} account`} />
              </div>
            </div>
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

        {/* Credentials section — hidden until broker selected, and after fetch/OAuth */}
        {broker && !(isTradovate && tvAccounts) && (
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
          {!broker ? (
            <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>
          ) : isTradovate && !tvAccounts ? (
            <>
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
      <div className="px-5 py-4 flex items-center gap-4">
        <div className="flex items-center gap-4 flex-1 min-w-0 cursor-pointer" onClick={onToggle}>
          <BrokerBadge broker={account.broker} accountType={account.account_type} />
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium text-base-100">
              {account.display_name || `${brokerLabel(account.broker)} / ${account.account_alias}`}
            </div>
            <div className="text-xs text-base-400 font-mono mt-0.5">
              {account.account_alias}
            </div>
          </div>
        </div>
        <AccountControls account={account} onRefresh={onRefresh} />
        <span className="text-base-500 text-xs cursor-pointer" onClick={onToggle}>{expanded ? '▲' : '▼'}</span>
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

          {/* Drawdown limits */}
          <DrawdownSettings account={account} onRefresh={onRefresh} />

          {/* Reconnect OAuth — Tradovate only */}
          {account.broker === 'tradovate' && (
            <ReconnectOAuth account={account} />
          )}

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

function AccountControls({ account, onRefresh }) {
  const [suspending, setSuspending] = useState(false)
  const [flattening, setFlattening] = useState(false)
  const [confirmFlatten, setConfirmFlatten] = useState(false)
  const [flattenResult, setFlattenResult] = useState(null)

  const handleToggleSuspend = async () => {
    setSuspending(true)
    try {
      await brokersApi.suspend(account.id, !account.is_active)
      onRefresh()
    } catch {}
    finally { setSuspending(false) }
  }

  const handleFlatten = async () => {
    setFlattening(true)
    setFlattenResult(null)
    try {
      const data = await brokersApi.flatten(account.id)
      setFlattenResult(data)
      setConfirmFlatten(false)
    } catch (err) {
      setFlattenResult({ errors: [err.detail || 'Flatten failed'] })
    }
    finally { setFlattening(false) }
  }

  return (
    <div className="flex items-center gap-2 flex-shrink-0" onClick={e => e.stopPropagation()}>
      <button
        onClick={handleToggleSuspend}
        disabled={suspending}
        className={`text-[10px] py-1 px-2 rounded font-medium transition-colors ${
          account.is_active
            ? 'bg-warn/10 border border-warn/30 text-warn hover:bg-warn/20'
            : 'bg-accent/10 border border-accent/30 text-accent hover:bg-accent/20'
        }`}
      >
        {suspending ? '...' : account.is_active ? 'Pause' : 'Resume'}
      </button>

      {confirmFlatten ? (
        <div className="flex items-center gap-1.5">
          <button
            onClick={handleFlatten}
            disabled={flattening}
            className="text-[10px] py-1 px-2 rounded font-medium bg-loss/20 border border-loss/40 text-loss hover:bg-loss/30"
          >
            {flattening ? 'Closing...' : 'Confirm'}
          </button>
          <button onClick={() => setConfirmFlatten(false)} className="text-[10px] text-base-500 hover:text-base-300">
            Cancel
          </button>
        </div>
      ) : (
        <button
          onClick={() => setConfirmFlatten(true)}
          className="text-[10px] py-1 px-2 rounded font-medium bg-loss/10 border border-loss/30 text-loss hover:bg-loss/20 transition-colors"
        >
          Flatten
        </button>
      )}

      {flattenResult && (
        <span className={`text-[10px] font-mono ${flattenResult.errors?.length ? 'text-loss' : 'text-accent'}`}>
          {flattenResult.closed != null
            ? `${flattenResult.closed}/${flattenResult.total} closed`
            : flattenResult.errors?.[0]}
        </span>
      )}
    </div>
  )
}

function ReconnectOAuth({ account }) {
  const handleReconnect = async () => {
    try {
      const env = account.credential_summary?.base_url?.includes('demo') ? 'demo' : 'live'
      const { url } = await brokersApi.tradovateOAuthUrl(env, true)
      window.location.href = url
    } catch {}
  }

  return (
    <div className="border-t border-base-800 pt-4">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs font-medium text-base-300">OAuth connection</p>
          <p className="text-xs text-base-500 mt-0.5">
            Re-authorize if token expired (Access is denied errors)
          </p>
        </div>
        <button
          onClick={handleReconnect}
          className="text-xs py-1.5 px-3 rounded font-medium bg-orange-500/10 border border-orange-500/30 text-orange-400 hover:bg-orange-500/20 transition-colors"
        >
          Reconnect Tradovate
        </button>
      </div>
    </div>
  )
}

function DrawdownSettings({ account, onRefresh }) {
  const [totalDD, setTotalDD] = useState(account.max_total_drawdown ?? '')
  const [dailyDD, setDailyDD] = useState(account.max_daily_drawdown ?? '')
  const [ddFloor, setDdFloor] = useState(account.drawdown_floor ?? '')
  const [commission, setCommission] = useState(account.commission_per_contract ?? '')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    setTotalDD(account.max_total_drawdown ?? '')
    setDailyDD(account.max_daily_drawdown ?? '')
    setDdFloor(account.drawdown_floor ?? '')
    setCommission(account.commission_per_contract ?? '')
  }, [account.max_total_drawdown, account.max_daily_drawdown, account.drawdown_floor, account.commission_per_contract])

  const handleSave = async () => {
    setSaving(true)
    try {
      await brokersApi.updateDrawdown(account.id, {
        max_total_drawdown: totalDD === '' ? null : parseFloat(totalDD),
        max_daily_drawdown: dailyDD === '' ? null : parseFloat(dailyDD),
        drawdown_floor: ddFloor === '' ? null : parseFloat(ddFloor),
        commission_per_contract: commission === '' ? null : parseFloat(commission),
      })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
      onRefresh()
    } catch {}
    finally { setSaving(false) }
  }

  return (
    <div className="border-t border-base-800 pt-4">
      <p className="text-xs font-medium text-base-300 mb-3">Account settings</p>
      <div className="flex items-end gap-4 flex-wrap">
        <div>
          <label className="block text-[10px] text-base-500 mb-1">Commission $/contract/side</label>
          <input
            className="input py-1 text-xs font-mono w-28"
            type="number" step="0.01" min="0"
            placeholder="e.g. 2.88"
            value={commission}
            onChange={e => setCommission(e.target.value)}
          />
        </div>
        <div>
          <label className="block text-[10px] text-base-500 mb-1">Max total drawdown ($)</label>
          <input
            className="input py-1 text-xs font-mono w-28"
            type="number" step="0.01" min="0"
            placeholder="e.g. 2500"
            value={totalDD}
            onChange={e => setTotalDD(e.target.value)}
          />
        </div>
        <div>
          <label className="block text-[10px] text-base-500 mb-1">Drawdown floor ($)</label>
          <input
            className="input py-1 text-xs font-mono w-28"
            type="number" step="0.01" min="0"
            placeholder="e.g. 48354"
            value={ddFloor}
            onChange={e => setDdFloor(e.target.value)}
          />
        </div>
        <button
          onClick={handleSave}
          disabled={saving}
          className="btn-primary text-xs py-1 px-3"
        >
          {saving ? 'Saving…' : saved ? '✓' : 'Save'}
        </button>
      </div>
      <p className="text-[10px] text-base-500 mt-2">
        Drawdown floor = liquidation level from prop firm. When set, remaining = live balance - floor.
      </p>
    </div>
  )
}

function ImportHistory({ accountId }) {
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  const handleSync = async () => {
    setSyncing(true)
    setError(null)
    setResult(null)
    try {
      const data = await brokersApi.syncHistory(accountId)
      setResult(data)
    } catch (err) {
      setError(err.detail || 'Sync failed')
    } finally {
      setSyncing(false)
    }
  }

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
      e.target.value = ''
    }
  }

  const busy = loading || syncing

  return (
    <div className="border-t border-base-800 pt-4">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-xs font-medium text-base-300">Import trade history</p>
          <p className="text-xs text-base-500 mt-0.5">
            Sync from Tradovate or upload a CSV export
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleSync}
            disabled={busy}
            className="btn-primary text-xs py-1.5 px-3 flex items-center gap-2"
          >
            {syncing && <Spinner size="sm" />}
            {syncing ? 'Syncing…' : 'Sync fills'}
          </button>
          <label className={`btn-ghost text-xs py-1.5 px-3 flex items-center gap-2 cursor-pointer ${busy ? 'opacity-50 pointer-events-none' : ''}`}>
            {loading && <Spinner size="sm" />}
            {loading ? 'Importing…' : 'Upload CSV'}
            <input type="file" accept=".csv" onChange={handleCsvUpload} className="hidden" />
          </label>
        </div>
      </div>
      {result && (
        <div className="mt-2 text-xs font-mono text-accent">
          {result.message || `Imported ${result.imported} ${result.format === 'fills' ? 'fills' : 'orders'} (${result.skipped || 0} duplicates skipped${result.format ? `, ${result.format} format` : ''})`}
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

function BrokerBadge({ broker, accountType }) {
  const colors = {
    oanda:     'bg-blue-500/10',
    ibkr:      'bg-purple-500/10 text-purple-400',
    tradovate: 'bg-orange-500/10',
    etrade:    'bg-green-500/10 text-green-400',
  }
  const hasLogo = ['oanda', 'tradovate'].includes(broker)
  return (
    <div className={`flex items-center justify-center text-xs font-mono font-bold px-2 py-1 rounded uppercase w-28 ${colors[broker] || 'bg-base-700 text-base-300'}`}>
      {hasLogo ? (
        <BrokerLogo broker={broker} accountType={accountType} height={20} />
      ) : broker}
    </div>
  )
}

function InstrumentMap({ accountId }) {
  const { data, loading, refetch } = useApi(() => brokersApi.instruments(accountId))
  const [adding, setAdding]     = useState(false)
  const [symbol, setSymbol]     = useState('')
  const [targetSym, setTargetSym] = useState('')
  const [conid, setConid]       = useState('')
  const [secType, setSecType]   = useState('STK')
  const [exchange, setExchange] = useState('')
  const [multiplier, setMult]   = useState('')
  const [commissionVal, setCommissionVal] = useState('')

  const handleAdd = async (e) => {
    e.preventDefault()
    const entry = {}
    if (targetSym)     entry.target_symbol = targetSym.toUpperCase()
    if (conid)         entry.conid      = parseInt(conid)
    if (secType)       entry.sec_type   = secType
    if (exchange)      entry.exchange   = exchange
    if (multiplier)    entry.multiplier = parseFloat(multiplier)
    if (commissionVal) entry.commission = parseFloat(commissionVal)
    await brokersApi.upsertInstrument(accountId, symbol.toUpperCase(), entry)
    setAdding(false)
    setSymbol(''); setTargetSym(''); setConid(''); setExchange(''); setMult(''); setCommissionVal('')
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
          <div className="flex items-center gap-2 mb-1">
            <input className="input py-1 text-xs flex-1" placeholder="TradingView symbol (e.g. MNQ1!)" value={symbol} onChange={e => setSymbol(e.target.value)} required />
            <span className="text-base-500 text-xs">→</span>
            <input className="input py-1 text-xs flex-1" placeholder="Broker symbol (e.g. MNQM6)" value={targetSym} onChange={e => setTargetSym(e.target.value)} />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <input className="input py-1 text-xs" placeholder="Commission $/side (e.g. 2.50)" value={commissionVal} onChange={e => setCommissionVal(e.target.value)} />
            <input className="input py-1 text-xs" placeholder="Multiplier override (e.g. 2)" value={multiplier} onChange={e => setMult(e.target.value)} />
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
              <span className="font-mono font-bold text-base-100">{sym}</span>
              {cfg.target_symbol && (
                <span className="text-base-500 font-mono">→ <span className="text-base-300">{cfg.target_symbol}</span></span>
              )}
              <span className="text-base-500 font-mono flex-1 ml-2">
                {[
                  cfg.commission != null && `$${cfg.commission}/side`,
                  cfg.multiplier && `×${cfg.multiplier}`,
                  cfg.exchange,
                ].filter(Boolean).join(' · ') || ''}
              </span>
              <button onClick={() => handleDelete(sym)} className="text-base-600 hover:text-loss transition-colors">✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
