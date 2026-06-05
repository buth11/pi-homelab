import { useState, useMemo } from 'react'
import './PodTable.css'

const STATUS_BADGE = {
  Running:           'badge-green',
  Succeeded:         'badge-blue',
  Pending:           'badge-yellow',
  Failed:            'badge-red',
  Error:             'badge-red',
  CrashLoopBackOff:  'badge-red',
  OOMKilled:         'badge-red',
  Terminating:       'badge-yellow',
  Unknown:           'badge-gray',
}

function badgeClass(status) {
  return STATUS_BADGE[status] || 'badge-gray'
}

export default function PodTable({ pods, onViewLogs, onRefresh }) {
  const [nsFilter, setNsFilter] = useState('')
  const [search, setSearch]     = useState('')
  const [confirming, setConfirming] = useState(null)

  const namespaces = useMemo(() => {
    const s = new Set(pods.map(p => p.namespace))
    return ['', ...Array.from(s).sort()]
  }, [pods])

  const filtered = useMemo(() => {
    return pods.filter(p => {
      if (nsFilter && p.namespace !== nsFilter) return false
      if (search && !`${p.namespace}/${p.name}`.toLowerCase().includes(search.toLowerCase())) return false
      return true
    })
  }, [pods, nsFilter, search])

  const handleRestart = async (pod) => {
    if (confirming !== `${pod.namespace}/${pod.name}`) {
      setConfirming(`${pod.namespace}/${pod.name}`)
      return
    }
    setConfirming(null)
    await fetch(`/api/action/restart/${pod.namespace}/${pod.name}`, { method: 'POST' })
    onRefresh()
  }

  const errorCount = pods.filter(p => ['Error', 'CrashLoopBackOff', 'OOMKilled', 'Failed'].includes(p.status)).length

  return (
    <div className="card">
      <div className="card-header">
        <span>Pods {errorCount > 0 && <span className="badge badge-red">{errorCount} errors</span>}</span>
        <div className="pod-filters">
          <input
            placeholder="Search…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            style={{ width: 160 }}
          />
          <select value={nsFilter} onChange={e => setNsFilter(e.target.value)}>
            <option value="">All namespaces</option>
            {namespaces.filter(Boolean).map(ns => (
              <option key={ns} value={ns}>{ns}</option>
            ))}
          </select>
          <span className="text-dim" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
            {filtered.length} / {pods.length}
          </span>
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Namespace</th>
              <th>Pod</th>
              <th>Status</th>
              <th>Ready</th>
              <th>Node</th>
              <th>IP</th>
              <th>Restarts</th>
              <th>Age</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr><td colSpan={9} className="empty-row">No pods match filter</td></tr>
            ) : filtered.map(pod => {
              const key = `${pod.namespace}/${pod.name}`
              const isErrorState = ['Error', 'CrashLoopBackOff', 'OOMKilled', 'Failed'].includes(pod.status)
              return (
                <tr key={key} className={isErrorState ? 'row-error' : ''}>
                  <td className="mono text-dim" style={{ fontSize: 12 }}>{pod.namespace}</td>
                  <td className="pod-name">{pod.name}</td>
                  <td>
                    <span className={`badge ${badgeClass(pod.status)}`}>{pod.status}</span>
                  </td>
                  <td className="mono" style={{ fontSize: 12 }}>{pod.ready}</td>
                  <td className="text-dim" style={{ fontSize: 12 }}>{pod.node || '—'}</td>
                  <td className="mono text-dim" style={{ fontSize: 12 }}>{pod.ip || '—'}</td>
                  <td>
                    {pod.restarts > 0
                      ? <span className={pod.restarts > 5 ? 'text-red' : 'text-yellow'}>{pod.restarts}</span>
                      : <span className="text-dim">0</span>
                    }
                  </td>
                  <td className="text-dim" style={{ fontSize: 12 }}>{pod.age}</td>
                  <td>
                    <div className="row-actions">
                      <button className="btn-sm btn-ghost" onClick={() => onViewLogs(pod)} title="View logs">
                        📋
                      </button>
                      <button
                        className={`btn-sm ${confirming === key ? 'btn-danger' : 'btn-ghost'}`}
                        onClick={() => handleRestart(pod)}
                        title={confirming === key ? 'Click again to confirm restart' : 'Restart pod'}
                      >
                        {confirming === key ? 'Confirm?' : '↺'}
                      </button>
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
