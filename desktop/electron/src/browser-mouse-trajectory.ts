export type MousePoint = { x: number; y: number }

/** Plans bounded viewport motion without changing the input's destination. */
export function planMouseTrajectory(
  from: MousePoint,
  to: MousePoint,
  viewport: { width: number; height: number },
  sequence: number,
): Array<MousePoint & { at: number }> {
  const direct = () => [{ ...to, at: 0 }]
  const { width, height } = viewport
  const inside = (point: MousePoint) => Number.isFinite(point.x) && Number.isFinite(point.y)
    && point.x >= 0 && point.x < width && point.y >= 0 && point.y < height
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0
    || !inside(from) || !inside(to)) return direct()

  const dx = to.x - from.x, dy = to.y - from.y
  const distance = Math.hypot(dx, dy)
  if (!Number.isFinite(distance) || distance <= 10) return direct()
  const normal = { x: -dy / distance, y: dx / distance }
  const first = { x: from.x + dx / 3, y: from.y + dy / 3 }
  const second = { x: from.x + dx * 2 / 3, y: from.y + dy * 2 / 3 }

  const room = (direction: number) => {
    const nx = normal.x * direction, ny = normal.y * direction
    let available = 48
    for (const point of [first, second]) {
      if (nx > 0) available = Math.min(available, (width - point.x) / nx)
      else if (nx < 0) available = Math.min(available, -point.x / nx)
      if (ny > 0) available = Math.min(available, (height - point.y) / ny)
      else if (ny < 0) available = Math.min(available, -point.y / ny)
    }
    return Math.max(0, available)
  }
  const phase = Number.isFinite(sequence) ? ((Math.trunc(sequence) % 8) + 8) % 8 : 0
  const requestedBend = Math.min(48, distance * .09) * (.76 + Math.floor(phase / 2) * .08)
  const positiveRoom = room(1), negativeRoom = room(-1)
  let direction = phase % 2 === 0 ? 1 : -1
  if (Math.min(positiveRoom, negativeRoom) < requestedBend && positiveRoom !== negativeRoom) {
    direction = positiveRoom > negativeRoom ? 1 : -1
  }
  const bend = Math.min(requestedBend, direction > 0 ? positiveRoom : negativeRoom) * direction
  const control1 = { x: first.x + normal.x * bend, y: first.y + normal.y * bend }
  const control2 = { x: second.x + normal.x * bend, y: second.y + normal.y * bend }
  const duration = Math.min(560, Math.round(190 + distance * 370 / 1200))
  const count = Math.min(36, Math.max(10, Math.ceil(duration / 16)))
  const points: Array<MousePoint & { at: number }> = []
  for (let index = 1; index <= count; index++) {
    const at = Math.round(duration * index / count)
    if (index === count) { points.push({ ...to, at }); break }
    const time = index / count
    const t = time * time * (3 - 2 * time)
    const u = 1 - t
    points.push({
      x: u ** 3 * from.x + 3 * u * u * t * control1.x + 3 * u * t * t * control2.x + t ** 3 * to.x,
      y: u ** 3 * from.y + 3 * u * u * t * control1.y + 3 * u * t * t * control2.y + t ** 3 * to.y,
      at,
    })
  }
  return points
}
