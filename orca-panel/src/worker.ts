import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { OrcaWorkerApi } from 'orca/worker'

const HOME = homedir()
const PYTHON = process.env.IPHONE_USE_COREDEVICE_PYTHON
  ?? join(HOME, '.local/share/iphone-use-coredevice/venv/bin/python')
const RUNTIME = import.meta.dirname ?? dirname(fileURLToPath(import.meta.url))

/** The worker may run from a build directory, so walk up until the helper appears. */
function findHelper(): string {
  if (process.env.IPHONE_USE_HELPER) return process.env.IPHONE_USE_HELPER
  let directory = RUNTIME
  for (let step = 0; step < 6; step += 1) {
    const candidate = join(directory, 'server', 'coredevice_helper.py')
    if (existsSync(candidate)) return candidate
    directory = dirname(directory)
  }
  return join(RUNTIME, '..', '..', '..', 'server', 'coredevice_helper.py')
}

const HELPER = findHelper()
let UDID = process.env.IPHONE_USE_COREDEVICE_UDID ?? ''

type Json = Record<string, unknown>

let stream: ChildProcess | null = null
let wantStream = false
const STREAM_MODE = process.env.IPHONE_USE_PANEL_STREAM === '1'
let shotBusy = false
let restartTimer: NodeJS.Timeout | null = null
let frames: Buffer | null = null
let sequence = 0
let waiters: Array<() => void> = []
let action: ChildProcess | null = null
let actionBuffer = ''
let pending = new Map<number, { resolve: (value: Json) => void; reject: (error: Error) => void }>()
let nextId = 1
let size = { width: 402, height: 874 }

function wake() {
  const list = waiters
  waiters = []
  for (const fn of list) fn()
}

/** The helper prints multipart JPEG; only the last frame matters here. */
function watchStream(child: ChildProcess) {
  let buffer = Buffer.alloc(0)
  child.stdout?.on('data', (chunk: Buffer) => {
    buffer = Buffer.concat([buffer, chunk])
    for (;;) {
      const start = buffer.indexOf(Buffer.from([0xff, 0xd8]))
      if (start < 0) { buffer = Buffer.alloc(0); return }
      const end = buffer.indexOf(Buffer.from([0xff, 0xd9]), start + 2)
      if (end < 0) { buffer = buffer.subarray(start); return }
      frames = buffer.subarray(start, end + 2)
      buffer = buffer.subarray(end + 2)
      sequence += 1
      wake()
    }
  })
  child.on('exit', () => {
    stream = null
    wake()
    // The upstream decoder grows without a consuming VNC client and recycles itself
    // at IPHONE_USE_STREAM_MAX_MB; bring a fresh one up so the panel keeps flowing.
    if (wantStream && !restartTimer) {
      restartTimer = setTimeout(() => { restartTimer = null; if (wantStream) startStream() }, 400)
    }
  })
}

function startStream(): boolean {
  if (stream || !existsSync(HELPER) || !existsSync(PYTHON)) return false
  wantStream = true
  stream = spawn(PYTHON, [HELPER, '--stream', '--udid', UDID, '--fps', '10', '--quality', '60', '--scale', '0.6'],
    { stdio: ['ignore', 'pipe', 'ignore'], env: { ...process.env, IPHONE_USE_STREAM_MAX_MB: process.env.IPHONE_USE_STREAM_MAX_MB ?? '500' } })
  watchStream(stream)
  return true
}

function call(op: string, args: Json = {}): Promise<Json> {
  if (!action || action.exitCode !== null) {
    action = spawn(PYTHON, [HELPER], { stdio: ['pipe', 'pipe', 'ignore'], env: { ...process.env, IPHONE_USE_COREDEVICE_UDID: UDID } })
    actionBuffer = ''
    action.stdout?.on('data', (chunk: Buffer) => {
      actionBuffer += chunk.toString('utf8')
      let index = actionBuffer.indexOf('\n')
      while (index >= 0) {
        const line = actionBuffer.slice(0, index)
        actionBuffer = actionBuffer.slice(index + 1)
        try {
          const reply = JSON.parse(line) as { id: number; ok: boolean; result?: Json; error?: Json }
          const waiter = pending.get(reply.id)
          pending.delete(reply.id)
          if (!waiter) continue
          if (reply.ok) waiter.resolve(reply.result ?? {})
          else waiter.reject(Object.assign(new Error(String(reply.error?.message ?? 'helper failed')), { code: reply.error?.code }))
        } catch { /* a partial line is fine */ }
        index = actionBuffer.indexOf('\n')
      }
    })
    action.on('exit', () => { action = null })
  }
  const id = nextId++
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject })
    action?.stdin?.write(JSON.stringify({ id, op, args }) + '\n')
    setTimeout(() => {
      if (!pending.delete(id)) return
      reject(new Error(`helper did not answer ${op}`))
    }, 20_000)
  })
}

async function still(): Promise<Json | null> {
  if (shotBusy) return null
  shotBusy = true
  const result = await call('screenshot').catch(() => null)
  shotBusy = false
  return result
}

async function frame(after: number, timeout: number): Promise<Json> {
  if (!STREAM_MODE) {
    // The pymobiledevice3 decoder balloons to gigabytes without a consuming client,
    // so the panel polls stills unless a stream is explicitly requested.
    const shot = await still()
    if (shot?.jpeg_b64) {
      sequence += 1
      return { sequence, jpeg: String(shot.jpeg_b64), width: size.width, height: size.height }
    }
    return { sequence, waiting: true }
  }
  if (sequence <= after) {
    if (!stream && !startStream()) return { error: 'coredevice helper is not installed', sequence }
    const deadline = Date.now() + timeout
    while (sequence <= after && Date.now() < deadline) {
      await Promise.race([new Promise<void>((resolve) => waiters.push(resolve)), new Promise<void>((resolve) => setTimeout(resolve, 250))])
    }
  }
  if (!frames) return { sequence, waiting: true }
  const stale = sequence <= after
  if (stale) {
    // The device encoder only emits frames when the screen changes, so an idle phone
    // would look frozen; a still keeps the panel honest about what is on screen.
    const still = await call('screenshot').catch(() => null)
    if (still?.jpeg_b64) return { sequence, jpeg: String(still.jpeg_b64), width: size.width, height: size.height, idle: true }
  }
  return { sequence, jpeg: frames.toString('base64'), width: size.width, height: size.height }
}

process.on('exit', () => {
  wantStream = false
  stream?.kill()
  action?.kill()
})

export default function activate(orca: OrcaWorkerApi): void {
  void call('info').then((info) => {
    size = { width: Number(info.point_width) || size.width, height: Number(info.point_height) || size.height }
  }).catch(() => {})

  orca.panels.handle('frame', async (input: Json) => frame(Number(input.after) || 0, Number(input.timeout) || 2500))
  orca.panels.handle('size', async () => ({ ...size, udid: UDID, streaming: stream !== null }))
  orca.panels.handle('device', async (input: Json) => {
    const next = String(input.udid ?? '').trim()
    if (next && next !== UDID) {
      wantStream = false
      stream?.kill(); stream = null
      action?.kill(); action = null
      UDID = next
      frames = null; sequence = 0
      const info = await call('info').catch(() => ({}))
      if (info.point_width) size = { width: Number(info.point_width), height: Number(info.point_height) }
    }
    return { ...size, udid: UDID, streaming: stream !== null }
  })
  orca.panels.handle('tap', async (input: Json) => {
    const x = Number(input.x), y = Number(input.y)
    await call('tap', { x: x / size.width, y: y / size.height })
    return { tapped: { x, y } }
  })
  orca.panels.handle('swipe', async (input: Json) => {
    await call('swipe', { start_x: Number(input.fromX) / size.width, start_y: Number(input.fromY) / size.height,
      end_x: Number(input.toX) / size.width, end_y: Number(input.toY) / size.height, duration: 0.25 })
    return { swiped: true }
  })
  orca.panels.handle('button', async (input: Json) => {
    await call('button', { name: String(input.name ?? 'home') })
    return { pressed: input.name ?? 'home' }
  })
}
