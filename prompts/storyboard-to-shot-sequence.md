---
name: Storyboard to full shot sequence in one run (Seedance 2.x)
tags: Seedance, Higgsfield, Image-to-Video, Character Reference, Video
tool: Higgsfield + Seedance
for: Generating a whole multi-shot sequence in ONE Seedance run from a storyboard sheet, with character, wardrobe, location and lighting held consistent across every shot.
origin: Pixel Juice Studio
---
Use when you have a storyboard sheet (a grid or column of numbered panels) and want Seedance to render the whole sequence as one continuous, edited scene rather than shot by shot. The storyboard is the master shot list; reference images lock identity and location so nothing drifts between panels.

How it works:
- Generate a storyboard sheet with any image model: numbered panels, one per shot, a short direction line each.
- Generate or collect the reference images: a character reference sheet, and optionally a location plate.
- Attach all images and paste the matching master prompt below. Seedance treats each panel as a separate camera setup, in order, and cuts them into one finished scene.
- Order the attachments so the numbers line up with the roles in the prompt (`<<<image_1>>>` character, `<<<image_2>>>` storyboard, `<<<image_3>>>` location).

## The Prompt

Master Prompt A - cinematic luxury food commercial (15 s spot, 15 panels at ~1 s each):

Use the attached <<<image_2>>> storyboard as the master shot list and composition guide. The <<<image_1>>> female chef from the reference sheet must remain the exact same person in every shot.

Cinematic luxury food commercial. Dark moody restaurant kitchen. Warm tungsten practical lighting, dramatic shadows, volumetric smoke, shallow depth of field, macro food cinematography, premium commercial aesthetic.

Camera style: Cooke anamorphic lenses, 85mm and 100mm macro food photography, cinematic rack focus, subtle handheld movement, premium advertising quality.

Atmosphere: smoke, steam, sparks, sizzling fat, floating seasoning particles, dramatic contrast, luxury restaurant branding.

Hyper-realistic. Photorealistic skin texture. Consistent character identity throughout every shot. No character drift. No extra people. No text. No logos. No watermarks.

Master Prompt B - crime-thriller procedural, continuous edited scene (9-panel storyboards):

Use the <<<image_2>>> reference storyboard to create a complete cinematic scene as a continuous edited sequence. Follow the storyboard shot order exactly and treat each panel as a separate camera setup within the same scene. Preserve continuity of character appearance, wardrobe, environment, lighting, weather, props, and geography across all shots.

Photorealistic crime-thriller cinematography. True Detective, Prisoners, Mare of Easttown visual tone. Cold overcast daytime. Muted blue-grey color palette. <<<image_3>>> Lakeside campsite surrounded by pine forest. Damp ground, patches of melting snow, heavy cloud cover. Natural lighting only.

Camera language should follow the storyboard exactly:
- Wide establishing coverage
- Medium investigative coverage
- Over-the-shoulder examination shots
- Extreme close-up evidence inserts
- Reaction close-ups
- Slow push-ins on discoveries
- Procedural observational pacing

Character behavior:
<<<image_1>>> The detective is a professional conducting a careful evidence search. Movements are deliberate, methodical, and realistic. She studies details, kneels to inspect evidence, photographs discoveries, examines surfaces, and slowly pieces together information. No exaggerated acting. No dramatic gestures.

Editing:
Use natural cinematic cuts between storyboard panels. Maintain temporal continuity between shots. Each shot should feel like the next piece of coverage from the same scene. Slow investigative pacing. Allow moments to breathe. Prioritize atmosphere and observation over action.

Audio:
Diegetic sound only. Natural wind through trees. Distant lake ambience. Footsteps on damp ground. Fabric movement. Tent material rustling. Zipper sounds. Camera shutter clicks. Evidence handling sounds. Subtle environmental foley. No music. No narration. No voice-over. No dialogue. No text.

Visual requirements:
Photorealistic. Grounded procedural realism. Natural handheld camera movement. Professional crime-drama cinematography. Consistent character continuity throughout. No subtitles. No overlays. No UI elements. No title cards. No on-screen text.

Generate a complete finished scene from the storyboard panels, preserving the exact narrative progression, shot order, discoveries, and emotional pacing shown in the reference.

## Notes

- One storyboard sheet + role-tagged references = a full sequence in a single run. Cheaper and far more consistent than generating shots individually.
- Name each token's role in the prose ("The <<<image_1>>> female chef... must remain the exact same person"). Don't just attach the image; tell the model what each one is for.
- Close with a hard negative block (no text / logos / watermarks / extra people / character drift). It noticeably reduces drift and stray on-screen text.
- For continuous scenes, spell out editing and audio intent (cuts, pacing, diegetic-only sound) so the model outputs an edited scene, not disconnected clips.
- Panel count sets the shot count; keep each panel to roughly one beat.
