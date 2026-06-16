---
name: infographic
description: >
  Turn dense research, reports, or data into trustworthy single-file HTML infographics.
  Use this skill whenever the user pastes in a research paper, deep-dive report, article,
  or dataset and wants to visualize it, understand it better, or create an infographic.
  Also trigger when the user says "make this visual", "help me understand this data",
  "infographic", "dashboard", "data visualization", or shares a wall of text with
  scientific claims they want to digest. This skill is about methodology — verifying
  claims before visualizing them — not just making things pretty. Even if the user
  doesn't ask for fact-checking, do it anyway. That's the whole point.
---

# Infographic Skill

You are helping the user turn dense research into a visual, trustworthy, single-file HTML infographic. The default mode is **personal learning** — the user is making this to understand the material better for themselves. Optimize for accuracy, density, and "show the math" over general-audience polish.

The most important thing this skill teaches: **verify before you visualize.** Every chart you render inherits the credibility of its source data. A beautiful chart built on a debunked number is worse than no chart at all, because it makes the wrong thing memorable.

## The Workflow

There are six phases. Do them in order. Don't skip to rendering.

### Phase 1: Source Audit

Before designing anything, read through every quantitative claim in the source material. Be the skeptic first — don't wait for the user to ask "is this true?"

**For each number, answer these questions:**
- What study produced this number? Is it cited?
- Was it measured directly, or derived/extrapolated?
- Has it been contested, critiqued, or debunked?
- What were the experimental conditions (sample size, methodology, detection limits)?
- Is this a single study or replicated consensus?

**The Sanity Check — do this for every dramatic claim:**

Take the headline number and translate it into something physical. If the result sounds absurd, it probably is.

Examples from real experience:
- "5 grams of plastic per week" → That's 260g/year. A human body is ~62kg. After 10 years you'd be 4% plastic by mass. Does that pass the smell test? No — and indeed it was debunked (overestimated by ~10⁶×).
- "Your brain is 0.48% plastic" → That's ~7g in a 1.4kg brain. A visible, weighable lump of plastic in your skull. From one study with a published critique. Flag it hard.
- "68,000 particles inhaled per day" vs another study saying "272 per day" — a 250× discrepancy means at least one methodology is off. Present as a range, not a fact.

The heuristic: **if a number implies you could physically see or weigh the thing in your body, but nobody noticed until one study — be very suspicious.** The more dramatic the claim, the higher the evidence bar should be.

**Common red flags to call out proactively:**
- Round numbers that sound like PR ("a credit card per week") — often simplified to the point of being wrong
- Numbers without units or with mixed units across comparisons
- Single studies presented as settled science (especially dramatic claims)
- Detection limit mismatches (counting small particles but weighting them as large — extremely common in environmental science)
- Studies where the headline number requires unstated assumptions

**What to do when you find issues:** Tell the user explicitly before building anything: "I found X issues with the source data. Here's what looks solid, what's contested, and what has no real data behind it." Let them decide how to proceed. If a contested claim goes in the infographic, it gets flagged visually (Phase 3).

### Phase 2: Data Normalization

The source material will almost certainly mix units, timeframes, and measurement scales. Before you can compare anything visually, normalize everything.

**The process:**
1. Identify every quantitative claim
2. Pick the most natural common unit for comparison (e.g., particles/day, mg/kg, events/year)
3. Convert each claim to that unit, showing your math explicitly
4. State every assumption required for the conversion (e.g., "assumes 2 cups/day", "assumes 400 cm² container surface")
5. Where conversion requires assumptions the source didn't test, present a range rather than a point estimate

**Watch for these traps:**
- Per-use metrics mixed with per-day or per-year metrics
- Mass-based metrics mixed with particle-count metrics (these answer fundamentally different questions)
- Percentages mixed with absolute numbers
- Acute exposure events presented alongside chronic baselines without distinguishing them

If two data points genuinely can't be compared on the same axis, say so. A dashed bar with "different unit — not comparable" is more honest than forcing everything onto one scale. This honesty is what makes the infographic trustworthy.

### Phase 3: Confidence Grading

Every data point in the infographic gets a confidence grade. This is non-negotiable — it's what separates a trustworthy visualization from a scary slideshow.

**The grades:**
- **Directly measured** (green) — A study measured this specific thing under these specific conditions. You can cite the paper and methodology.
- **Measured + assumption** (yellow) — A study measured the per-unit rate, but the daily/annual estimate requires an assumption (cups/day, liters consumed, etc.). State the assumption.
- **Extrapolated or contested** (orange) — Derived from models, or a published critique exists, or it's from a single unreplicated study with dramatic claims.
- **No data** (red/dashed) — The original source implied this was quantified, but no per-use or per-unit measurement exists. Call it out as a gap.

These grades must appear visually in the final output — as colored dots, badges, border treatments, or annotations. The user needs to see at a glance which numbers to trust and which to hold loosely.

### Phase 4: Structure the Data

Before writing any HTML, organize all verified, normalized, graded data into a structured JavaScript object. This becomes the single source of truth that Chart.js renders from.

```javascript
const data = {
  topic: "Microplastics Exposure",
  normalized_unit: "particles/day",
  items: [
    {
      label: "Nylon tea bags",
      value: [16400000, 4100000000],  // range when uncertain
      confidence: "measured_plus_assumption",
      assumption: "2 cups/day × 250mL × 8.2M/mL at 95°C",
      source: "UAB 2020",
      notes: "No data at 80°C; range estimate"
    }
  ],
  debunked: [
    {
      claim: "5g of plastic per week",
      corrected_value: "4.1 μg/week",
      explanation: "Detection limit mismatch — counted tiny particles but weighted them as large",
      source: "Pletz 2022"
    }
  ],
  gaps: [
    "No per-use shedding data for take-out containers",
    "Inhalation estimates conflict by 250×"
  ]
};
```

This structure forces you to confront gaps and confidence levels before rendering. If you can't fill in the `source` field, the data point needs a caveat or shouldn't be charted as established fact.

### Phase 4b: Context & Accessibility

Before rendering, ensure the infographic is grounded — not just accurate, but understandable and motivating.

**"Why should I care?" section** — Every infographic should open with human-level context before diving into mechanisms. Answer: why does this topic matter? What changes in daily life? How will you *feel* differently? This section uses cards and callouts, not charts — it's qualitative and motivational. Lead with tangible, felt benefits (e.g., "stairs stop being hard", "you sleep deeper", "afternoon energy crash fades") before any molecular biology.

**Key Terms / Glossary** — If the topic uses more than a handful of acronyms or scientific terms (ATP, VO2max, CRF, AMPK, mTOR, etc.), include a dedicated glossary section early in the infographic. Group terms by category (e.g., Metrics, Thresholds, Cellular). Each term gets: bold name, one-liner plain-English definition. The reader should never have to Google a term to understand the infographic. This is non-negotiable for science-heavy topics.

The pattern: **Why It Matters → Key Terms → The Science.** Don't lead with the science.

### Phase 5: Render

Build a single HTML file. Use Chart.js from CDN for all quantitative visualizations. Use HTML/CSS for editorial content (layout, cards, prose).

```html
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
```

**What Chart.js handles (always use it for these):**
- Bar charts, line charts, donut/pie charts, scatter plots
- Axis scaling, label positioning, responsive resizing
- Tooltips showing source and confidence on hover

**Never hand-code chart geometry in SVG or CSS.** You can't see the output. Bar widths, label positions, and axis scales will be wrong. The entire reason this skill exists is that hand-coded charts kept breaking. Use Chart.js — it does the math correctly.

**What HTML/CSS handles (don't use Chart.js for these):**
- Hero sections, stat banners, narrative text
- Information cards with source citations
- Tabbed sections for categories
- Callout boxes for debunked claims or caveats
- "Gaps and Caveats" prose sections
- Mitigation / action plan cards

**Key rendering principles:**
- Confidence grades visible on every chart and data card
- Every derived number shows its assumptions in smaller text below (in personal mode)
- Debunked claims get a dedicated visual treatment (strikethrough + correction side by side)
- Include a "Gaps and Caveats" section — what the research doesn't answer yet
- Source tags on every card or chart (author, year, journal minimum)
- When values span more than 3 orders of magnitude, use logarithmic axes and explain them ("each gridline = 1,000× increase")

### Phase 6: Research Gaps File (when needed)

If you hit blocked searches, paywalled papers, or identified claims you couldn't verify, generate a companion markdown file listing:
- Specific searches the user can run (with exact queries and URLs)
- Key papers to read, with DOIs
- What to look for in each source
- Priority order for follow-up

Save this alongside the infographic. Skip this phase if you were able to verify everything.

## What NOT to Do

- **Don't hand-code chart geometry in SVG or CSS.** This is the #1 lesson from building infographics with LLMs. You cannot see the output. Use Chart.js.
- **Give every chart canvas a unique id.** It must not collide with a section or anchor id, or `getElementById` returns the wrong element and the chart silently fails ("can't acquire context").
- **Don't present debunked or contested claims at face value.** If you can't verify it, flag it. If it's been debunked, say so prominently.
- **Don't mix units in the same visual comparison** without converting first. If you can't convert, show them separately and explain why.
- **Don't bury caveats in small print.** If a number is shaky, the visual treatment should make that obvious, not hide it.
- **Don't skip Phase 1.** The temptation is to jump straight to layout. Resist it. The source audit is the highest-value step.
- **Don't silently correct bad numbers.** Tell the user what you found wrong and why. The learning is in the correction, not the final number.
- **Don't add primer or condescending framing.** Labels like "FULL PRIMER", "no prior knowledge assumed", "in plain English", or "read this first" read as condescending and carry no information. *Why:* the reader asked for an explainer, not an announcement that you are about to explain. *How to apply:* keep the glossary/explainer content, drop the framing; use plain section headings ("Key terms", not "Key terms, in plain English"); never narrate that you are simplifying. Trust the reader.

## Output Checklist

Before presenting the final infographic to the user, verify:

- [ ] A "Why this matters" section exists before the science, with tangible human-level benefits
- [ ] A Key Terms glossary exists if the topic uses scientific jargon or acronyms
- [ ] Every quantitative claim has a cited source
- [ ] All comparable data points use the same unit
- [ ] Confidence grades are visually marked on every data point
- [ ] Debunked or contested claims are flagged, not just quietly corrected
- [ ] Assumptions behind derived numbers are stated
- [ ] A "Gaps and Caveats" section exists
- [ ] All charts use Chart.js (no hand-coded SVG bar charts)
- [ ] The HTML file is a single file, loadable directly in a browser with no build step
- [ ] Research gaps file saved alongside (if there were unresolvable verification gaps)
