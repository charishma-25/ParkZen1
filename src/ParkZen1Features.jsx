import { useEffect, useRef, useState } from 'react'

const API = import.meta.env.VITE_API_URL || 'http://127.0.0.1:5000'
const request = async (path, options = {}) => {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) }
  const token = localStorage.getItem('parkzen-token')
  if (token) headers.Authorization = `Bearer ${token}`
  const response = await fetch(`${API}${path}`, { ...options, headers })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.error || 'Unable to load this ParkZen feature.')
  return data
}

export function ParkZen1Features({ page, onError }) {
  const [reservations, setReservations] = useState([])
  const [membership, setMembership] = useState(null)
  const [passes, setPasses] = useState([])
  const [notifications, setNotifications] = useState([])
  const [tickets, setTickets] = useState([])
  const [destinations, setDestinations] = useState([])
  const [floors, setFloors] = useState([])
  const [vehicles, setVehicles] = useState([])
  const [booking, setBooking] = useState({ destination: '', slot: '', vehicle: '', start: '', end: '' })
  const [bookingMessage, setBookingMessage] = useState('')
  const [form, setForm] = useState({ kind: 'question', category: 'booking', subject: '', description: '' })
  const [scanner, setScanner] = useState(null)
  const videoRef = useRef(null)
  const streamRef = useRef(null)
  const scannerReservationId = scanner?.reservationId

  useEffect(() => {
    const load = async () => {
      try {
        if (page === 'prebook') {
          const destinationData = await request('/api/destinations')
          const vehicleData = await request('/api/vehicles')
          setDestinations(destinationData.destinations)
          setVehicles(vehicleData.vehicles)
          if (destinationData.destinations.length) setBooking(current => ({ ...current, destination: current.destination || destinationData.destinations[0].id }))
        }
        if (page === 'reservations') setReservations((await request('/api/reservations')).reservations)
        if (page === 'membership') {
          setMembership(await request('/api/memberships/me'))
          setPasses((await request('/api/passes')).passes)
        }
        if (page === 'notifications') setNotifications((await request('/api/notifications')).notifications)
        if (page === 'help') setTickets((await request('/api/support')).tickets)
      } catch (error) { onError(error.message) }
    }
    load()
  }, [page, onError])

  useEffect(() => {
    if (page !== 'prebook' || !booking.destination) return
    request(`/api/parking/${booking.destination}`).then(data => setFloors(data.floors)).catch(error => onError(error.message))
  }, [page, booking.destination, onError])

  useEffect(() => () => {
    streamRef.current?.getTracks().forEach(track => track.stop())
  }, [])

  useEffect(() => {
    if (!scannerReservationId || !videoRef.current) return undefined
    let stopped = false
    let timer
    const start = async () => {
      if (!('BarcodeDetector' in window) || !navigator.mediaDevices?.getUserMedia) {
        onError('This browser does not provide camera QR scanning. Use the QR image download or enter the reference manually.')
        setScanner(current => ({ ...current, unsupported: true }))
        return
      }
      try {
        const detector = new window.BarcodeDetector({ formats: ['qr_code'] })
        streamRef.current = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false })
        videoRef.current.srcObject = streamRef.current
        await videoRef.current.play()
        const scan = async () => {
          if (stopped) return
          const codes = await detector.detect(videoRef.current)
          const value = codes.find(code => code.rawValue)?.rawValue
          if (value) setScanner(current => ({ ...current, value }))
          else timer = window.setTimeout(scan, 250)
        }
        scan()
      } catch (error) {
        onError(`Camera access failed: ${error.message}`)
      }
    }
    start()
    return () => {
      stopped = true
      window.clearTimeout(timer)
      streamRef.current?.getTracks().forEach(track => track.stop())
      streamRef.current = null
    }
  }, [scannerReservationId, onError])

  const submitBooking = async event => {
    event.preventDefault()
    setBookingMessage('')
    try {
      const result = await request('/api/reservations', {
        method: 'POST',
        body: JSON.stringify({
          slot_id: Number(booking.slot),
          vehicle_id: booking.vehicle ? Number(booking.vehicle) : null,
          start_at: new Date(booking.start).toISOString(),
          end_at: new Date(booking.end).toISOString(),
        }),
      })
      setBookingMessage(`Booking ${result.reservation.booking_ref} confirmed for ${result.reservation.slot_code}.`)
    } catch (error) { onError(error.message) }
  }

  const updateReservation = async (reservation, action, body) => {
    try {
      await request(`/api/reservations/${reservation.id}/${action}`, { method: 'POST', body: body ? JSON.stringify(body) : undefined })
      setReservations((await request('/api/reservations')).reservations)
    } catch (error) { onError(error.message) }
  }

  const updateMembership = async (path, body) => {
    try {
      await request(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined })
      setMembership(await request('/api/memberships/me'))
    } catch (error) { onError(error.message) }
  }

  const confirmTestPayment = async reservation => {
    try {
      await request(`/api/payments/${reservation.id}/test-confirm`, { method: 'POST' })
      setReservations((await request('/api/reservations')).reservations)
    } catch (error) { onError(error.message) }
  }

  const closeScanner = () => setScanner(null)
  const submitScan = async () => {
    if (!scanner?.value) return
    await updateReservation({ id: scanner.reservationId }, 'check-in', { qr_reference: scanner.value })
    closeScanner()
  }
  const downloadQr = async reservation => {
    try {
      const response = await fetch(`${API}/api/reservations/${reservation.id}/qr`, { headers: { Authorization: `Bearer ${localStorage.getItem('parkzen-token')}` } })
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || 'Unable to generate the QR image.')
      const url = URL.createObjectURL(await response.blob())
      const link = document.createElement('a')
      link.href = url
      link.download = `${reservation.booking_ref}.png`
      link.click()
      URL.revokeObjectURL(url)
    } catch (error) { onError(error.message) }
  }

  if (page === 'prebook') return <main className="pz-content"><div className="pz-page-heading"><span className="pz-kicker">PARKZEN PREBOOKING</span><h1>Reserve your parking space</h1><p>Choose a location, floor, slot, vehicle, and arrival window. Availability is checked again securely when you confirm.</p></div><form className="pz-profile-form pz-parking-card" onSubmit={submitBooking}><label>Parking location<select required value={booking.destination} onChange={event => setBooking({ ...booking, destination: event.target.value, slot: '' })}>{destinations.map(item => <option key={item.id} value={item.id}>{item.name} · {item.address}</option>)}</select></label><label>Parking slot<select required value={booking.slot} onChange={event => setBooking({ ...booking, slot: event.target.value })}><option value="">Choose an available slot</option>{floors.map(floor => <optgroup key={floor.id} label={`${floor.name} · ${floor.capacity} spaces`}>{floor.slots.filter(slot => !slot.reserved && slot.status === 'available').map(slot => <option key={slot.id} value={slot.id}>{slot.code} · {slot.vehicle_type}</option>)}</optgroup>)}</select></label><label>Vehicle<select value={booking.vehicle} onChange={event => setBooking({ ...booking, vehicle: event.target.value })}><option value="">No vehicle selected</option>{vehicles.map(vehicle => <option key={vehicle.id} value={vehicle.id}>{vehicle.plate} · {vehicle.vehicle_type}</option>)}</select></label><div className="pz-booking-times"><label>Arrival<input required type="datetime-local" value={booking.start} onChange={event => setBooking({ ...booking, start: event.target.value })} /></label><label>Departure<input required type="datetime-local" value={booking.end} onChange={event => setBooking({ ...booking, end: event.target.value })} /></label></div><p className="pz-booking-note">Prototype estimate: ₹50 per hour. The backend rechecks conflicts and calculates the amount at confirmation.</p><button className="pz-button" disabled={!booking.slot || !booking.start || !booking.end}>Confirm prebooking →</button>{bookingMessage && <div className="pz-inline-success">{bookingMessage}</div>}</form></main>

  if (page === 'reservations') return <main className="pz-content"><div className="pz-page-heading"><span className="pz-kicker">RESERVATION MANAGEMENT</span><h1>Your bookings</h1><p>Keep your booking reference private. Check-in requires the server-validated QR reference.</p></div><section className="pz-history-page">{reservations.length ? reservations.map(item => <article className="pz-reservation-card" key={item.id}><div className="pz-history-row"><span className="pz-history-icon">P</span><b>{item.booking_ref}</b><small>{item.destination_name} · {item.slot_code} · {item.status}</small><time>{new Date(item.start_at).toLocaleString()}</time></div><div className="pz-reservation-details"><span>₹{Number(item.quoted_amount).toFixed(2)} estimate</span><span>Until {new Date(item.end_at).toLocaleString()}</span>{item.status === 'confirmed' && <button className="pz-link" onClick={() => downloadQr(item)}>Download QR image</button>}</div><div className="pz-reservation-actions">{item.status === 'confirmed' && <><button className="pz-button secondary small" onClick={() => updateReservation(item, 'cancel')}>Cancel</button><button className="pz-button small" onClick={() => updateReservation(item, 'check-in', { qr_reference: item.qr_reference })}>Check in</button><button className="pz-button secondary small" onClick={() => setScanner({ reservationId: item.id, value: '' })}>Scan QR</button></>}{item.status === 'checked_in' && <><label className="pz-inline-field">Redeem points<input type="number" min="0" step="10" defaultValue="0" id={`points-${item.id}`} /></label><button className="pz-button small" onClick={() => updateReservation(item, 'check-out', { points_redeemed: Number(document.getElementById(`points-${item.id}`)?.value || 0) })}>Check out</button></>}{item.status === 'completed' && <button className="pz-button secondary small" onClick={() => confirmTestPayment(item)}>Pay test bill</button>}</div></article>) : <div className="pz-empty-state">No reservations yet. Use Prebook to reserve a space.</div>}</section>{scanner && <section className="pz-scanner-card"><h2>Scan booking QR</h2><p>Allow camera access and point it at the reservation QR image.</p>{scanner.unsupported ? <p>Camera QR scanning is unavailable in this browser. Download or open the QR image instead.</p> : <video ref={videoRef} className="pz-scanner-video" muted playsInline />}{scanner.value && <p className="pz-inline-success">QR detected. The server will validate this reference.</p>}<div className="pz-reservation-actions"><button className="pz-button" disabled={!scanner.value} onClick={submitScan}>Validate and check in</button><button className="pz-cancel" onClick={closeScanner}>Close scanner</button></div></section>}</main>

  if (page === 'membership') return <main className="pz-content"><div className="pz-page-heading"><span className="pz-kicker">PARKZEN MEMBERSHIP</span><h1>Memberships, rewards & passes</h1><p>Plans and parking charges are calculated on the server. Test activation is available only when explicitly enabled by the operator.</p></div>{membership && <><section className="pz-parking-card"><span className="pz-kicker">{membership.membership.name.toUpperCase()} TIER</span><h2>{membership.membership.membership_number}</h2><p>{membership.membership.benefits} · Valid until {new Date(membership.membership.valid_until).toLocaleDateString()}</p><div className="pz-detail-stats"><div><span>DISCOUNT</span><b>{membership.membership.discount_percent}%</b></div><div><span>POINTS</span><b className="green">{membership.membership.points_balance}</b></div><div><span>PRIORITY BOOKING</span><b>{membership.membership.priority_booking ? 'Yes' : 'No'}</b></div></div><div className="pz-reservation-actions"><button className="pz-button secondary small" onClick={() => updateMembership('/api/memberships/renew', { payment_mode: 'test' })}>Renew (test mode)</button><button className="pz-cancel" onClick={() => updateMembership('/api/memberships/cancel')}>Cancel membership</button></div></section><section className="pz-plan-grid">{membership.plans.map(plan => <article className="pz-parking-card" key={plan.id}><span className="pz-kicker">{plan.name}</span><h2>₹{Number(plan.price).toFixed(2)}</h2><p>{plan.benefits}</p><p>{plan.discount_percent}% discount · {plan.duration_days} days · {plan.points_per_transaction} points/hour</p><button className="pz-button small" onClick={() => updateMembership('/api/memberships/purchase', { plan_id: plan.id, payment_mode: 'test' })}>Activate in test mode</button></article>)}</section><section className="pz-parking-card"><h2>Loyalty history</h2>{membership.transactions.map(item => <div className="pz-history-row" key={item.id}><b>{item.kind}</b><small>{item.points} points</small><time>{new Date(item.created_at).toLocaleString()}</time></div>)}</section></>}{<section className="pz-parking-card"><h2>Parking passes</h2><div className="pz-reservation-actions">{['daily', 'weekly', 'monthly'].map(type => <button key={type} className="pz-button secondary small" onClick={async () => { try { await request('/api/passes', { method: 'POST', body: JSON.stringify({ pass_type: type, payment_mode: 'test' }) }); setPasses((await request('/api/passes')).passes) } catch (error) { onError(error.message) } }}>Buy {type} pass (test mode)</button>)}</div>{passes.length ? passes.map(item => <div className="pz-history-row" key={item.id}><b>{item.pass_type} pass</b><small>{item.status} · ₹{item.price}</small><time>{new Date(item.valid_until).toLocaleDateString()}</time></div>) : <div className="pz-empty-state">No passes yet.</div>}</section>}</main>
  if (page === 'notifications') return <main className="pz-content"><div className="pz-page-heading"><span className="pz-kicker">NOTIFICATION CENTER</span><h1>Updates for you</h1></div><section className="pz-history-page">{notifications.length ? notifications.map(item => <div className="pz-history-row" key={item.id}><span className="pz-history-icon">!</span><b>{item.title}</b><small>{item.message}</small><time>{new Date(item.created_at).toLocaleString()}</time></div>) : <div className="pz-empty-state">You have no notifications.</div>}</section></main>
  if (page === 'help') return <main className="pz-content"><div className="pz-page-heading"><span className="pz-kicker">HELP & FEEDBACK</span><h1>How can we help?</h1><p>Questions, suggestions, and complaints stay private to your account.</p></div><form className="pz-profile-form pz-parking-card" onSubmit={async event => { event.preventDefault(); try { await request('/api/support', { method: 'POST', body: JSON.stringify(form) }); setForm({ ...form, subject: '', description: '' }); setTickets((await request('/api/support')).tickets) } catch (error) { onError(error.message) } }}><label>Type<select value={form.kind} onChange={event => setForm({ ...form, kind: event.target.value })}><option value="question">Ask a question</option><option value="suggestion">Suggestion</option><option value="complaint">Complaint</option></select></label><label>Subject<input required value={form.subject} onChange={event => setForm({ ...form, subject: event.target.value })} /></label><label>Description<textarea required rows="5" value={form.description} onChange={event => setForm({ ...form, description: event.target.value })} /></label><button className="pz-button">Submit ticket</button></form><section className="pz-history-page">{tickets.map(ticket => <div className="pz-history-row" key={ticket.id}><span className="pz-history-icon">?</span><b>{ticket.ticket_ref}</b><small>{ticket.subject} · {ticket.status}</small><time>{new Date(ticket.updated_at).toLocaleString()}</time></div>)}</section></main>
  return <main className="pz-content"><div className="pz-page-heading"><span className="pz-kicker">RESERVATION HISTORY</span><h1>Your bookings</h1><p>QR references are opaque and validated by the backend at check-in.</p></div><section className="pz-history-page">{reservations.length ? reservations.map(item => <div className="pz-history-row" key={item.id}><span className="pz-history-icon">P</span><b>{item.booking_ref}</b><small>{item.destination_name} · {item.slot_code} · {item.status}</small><time>{new Date(item.start_at).toLocaleString()}</time></div>) : <div className="pz-empty-state">No reservations yet. Choose a parking location to get started.</div>}</section></main>
}
