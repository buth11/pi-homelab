import { useState, useMemo } from 'react'
import './ServiceTable.css'

const KNOWN_SERVICES = {
  'pihole':       { icon: '🛡️', label: 'Pi-hole Admin' },
  'qbittorrent':  { icon: '🌊', label: 'qBittorrent' },
  'firefox':      { icon: '🦊', label: 'Firefox noVNC' },
  'jellyfin':     { icon: '🎬', label: 'Jellyfin' },
  'grafana':      { icon: '📊', label: 'Grafana' },
  'prometheus':   { icon: '🔥', label: 'Prometheus' },
  'dashboard':    { icon: '🖥️', label: 'Dashboard' },
}

function serviceIcon(name) {
  const key = Object.keys(KNOWN_SERVICES).find(k => name.toLowerCase().includes(k))
  return key ? KNOWN_SERVICES[key].icon : '🌐'
}
function serviceLabel(name) {
  // Always prefer name-based lookup so port-derived backend labels don't override
  const key = Object.keys(KNOWN_SERVICES).find(k => name.toLowerCase().includes(k))
  return key ? KNOWN_SERVICES[key].label : name
}

const COLS = [
  { key: 'namespace',   label: 'Namespace' },
  { key: 'name',        label: 'Name' },
  { key: 'type',        label: 'Type' },
  { key: 'cluster_ip',  label: 'Cluster IP' },
  { key: 'external_ip', label: 'External IP' },
  { key: 'ports',       label: 'Ports' },
  { key: '_link',       label: 'Link', noSort: true },
]

function SortIcon({ col, sort }) {
  if (sort.col !== col) return <span className="sort-icon sort-none">⇅</span>
  return sort.dir === 1
    ? <span className="sort-icon sort-asc">↑</span>
    : <span className="sort-icon sort-desc">↓</span>
}

export default function ServiceTable({ services }) {
  const [sort, setSort] = useState({ col: null, dir: 1 })

  function toggleSort(col) {
    setSort(s => {
      if (s.col !== col) return { col, dir: 1 }
      if (s.dir === 1) return { col, dir: -1 }
      return { col: null, dir: 1 }
    })
  }

  const lbServices = services.filter(s => s.type === 'LoadBalancer')

  const sorted = useMemo(() => {
    if (!sort.col) return services
    return [...services].sort((a, b) => {
      let av = a[sort.col] ?? ''
      let bv = b[sort.col] ?? ''
      if (sort.col === 'ports') {
        av = a.ports.map(p => p.port).join(',')
        bv = b.ports.map(p => p.port).join(',')
      }
      const cmp = String(av).localeCompare(String(bv))
      return sort.dir * cmp
    })
  }, [services, sort])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      {/* LoadBalancer quick links */}
      <div>
        <div className="section-title">Service Quick Links</div>
        <div className="service-grid">
          {lbServices
            .filter(s => s.url)
            .map(s => (
              <a key={`${s.namespace}/${s.name}`} href={s.url} target="_blank" rel="noreferrer" className="service-card">
                <span className="service-icon">{serviceIcon(s.name)}</span>
                <div className="service-info">
                  <span className="service-name">{serviceLabel(s.name)}</span>
                  <span className="service-url">{s.url}</span>
                </div>
                <span className="service-arrow">↗</span>
              </a>
            ))}
        </div>
      </div>

      {/* Full table */}
      <div className="card">
        <div className="card-header">
          <span>All Services</span>
          <span className="text-dim" style={{ fontSize: 12 }}>{services.length} total</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
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
              {sorted.length === 0 ? (
                <tr><td colSpan={7} className="empty-row">No services found</td></tr>
              ) : sorted.map(svc => (
                <tr key={`${svc.namespace}/${svc.name}`}>
                  <td className="mono text-dim" style={{ fontSize: 12 }}>{svc.namespace}</td>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span>{serviceIcon(svc.name)}</span>
                      <span className="mono" style={{ fontSize: 12 }}>{svc.name}</span>
                    </div>
                  </td>
                  <td>
                    <span className={`badge ${svc.type === 'LoadBalancer' ? 'badge-blue' : 'badge-gray'}`}>
                      {svc.type}
                    </span>
                  </td>
                  <td className="mono text-dim" style={{ fontSize: 12 }}>{svc.cluster_ip}</td>
                  <td className="mono" style={{ fontSize: 12, color: svc.external_ip ? 'var(--accent)' : 'var(--text2)' }}>
                    {svc.external_ip || '—'}
                  </td>
                  <td className="mono text-dim" style={{ fontSize: 12 }}>
                    {svc.ports.map(p => `${p.port}/${p.protocol}`).join(', ') || '—'}
                  </td>
                  <td>
                    {svc.url ? (
                      <a href={svc.url} target="_blank" rel="noreferrer" className="link-btn">
                        Open ↗
                      </a>
                    ) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
