# Modes and Interaction Notes (JiMeng Seedance 2.0)

## Mode Selection

### 1) First/Last Frame Mode
Use when user provides only first-frame image (or first+last frame) plus text prompt.

Recommended phrasing:
- `Use @image1 as first frame.`
- `End near @image2 composition.`

### 2) Text-Only Mode
Use when no reference assets are provided. The prompt must carry all visual direction.

Recommended phrasing:
- Declare ratio, duration, fps, and style upfront.
- Describe characters/creatures with fully original names and unique features.
- Use timecoded beats for all action.
- Ideal for IP-safe original concepts where no reference image exists.

### 3) All-Reference Mode
Use for multimodal control with images/videos/audio/text.

Recommended phrasing:
- `@image1 for character design consistency`
- `@video1 for camera language and transition rhythm`
- `@audio1 for pacing and atmosphere`

## @Asset Invocation Pattern

Always map assets before prompt body to reduce mistakes:

```text
Assets Mapping:
- @image1 = first frame
- @image2 = environment style
- @video1 = motion + camera reference
- @audio1 = music rhythm
```

## Control Strengths to Exploit

Seedance 2.0 is strong at:
- Motion/camera replication from reference video
- Character and style consistency via image references
- Smooth extension/continuation of existing clips
- Directed editing of existing videos (replace/add/remove elements)
- Music-beat synchronization and emotional timing
- 360° product rotation and 3D exploded-view animation
- One-take continuous tracking shots using multi-image waypoints
- Multi-segment stitching for videos beyond the 15s limit

## Dialogue & Sound Design

When prompts include character speech or sound effects, keep them as **separate layers** from visual direction:

```text
0-5s: [visual action and camera]
Dialogue (CharacterName, emotion): "line"
Sound: [ambient/FX description]
```

Best practices:
- One dialogue line per 3-5s segment maximum.
- Tag emotion explicitly: `cold`, `desperate`, `cheerful`, `whispering`.
- Describe sound FX separately: footsteps, ambient hum, score swell, silence.
- End with audio resolution: `score fades`, `ambient wind`, `silence`.

## Multi-Segment Workflow (>15s)

For videos longer than 15 seconds:

1. Declare **total duration** and **segment count** upfront.
2. Each segment is a separate prompt (max 15s each).
3. Segment 1: normal generation. End on a **clean handoff frame**.
4. Segment 2+: upload previous output as `@video1`, use `Extend @video1 by Xs`.
5. Include a **Handoff Frame** description at end of each segment.
6. Carry forward: identity, outfit, lighting, camera style, scene continuity.

## One-Take / Continuous Shot Technique

For unbroken tracking shots:
- Assign `@image1` as first frame (protagonist / opening composition).
- Use additional `@image` refs as **scene waypoints** — locations, characters, or props the camera passes.
- Write the prompt as a single continuous camera path visiting each waypoint in order.
- Explicitly state: `no cuts, single continuous shot, one-take`.
- Works best at 15s with 3-5 waypoints.

## Product Showcase Tips

- Bind product photo to `@image1` as identity anchor.
- Techniques: **360° rotation**, **3D exploded view**, **reassembly snap**, **hero lighting**.
- Specify material rendering: `glass reflections`, `metallic sheen`, `matte finish`, `translucent glow`.
- Keep background clean: studio gradient, neutral surface, or contextual lifestyle scene.
- See `camera-and-styles.md` for lighting vocabulary.

## Common Pitfalls

- Overloading too many files without explicit roles
- Missing continuity instruction for identity/wardrobe/props
- Confusing full target duration vs extension length
- Asking for realistic face references that policy may block
- Using franchise names, character names, or brand-adjacent terms (triggers moderation rejection)
- Describing recognizable signature features too closely (e.g., "yellow lightning mouse", "red-gold armor with chest reactor")
- Keeping brand logos or trademarks from reference images without explicitly stripping them
- Not adding negative constraints for inferable franchise references

## Quick Validation Checklist

- [ ] File counts and size limits respected
- [ ] Total mixed files <= 12
- [ ] Duration between 4 and 15 seconds
- [ ] Every reference has explicit `@asset` role
- [ ] Prompt has clear timeline beats
- [ ] Negative constraints included when required
- [ ] No franchise names, character names, or brand terms in prompt or asset descriptions
- [ ] Negative constraints explicitly list all inferable IP references
- [ ] Characters/creatures use fully original names and unique visual features
- [ ] Dialogue and sound FX written as separate layers from visual action
- [ ] Multi-segment videos have explicit handoff frame descriptions
- [ ] Camera terms match vocabulary from `camera-and-styles.md`
