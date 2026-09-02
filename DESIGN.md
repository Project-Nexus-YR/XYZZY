# XYZZY design

XYZZY is for a technical team of three to five people making one consequential
decision together. Its mechanism is the branch: a conversation splits into
parallel specialist runs, a person includes or excludes each output, and the
included ones become a Decision Brief with an evidence chain and a hash-chained log. The identity is **rigorous and warm**: paper and a deep forest
green in alternating bands, gold for the one action and the one italic word,
Cormorant Garamond for display over IBM Plex for everything read. Green is the
brand and the verdict "included", gold the action, red "excluded". The
distinctive move is the stage: three real views of one seeded room, tabbed,
receding in perspective on the green, with the branch card floating in front.

## Not this

- A team headline, a logo wall, a stat beside the logos and a "Get started
  free" button: four of the five nearest homepages open that way.
- A gold full stop, a purple gradient, a mock-up of a screen that does not
  exist, a blue or teal accent (the AI-workspace shelf sits there).

## Colour

The site carries these values; the app's `--accent` family carries the greens.
```css
:root {
  --bg: #F6F2EB;  --surface: #ECF6EE;  --raised: #FDFAF3;  --sunken: #EFEAE3;
  --line: #D6E6DA;  --line-2: #C4D9C8;  --line-3: #6E8F75;
  --text: #262018;  --text-2: #5C5548;  --text-3: #6B6457;
  --accent: #1B4529;  --accent-2: #2F623F;  --accent-ink: #FDFAF3;
  --spark: #6D5100;  --spark-fill: #D9AC42;  --on-spark: #262018;
  --ok: #1B4529;  --danger: #B03A41;
  --band: #0F2A19;  --band-2: #143521;  --band-text: #F6F2EB;  --band-text-2: #C4D9C8;  --gold: #E2B54E;  --gold-glow: rgba(226,181,78,.38);
  --shadow-1: 0 1px 2px rgba(38,32,24,.08);  --shadow-2: 0 8px 20px rgba(38,32,24,.12);  --shadow-3: 0 24px 60px rgba(27,69,41,.18);  --shadow-4: 0 40px 100px rgba(0,0,0,.45);
}
@media (prefers-color-scheme: dark) { :root {
  --bg: #040905;  --surface: #092814;  --raised: #0D150F;  --sunken: #020402;
  --line: #202B22;  --line-2: #313E34;  --line-3: #5C7561;
  --text: #F6F2EB;  --text-2: #D7D2C9;  --text-3: #A9B3AB;
  --accent: #A1C1A9;  --accent-2: #C4D9C8;  --accent-ink: #040905;
  --spark: #E2B54E;  --spark-fill: #E2B54E;  --on-spark: #040905;
  --ok: #A1C1A9;  --danger: #E0646B;  --band: #0B2114;  --band-2: #0F2A19;
  --shadow-1: 0 2px 4px rgba(0,0,0,.4);  --shadow-2: 0 8px 20px rgba(0,0,0,.45);  --shadow-3: 0 24px 60px rgba(0,0,0,.6);  --shadow-4: 0 40px 100px rgba(0,0,0,.7);
} }
```

Gold fills carry a `--spark` border; gold words use `--spark` on paper and
`--gold` on the band. Measured with `project-design/contrast.py`, floor 4.5:

| Pair | Light | Dark |
|---|---|---|
| text on bg | 14.45 | 17.98 |
| text-2 on bg | 6.61 | 13.33 |
| text-3 on surface | 5.29 | 7.34 |
| accent on bg | 9.75 | 10.24 |
| accent-ink on accent | 10.44 | 10.24 |
| spark as text on bg | 6.66 | 10.47 |
| on-spark on spark-fill | 7.63 | 10.47 |
| danger on bg | 5.33 | 5.93 |
| line-3 edge on bg (3:1) | 3.22 | 3.99 |
| band-text on band | 13.78 | 15.2 |
| gold on band | 8.02 | 8.7 |
| band on gold (tab) | 8.02 | 8.7 |

## Type

| Role | Face | Size | Weight | Tracking |
|---|---|---|---|---|
| Display (site h1) | Cormorant Garamond, one italic word in `--spark` | clamp(2.75rem, 6.6vw, 5.25rem), line-height 0.98 | 500 | -0.012em |
| Section (site h2), ledger and step titles | Cormorant Garamond | clamp(2rem, 3.8vw, 2.875rem) and 1.625rem, 1.05 to 1.1 | 600 | -0.01em |
| Wordmark | Cormorant Garamond | 1.5rem | 600 | +0.02em |
| Body (site) | IBM Plex Sans | 1.0625rem, 1.6 | 400 | -0.013em |
| Eyebrow, tab, label | IBM Plex Sans, eyebrow uppercase in `--spark` | 0.75 to 0.875rem | 600 | +0.1em eyebrow |
| App UI | IBM Plex Sans | 11 to 30 px, the `--t-*` scale with its `--k-*` tracking | 400 to 600 | as paired |
| Commands, ids, hashes, proof paths | IBM Plex Mono | 0.8125 to 0.9375rem site, 12 px app | 400 to 500 | 0 |

Source: Google Fonts Cormorant Garamond 500, 600, italic 500; IBM Plex Sans
400 to 600; IBM Plex Mono 400, 500; `display=swap`, metric-matched fallbacks.

## Shape and space

Radius is a document language: 2 px chips, 4 px inputs and buttons, 6 px
panels, 10 px for the stage frames, tabs are pills. Spacing 4 8 12 16 24 32,
plus 48 64 96 on the site. Hairlines carry hierarchy; shadow only on what
floats (`--shadow-4`, 100 px blur, on the stage frames and the card). Layout
tokens: `--container-max: 68rem`, `--gutter: 24px` (20 px under 480 px),
`--section-y: 104px` (64 px under 480 px), `--focus-ring: 2px solid var(--accent)`.

## Motif and mark

The **verdict pair** (check, included, `--ok`; cross, excluded, `--danger`)
from the disposition every AgentOutput carries: the favicon (green square,
gold check), the branch view's controls, the loop diagram's marks. The
**stage**: three real captures of one room as gold tabs over a perspective
stack on the green band, the chosen one in front, the rest receding 5 and 9 degrees.

## Motion

`ease` at 100 ms for colour, `cubic-bezier(.25,.46,.45,.94)` at 160 ms in the
app, `cubic-bezier(.22,1,.36,1)` at 500 to 1100 ms for the site's entrances,
`transform` and `opacity` only. Three noticed moments on the site, each once:
the hero rising in reading order; the stack settling from 16 degrees and 70 px
below with the branch card floating in after it, then tilting up to 6 degrees
toward a fine pointer; and the loop scene, where the pinned diagram draws a
stage per step scrolled past. A tab switch moves the frames in 720 ms.
Sections reveal once, 560 ms, 60 ms stagger. Under `prefers-reduced-motion`
everything keeps its order and becomes opacity only; the stack lies flat.

## Voice

Name the artifact, name the check, give the number. Sentence case. No
"seamless", "powerful", "any", no dash as punctuation; "AI" is allowed, it runs
models. On voice: "Every event is hashed against the one before it." Off:
"Seamlessly collaborate with AI to unlock better decisions."

## Surfaces

- `web/index.html` holds the app's three token blocks; its accent is this green.
- `site/index.html` carries both blocks and reads the light one by default.
- `site/assets/` comes from `scripts/capture_hero.py` (three views and the
  card, demo at port 8010), `build_og.py` and `build_demo_gif.py`.
