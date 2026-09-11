import { useCallback, useEffect, useRef, useState } from "react"
import {
  Alert,
  Badge,
  Box,
  Card,
  Container,
  Group,
  Paper,
  ScrollArea,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
} from "@mantine/core"
import { AlertTriangle, CircleDot, FileWarning, Radio } from "lucide-react"

/* The shapes the backend sends. Kept minimal on purpose: this page renders the
   run status and nothing else, and a field it does not draw is a field that
   cannot go stale on it. */

type ProcessState = {
  label: string
  running: boolean
  pid: number | null
  since: string | null
}

type DataFile = {
  name: string
  run: number
  started: string
  part: number
  size: number
  modified: string
}

type Attempt = {
  run: number
  started: string
  stopped: string | null
  note: string
  live: boolean
  elapsed_s: number | null
  size: number
  storage_off: boolean
  from_server_log: boolean
  files: DataFile[]
}

type Status = {
  now: string
  live: Attempt | null
  rate_bytes_per_s: number | null
  attempts: number
  last_poll: string | null
  log_error: string | null
  processes: ProcessState[]
  daq_up: boolean
  acq_root: string
  container: string
}

/** Bytes as an operator reads them. Binary units, because that is what `ls -lh`
    on the run directory says and the two must agree. */
function humanSize(bytes: number): string {
  if (!bytes) return "0 B"
  const units = ["B", "kB", "MB", "GB", "TB"]
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`
}

/** A duration a shifter can read at a glance: 4h 12m, not 15120 s. */
function humanDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—"
  const total = Math.max(0, Math.round(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`
  return `${s}s`
}

/** Local time. The logs are on the DAQ container's UTC clock and the backend
    hands them over as aware timestamps, so the browser does the last step and
    the hour shown is the hour the shifter was standing there. */
function localTime(iso: string | null): string {
  if (!iso) return "—"
  const when = new Date(iso)
  return Number.isNaN(when.getTime()) ? "—" : when.toLocaleString()
}

export default function App() {
  const [status, setStatus] = useState<Status | null>(null)
  const [attempts, setAttempts] = useState<Attempt[]>([])
  const [orphans, setOrphans] = useState<DataFile[]>([])
  const [connected, setConnected] = useState(false)
  const lastRunKey = useRef<string | null>(null)

  const loadRuns = useCallback(async () => {
    try {
      const answer = await fetch("api/runs?limit=500")
      if (!answer.ok) return
      const data = await answer.json()
      setAttempts(data.attempts ?? [])
      setOrphans(data.orphan_files ?? [])
    } catch {
      /* The socket already tells the operator the service is unreachable;
         a second banner saying so adds nothing. */
    }
  }, [])

  useEffect(() => {
    void loadRuns()
  }, [loadRuns])

  // The live view arrives on a socket; the catalog is refetched only when the
  // run boundary moves, which is the only thing that can change it.
  useEffect(() => {
    let socket: WebSocket | null = null
    let retry: ReturnType<typeof setTimeout> | null = null
    let closed = false

    const open = () => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws"
      const base = new URL("ws/status", window.location.href)
      base.protocol = `${scheme}:`
      socket = new WebSocket(base.href)
      socket.onopen = () => setConnected(true)
      socket.onmessage = (event) => {
        const next = JSON.parse(event.data) as Status
        setStatus(next)
        const key = next.live ? `${next.live.run}@${next.live.started}` : "idle"
        if (key !== lastRunKey.current) {
          lastRunKey.current = key
          void loadRuns()
        }
      }
      socket.onclose = () => {
        setConnected(false)
        if (!closed) retry = setTimeout(open, 3000)
      }
      socket.onerror = () => socket?.close()
    }
    open()
    return () => {
      closed = true
      if (retry) clearTimeout(retry)
      socket?.close()
    }
  }, [loadRuns])

  const live = status?.live ?? null
  const rate = status?.rate_bytes_per_s ?? null

  return (
    <Container size="xl" py="lg">
      <Group justify="space-between" align="flex-end" mb="md">
        <Box>
          <Title order={2}>GANIL DAQ</Title>
          <Text size="sm" c="dimmed">
            {status?.acq_root ?? "…"} · watched read-only
          </Text>
        </Box>
        <Group gap="xs">
          <Badge
            size="lg"
            variant={connected ? "light" : "filled"}
            color={connected ? "gray" : "red"}
          >
            {connected ? "connected" : "service unreachable"}
          </Badge>
          <Badge
            size="lg"
            color={status?.daq_up ? "teal" : "red"}
            variant={status?.daq_up ? "light" : "filled"}
          >
            {status?.daq_up ? "DAQ up" : "DAQ down"}
          </Badge>
        </Group>
      </Group>

      {status?.log_error && (
        <Alert color="red" icon={<AlertTriangle size={18} />} mb="md">
          {status.log_error}
        </Alert>
      )}

      {/* ── The run going now ─────────────────────────────────────────────── */}
      <Card withBorder radius="md" padding="lg" mb="md">
        {live ? (
          <Stack gap="sm">
            <Group justify="space-between" align="flex-start">
              <Group gap="sm" align="center">
                <Radio size={26} color="var(--mantine-color-teal-6)" />
                <Title order={1} style={{ lineHeight: 1 }}>
                  Run {live.run}
                </Title>
                <Badge color="teal" size="lg" variant="light">
                  running
                </Badge>
              </Group>
              <Text size="sm" c="dimmed">
                started {localTime(live.started)}
              </Text>
            </Group>

            {/* The note is the reason the run exists, so it is the biggest
                thing on the card after the number. It is also the only part of
                a GANIL run nobody can reconstruct afterwards. */}
            <Paper withBorder radius="sm" p="md" bg="var(--mantine-color-gray-0)">
              <Text style={{ whiteSpace: "pre-wrap" }} fw={500}>
                {live.note || <Text span c="dimmed" fs="italic">started with no note</Text>}
              </Text>
            </Paper>

            <Group gap="xl">
              <Metric label="elapsed" value={humanDuration(live.elapsed_s)} />
              <Metric label="written" value={humanSize(live.size)} />
              <Metric
                label="rate"
                value={rate === null ? "measuring…" : `${humanSize(rate)}/s`}
              />
              <Metric label="files" value={String(live.files.length)} />
            </Group>

            {live.files.length > 0 && (
              <Text size="xs" c="dimmed" ff="monospace">
                {live.files.map((f) => f.name).join("  ·  ")}
              </Text>
            )}
            {live.files.length === 0 && (
              <Text size="sm" c="orange.7">
                No data file yet — storage may be off for this run.
              </Text>
            )}
          </Stack>
        ) : (
          <Group gap="sm">
            <CircleDot size={22} color="var(--mantine-color-gray-5)" />
            <Text size="lg" c="dimmed">
              No run in progress
            </Text>
            <Text size="sm" c="dimmed">
              · {status?.attempts ?? 0} attempts logged
            </Text>
          </Group>
        )}
      </Card>

      {/* ── The DAQ's components ──────────────────────────────────────────── */}
      <Card withBorder radius="md" padding="md" mb="md">
        <Text size="sm" fw={600} mb="xs">
          Components in {status?.container ?? "the DAQ container"}
        </Text>
        <Group gap="xs">
          {(status?.processes ?? []).map((process) => (
            <Tooltip
              key={process.label}
              label={process.running ? `pid ${process.pid}` : "not running"}
            >
              <Badge
                color={process.running ? "teal" : "red"}
                variant={process.running ? "light" : "filled"}
                size="sm"
              >
                {process.label}
              </Badge>
            </Tooltip>
          ))}
        </Group>
      </Card>

      {/* ── Every run taken ───────────────────────────────────────────────── */}
      <Card withBorder radius="md" padding="md">
        <Text size="sm" fw={600} mb="xs">
          Runs · newest first
        </Text>
        <ScrollArea.Autosize mah={520}>
          <Table stickyHeader highlightOnHover verticalSpacing="xs" fz="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th w={70}>Run</Table.Th>
                <Table.Th w={190}>Started</Table.Th>
                <Table.Th w={100}>Length</Table.Th>
                <Table.Th w={110}>Size</Table.Th>
                <Table.Th>Note</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {attempts.map((attempt) => (
                <Table.Tr key={`${attempt.run}@${attempt.started}`}>
                  <Table.Td>
                    <Group gap={6} wrap="nowrap">
                      <Text fw={600}>{attempt.run}</Text>
                      {attempt.live && (
                        <Badge size="xs" color="teal" variant="light">
                          live
                        </Badge>
                      )}
                      {attempt.from_server_log && (
                        <Badge
                          size="xs"
                          color="gray"
                          variant="light"
                          title="The run-control GUI never logged this run; it was recovered from the server log (rcc.txt), which is why it has no note."
                        >
                          server log
                        </Badge>
                      )}
                    </Group>
                  </Table.Td>
                  <Table.Td>{localTime(attempt.started)}</Table.Td>
                  <Table.Td>{humanDuration(attempt.elapsed_s)}</Table.Td>
                  <Table.Td>
                    {attempt.files.length ? (
                      humanSize(attempt.size)
                    ) : (
                      <Text span c="dimmed" size="xs">
                        {attempt.storage_off ? "storage off" : "no file"}
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
                      {attempt.note}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea.Autosize>
      </Card>

      {/* Files on disk that belong to no logged attempt. Small, and worth
          saying: they are usually an aborted storage, but a run whose log
          lines were rotated away looks exactly the same and is not. */}
      {orphans.length > 0 && (
        <Card withBorder radius="md" padding="md" mt="md">
          <Group gap="xs" mb="xs">
            <FileWarning size={16} />
            <Text size="sm" fw={600}>
              {orphans.length} file(s) matching no logged run
            </Text>
          </Group>
          <Stack gap={2}>
            {orphans.map((file) => (
              <Text key={file.name} size="xs" ff="monospace" c="dimmed">
                {file.name} · {humanSize(file.size)}
              </Text>
            ))}
          </Stack>
        </Card>
      )}
    </Container>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <Box>
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      <Text size="xl" fw={600} ff="monospace">
        {value}
      </Text>
    </Box>
  )
}
