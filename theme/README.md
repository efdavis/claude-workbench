# theme

`national-park-palette.css` — the color scheme behind my wiki UI, pulled out so
it's reusable. National Park Poster vibe: muted greens, sand, sunset orange, on
a near-black green background in dark mode.

It's the same palette `wiki-site/` ships in `src/css/custom.css`. This copy is
the standalone, project-agnostic source.

## Tokens

| Token | Hex | Role |
|-------|-----|------|
| `--np-pine` | `#2b3a1f` | darkest green |
| `--np-meadow` | `#5a7e3b` | primary green (light mode) |
| `--np-sand` | `#d4a574` | warm tan accent |
| `--np-sunset` | `#c25e28` | orange accent |
| `--np-dusk` | `#1e2a3a` | cool navy |
| `--np-cream` | `#f1ebd9` | paper / cream |
| `--np-bark` | `#1a1410` | near-black brown |

Dark-mode primary lightens meadow to `#94b074`; dark background is `#0f1815`.

## Reuse

**Docusaurus site** — copy the file in as the theme's custom CSS, or `@import` it:

```bash
cp theme/national-park-palette.css <site>/src/css/custom.css
```

The `--ifm-*` lines already wire the palette into Docusaurus, so that's all it takes.

**Any other project** — copy the file, drop the `--ifm-*` / `--docusaurus-*`
lines (those are Docusaurus-only), and reference the tokens:

```css
@import 'national-park-palette.css';

.button { background: var(--np-meadow); }
.callout { border-left: 3px solid var(--np-sunset); }
```

**Just need the hexes** — they're in the table above; the `:root` block is the source.
