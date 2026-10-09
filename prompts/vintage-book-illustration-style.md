---
name: Vintage book-illustration style (pen-and-ink plate), text only
tags: Image Generation, Higgsfield
tool: Higgsfield
for: Reproducing an old illustrated book's house style from a text prompt alone, with no reference image. Example use - filling missing plates in a vintage illustrated book.
origin: Pixel Juice Studio
---
Two-pass recipe (the key trick):
- Pass 1: compose. Run the scene + style boilerplate; generate several, pick the strongest composition.
- Pass 2: plate finish. Feed the winner back in with the plate-finish block appended to get the aged, bordered-plate look. Generating composition and finish together is weaker than splitting them.

## The Prompt

[SCENE DESCRIPTION]. 1980s British fantasy gamebook interior illustration, black and white pen-and-ink drawing, dense scratchy cross-hatching, high contrast with large areas of solid black ink, no greyscale, no colour. Grotesque exaggerated features, gaunt caricature horror style, feverish nervous linework like an etching or woodcut. Full-bleed portrait composition, claustrophobic low viewpoint. Vintage 1984 paperback interior plate, printed ink on paper texture.

Plate-finish block (second pass, aged ivory bordered plate):

Printed as a vintage book plate on warm ivory cream-coloured aged paper, the inked illustration contained within a clean rectangular frame with a thin, even margin of blank cream paper surrounding all four edges, a thin dark ruled keyline border around the image. Slightly yellowed antique paper tone, faint printed-ink texture, no pure white — the lightest areas are warm off-white paper.

## Notes

- Aspect ratio matters: match the book's plate shape (2:3 is typical). Set it explicitly.
- The three load-bearing phrases in the plate block: "warm ivory cream-coloured aged paper" (kills pure white), "even margin of blank cream paper surrounding all four edges" (makes the border), "the lightest areas are warm off-white paper" (holds the tone across the image).
- Calmer line: swap "feverish nervous linework like an etching or woodcut" for "fine controlled engraving lines, precise steel-etched crosshatching".
- If the model returns smooth greyscale shading, prepend "1-bit black and white, pure line art, inked with a dip pen."
- If the border gets cropped, add near the front: "generous paper border, the illustration does not reach the edges of the page."
- To match a different book, analyse its plates and swap the style boilerplate for that book's medium; the two-pass method carries over.
