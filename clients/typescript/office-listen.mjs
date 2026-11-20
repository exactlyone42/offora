import { readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const [base, ...specs] = process.argv.slice(2)
const rooms = specs.map(spec => {
  const [channel, token, sessionId] = spec.split(':')
  return { channel, token, sessionId, events: 0 }
})
if (!base || rooms.length === 0 || rooms.some(r => !r.channel || !r.token || !r.sessionId)) {
  console.error('usage: office-listen.mjs <base_url> <channel:token:session_id>...')
  process.exit(2)
}

const wait = ms => new Promise(done => setTimeout(done, ms))

async function api(room, path, init = {}) {
  const response = await fetch(base + path, {
    ...init,
    headers: { Authorization: `Bearer ${room.token}`, 'Content-Type': 'application/json' },
    signal: AbortSignal.timeout(15000)
  })
  if (!response.ok) throw new Error(`${path} ${response.status}`)
  return response.json()
}

function cursorFile(room) {
  return join(process.env.TMPDIR || tmpdir(), `offora-wake-${room.channel}`)
}

async function messageCursor(room) {
  try {
    return Number(readFileSync(cursorFile(room), 'utf8').trim())
  } catch {
    const first = await api(room, `/api/v1/messages/since?channel_id=${room.channel}&after_seq=0&limit=1`)
    writeFileSync(cursorFile(room), String(first.latest_seq))
    return first.latest_seq
  }
}

function finish(payload) {
  console.log(JSON.stringify(payload))
  process.exit(0)
}

async function checkMessages(room) {
  const after = await messageCursor(room)
  const found = await api(room, `/api/v1/messages/since?channel_id=${room.channel}&after_seq=${after}&for_me=true`)
  if (found.messages.length > 0) {
    writeFileSync(cursorFile(room), String(found.next_seq))
    finish(found)
  }
}

async function listen(room) {
  for (;;) {
    try {
      await checkMessages(room)
      if (!room.events) room.events = (await api(room, `/api/v1/events/since?channel_id=${room.channel}&after_seq=0&limit=1`)).latest_seq
      const { ticket } = await api(room, '/api/v1/socket-ticket', {
        method: 'POST',
        body: JSON.stringify({ channel_id: room.channel, after_seq: room.events, stream: 'events' })
      })
      await new Promise((resolve, reject) => {
        const socket = new WebSocket(base.replace(/^http/, 'ws') + '/api/v1/socket', ['offora.v1', `offora.ticket.${ticket}`])
        socket.onerror = () => reject(new Error('socket error'))
        socket.onclose = () => resolve()
        socket.onmessage = async frame => {
          const data = JSON.parse(frame.data)
          if (data.type !== 'events') return
          room.events = data.next_seq
          const foreign = data.events.filter(event => event.actor_session_id !== room.sessionId)
          const invite = foreign.find(event => event.type === 'conversation.intent' && event.participants.includes(room.sessionId))
          if (invite) finish({ directive: 'join_conversation', event: invite })
          if (foreign.some(event => event.type === 'conversation.message')) {
            try {
              await checkMessages(room)
            } catch {
              socket.close()
            }
          }
        }
      })
    } catch (error) {
      console.error(`office-listen: ${error.message}`)
    }
    await wait(5000)
  }
}

await Promise.all(rooms.map(listen))
