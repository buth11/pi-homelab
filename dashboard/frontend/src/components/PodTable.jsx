import { useState, useMemo, useEffect, useRef } from 'react'
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
const ERROR_STATUSES = ['Error', 'CrashLoopBackOff', 'OOMKilled', 'Failed']

function badgeClass(status) {
  return STATUS_BADGE[status] || 'badge-gray'
}

// ─── Log Modal ────────────────────────────────────────────────────────────────

function colorLine(line) {
  if (/error|fail|fatal|exception/i.test(line)) return 'log-error'
  if (/warn/i.test(line)) return 'log-warn'
  if (/info/i.test(line)) return 'log-info'
  if (/debug/i.test(line)) return 'log-debug'
  return ''
}

function LogModal({ pod, onClose }) {
  const [container, setContainer] = useState(pod.containers?.[0] || '')
  const [lines, setLines]         = useState(50)
  const [logs, setLogs]           = useState('')
  const [loading, setLoading]     = useState(false)
  const [error, setError]         = useState('')
  const logRef = useRef(null)

  const fetchLogs = async (c = container, l = lines) => {
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams({ lines: l })
      if (c) params.set('container', c)
      const r = await fetch(`/api/logs/${pod.namespace}/${pod.name}?${params}`)
      if (!r.ok) {
        const e = await r.json()
        throw new Error(e.detail || r.statusText)
      }
      const d = await r.json()
      setLogs(d.logs || '')
    } catch (e) {
      setError(String(e))
      setLogs('')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchLogs() }, [])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [logs])

  const logLines = logs.split('\n')
  const multiContainer = pod.containers && pod.containers.length > 1

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="log-modal" onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="log-modal-header">
          <div className="log-modal-title">
            <span className="log-modal-ns">{pod.namespace}</span>
            <span className="log-modal-sep">/</span>
            <span className="log-modal-name">{pod.name}</span>
          </div>
          <div className="log-modal-controls">
            {multiContainer && (
              <select
                value={container}
                onChange={e => { setContainer(e.target.value); fetchLogs(e.target.value) }}
                className="log-select"
              >
                {pod.containers.map(c => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
            )}
            <select
              value={lines}
              onChange={e => { setLines(Number(e.target.value)); fetchLogs(container, Number(e.target.value)) }}
              className="log-select"
            >
              <option value={50}>50 lines</option>
              <option value={100}>100 lines</option>
              <option value={200}>200 lines</option>
              <option value={500}>500 lines</option>
            </select>
            <button className="log-refresh-btn" onClick={() => fetchLogs()} disabled={loading}>
              {loading ? <span className="spinner" style={{ width: 13, height: 13 }} /> : '↻'} Refresh
            </button>
            <button className="log-close-btn" onClick={onClose}>✕</button>
          </div>
        </div>

        {/* Error */}
        {error && <div className="log-modal-error">⚠ {error}</div>}

        {/* Log body */}
        <div className="log-modal-body" ref={logRef}>
          {!logs && !loading && !error && (
            <div className="log-modal-empty">No logs available.</div>
          )}
          {logs && logLines.map((line, i) => (
            <div key={i} className={`log-line ${colorLine(line)}`}>
              <span className="log-num">{i + 1}</span>
              <span className="log-text">{line}</span>
            </div>
          ))}
        </div>

        {/* Footer */}
        {logs && (
          <div className="log-modal-footer">
            {logLines.length} lines
            {multiContainer && container && <span> · container: <code>{container}</code></span>}
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Sort helpers ─────────────────────────────────────────────────────────────

const COLS = [
  { key: 'name',      label: 'Pod' },
  { key: 'status',    label: 'Status' },
  { key: 'ready',     label: 'Ready' },
  { key: 'node',      label: 'Node' },
  { key: 'ip',        label: 'IP' },
  { key: 'restarts',  label: 'Restarts' },
  { key: 'age',       label: 'Age' },
  { key: '_logs',     label: 'Logs', noSort: true },
  { key: '_actions',  label: '',     noSort: true },
]

function SortIcon({ col, sort }) {
  if (sort.col !== col) return <span className="sort-icon sort-none">⇅</span>
  return sort.dir === 1
    ? <span className="sort-icon sort-asc">↑</span>
    : <span className="sort-icon sort-desc">↓</span>
}

// ─── Namespace group header ───────────────────────────────────────────────────

function GroupHeader({ ns, pods, expanded, onToggle }) {
  const running = pods.filter(p => p.status === 'Running' || p.status === 'Succeeded').length
  const errors  = pods.filter(p => ERROR_STATUSES.includes(p.status)).length

  return (
    <tr className="group-header" onClick={onToggle}>
      <td colSpan={9}>
        <div className="group-header-inner">
          <span className={`group-chevron ${expanded ? 'chevron-open' : ''}`}>›</span>
          <span className="group-ns">{ns}</span>
          <span className="group-count">{pods.length} pods</span>
          <span className="group-running">{running} running</span>
          {errors > 0 && <span className="group-errors">{errors} error{errors !== 1 ? 's' : ''}</span>}
        </div>
      </td>
    </tr>
  )
}

// ─── Main component ───────────────────────────────────────────────────────────

export default function PodTable({ pods, onViewLogs, onRefresh }) {
  const [nsFilter,    setNsFilter]    = useState('')
  const [nodeFilter,  setNodeFilter]  = useState('')
  const [search,      setSearch]      = useState('')
  const [confirming,  setConfirming]  = useState(null)
  const [sort,        setSort]        = useState({ col: null, dir: 1 })
  const [logModal,    setLogModal]    = useState(null)

  const namespaces = useMemo(() => {
    const s = new Set(pods.map(p => p.namespace))
    return Array.from(s).sort()
  }, [pods])

  const nodes = useMemo(() => {
    const s = new Set(pods.map(p => p.node).filter(Boolean))
    return Array.from(s).sort()
  }, [pods])

  // Start with all groups collapsed
  const [expanded, setExpanded] = useState({})
  useEffect(() => {
    setExpanded(prev => {
      const next = {}
      namespaces.forEach(ns => { next[ns] = ns in prev ? prev[ns] : false })
      return next
    })
  }, [namespaces.join(',')])

  function toggleNs(ns) {
    setExpanded(e => ({ ...e, [ns]: !e[ns] }))
  }
  function expandAll()  { setExpanded(Object.fromEntries(namespaces.map(ns => [ns, true]))) }
  function collapseAll(){ setExpanded(Object.fromEntries(namespaces.map(ns => [ns, false]))) }

  function toggleSort(col) {
    setSort(s => {
      if (s.col !== col) return { col, dir: 1 }
      if (s.dir === 1) return { col, dir: -1 }
      return { col: null, dir: 1 }
    })
  }

  const filtered = useMemo(() => {
    return pods.filter(p => {
      if (nsFilter   && p.namespace !== nsFilter) return false
      if (nodeFilter && p.node      !== nodeFilter) return false
      if (search && !`${p.namespace}/${p.name}`.toLowerCase().includes(search.toLowerCase())) return false
      return true
    })
  }, [pods, nsFilter, nodeFilter, search])

  const sortedFiltered = useMemo(() => {
    if (!sort.col) return filtered
    return [...filtered].sort((a, b) => {
      const av = sort.col === 'restarts' ? (a.restarts ?? 0) : (a[sort.col] ?? '')
      const bv = sort.col === 'restarts' ? (b.restarts ?? 0) : (b[sort.col] ?? '')
      const cmp = typeof av === 'number' && typeof bv === 'number'
        ? av - bv
        : String(av).localeCompare(String(bv))
      return sort.dir * cmp
    })
  }, [filtered, sort])

  // Group by namespace
  const groups = useMemo(() => {
    const map = {}
    sortedFiltered.forEach(p => {
      if (!map[p.namespace]) map[p.namespace] = []
      map[p.namespace].push(p)
    })
    return Object.entries(map).sort(([a], [b]) => a.localeCompare(b))
  }, [sortedFiltered])

  const handleRestart = async (pod) => {
    const key = `${pod.namespace}/${pod.name}`
    if (confirming !== key) { setConfirming(key); return }
    setConfirming(null)
    await fetch(`/api/action/restart/${pod.namespace}/${pod.name}`, { method: 'POST' })
    onRefresh()
  }

  const errorCount = pods.filter(p => ERROR_STATUSES.includes(p.status)).length

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
            {namespaces.map(ns => (
              <option key={ns} value={ns}>{ns}</option>
            ))}
          </select>
          <select value={nodeFilter} onChange={e => setNodeFilter(e.target.value)}>
            <option value="">All nodes</option>
            {nodes.map(n => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
          <span className="text-dim" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
            {filtered.length} / {pods.length}
          </span>
          <div className="group-btns">
            <button className="btn-xs btn-ghost" onClick={expandAll}>Rozwiń wszystkie</button>
            <button className="btn-xs btn-ghost" onClick={collapseAll}>Zwiń wszystkie</button>
          </div>
        </div>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {/* namespace column in grouped view is part of group header — keep empty spacer */}
              {COLS.map(c => (
                <th
                  key={c.key}
                  className={c.noSort ? '' : `th-sortable ${sort.col === c.key ? 'th-active' : ''}`}
                  onClick={c.noSort ? undefined : () => toggleSort(c.key)}
                >
                  {c.label}
                  {!c.noSort && <SortIcon col={c.key} sort={sort} />}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {groups.length === 0 ? (
              <tr><td colSpan={9} className="empty-row">No pods match filter</td></tr>
            ) : groups.map(([ns, nsPods]) => (
              <>
                <GroupHeader
                  key={`hdr-${ns}`}
                  ns={ns}
                  pods={nsPods}
                  expanded={!!expanded[ns]}
                  onToggle={() => toggleNs(ns)}
                />
                {nsPods.map(pod => {
                  const key = `${pod.namespace}/${pod.name}`
                  const isError = ERROR_STATUSES.includes(pod.status)
                  return (
                    <tr
                      key={key}
                      className={`pod-row ${isError ? 'row-error' : ''} ${expanded[ns] ? 'row-visible' : 'row-hidden'}`}
                    >
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
                        <button
                          className="btn-sm btn-log"
                          onClick={() => setLogModal(pod)}
                          title="View logs"
                        >
                          📋 Logi
                        </button>
                      </td>
                      <td>
                        <button
                          className={`btn-sm ${confirming === key ? 'btn-danger' : 'btn-ghost'}`}
                          onClick={() => handleRestart(pod)}
                          title={confirming === key ? 'Click again to confirm' : 'Restart pod'}
                        >
                          {confirming === key ? 'Confirm?' : '↺'}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </>
            ))}
          </tbody>
        </table>
      </div>

      {logModal && <LogModal pod={logModal} onClose={() => setLogModal(null)} />}
    </div>
  )
}
