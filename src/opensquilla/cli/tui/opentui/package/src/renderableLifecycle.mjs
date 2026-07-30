// OpenTUI's ContainerRenderable.remove() only detaches a node from Yoga and the
// visible tree. It does not release the renderable, its native buffer, or its
// descendants. Dynamic TUI regions therefore need to destroy replaced nodes,
// not merely remove them, or every keystroke/stream update leaves retained
// renderables behind.

function directChild(parent, id) {
  return (parent?.getChildren?.() ?? []).find((child) => child?.id === id) ?? null;
}

/** Destroy one direct child, with a remove-only fallback for lightweight tests. */
export function destroyRenderable(parent, nodeOrId) {
  if (!parent || nodeOrId === null || nodeOrId === undefined) return false;
  const node = typeof nodeOrId === "object"
    ? nodeOrId
    : directChild(parent, nodeOrId);
  if (!node) return false;
  if (typeof node.destroyRecursively === "function") {
    node.destroyRecursively();
  } else {
    // OpenTUI 0.4 made remove() object-based. Keep the fallback on that public
    // contract so lightweight adapters do not silently retain detached nodes.
    parent.remove?.(node);
  }
  return true;
}

/** Destroy a stable snapshot of all direct children of a container. */
export function destroyChildren(parent) {
  const children = [...(parent?.getChildren?.() ?? [])];
  for (const child of children) destroyRenderable(parent, child);
  return children.length;
}
