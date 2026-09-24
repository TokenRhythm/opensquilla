import assert from 'node:assert/strict'
import { planMouseTrajectory } from '../dist/browser-mouse-trajectory.js'

const viewport = { width: 1200, height: 800 }
const from = { x: 100, y: 400 }, to = { x: 1000, y: 400 }
const planned = planMouseTrajectory(from, to, viewport, 0)
assert.deepEqual(planned, planMouseTrajectory(from, to, viewport, 0))
assert.notDeepEqual(planned, planMouseTrajectory(from, to, viewport, 1))
assert.deepEqual(planned.at(-1), { ...to, at: planned.at(-1).at })
const segment = (a, b) => Math.hypot(b.x - a.x, b.y - a.y)
const middle = Math.floor(planned.length / 2)
assert.ok(segment(from, planned[0]) < segment(planned[middle - 1], planned[middle]))
assert.ok(segment(planned.at(-2), planned.at(-1)) < segment(planned[middle - 1], planned[middle]))

const grid = [
  { x: 0, y: 0 }, { x: 1199.99, y: 0 }, { x: 0, y: 799.99 },
  { x: 1199.99, y: 799.99 }, { x: 600, y: 2 }, { x: 600, y: 798 },
  { x: 2, y: 400 }, { x: 1198, y: 400 }, { x: 600, y: 400 },
]
let paths = 0
for (const start of grid) for (const end of grid) for (let sequence = 0; sequence < 8; sequence++) {
  const points = planMouseTrajectory(start, end, viewport, sequence)
  const distance = segment(start, end)
  assert.ok(points.length <= 36)
  assert.deepEqual({ x: points.at(-1).x, y: points.at(-1).y }, end)
  let previousAt = -1, previousProgress = -1e-8
  for (const point of points) {
    assert.ok(point.at > previousAt)
    previousAt = point.at
    assert.ok(Number.isFinite(point.x) && Number.isFinite(point.y))
    assert.ok(point.x >= 0 && point.x < viewport.width && point.y >= 0 && point.y < viewport.height)
    if (distance <= 10) continue
    const progress = ((point.x - start.x) * (end.x - start.x)
      + (point.y - start.y) * (end.y - start.y)) / (distance * distance)
    assert.ok(progress >= previousProgress - 1e-8 && progress >= -1e-8 && progress <= 1 + 1e-8)
    previousProgress = progress
    assert.notDeepEqual({ x: point.x, y: point.y }, start)
  }
  if (distance > 10) assert.ok(previousAt >= 190 && previousAt <= 560)
  paths++
}

for (const sequence of [0, 1, 6, 7]) {
  const edge = planMouseTrajectory({ x: 100, y: 1 }, { x: 1000, y: 1 }, viewport, sequence)
  assert.ok(edge.every(point => point.y >= 1), 'near an edge the curve should bend inward')
}
const short = planMouseTrajectory({ x: 0, y: 400 }, { x: 20, y: 400 }, viewport, 0)
assert.ok(short.at(-1).at < planned.at(-1).at)
for (const start of [{ x: -1, y: 0 }, { x: NaN, y: 1 }, { x: Infinity, y: 0 }]) {
  assert.deepEqual(planMouseTrajectory(start, to, viewport, 0), [{ ...to, at: 0 }])
}
assert.deepEqual(planMouseTrajectory(from, { x: 1210, y: 0 }, viewport, 0), [{ x: 1210, y: 0, at: 0 }])
assert.deepEqual(planMouseTrajectory(from, to, { width: 0, height: 800 }, 0), [{ ...to, at: 0 }])
assert.deepEqual(planMouseTrajectory(from, { x: 106, y: 408 }, viewport, 0), [{ x: 106, y: 408, at: 0 }])
assert.deepEqual(planMouseTrajectory(from, from, viewport, 0), [{ ...from, at: 0 }])
console.log(`Mouse trajectory passed: ${paths} viewport paths, deterministic variation, exact endpoints, easing and direct fallbacks.`)
