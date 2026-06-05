import { useState, useEffect, useCallback } from 'react'
import NodeTable from './components/NodeTable.jsx'
import PodTable from './components/PodTable.jsx'
import ServiceTable from './components/ServiceTable.jsx'
import QuickActions from './components/QuickActions.jsx'
import LogViewer from './components/LogViewer.jsx'
import './App.css'

const TABS = ['Nodes', 'Pods', 'Services', 'Actions', 'Logs']
const REFRESH_INTERVAL = 30_000

export default function App() {
  const [tab, setTab] = useState('Nodes')
  const [nodes, setNodes] = useState([])
  const [pods, setPods] = useState([])
  const [services, setServices] = useState([])
  const [alerts, setAlerts] = useState([])
  const [loading, setLoading] = useState(true)
  const [lastRefresh, setLastRefresh] = useState(null)
  const [selectedPod, setSelectedPod] = useState(null)

  const fetchAll = useCallback(async () => {
    try {
      const [nr, pr, sr] = await Promise.all([
        fetch('/api/nodes'),
        fetch('/api/pods'),
        fetch('/api/services'),
      ])
      const [nd, pd, sd] = await Promise.all([nr.json(), pr.json(), sr.json()])
      setNodes(nd)
      setPods(pd)
      setServices(sd)
      setLastRefresh(new Date())

      const a = []
      nd.filter(n => n.status !== 'Ready').forEach(n =>
        a.push({ type: 'error', msg: `Node ${n.name} is ${n.status}` })
      )
      pd.filter(p => ['Error', 'CrashLoopBackOff', 'OOMKilled'].includes(p.status))
        .slice(0, 5)
        .forEach(p => a.push({ type: 'error', msg: `${p.namespace}/${p.name}: ${p.status}` }))
      setAlerts(a)
    } catch (e) {
      console.error('Fetch error:', e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const id = setInterval(fetchAll, REFRESH_INTERVAL)
    return () => clearInterval(id)
  }, [fetchAll])

  const handleViewLogs = (pod) => {
    setSelectedPod(pod)
    setTab('Logs')
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-left">
          <span className="logo">🖥️</span>
          <span className="title">Homelab Dashboard</span>
          {alerts.length > 0 && (
            <span className="alert-count" title={alerts.map(a => a.msg).join('\n')}>
              ⚠️ {alerts.length} alert{alerts.length !== 1 ? 's' : ''}
            </span>
          )}
        </div>
        <div className="header-right">
          {lastRefresh && (
            <span className="last-refresh">
              Refreshed {lastRefresh.toLocaleTimeString()}
            </span>
          )}
          <button className="btn-refresh" onClick={fetchAll} title="Refresh now">
            ↻
          </button>
        </div>
      </header>

      {alerts.length > 0 && (
        <div className="alert-bar">
          {alerts.map((a, i) => (
            <div key={i} className={`alert alert-${a.type}`}>
              <span className="alert-icon">●</span> {a.msg}
            </div>
          ))}
        </div>
      )}

      <nav className="tabs">
        {TABS.map(t => (
          <button
            key={t}
            className={`tab-btn ${tab === t ? 'active' : ''}`}
            onClick={() => setTab(t)}
          >
            {TAB_ICONS[t]} {t}
            {t === 'Nodes' && nodes.length > 0 && (
              <span className="tab-count">{nodes.length}</span>
            )}
            {t === 'Pods' && pods.length > 0 && (
              <span className="tab-count">{pods.length}</span>
            )}
          </button>
        ))}
      </nav>

      <main className="content">
        {loading ? (
          <div className="loading-state">
            <div className="spinner" />
            <span>Connecting to cluster…</span>
          </div>
        ) : (
          <>
            {tab === 'Nodes' && <NodeTable nodes={nodes} />}
            {tab === 'Pods' && <PodTable pods={pods} onViewLogs={handleViewLogs} onRefresh={fetchAll} />}
            {tab === 'Services' && <ServiceTable services={services} />}
            {tab === 'Actions' && <QuickActions pods={pods} onRefresh={fetchAll} />}
            {tab === 'Logs' && <LogViewer pods={pods} initialPod={selectedPod} />}
          </>
        )}
      </main>
    </div>
  )
}

const TAB_ICONS = {
  Nodes: '🖥',
  Pods: '📦',
  Services: '🌐',
  Actions: '⚡',
  Logs: '📋',
}
