# Theme: DrainClamp board

One committed dark mode (no light decode, no invert). Components read these tokens only.

| role | token | value | use |
|---|---|---|---|
| paper | `--bg` | `#0e0f12` | page ground |
| sheet | `--panel` | `#15161a` | cards, drawer |
| raised | `--raise` | `#1b1d22` | selected row, menus, banners |
| hover | `--hover` | `#1f2127` | row hover |
| rule | `--line` / `--line-2` | `#24262d` / `#2f323a` | hairlines, input borders |
| ink | `--text` / `--text-2` / `--text-3` | `#ecedf0` / `#a4a7b0` / `#6e727c` | primary, secondary, muted |
| mark / focus | `--accent` | `#3987e5` | primary button, focus ring, single-series bars |
| waiting on you | `--warn` | `#fab219` | status icon + label, next-step rule |
| moving | `--good` | `#0ca30c` | status icon + label, done milestones |
| ready to close | `--info` | `#3987e5` | status icon + label |
| completed | `--neutral` | `#5b5f68` | status icon + label |
| danger | `--bad` | `#e66767` | negative delta, unreadable project |
| series 1 / 2 | `--s1` / `--s2` | `#3987e5` / `#d95926` | value vs build cost (validated dark categorical slots 1-2) |
| meter track | `--track` | `#26282f` | meters, rings |

Status colours never carry meaning alone: each ships with its icon and label.

Type: Inter 400/500/600 only, `cv11` + `ss01`, tabular numbers in figures. Hero stat 28px/600, page title 20px/600, body 14px.
Space: 4px base; cards 10px radius, controls 7px; 12px grid gaps; 16px gutter on phones.
Motion: none beyond 0.2s toast and hover; respects reduced motion.
Voice: plain sentences, sentence case, verbs on buttons ("Add task", "Mark complete", "Measure now").
