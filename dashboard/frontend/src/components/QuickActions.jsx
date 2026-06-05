import { useState, useEffect } from 'react'
import './QuickActions.css'

const BUILTIN_ACTIONS = [
  {
    id: 'stop-media',
    icon: '🔴',
    label: 'Stop Media',
    description: 'Scale down qBittorrent & Jellyfin → drain g3-worker3 → shutdown G3',
    danger: true,
    confirmMsg: 'This will shut down all media services and the G3 node. Continue?',
    endpoint: '/api/action/stop-media',
    method: 'POST',
  },
  {
    id: 'start-media',
    icon: '🟢',
    label: 'Start Media',
    description: 'Wake-on-LAN G3 → wait for node Ready → scale up qBittorrent & Jellyfin',
    danger: false,
    endpoint: '/api/action/start-media',
    method: 'POST',
  },
  {
    id: 'grafana',
    icon: '📊',
    label: 'Grafana',
    description: 'Open Grafana dashboard',
    link: 'http://192.168.50.59:3000',
  },
  {
    id: 'pihole',
    icon: '🛡️',
    label: 'Pi-hole Admin',
    description: 'Open Pi-hole web admin',
    link: 'http://192.168.50.57/admin',
  },
]

function ResultModal({ title, result, onClose }) {
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <span>{title}</span>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>
        <pre className="modal-body">{JSON.stringify(result, null, 2)}</pre>
      </div>
    </div>
  )
}

function AddActionModal({ onSave, onClose }) {
  const [form, setForm] = useState({ id: '', name: '', icon: '⚙️', command: '', type: 'kubectl', host: '', confirm: true })

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const handleSave = async () => {
    if (!form.name || !form.command) return
    const id = form.id || `custom-${Date.now()}`
    await fetch('/api/custom-actions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...form, id }),
    })
    onSave()
    onClose()
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal modal-form" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <span>Add Custom Action</span>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>
        <div className="form-body">
          <label>Icon (emoji)</label>
          <input value={form.icon} onChange={e => set('icon', e.target.value)} style={{ width: 60 }} />

          <label>Name *</label>
          <input value={form.name} onChange={e => set('name', e.target.value)} placeholder="My Action" />

          <label>Type</label>
          <select value={form.type} onChange={e => set('type', e.target.value)}>
            <option value="kubectl">kubectl / bash</option>
            <option value="ssh">SSH</option>
          </select>

          {form.type === 'ssh' && (
            <>
              <label>SSH Host</label>
              <input value={form.host} onChange={e => set('host', e.target.value)} placeholder="192.168.50.13" />
            </>
          )}

          <label>Command *</label>
          <textarea
            value={form.command}
            onChange={e => set('command', e.target.value)}
            placeholder={form.type === 'ssh' ? 'sudo systemctl restart nginx' : 'kubectl get pods -A'}
            rows={3}
            style={{ resize: 'vertical' }}
          />

          <label className="checkbox-label">
            <input type="checkbox" checked={form.confirm} onChange={e => set('confirm', e.target.checked)} />
            Require confirmation before running
          </label>

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 8 }}>
            <button className="btn-secondary" onClick={onClose}>Cancel</button>
            <button className="btn-primary" onClick={handleSave}>Save Action</button>
          </div>
        </div>
      </div>
    </div>
  )
}

export default function QuickActions({ pods, onRefresh }) {
  const [customActions, setCustomActions] = useState([])
  const [running, setRunning] = useState({})
  const [result, setResult] = useState(null)
  const [showAdd, setShowAdd] = useState(false)
  const [startStatus, setStartStatus] = useState(null)
  const [stopStatus,  setStopStatus]  = useState(null)

  const loadCustom = async () => {
    const r = await fetch('/api/custom-actions')
    const d = await r.json()
    setCustomActions(d)
  }

  useEffect(() => { loadCustom() }, [])

  // Poll start-media status when running
  useEffect(() => {
    if (!running['start-media']) return
    const id = setInterval(async () => {
      const r = await fetch('/api/action/start-media/status')
      const s = await r.json()
      setStartStatus(s)
      if (s.state === 'done' || s.state === 'error') {
        setRunning(prev => ({ ...prev, 'start-media': false }))
        onRefresh()
        clearInterval(id)
      }
    }, 3000)
    return () => clearInterval(id)
  }, [running['start-media']])

  // Poll stop-media status when running
  useEffect(() => {
    if (!running['stop-media']) return
    const id = setInterval(async () => {
      const r = await fetch('/api/action/stop-media/status')
      const s = await r.json()
      setStopStatus(s)
      if (s.state === 'done' || s.state === 'error') {
        setRunning(prev => ({ ...prev, 'stop-media': false }))
        onRefresh()
        clearInterval(id)
      }
    }, 3000)
    return () => clearInterval(id)
  }, [running['stop-media']])

  const runBuiltin = async (action) => {
    if (action.link) {
      window.open(action.link, '_blank')
      return
    }
    if (action.danger && !window.confirm(action.confirmMsg || `Run "${action.label}"?`)) return

    setRunning(prev => ({ ...prev, [action.id]: true }))
    if (action.id === 'start-media') setStartStatus({ state: 'starting', log: [] })
    if (action.id === 'stop-media')  setStopStatus({ state: 'starting', log: [] })

    const isAsync = action.id === 'start-media' || action.id === 'stop-media'

    try {
      const r = await fetch(action.endpoint, { method: action.method })
      const data = await r.json()
      if (!isAsync) {
        setResult({ title: action.label, data })
        setRunning(prev => ({ ...prev, [action.id]: false }))
        onRefresh()
      }
    } catch (e) {
      setResult({ title: action.label, data: { error: String(e) } })
      setRunning(prev => ({ ...prev, [action.id]: false }))
    }
  }

  const runCustom = async (action) => {
    if (action.confirm && !window.confirm(`Run "${action.name}"?\n\n${action.command}`)) return
    setRunning(prev => ({ ...prev, [action.id]: true }))
    try {
      const r = await fetch(`/api/custom-actions/${action.id}/run`, { method: 'POST' })
      const data = await r.json()
      setResult({ title: action.name, data })
    } catch (e) {
      setResult({ title: action.name, data: { error: String(e) } })
    } finally {
      setRunning(prev => ({ ...prev, [action.id]: false }))
    }
  }

  const deleteCustom = async (id) => {
    if (!window.confirm('Delete this custom action?')) return
    await fetch(`/api/custom-actions/${id}`, { method: 'DELETE' })
    loadCustom()
  }

  return (
    <div className="actions-page">
      {/* Builtin */}
      <div className="section-title">Quick Actions</div>
      <div className="actions-grid">
        {BUILTIN_ACTIONS.map(action => (
          <div key={action.id} className={`action-card ${action.danger ? 'action-danger' : ''}`}>
            <div className="action-top">
              <span className="action-icon">{action.icon}</span>
              <span className="action-label">{action.label}</span>
            </div>
            <p className="action-desc">{action.description}</p>
            {action.id === 'start-media' && startStatus && (
              <div className="status-log">
                <div className={`status-state state-${startStatus.state}`}>{startStatus.state}</div>
                {startStatus.log?.slice(-3).map((l, i) => (
                  <div key={i} className="status-line">{l}</div>
                ))}
              </div>
            )}
            {action.id === 'stop-media' && stopStatus && (
              <div className="status-log">
                <div className={`status-state state-${stopStatus.state}`}>{stopStatus.state}</div>
                {stopStatus.log?.slice(-3).map((l, i) => (
                  <div key={i} className="status-line">{l}</div>
                ))}
              </div>
            )}
            <button
              className={`action-btn ${action.danger ? 'btn-danger-outline' : action.link ? 'btn-link-outline' : 'btn-primary-outline'}`}
              onClick={() => runBuiltin(action)}
              disabled={!!running[action.id]}
            >
              {running[action.id] ? <><span className="spinner" style={{ width: 14, height: 14 }} /> Running…</> : action.link ? 'Open ↗' : 'Run'}
            </button>
          </div>
        ))}
      </div>

      {/* Pod restart */}
      <PodRestartSection pods={pods} onRefresh={onRefresh} />

      {/* Custom actions */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', margin: '24px 0 12px' }}>
        <div className="section-title" style={{ margin: 0 }}>Custom Actions</div>
        <button className="btn-primary" onClick={() => setShowAdd(true)}>+ Add Action</button>
      </div>
      {customActions.length === 0 ? (
        <div className="empty-custom">
          No custom actions yet. Click "+ Add Action" to create one.
        </div>
      ) : (
        <div className="actions-grid">
          {customActions.map(action => (
            <div key={action.id} className="action-card">
              <div className="action-top">
                <span className="action-icon">{action.icon}</span>
                <span className="action-label">{action.name}</span>
              </div>
              <p className="action-desc mono" style={{ fontSize: 11 }}>{action.command}</p>
              <div style={{ display: 'flex', gap: 6, marginTop: 'auto' }}>
                <button
                  className="action-btn btn-primary-outline"
                  style={{ flex: 1 }}
                  onClick={() => runCustom(action)}
                  disabled={!!running[action.id]}
                >
                  {running[action.id] ? 'Running…' : 'Run'}
                </button>
                <button className="action-btn btn-ghost-sm" onClick={() => deleteCustom(action.id)}>🗑</button>
              </div>
            </div>
          ))}
        </div>
      )}

      {result && <ResultModal title={result.title} result={result.data} onClose={() => setResult(null)} />}
      {showAdd && <AddActionModal onSave={loadCustom} onClose={() => setShowAdd(false)} />}
    </div>
  )
}

function PodRestartSection({ pods, onRefresh }) {
  const [selected, setSelected] = useState('')
  const [running, setRunning] = useState(false)
  const [confirming, setConfirming] = useState(false)

  const handleRestart = async () => {
    if (!selected) return
    if (!confirming) { setConfirming(true); return }
    const [ns, name] = selected.split('|')
    setRunning(true)
    setConfirming(false)
    await fetch(`/api/action/restart/${ns}/${name}`, { method: 'POST' })
    setRunning(false)
    onRefresh()
  }

  return (
    <div style={{ marginTop: 24 }}>
      <div className="section-title">Restart Pod</div>
      <div className="restart-row">
        <select value={selected} onChange={e => { setSelected(e.target.value); setConfirming(false) }} style={{ flex: 1 }}>
          <option value="">Select a pod…</option>
          {pods.map(p => (
            <option key={`${p.namespace}/${p.name}`} value={`${p.namespace}|${p.name}`}>
              {p.namespace} / {p.name}
            </option>
          ))}
        </select>
        <button
          className={`action-btn ${confirming ? 'btn-danger-outline' : 'btn-primary-outline'}`}
          onClick={handleRestart}
          disabled={!selected || running}
        >
          {running ? 'Restarting…' : confirming ? 'Confirm restart?' : '↺ Restart'}
        </button>
      </div>
    </div>
  )
}
