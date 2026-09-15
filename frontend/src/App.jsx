import { useEffect, useState } from 'react'
import './App.css'

const STATUS = {
  loading: { text: 'checking...', color: '#666' },
  connected: { text: 'API: connected', color: 'green' },
  unreachable: { text: 'API: unreachable', color: 'red' },
}

function App() {
  const [status, setStatus] = useState('loading')

  useEffect(() => {
    let cancelled = false

    fetch(`${import.meta.env.VITE_API_URL}/health`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then(() => {
        if (!cancelled) setStatus('connected')
      })
      .catch(() => {
        if (!cancelled) setStatus('unreachable')
      })

    return () => {
      cancelled = true
    }
  }, [])

  return (
    <main>
      <h1>SignalStack</h1>
      <p style={{ color: STATUS[status].color }}>{STATUS[status].text}</p>
    </main>
  )
}

export default App
