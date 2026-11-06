# API docs portals: design note

Staff note. Not served to partners. Covers `/tuloy/docs` (partner) and `/tuloydswd/docs` (DSWD internal).

## Direction

Calm, documentation-first, light and dark. Patterns borrowed from current reference sites (Stripe, Vercel, Linear, Twilio): a slim branded top bar, a quiet sidebar with small coloured method labels, generous line height, one accent colour, code on its own surface, and a short "get started" page before the reference. This was designed from established patterns; no web research was run.

- **Elements stays the engine.** Elements 9.0.28 (vendored) renders the reference. We only override its CSS custom properties and a few classes in `docs_site/assets/docs.css`, loaded after `styles.min.css`. No fork, no patched vendor files.
- **Two pages, one visual system.** Same top bar, type and tokens. The partner page uses a teal accent. The internal page uses indigo plus an amber "Internal – authorised staff only" badge, so staff cannot mistake it for the public page.
- **No external requests.** System font stack, inline SVG wordmark, `data:` favicon. CSP stays `script-src 'self'`; there is no inline script or handler.

## Tokens

All in `:root` of `docs.css` (`--tl-*`), flipped by `prefers-color-scheme`.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--tl-bg` / `--tl-bg-soft` | `#ffffff` / `#f8fafc` | `#0b1220` / `#0f1829` | page, sidebar and panels |
| `--tl-text` / `--tl-text-muted` | `#0f172a` / `#475569` | `#e6edf7` / `#a8b5c9` | body, secondary text |
| `--tl-accent` (partner) | `#0f766e` | `#0f766e` | buttons, active states (white text 5.5:1) |
| `--tl-accent-text` (partner) | `#0f766e` | `#5eead4` | links, labels |
| `--tl-accent` (internal) | `#4338ca` | `#4f46e5` | same roles, indigo |
| `--tl-get` / `--tl-post` / `--tl-put` / `--tl-delete` | `#15803d` / `#1d4ed8` / `#b45309` / `#b91c1c` | same (badge fills); `*-text` variants are lighter in dark | method badges, white text |
| `--tl-border-input` | `#7c8aa0` | `#62748f` | form controls (3:1) |
| `--tl-tok-key/str/num/kw` | `#1d4ed8 #0f766e #b45309 #6d28d9` | `#93c5fd #5eead4 #fbbf24 #c4b5fd` | code colours |

Type: system UI stack at 15px body, 14px inside Elements (stock is 13px), tight heading tracking, `ui-monospace` for code.

## What was overridden, and why

| Area | Override | Why |
|---|---|---|
| Colour | Elements variables (`--color-canvas*`, `--color-text*`, `--color-primary*`, `--color-border*`, status colours) set on `html[data-theme=light|dark]` and also inside `.sl-inverted` | Elements themes from `data-theme`; `docs-ui.js` sets it from the OS setting. Overriding the variables retints everything without touching vendor rules |
| Try It panel | Same palette as the sample panels | Stock shows it dark in light mode next to a light sample panel |
| Method badges | Solid GET green, POST blue, PUT amber, DELETE red, in the header and sidebar | Stock uses the brand colour for POST, which would make it look like a button |
| Code | Token colours forced with `!important` | Elements colours tokens inline and they were unreadable in dark mode |
| Sidebar | 300px, rounded items, active tint; a spare element Elements places beside the sidebar is hidden | It carried its own 300px width and broke the mobile drawer |
| Layout | Height fills the space under the top bar; sidebar and content scroll separately | Stock assumes the page scrolls |
| Narrow screens | At 1100px the Try It column drops under the text. At 960px the sidebar becomes a drawer opened from "Menu" | Elements has no phone layout |
| Tables | Rounded, uppercase header, horizontal scroll | Used for the per-operation error tables |
| Focus | `:focus-visible` outline in every state | Elements removes outlines in places |
| Dark mode | Supported | Elements already has a dark variable set, so it does not fight the theme |

Hidden in the Elements UI: the Schemas list (`hideSchemas`), which exposed internal model names such as `Body_login_for_access_token_token_post`.

## Files

| File | Role |
|---|---|
| `docs_site/partner.html` | Top bar, Getting started page, `<elements-api>` |
| `docs_site/internal.html` | Top bar with badge, sign-in card, empty docs container (no API content) |
| `docs_site/assets/docs.css` | Tokens, chrome, Elements overrides |
| `docs_site/assets/docs-ui.js` | Theme, mobile menu, copy buttons, small accessibility fixes for Elements output |
| `docs_site/assets/partner.js` | Switches between Getting started (empty hash) and the reference (hash starting `#/`) |
| `docs_site/assets/internal.js` | Sign-in, spec fetch, sign-out, expiry |

## Internal page security properties (unchanged, plus sign-out)

- Token lives in a JS variable only: no storage, cookie, URL or console. The password field is cleared as soon as the form is read.
- Messages are generic: 401 "Sign-in failed.", 403 "This account has no access to the internal docs.", 429 "Too many attempts. Try again later."
- A timer at `expires_in` seconds, or the Sign out button, drops the token, removes the rendered docs, clears the URL hash and shows the sign-in card.
- The HTML shell holds no API content and no spec URL. The spec is passed to Elements as a string.
- Elements itself stores one key, `mosaic-theme` (light or dark mode), in `localStorage`. It holds no token or API data.

## Known limits

- Tag descriptions and `info.description` cannot be edited from the repo without changing `main.py` or `routers/docs_portal.py` (`spec["tags"]` is empty and the overview text is a constant there). The Getting started page carries that content instead.
- A few accessibility findings come from Elements itself and cannot be fixed in CSS: a heading without `aria-level`, a tab id reference, an unlabelled code-editor textarea and repeated region landmarks in the Try It panel.
- Colour scheme follows the operating system. There is no manual toggle, because a saved choice would need storage.
