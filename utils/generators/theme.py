#!/usr/bin/env python3
"""
One design system for every generated page (spec: docs/specs/cx-rehaul.md §4).

The visual language follows the Money Flow dashboard / cockpit direction the
user approved 2026-07-15: dark, dense, decision-first. Tokens live in CSS
custom properties on :root so a future light toggle is a variable swap, not a
rewrite. Every page embeds BASE_CSS (marker: "BASE_CSS v1") and wraps its body
in page_shell(), so the whole product reads as one skin.

Keep this file dependency-free (stdlib only) — every generator imports it.
"""

# Marker "BASE_CSS v1" is asserted by the theme-migration smoke tests.
BASE_CSS = """/* BASE_CSS v1 — one design system (docs/specs/cx-rehaul.md) */
:root{
  --bg:#0b1220; --surface:#101a2c; --surface2:#16233a; --border:#223049;
  --text:#dbe4f0; --head:#f1f5f9; --muted:#8b98ab; --link:#60a5fa;
  --green:#4ade80; --green-bg:#0b3b2b; --green-text:#6ee7b7; --green-strong:#15803d;
  --red:#f87171; --red-bg:#431418; --red-text:#fca5a5; --red-strong:#b91c1c;
  --amber:#fcd34d; --amber-bg:#3a2b09; --amber-strong:#b45309;
  --blue-bg:#14233f; --blue-text:#93c5fd;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:var(--bg);color:var(--text);padding:20px;max-width:1280px;margin:0 auto}
h1{font-size:1.35rem;font-weight:800;color:var(--head);margin-bottom:4px}
h2{font-size:.78rem;font-weight:700;color:var(--muted);text-transform:uppercase;
   letter-spacing:.08em;margin:26px 0 12px}
.subtitle{color:var(--muted);font-size:.8rem;margin-bottom:18px}
a{color:var(--link);text-decoration:none}
a:hover{text-decoration:underline}
.mono{font-variant-numeric:tabular-nums}
.bold{font-weight:700}
.tbl{width:100%;border-collapse:collapse;font-size:.82rem;background:var(--surface);
     border:1px solid var(--border);border-radius:10px;overflow:hidden}
.tbl th{text-align:left;padding:8px 10px;color:var(--muted);font-weight:600;
        border-bottom:1px solid var(--border);text-transform:uppercase;
        font-size:.64rem;letter-spacing:.05em;background:var(--surface2)}
.tbl td{padding:8px 10px;border-bottom:1px solid var(--border)}
.tbl tr:last-child td{border-bottom:none}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px}
.badge{display:inline-block;padding:1px 7px;border-radius:4px;font-size:.66rem;font-weight:700}
.chip{display:inline-block;padding:3px 10px;margin:2px 4px;border-radius:999px;
      background:var(--surface2);border:1px solid var(--border);color:var(--text);font-size:.8rem}
.chip:hover{border-color:var(--link);text-decoration:none}
.empty{color:var(--muted);font-size:.85rem;padding:18px;text-align:center;
       background:var(--surface);border:1px dashed var(--border);border-radius:10px}
.footer{margin-top:28px;font-size:.7rem;color:var(--muted)}
details{margin:10px 0}
details>summary{cursor:pointer;font-weight:700;color:var(--head);padding:8px 0;
                font-size:.92rem;border-bottom:1px solid var(--border);list-style:none;user-select:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"▸ ";color:var(--muted);font-size:.8rem}
details[open]>summary::before{content:"▾ "}
details>summary:hover{color:var(--link)}
.heat-pos{background:var(--green-bg);color:var(--green-text)}
.heat-neg{background:var(--red-bg);color:var(--red-text)}
.heat-zero{color:var(--muted)}
@media (max-width:640px){body{padding:12px}}
"""


def page_shell(title: str, nav: str, body: str, subtitle: str = "",
               h1: str = "", extra_head: str = "", extra_css: str = "",
               extra_script: str = "") -> str:
    """Standard HTML skeleton: theme + nav + optional h1/subtitle + body."""
    head_block = f"<h1>{h1}</h1>" if h1 else ""
    sub_block = f'<p class="subtitle">{subtitle}</p>' if subtitle else ""
    script_block = f"<script>{extra_script}</script>" if extra_script else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>{title}</title>
{extra_head}
<style>{BASE_CSS}{extra_css}</style>
</head><body>
{nav}
{head_block}
{sub_block}
{body}
{script_block}
</body></html>
"""
