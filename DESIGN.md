---
name: HookWise
description: A dense, calm broadcast master-control system for webhook operations.
colors:
  canvas: "#091925"
  surface: "#0e202b"
  raised: "#142630"
  sunken: "#07131b"
  line: "#29404c"
  line-strong: "#3b5865"
  cool-white: "#e8edf0"
  text-muted: "#91a5af"
  text-faint: "#6f848e"
  signal: "#61d5e2"
  signal-hover: "#8cecf4"
  signal-soft: "#173d46"
  signal-ink: "#071017"
  warning: "#ffb454"
  warning-soft: "#3d2b16"
  failure: "#e9655d"
  failure-soft: "#3a1c1d"
typography:
  display:
    fontFamily: "Chivo, Geist, Segoe UI, sans-serif"
    fontSize: "28px"
    fontWeight: 700
    lineHeight: 1.05
    letterSpacing: "-0.025em"
  headline:
    fontFamily: "Chivo, Geist, Segoe UI, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Chivo, Geist, Segoe UI, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    letterSpacing: "-0.025em"
  body:
    fontFamily: "Chivo, Geist, Segoe UI, sans-serif"
    fontSize: "13.5px"
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: "JetBrains Mono, Cascadia Code, Consolas, ui-monospace, SFMono-Regular, monospace"
    fontSize: "11px"
    fontWeight: 700
    letterSpacing: "0.09em"
  data:
    fontFamily: "JetBrains Mono, Cascadia Code, Consolas, ui-monospace, SFMono-Regular, monospace"
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: 1.45
rounded:
  sm: "3px"
  md: "6px"
  lg: "8px"
  pill: "999px"
spacing:
  1: "4px"
  2: "8px"
  3: "16px"
  4: "24px"
  5: "32px"
  6: "48px"
components:
  button-primary:
    backgroundColor: "{colors.signal}"
    textColor: "{colors.signal-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "6px 14px"
    height: "38px"
  button-primary-hover:
    backgroundColor: "{colors.signal-hover}"
    textColor: "{colors.signal-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "6px 14px"
    height: "38px"
  button-outline:
    backgroundColor: "transparent"
    textColor: "{colors.signal}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "6px 14px"
    height: "38px"
  input-field:
    backgroundColor: "{colors.sunken}"
    textColor: "{colors.cool-white}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "6px 12px"
    height: "38px"
  nav-active:
    backgroundColor: "{colors.signal-soft}"
    textColor: "{colors.signal}"
    typography: "{typography.data}"
    rounded: "{rounded.md}"
    padding: "0 12px"
    height: "42px"
  chip-signal:
    backgroundColor: "{colors.signal}"
    textColor: "{colors.signal-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "4px 8px"
  card-operator:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.cool-white}"
    rounded: "{rounded.lg}"
    padding: "24px"
  signal-band:
    backgroundColor: "{colors.sunken}"
    textColor: "{colors.cool-white}"
    rounded: "{rounded.lg}"
    padding: "12px 16px"
  matrix-row:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.cool-white}"
    typography: "{typography.data}"
    height: "52px"
  signal-node:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.cool-white}"
    rounded: "{rounded.sm}"
    padding: "10px 12px"
---

# Design System: HookWise

## Overview

**Creative North Star: "Broadcast Master Control"**

HookWise is one calm live signal chain: a desktop-first operations console shaped like a broadcast control surface, not a collection of interchangeable admin cards. Powder-coated midnight planes, cool-white engraved labels, fine channel dividers, cyan live tallies, amber warnings, and restrained coral-red failures create a stable workspace for scanning, tracing, and acting.

The system is dense without feeling busy. Controls read as channel keys, tables carry the primary operational load, and numeric telemetry uses tabular figures. The exact HookWise routed-hook mark and geometric wordmark remain unchanged in geometry; only their semantic colors adapt to the theme. Decorative glow, gradients, and ornamental motion are outside this visual world.

**Key Characteristics:**

- Stable signal hierarchy across a continuous operator workspace.
- Dense tabular information with calm tonal separation.
- Cyan for live or primary state, amber for caution, and coral red for failure.
- Channel-key controls, compact labels, and tabular numerics.
- Motion limited to state communication.

## Colors

The default shipped theme is a midnight-to-cool-navy hierarchy with cool-white labeling and three unambiguous semantic signal families. The implemented light theme inverts those same roles to pale cool surfaces and darker semantic colors without changing their meaning.

### Primary

- **Live Signal Cyan:** Reserved for primary actions, active navigation, selection rails, focus, links, healthy state, and live routing indicators.
- **Signal Hover:** Brightens a deliberate interactive state without introducing glow.
- **Signal Soft:** Tints selected or active surfaces while preserving dense text contrast.

### Secondary

- **Caution Amber:** Marks paused, degraded, or attention-required states that are not failures.

### Tertiary

- **Failure Coral:** Marks failed deliveries, critical counts, and destructive or error states.

### Neutral

- **Midnight Canvas:** The deepest application field behind the operator workspace.
- **Console Surface:** The standard panel, table row, and card plane.
- **Raised Navy:** Hover, secondary emphasis, and compact control surfaces.
- **Sunken Navy:** Inputs, table headers, inset telemetry, and the fixed navigation rail.
- **Channel Lines:** Fine dividers establish structure before elevation does.
- **Cool White, Muted, and Faint Labels:** Three text levels separate primary readings, supporting labels, and tertiary metadata.

### Named Rules

**The Signal Scarcity Rule.** Cyan marks live signal, selection, focus, and primary action; it never becomes decorative atmosphere.

**The Warning Is Not Failure Rule.** Amber communicates caution or pause; coral red is reserved for failure, critical state, or destructive action.

## Typography

**Display Font:** Chivo with Geist and Segoe UI fallbacks
**Body Font:** Chivo with Geist and Segoe UI fallbacks
**Label/Mono Font:** JetBrains Mono with Cascadia Code, Consolas, and system-monospace fallbacks

**Character:** The sans face keeps headings compact and authoritative, while the monospace face makes labels, identifiers, timestamps, and telemetry feel instrument-like. Numeric data always uses tabular figures where the implementation applies the data face.

### Hierarchy

- **Display:** Page titles and the strongest route-level heading.
- **Headline:** Section titles and major panel headings.
- **Title:** Endpoint names, modal titles, and compact card headings.
- **Body:** Operational copy, descriptions, and field values.
- **Label:** Uppercase technical labels, table headers, badges, and small controls.
- **Data:** Timestamps, rates, counters, code, channel identifiers, and other scan-critical values.

### Named Rules

**The Operator Scan Rule.** Use the sans face for meaning and the monospace face for coordinates, state labels, and values that operators compare across rows.

## Layout

The desktop shell reserves a fixed 68px rail and a 58px utility bar. At 992px and above, the rail's 214px panel rests translated left by 146px, then expands over the content on hover or keyboard focus; the body inset remains 68px, so the workspace never shifts. The HookWise mark stays stationary while the wordmark is clipped open on the exact rail timing, including the explicit remote-desktop reduced-motion exception. Below 576px, the redundant route label is omitted from the mobile bar because the full page heading follows immediately below it. Main content uses 24px vertical rhythm, responsive 18–30px inline padding, and a 1480px maximum when the container constraint is present.

The endpoint surface follows the approved Balanced Signal Matrix composition. The signal band is four columns on desktop, two below 992px, and one below 575.98px. At 1400px and above, the endpoint matrix and 330px Recent Deliveries queue sit side by side with a 14px gap. From 992px through 1399.98px, Recent Deliveries stacks beneath the matrix. Below 992px it is hidden so the primary workflow retains width.

Toolbars wrap below 1400px, and the page heading releases its 250px basis below 1200px. At 575.98px and below, every endpoint filter/control becomes full width. Expanded route telemetry changes to a vertical signal path, and its inner region is bound to the viewport at `calc(100vw - 58px)` so wide table content cannot push diagnostics off-screen.

Spacing follows the implemented 8-point-derived scale, with the half-step used for compact optical adjustments. Prefer the named spacing tokens over arbitrary Bootstrap utilities when adding new system-owned UI.

## Elevation & Depth

Depth is tonal first and shadow second. Standard containers may use the low structural shadow (`0 1px 2px rgba(0,0,0,.28)` in the dark theme), while menus, flyouts, drawers, and other true overlays use the overlay shadow (`0 12px 30px rgba(0,0,0,.34)`). Endpoint cards, KPI cards, dashboard panels, active navigation, and primary buttons are explicitly flat; borders, surface shifts, and inset selection rails carry their hierarchy.

### Shadow Vocabulary

- **Structural Low** (`0 1px 2px rgba(0,0,0,.28)`): A restrained separator for standard containers when a border alone is insufficient.
- **Overlay** (`0 12px 30px rgba(0,0,0,.34)`): Reserved for floating menus, drawers, and system-health flyouts.

### Named Rules

**The Tonal-First Rule.** Establish hierarchy with navy surface steps and fine channel lines before adding a shadow; never use decorative glow.

## Shapes

The form language is compact and engineered. Small radii belong to inset signal nodes and precision focus geometry, medium radii belong to controls and navigation keys, and large radii belong to panels and tables. Pills are reserved for statuses, filters, endpoint tags, avatars, and true circular indicators. Fine one-pixel borders and two- or three-pixel inset rails create the recurring channel-panel silhouette.

The routed-hook logo and geometric wordmark are fixed brand geometry. Recolor their existing paths through semantic text and signal roles; never redraw, simplify, stretch, crop, or substitute the mark. Favicon SVG, PNG fallback, Apple touch icon, and health-state overlays use the same dark/light signal palette and content-versioned URLs.

## Components

### Buttons

Channel keys are compact, high-contrast, and stable at rest.

- **Shape:** Medium engineered corners with a consistent control height; small controls retain the same density.
- **Primary:** Live-signal fill with dark signal ink and a bold label.
- **Hover / Focus:** Hover brightens the signal fill; keyboard focus uses a two-pixel signal outline with a two-pixel offset. Active keys move down one pixel unless reduced motion is requested.
- **Outline:** Signal-colored text on a transparent plane with a strong channel border; hover adds the soft signal tint.

### Chips

- **Style:** True pills with monospace uppercase labels. Solid semantic chips use dark signal ink in the dark theme; passive filter chips use the raised navy surface and muted text.
- **State:** Cyan is active or processed, amber is warning or paused, and coral is failed. Do not encode these states by shape alone.

### Cards / Containers

- **Corner Style:** Large system corners for panels and cards; precision nodes use the small corner.
- **Background:** Surface navy for containers, sunken navy for inset data, and raised navy for hover or secondary emphasis.
- **Shadow Strategy:** Flat for matrix, KPI, endpoint, and dashboard surfaces; structural shadow only when separation requires it.
- **Border:** One-pixel channel lines are always the first separator.
- **Internal Padding:** The standard card body uses the large spacing step; dense signal components use smaller documented component padding.

### Inputs / Fields

- **Style:** Sunken navy field, strong channel border, medium corners, and cool-white value text.
- **Focus:** Border changes to live-signal cyan with a restrained two-pixel translucent signal ring.
- **Error / Disabled:** Preserve semantic failure or muted roles and maintain readable text contrast; do not add glow.

### Navigation

The desktop rail rests as an icon strip and expands over the workspace to reveal labels. Rows are 42px channel keys; hover moves to raised navy, while active navigation uses the soft signal surface, cyan text, and a two-pixel inset signal rail. Below 992px, the existing collapsed top navigation remains the mobile access pattern.

### Data Matrix

Table headers are compact uppercase monospace labels on the sunken plane. Endpoint rows use a 52px channel height, tabular values, tonal hover, and a three-pixel inset signal rail for keyboard focus or selection. Expanding a row reveals the Source → Route → ConnectWise signal path directly beneath it without displacing primary controls.

### Signal Band and System Health

The signal band is one joined instrument with internal dividers, not four floating cards. Each metric has a short bottom tally line; only failure metrics switch that tally and value to coral. System health is an 11px status point with a small hover/focus flyout. Healthy and failed states pulse as state communication, while warning remains static.

### Named Rules

**The Channel Key Rule.** Every recurring control should read as a deliberate console key with a stable footprint, visible state, and no ornamental lift.

## Do's and Don'ts

### Do:

- Do retain the exact HookWise logo geometry and recolor only through the existing semantic paths.
- Do keep dense operator information in aligned tables, joined signal bands, and compact telemetry.
- Do use tabular numerics for values that operators compare across rows or time.
- Do let the desktop rail expand above content without changing the 68px workspace inset.
- Do reduce all nonessential motion; preserve only the rail slide and tiny system-health pulse as narrow reduced-motion exceptions.

### Don't:

- Don't introduce decorative glow, gradients, glass effects, or floating-card dashboard composition.
- Don't use cyan as ambient decoration or amber as a substitute for failure red.
- Don't shift the matrix when navigation expands or when a row reveals its route path.
- Don't hide diagnostic detail merely to make a screen appear sparse.
- Don't invent logo variants, new color primitives, arbitrary radii, or one-off spacing values.
