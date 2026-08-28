# Seedance 2.0 Prompt Recipes

## A) Cinematic Adventure (10s)

```text
Mode: All-Reference
Assets Mapping:
- @image1: first frame and hero appearance
- @video1: camera rhythm
- @audio1: atmosphere pacing

Final Prompt:
9:16 vertical, 10s fantasy adventure cinematic, cel-shading blended with watercolor, cool blue-green palette with warm highlights.
0-3s: hero wakes in a dim ancient chamber; faint glowing runes pulse on wet stone walls; slow dolly out.
3-7s: hero walks to giant rune door and touches circular mechanism; energy ripples activate runes in sequence; heavy door opens into bright light; follow shot.
7-10s: reveal vast world from cliff edge with floating islands and distant glowing ruins; crane up + pullback for scale.
Audio: water-drop echoes and low temple resonance at start; layered activation tones at rune trigger; deep rumble on door opening; orchestral swell on world reveal; wind ambience to end.
Visual control: coherent lighting, physically plausible movement, stable identity.

Negative Constraints:
no watermark, no logo, no subtitles, no on-screen text.
```

## B) Extend Existing Video

```text
Mode: All-Reference
Assets Mapping:
- @video1: source clip to continue
- @image1: style/detail anchor

Final Prompt:
Extend @video1 by 5s. Keep continuity of character identity, outfit, camera direction, and lighting.
0-2s (new segment): continue current motion seamlessly without jump cut.
2-5s (new segment): introduce one escalation action and resolve naturally.
Camera: same lens feel and movement language as @video1.
Audio: continue ambience; add subtle transition swell only at climax.

Negative Constraints:
no watermark, no logo.
```

## C) Replace Character in Existing Video

```text
Mode: All-Reference
Assets Mapping:
- @video1: base choreography and camera
- @image1: replacement character identity

Final Prompt:
Replace the main performer in @video1 with the character from @image1.
Preserve original choreography, timing, transitions, and camera path.
Keep scene composition and lighting style; maintain clean edge blending and stable identity in all frames.

Negative Constraints:
no watermark, no logo, no text.
```

## D) IP-Safe Original Creature Battle (Text-Only, 10s)

```text
Mode: Text-only generation
Assets Mapping:
- none

Final Prompt:
vertical 9:16, 10s, 24fps, family-friendly original cartoon fantasy duel in a sunset forest clearing, vibrant cinematic lighting, expressive faces, smooth animation, high detail
Creature A: a tiny storm-rabbit with glowing cyan antlers and soft cloud-like fur, emits harmless spark particles
Creature B: a mini lava-iguana with crystal scales, ember freckles, and a floating flame orb above its tail
0-3s: dramatic stare-down, swirling leaves, warm sun rays through trees, slow push-in
3-7s: playful high-speed exchange, zigzag spark sprint versus curved ember spin, dynamic tracking camera, readable motion, clean silhouettes
7-10s: both land safely, smile, and perform a friendly team pose on two rocks under a glowing orange-pink sky, uplifting finish

Negative Constraints:
no existing franchise characters, no brand references, no logos, no watermark, no subtitles, no on-screen text, no violence, no injuries

Generation Settings:
Duration: 10s
Aspect Ratio: 9:16
Frame Rate: 24fps
```

## E) IP-Safe Sci-Fi Hero (Text-Only, 10s)

```text
Mode: Text-only generation
Assets Mapping:
- none

Final Prompt:
vertical 9:16, duration 10s, 24fps, original sci-fi hero short, cinematic city atmosphere after rain, reflective glass towers, cool blue and silver palette, family-friendly action tone, custom powered exo-suit with smooth ceramic panels, hex-light energy core, magnetic boots, forearm thrusters, expressive body language, polished animation quality
Main character nickname: Alloy Sentinel
Support character nickname: Orbit Mentor
0-3s: Alloy Sentinel stands on a high rooftop, suit systems boot sequence, hex-core pulses softly, vapor vents from shoulder ports, camera rises from boots to helmet in a dramatic reveal
3-7s: controlled flight through narrow skyline corridor, short lateral boosts, spiral climb around a tower, Orbit Mentor appears as a distant ally drone giving guidance lights, dynamic tracking camera, readable motion, crisp reflections
7-10s: Alloy Sentinel lands safely on a skybridge, energy core shifts from bright to calm glow, Orbit Mentor drone hovers at shoulder height, both face sunrise over the city, hopeful heroic ending

Negative Constraints:
no Marvel, no Iron Man, no Tony Stark, no Avengers, no arc reactor, no franchise references, no logos, no recognizable character likeness, no watermark, no subtitles, no on-screen text

Generation Settings:
Duration: 10s
Aspect Ratio: 9:16
Frame Rate: 24fps
```

## F) Toy Figure Dance Animation (All-Reference, 10s)

```text
Mode: All-Reference
Assets Mapping:
- @image1: toy figure identity, colors, outfit shape, proportions
- preserve only general toy/street-dance aesthetic, do not preserve any brand indicators

Final Prompt:
9:16 vertical, 10s, stylized toy dance animation in a clean studio set, one original vinyl-style toy figure performing an upbeat hip-hop routine, playful and family-friendly tone, smooth animation, clean silhouette readability, soft cinematic lighting, simple gradient backdrop, energetic music-video framing
0-3s: toy enters center frame in a neutral stance, subtle head nod and shoulder groove, camera slow push-in from full-body to medium shot
3-7s: main dance combo with clear toy-friendly movement, side step, heel-toe shuffle, arm wave, chest bounce, controlled half-turn, rhythmic camera drift, stable limb motion
7-10s: final combo, short forward glide, hand-point to camera, confident freeze pose, slight camera tilt and gentle punch-in for final beat

Negative Constraints:
no real person likeness, no celebrity likeness, no branded toy, no logos, no trademark symbols, no text on clothing, no watermark, no subtitles, no violent gestures, no broken joints, no extra limbs, no deformed hands, no flicker, no blur, no face distortion

Generation Settings:
Duration: 10s
Aspect Ratio: 9:16
Style: toy street-dance, clean studio, family-friendly
```

## G) Music Beat-Sync Montage

```text
Mode: All-Reference
Assets Mapping:
- @image1 @image2 @image3 @image4: visual set
- @video1: beat structure and cut rhythm
- @audio1: soundtrack timing reference

Final Prompt:
Create a 12s montage synced to @audio1 beat accents, using @video1 rhythm style.
0-4s: introduce subjects with quick punch-in cuts on beat.
4-8s: alternate medium and close shots with kinetic transitions.
8-12s: crescendo sequence with strongest visual motif, then clean landing frame.
Maintain consistent color grade and dynamic but readable composition.

Negative Constraints:
no watermark, no logo.
```

## H) Product Showcase / E-Commerce Ad (10s)

```text
Mode: All-Reference
Assets Mapping:
- @image1: product front-facing high-res photo (identity anchor)

Final Prompt:
16:9 widescreen, 10s, 3D product showcase, studio lighting with soft gradient backdrop, cinematic product commercial tone
0-3s: product rotates 360° at medium speed, clean reflections on surface, hero key light from upper-left
3-7s: product pauses, then splits into 3 sections (top/middle/bottom) in a 3D exploded view, each part floats apart with subtle particle trails, smooth transition
7-10s: parts rapidly reassemble with satisfying snap motion, final hero shot with brand-neutral backdrop glow, slight camera push-in for impact
Material rendering: accurate surface finish, glass reflections, metallic sheen where applicable

Negative Constraints:
no watermark, no logo overlay, no text overlay, no price tags, no competitor branding, no distorted proportions

Generation Settings:
Duration: 10s
Aspect Ratio: 16:9
```

## I) Short Drama with Dialogue (15s)

```text
Mode: All-Reference
Assets Mapping:
- @image1: main character appearance (first frame)
- @image2: secondary character appearance

Final Prompt:
9:16 vertical, 15s, cinematic short drama scene, indoor office setting, warm tungsten lighting with cool window backlight, shallow depth of field, emotionally charged atmosphere
0-5s: close-up of Character A at a desk, tense expression, hands gripping a document
Dialogue (Character A, cold and firm): "The deal is off. I'm done."
Sound: ambient office hum, paper rustling
6-10s: medium two-shot, Character B steps forward reaching out, camera dollies slowly right, tension in body language
Dialogue (Character B, desperate): "You can't just walk away — not after everything."
Sound: footstep on hard floor, subtle dramatic underscore rising
11-15s: Character A stands, turns away toward window, silhouette against city lights, holds document then drops it, pages flutter to floor, camera slow push-in on profile
Sound: paper hitting floor, score resolves to silence, faint city ambience

Negative Constraints:
no watermark, no subtitles, no on-screen text, no jump cuts, no shaky cam

Generation Settings:
Duration: 15s
Aspect Ratio: 9:16
```

## J) One-Take Long Shot with Multi-Image Waypoints (15s)

```text
Mode: All-Reference
Assets Mapping:
- @image1: protagonist appearance (first frame)
- @image2: alley corner building reference
- @image3: mysterious figure appearance
- @image4: destination building exterior

Final Prompt:
16:9 widescreen, 15s, espionage thriller style, continuous one-take tracking shot, no cuts, rainy evening, neon-reflected wet streets, suspenseful atmosphere
@image1 as first frame: protagonist in a dark coat walks forward, camera follows from the front at full-body framing
0-5s: steady front-tracking shot, passersby occasionally obscure the protagonist, rain droplets on lens edge, shallow depth of field on subject
6-10s: protagonist turns a corner past the building from @image2, camera pans to follow, a mysterious figure referencing @image3 is spotted lurking at the corner edge watching
11-15s: camera continues forward past the figure, protagonist approaches and enters the building from @image4, camera holds on the entrance as door closes, rain intensifies
Single continuous camera movement throughout, no cuts, no montage

Negative Constraints:
no watermark, no subtitles, no on-screen text, no jump cuts, no freeze frames

Generation Settings:
Duration: 15s
Aspect Ratio: 16:9
```

## K) Multi-Segment Long Video (>15s, 30s example)

```text
## Long Video Plan
Total duration: ~30s
Segments: 2
Aspect Ratio: 16:9

---

### Segment 1 (0-15s) — Normal Generation

Mode: Text-only generation
Assets Mapping:
- none

Final Prompt:
16:9 widescreen, 15s, fantasy cinematic, aerial establishing shot
0-5s: top-down view of swirling cloud sea over mountain peaks, camera slowly pushes down through cloud layer
6-10s: reveal a sword master standing on a cliff edge, back to camera, robes billowing, dark energy rising on the horizon
11-15s: sword master slowly turns to face camera, draws glowing blade, eyes determined, low voice: "It begins.", hold on a clean front-facing composition

Negative Constraints:
no watermark, no on-screen text

Handoff Frame: sword master facing camera in medium shot, glowing blade drawn, mountain backdrop, dark clouds ahead

---

### Segment 2 (15-30s) — Video Extension

Mode: All-Reference
Assets Mapping:
- @video1: Segment 1 output (upload after generation)

Final Prompt:
Extend @video1 by 15s. Preserve character identity, outfit, lighting, and camera style.
0-5s: continuing from the sword master's stance, dozens of shadow creatures emerge from the dark clouds and charge forward, sword master leaps to meet them
6-10s: aerial combat sequence, blade trails cut through shadow forms that dissolve into ash particles, orbiting camera with dynamic tracking
11-15s: sword master lands gracefully, sheathes blade, residual golden particles drift in the air, camera slow push-in on profile against clearing sky, score fades to wind ambience

Negative Constraints:
no watermark, no on-screen text

Generation Settings:
Duration: 15s (extension segment)
Aspect Ratio: 16:9
```
