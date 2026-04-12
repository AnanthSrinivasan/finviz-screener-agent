# Light Theme Audit

Check an HTML file or Python file (that generates HTML) for forbidden dark theme tokens.

## Forbidden dark tokens (NEVER use these)

```
#0f1117  #1e2130  #1a1f2e  #252d40  #141929  #0c2240  #161b27  #181d2b
#e2e8f0  #94a3b8  #64748b  (as body text — OK only as muted label)
#4ade80  #f87171  (use #16a34a / #dc2626 instead)
#7aa2f7  #38bdf8  (use #2563eb / #0369a1 instead)
```

## Standard light palette (use these)

| Role | Token |
|------|-------|
| Page background | `#f8f9fc` |
| Card/surface | `#ffffff` |
| Card border | `#e5e7eb` |
| Card shadow | `box-shadow: 0 1px 3px rgba(0,0,0,0.06)` |
| Primary text | `#111827` |
| Secondary text | `#6b7280` |
| Muted/label text | `#9ca3af` |
| Primary link | `#2563eb` |
| Link hover | `#1d4ed8` |
| Positive/green | `#16a34a` |
| Negative/red | `#dc2626` |
| Amber/warning | `#d97706` |
| Table header border | `border-bottom: 2px solid #e5e7eb` |
| Table row hover | `background: #f9fafb` |

## Badge/pill palette

| Badge | Background | Text |
|-------|-----------|------|
| Blue/info | `#eff6ff` | `#1d4ed8` |
| Green/positive | `#dcfce7` | `#15803d` |
| Amber/warning | `#fef9c3` | `#a16207` |
| Orange/alert | `#fff7ed` | `#c2410c` |
| Red/danger | `#fee2e2` | `#dc2626` |
| Purple/special | `#faf5ff` | `#7c3aed` |
| Sky/sector | `#f0f9ff` | `#0369a1` |

## How to audit a file

Grep the file for any of the forbidden dark tokens (case-insensitive). If any appear in CSS/style blocks of HTML output files or Python HTML generators, replace them with the equivalent light token.

When replacing, match the semantic role:
- Dark page background → `#f8f9fc`
- Dark card/surface → `#ffffff`
- Dark border → `#e5e7eb`
- Bright green text (dark bg) → `#16a34a`
- Bright red text (dark bg) → `#dc2626`

**Why:** Fund managers and investors expect clean professional presentation. Dark theme is a developer aesthetic. All HTML output from this system must use the light palette.
