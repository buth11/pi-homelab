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
function serviceLabel(name, label) {
  if (label) return label
  const key = Object.keys(KNOWN_SERVICES).find(k => name.toLowerCase().includes(k))
  return key ? KNOWN_SERVICES[key].label : name
}

export default function ServiceTable({ services }) {
  const lbServices = services.filter(s => s.type === 'LoadBalancer')
  const allServices = services

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
                  <span className="service-name">{serviceLabel(s.name, s.label)}</span>
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
          <span className="text-dim" style={{ fontSize: 12 }}>{allServices.length} total</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Namespace</th>
                <th>Name</th>
                <th>Type</th>
                <th>Cluster IP</th>
                <th>External IP</th>
                <th>Ports</th>
                <th>Link</th>
              </tr>
            </thead>
            <tbody>
              {allServices.length === 0 ? (
                <tr><td colSpan={7} className="empty-row">No services found</td></tr>
              ) : allServices.map(svc => (
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
