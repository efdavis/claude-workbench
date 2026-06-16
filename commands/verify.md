---
description: Visual verification of UI changes via Playwright
argument-hint: "[diff|assert|default] [URL or description]"
---

# Visual Verify

Visually verify UI changes using Playwright. Screenshots are saved to `~/Projects/screenshots/<branch>/` (or `$SCREENSHOT_DIR/<branch>/` if set) so they can be picked up by `/pr-prep`.

**Input**: `$ARGUMENTS` — mode + target (all optional)

**Prerequisite**: A dev server must be running (the skill does NOT start one). Default URL: `http://localhost:3000`. Override with `$APP_URL`.

---

## Step 0: Parse Mode

Parse the **first word** of `$ARGUMENTS`:

| First word | Mode | Remainder is... |
|---|---|---|
| `diff` | Before/after comparison (needs `$BASELINE_URL`) | target path or description |
| `assert` | Assertion check | assertion description (or empty = infer from diff) |
| anything else / empty | Default (screenshot) | entire `$ARGUMENTS` is the target |

Examples: `/verify diff /settings`, `/verify assert Email field visible`, `/verify /dashboard`, `/verify`.

---

## Step 1: Determine Target

**If a target path is provided** (starts with `/` or `http`): use as-is.

**If no target:** infer from the diff.

```bash
git diff --name-only HEAD
git diff --name-only --cached
git ls-files --others --exclude-standard
```

Find changed files with extensions typical for UI code (`.tsx`, `.jsx`, `.vue`, `.svelte`, `.astro`). Read the file(s) to figure out which route they render at (grep for route definitions, filenames matching route patterns).

If no UI changes found, tell the user and stop.

---

## Step 2: Navigate and Authenticate

Build the URL:
- Base: `$APP_URL` (default `http://localhost:3000`)
- Path: inferred or provided target
- Final: `$APP_URL<path>`

If the app requires auth:
- Look for known login patterns (Keycloak, Clerk, Auth0, custom form)
- If no credentials are configured in the environment, tell the user and stop — the skill should not guess credentials
- If the user has told you a login flow in the session or `CLAUDE.md`, follow it

---

## Step 3: Screenshot Saving

Resolve the screenshot directory:
- If `$SCREENSHOT_DIR` is set, use it.
- Otherwise default to `~/Projects/screenshots/`.

```bash
mkdir -p "${SCREENSHOT_DIR:-$HOME/Projects/screenshots}/$(git branch --show-current)"
```

After every `browser_take_screenshot`, copy the file:
```bash
cp <screenshot-file> "${SCREENSHOT_DIR:-$HOME/Projects/screenshots}/$(git branch --show-current)/<name>.png"
```

### Naming convention

| Mode | Filename |
|---|---|
| Default / explicit path | `localhost-<path-slug>.png` |
| Diff: baseline | `baseline-<path-slug>.png` |
| Diff: branch | `localhost-<path-slug>.png` |

At the end of `/verify`, note: "Screenshots saved to `<dir>/<branch>/` for `/pr-prep`."

---

## Mode: Default

1. `browser_resize` width 1920, height 1080
2. `browser_navigate` to the URL
3. Authenticate if needed
4. `browser_wait_for` content to load
5. `browser_take_screenshot` with a descriptive filename
6. Save screenshot (Step 3)
7. `browser_console_messages` level `error` — report errors (ignore favicon 404s, React dev warnings)
8. If page looks correct, say "Looks good." If broken, say so.

---

## Mode: Diff (before/after)

Compare a baseline (typically the deployed main branch) against the local branch. Requires `$BASELINE_URL`.

### Setup
1. `browser_resize` width 1920, height 1080
2. `browser_tabs` action `list` — check for existing authenticated tabs to reuse

### Tab 0: Baseline
1. Navigate to `$BASELINE_URL<path>`
2. Authenticate (skip if already on an authenticated page)
3. `browser_wait_for` content to load
4. `browser_take_screenshot` with `baseline-` prefix

### Tab 1: Local branch
1. `browser_tabs` action `new`
2. Navigate to `$APP_URL<path>`
3. Authenticate (skip if already on an authenticated page)
4. `browser_wait_for` content to load
5. `browser_take_screenshot` with `localhost-` prefix

### Report
Show both screenshots in the chat output. Call out visible differences. If local connection refused, show baseline only and tell the user to start the dev server.

---

## Mode: Assert

Verify expected elements are present on the page.

1. Run **Default** mode steps (navigate, screenshot, save)
2. `browser_snapshot` to get the accessibility tree
3. Determine expected elements:
   - **If assertion description provided**: search the snapshot for those terms
   - **If no description**: read the git diff, extract visible strings (labels, i18n keys), use those as expected elements
4. Search the snapshot text for each expected element (case-insensitive)
5. Report:

```
Assertions:
  [PASS] Email
  [PASS] Submit button
  [FAIL] Forgot password link — not found in accessibility tree
```

Note: conditionally rendered fields may be absent depending on data. Treat [FAIL] as "investigate" not "definitely broken."

---

## Gotchas

- **Dialog-closes-on-snapshot**: never `browser_snapshot` between opening a dialog and filling it — the snapshot can close the dialog. Use clipboard paste: stage content with `pbcopy`, open dialog, `Meta+A`, `Meta+V`, click OK.
- **Key name**: use `Enter`, NOT `Return`.
- **Do NOT close the browser**: leave it open so a follow-up `/verify` can reuse the authenticated session.
- **Clear cookies when switching environments**: stale session cookies cause OAuth errors on a different backend. `page.context().clearCookies()` first.
- **Auth failures are user-fixable**: if login hangs (stuck token refresh, MFA prompt not approved), tell the user. Do NOT silently wait forever.
- **Viewport 1920x1080**: wide enough to avoid responsive collapse in most apps.
