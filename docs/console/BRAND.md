# Demo console: dark theme with Temporal brand styling

The user's direction (2026-11-02): **dark theme**, styled after https://temporal.io/brand.
This file overrides the "Visual system" section of DESIGN.md wherever they conflict.

## Brand facts (from temporal.io/brand, fetched 2026-11-02)

Primary colours:

| Name | Hex | Use |
|---|---|---|
| Space Black | `#141414` | page background (dark theme base) |
| Off White | `#F8FAFC` | primary text on dark |
| UV | `#444CE7` | primary brand / interactive accent (buttons, focus, selected) |

Accent colours used across the brand page (illustrations and gradients):

| Hex | Rough role |
|---|---|
| `#B664FF` | purple |
| `#FF6BFF` | pink |
| `#FF5555` | red |
| `#C3FF62` | lime |
| `#1FF1A5` | mint green |
| `#34D399` | green |
| `#0066FF` | blue |

Brand gradients (stops exactly as on the page):

- red → purple: `#FF5555 → #B664FF`
- purple → UV ("purple-ultraviolet"): `#B664FF → #444CE7`
- lime → mint: `#C3FF62 → #1FF1A5`
- the UV swatch itself: a lighter-to-deeper royal blue vertical gradient

Typography: temporal.io uses **Aeonik** (a commercial, licensed typeface; Light weight is
preloaded). Do **not** copy or hotlink Aeonik. Use a free, OFL-licensed geometric grotesk as the
stand-in. Vendor it as woff2 under `static/vendor/fonts/` with its licence, for example Inter
Tight or Manrope. Use a mono such as IBM Plex Mono or JetBrains Mono (OFL) for IDs, commands and
Build IDs. Headings are light or regular weight at large sizes, not heavy, as on temporal.io. The
huge task-state word should look the same way: light weight, very large, tight tracking.

Illustration style: flat, abstract, geometric shapes over gradients. The logomark is a rounded
four-pointed star. Do **not** embed the Temporal logo or logomark files. "AgentCore demo
console" set in the brand typeface style is enough.

## How to apply it to the console

- **Dark is the default and primary theme.** Tokens on `:root`:
  - background `#141414`, with raised surfaces a step lighter (around `#1C1C1F` and `#26262B`);
  - hairline borders at about 8–12% white;
  - text `#F8FAFC`, with secondary text at about 65% and tertiary at about 45% of Off White.
- If a light theme is kept at all, it is secondary (behind the theme toggle) and uses Off White
  `#F8FAFC` as its background. The page must render dark with no preference set.
- **Semantic mapping.** Keep it consistent everywhere: swimlane headers, status dots, the run
  list, the step list.
  - UV `#444CE7` (and the purple→UV gradient) = Temporal itself: the **Nexus hop / server**. The
    Nexus seam between the swimlanes is the hero element: a glowing purple→UV vertical band.
  - Support lane accent = blue `#0066FF`; Finance lane accent = pink `#FF6BFF` or purple
    `#B664FF`. Pick whichever keeps the two lanes clearly distinct from the Nexus UV.
  - Success / completed / healthy = mint `#1FF1A5` (or `#34D399` for small text, for contrast).
  - input-required / waiting for a human = lime `#C3FF62`. The hazard tape under
    "input-required" becomes a lime→mint gradient bar or lime stripes on black.
  - rejected / failed / error = red `#FF5555`.
  - Worker / AgentCore compute "alive" = mint glow; scale-to-zero "nothing running" = an empty,
    dashed, dim lane. The contrast between them must be obvious on a projector.
- Use the gradients sparingly: the Nexus band, the active state word or underline, the primary
  CTA (Approve).
- Every text/background pair must meet WCAG AA contrast on `#141414`. Check the semantic colours
  used as text, not just as dots.
- Motion: subtle glow or pulse on live elements (the open Nexus call, the active lifetime). Respect
  `prefers-reduced-motion`.
- Keep the layout, information architecture and API contract from DESIGN.md unchanged. This is
  a restyle.
