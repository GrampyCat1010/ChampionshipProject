---
name: "Медиаподборка"
description: "Простой современный красно-белый рабочий интерфейс поиска и проверки публикаций."
colors:
  red: "#bb2835"
  red-hover: "#9e1e2a"
  red-soft: "#fbefef"
  ink: "#262329"
  muted: "#726c73"
  border: "#e8e4e5"
  ground: "#f8f7f8"
  white: "#fff"
  green: "#27664c"
  green-soft: "#edf5f0"
  amber: "#815a15"
  amber-soft: "#fbf5e9"
typography:
  headline:
    fontFamily: '"Segoe UI", Tahoma, Arial, sans-serif'
    fontSize: "32px"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-1px"
  title:
    fontFamily: '"Segoe UI", Tahoma, Arial, sans-serif'
    fontSize: "20px"
    fontWeight: 650
    lineHeight: 1.3
    letterSpacing: "-.35px"
  body:
    fontFamily: '"Segoe UI", Tahoma, Arial, sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  button:
    fontFamily: '"Segoe UI", Tahoma, Arial, sans-serif'
    fontSize: "13px"
    fontWeight: 600
    lineHeight: 1.5
  label:
    fontFamily: '"Segoe UI", Tahoma, Arial, sans-serif'
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.5
rounded:
  badge: "5px"
  field: "7px"
  control: "8px"
  feedback: "9px"
  panel: "12px"
spacing:
  compact: "8px"
  control-gap: "12px"
  mobile-gutter: "16px"
  section-gap: "20px"
  panel-inset: "24px"
  desktop-gutter: "40px"
components:
  button-primary:
    backgroundColor: "{colors.red}"
    textColor: "{colors.white}"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "11px 17px"
  button-primary-hover:
    backgroundColor: "{colors.red-hover}"
  button-secondary:
    backgroundColor: "{colors.white}"
    textColor: "#5d565e"
    typography: "{typography.button}"
    rounded: "{rounded.control}"
    padding: "11px 17px"
  source-input:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.field}"
    padding: "11px 12px"
  nav-active:
    backgroundColor: "{colors.red-soft}"
    textColor: "{colors.red}"
    rounded: "{rounded.control}"
    padding: "12px"
  badge-relevant:
    backgroundColor: "{colors.green-soft}"
    textColor: "{colors.green}"
    rounded: "{rounded.badge}"
    padding: "4px 7px"
  badge-review:
    backgroundColor: "{colors.amber-soft}"
    textColor: "{colors.amber}"
    rounded: "{rounded.badge}"
    padding: "4px 7px"
  search-panel:
    backgroundColor: "{colors.white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "24px 26px"
---

# Design System: Медиаподборка

## Overview

**Creative North Star: "Простой современный красно-белый рабочий интерфейс"**

The user's stated direction is the authority: a simple, modern red-and-white work interface. Quiet neutral surfaces support a practical Russian-language tool for searching, checking, and selecting publications. Red identifies primary actions, links, and the current location.

This document records the implemented system in `static/app.css`, `templates/index.html`, and `static/app.js`, checked against `.impeccable/review/desktop.png` and `mobile.png`. It is code-led; there is no approved generated design comp. The agency website is a user reference, but its exact color was not verified. The implemented red is not claimed to be an official agency brand color.

**Key Characteristics:**
- White working surfaces on a subtly tinted ground.
- Compact system typography with clear page and section titles.
- Restrained red actions and explicit textual status labels.
- Flat bordered panels, responsive navigation, and visible keyboard focus.

## Colors

The palette combines a deep practical red with warm gray neutrals. Normative values are in the frontmatter and map directly to CSS root variables of the same names.

The sidecar's eight-step OKLCH ramps are synthesized preview metadata for the documentation panel; they are not additional application tokens or approved color variants.

### Primary

- **Work red** (`red`): primary buttons, links, selected navigation, checkbox accents, and form errors.
- **Deep work red** (`red-hover`): primary button hover state.
- **Soft red** (`red-soft`): active navigation, result filters, progress feedback, and empty-state symbols.

### Neutral

- **Ink** (`ink`): headings and primary text; toast background.
- **Muted gray** (`muted`): metadata, explanations, helper text, and secondary navigation.
- **Fine border** (`border`): panel outlines and list separators.
- **Workspace ground** (`ground`): canvas, quiet controls, and original-text areas.
- **White** (`white`): sidebar, top bar, panels, dialog, and primary-button text.

Green and amber are semantic statuses, not additional brand accents. Pair `green` with `green-soft` for “Подходит”; pair `amber` with `amber-soft` for “На проверку” and warnings. Platform identifiers have their existing small blue, orange, and violet treatments; do not apply them to general actions.

**The Explicit Status Rule.** Keep a text label beside every relevance color; color alone must not communicate the result.

## Typography

The entire interface uses the existing Segoe UI stack, with Tahoma and Arial fallbacks. This system font is intentional for a simple internal tool; no display face or external font is required. The hierarchy is functional rather than a mathematical type scale.

- **Headline:** page titles use the frontmatter headline role; they reduce to 27px at 760px and 25px at 500px.
- **Title:** section titles use the title role; result titles reduce further on narrow screens.
- **Body:** the body role is the baseline. Supporting copy commonly uses 12–13px and longer explanations use line heights from 1.7 to 1.85.
- **Label:** field legends use the label role. Metadata and compact labels range from 10–12px; the smallest mobile supporting text reaches 9px in the empty-state steps.
- **Query:** the main textarea uses 16px, reducing to 14px on phones. Original publication text uses 13px with a line height of 1.8.

## Layout

Desktop uses a fixed white sidebar (248px), a top bar (73px), and a main area capped at 1536px. Main padding is 38px above and 40px horizontally. Search, results, sources, history, and help share this shell. Data is presented in compact rows inside one containing surface.

- At 1600px and above, top padding becomes 48px and search-panel padding becomes 28px 32px.
- At 1200px and below, the sidebar narrows to 220px and main horizontal gutters become 26px.
- At 1000px and below, the sidebar becomes a 78px icon rail and help content becomes one column.
- At 760px and below, filters stack, the search action becomes full width, and publication metadata moves below the title column. Main horizontal gutters become 20px.
- At 500px and below, navigation becomes a fixed 62px top row. The app starts below it; the breadcrumb bar is 45px tall. Main gutters become 16px and the search panel uses 15px padding and 10px corners.

Operate mode surface note: the search screen prioritizes query, constraints, one search action, then a reviewable publication list and export. This describes the current surface, not a mandatory layout for every future screen. Preserve distinct empty, loading, partial, failed, and populated states; do not fill them with invented publications.

## Elevation & Depth

Ordinary surfaces are flat. Background contrast and thin borders separate the sidebar, canvas, forms, and lists. Elevation is limited to feedback and overlays: the shared shadow (`0 16px 50px #24202820`) belongs to the toast and side dialog. The selected query segment has a subtle local shadow (`0 1px 4px #2c24320d`). The query focus halo (`0 0 0 3px #bb28350b`) communicates state rather than height.

**The Flat Surface Rule.** Keep working panels and list rows flat; use elevation for overlays and the existing selected-control treatment.

## Shapes

Panels use the panel radius. Controls and navigation use the control radius, source inputs use the field radius, and badges use the badge radius. Thin solid borders define containment. Empty-state symbols and the progress spinner are circular. The brand mark consists of three red bars in CSS; it is an application mark, not an asserted official seal.

## Components

### Buttons

Primary actions use red with white text; secondary actions use a white surface, fine border, and dark muted text. Standard buttons have a minimum height of 44px. Compact variants are 39px high with 8px 13px padding. Active presses shift downward by 1px. Disabled buttons use 0.45 opacity and a not-allowed cursor. Primary hover uses the deeper red; secondary hover uses the existing pale neutral fill.

All focusable controls inherit the visible focus outline (`3px solid #bb28356b`, offset 3px). Preserve keyboard focus when adding controls.

### Inputs / Fields

The query is a multiline textarea within a bordered control with a search icon and desktop keyboard hint. Focus changes its border to red and adds the faint halo. Source inputs use a solid border and field corners. Dates remain native date inputs. Region uses the existing understated dashed underline. Retain labels, required states, and adjacent textual errors.

### Navigation

The current destination has soft red fill, red text, heavier weight, and `aria-current`. Desktop labels accompany outline icons; compact layouts use the existing icon rail or phone top row. Current route and breadcrumb stay in sync.

### Chips / Statuses

Relevance badges are compact, noninteractive labels. Result filters are separate interactive controls with a soft red active state. Query-type segments use `aria-pressed`; their selected state is white over a neutral track.

### Cards / Containers

Search forms, empty states, publication lists, and help aside share white backgrounds, fine borders, and panel corners. Use dividers between publication rows instead of individual floating cards. Empty states explain the next action in restrained text and a simple outline icon.

### Publication Detail and Feedback

Publication detail opens in a native modal side dialog capped at 650px, with a sticky header and a dim backdrop (`#26232955`). It separates original text, evidence, explanation, and metadata. Notifications use the dark toast; inline amber notices retain actionable warnings. The spinner rotates every 0.8s. Control transitions use 0.15s and the query border uses 0.2s. The reduced-motion media query shortens animations and transitions to 0.01ms.

## Do's and Don'ts

### Do:
- **Do** preserve the user's simple modern red-and-white direction.
- **Do** use the existing CSS variables and system font stack.
- **Do** distinguish selected state, keyboard focus, disabled state, and textual feedback.
- **Do** retain responsive navigation, wrapping controls, and honest empty states.

### Don't:
- **Don't** claim the implemented color or bar mark is verified official agency branding.
- **Don't** replace publication lists with decorative nested cards.
- **Don't** encode relevance only through color or invent results for an empty screen.
- **Don't** add decorative shadows to ordinary working panels.
