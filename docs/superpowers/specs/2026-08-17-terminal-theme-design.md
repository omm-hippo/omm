# Terminal theme support for `omm setup` / `omm setting`

## Problem

All CLI output color is hardcoded (rich markup like `[red]`, `[green]`,
`style="white"`, `dim`, etc.), tuned against one developer's own terminal
background (`#FBF0D9`, light beige). On terminals with a different
background — especially plain dark backgrounds — some of that styling
(a low-contrast accent blue in particular) is hard to read. There is no
way for a user to pick or override a color scheme.

## Goals

- Detect (best-effort) whether the user's terminal looks light or dark,
  recommend a matching preset, but always let them see and pick from a
  fixed set of options.
- Four presets: `light` (today's tuned colors, kept as-is), `dark` (new,
  tuned for dark backgrounds), `high-contrast` (bold/reverse-video,
  background-agnostic), `no-color` (reuses the existing `--no-color`
  mechanism).
- Apply the choice consistently everywhere the CLI prints colored output
  — not just the setup wizard.
- Let the user change it later without re-running the whole wizard.

## Non-goals

- True background-color detection via OSC 11 terminal queries (rejected:
  unreliable inside multiplexers/some terminals, can hang waiting for a
  response). Detection here is env-var heuristic only, always followed by
  a visual, user-confirmed pick.
- Custom/user-defined color values. Four fixed presets only.
- Re-theming the ASCII banner in `omm setup` (`bold blue`, printed before
  a theme is chosen). Left as-is.

## Design

### 1. `omm/theme.py` (new module)

Defines six semantic style roles used everywhere in the CLI instead of
literal color names:

- `error`, `warning`, `success`, `accent`, `muted`, `value`

Each preset is a `rich.theme.Theme` mapping those six names to concrete
styles:

| role | light (= today's hardcoded values) | dark (new) | high-contrast |
|---|---|---|---|
| error | bold red | bold red | bold white on red |
| warning | yellow | yellow | bold black on yellow |
| success | bold green | bold green | bold black on green |
| accent | bold blue | bold bright_cyan | bold cyan |
| muted | dim | dim | white |
| value | white | white | bold white |

Only `accent` changes between `light` and `dark` — plain terminal blue
is classically low-contrast on a black background (the same issue as
the old default Ubuntu bash directory-color problem); everything else
carries over unchanged.

`no-color` is not a `Theme` object. Selecting it sets `console.no_color
= True` / `err_console.no_color = True` at startup — the same flag
`--no-color` already sets (`cli.py:222-224, 388-391`). No new mechanism.

`theme.py` exposes:

```python
THEME_NAMES = ("light", "dark", "high-contrast", "no-color")

def build_rich_theme(name: str) -> Theme | None:
    """None for "no-color" (caller sets console.no_color instead)."""

def detect_recommended() -> str:
    """Best-effort guess: "light" or "dark". Never raises."""

def render_preview(name: str) -> Text:
    """One line per role, rendered in that preset's actual styles, for
    the picker UI."""
```

### 2. Config

`config.py`: add `"theme": "dark"` to `DEFAULT_CONFIG` (arbitrary
fallback for configs that predate this feature — anyone who already has
a config gets `dark` until they visit the picker; fresh installs go
through the wizard and always pick explicitly).

### 3. Detection heuristic (`detect_recommended`)

1. If `COLORFGBG` env var is set (format `"fg;bg"`, set by rxvt and some
   xterm-family terminals): parse the background index. Standard
   convention — index `7` or `9-15` → light, anything else → dark.
2. Otherwise → default guess `"dark"`.

No OSC terminal queries. The guess only pre-selects the picker cursor
and is labeled "(recommended)" — the user always sees all four and
picks visually.

### 4. Wizard UX (`onboarding.py`)

New step inserted between `print_banner` and `print_hardware_summary`:

1. Compute `detect_recommended()`.
2. Print all four presets' preview blocks (via `render_preview`), each
   showing the six roles rendered in that preset's real styles, so the
   user visually judges legibility on their own screen. The recommended
   one is labeled.
3. `questionary.select` across the four names, cursor defaulting to the
   recommendation.
4. Save the pick via `config_mod.update_config(theme=...)` and rebuild
   `console`/`err_console` in place (either a new `Theme` via
   `console.push_theme(...)`, or `console.no_color = True` for
   `no-color`) so the rest of the wizard (hardware summary, engine
   checklist) already reflects it.

Non-interactive stdin (no TTY): skip the prompt, silently keep the
detected guess (already the applied default) — unlike the engine
checklist, this isn't destructive enough to justify hard-erroring.

### 5. `omm setting theme` (new subcommand)

Same shape as `configure_telemetry` / `configure_version`: an optional
`--set {light,dark,high-contrast,no-color}` flag; bare invocation shows
the current value in a table. Reuses `render_preview` +
`questionary.select` when run interactively without `--set`. Added to
`setting_menu`'s bare-TUI choice list alongside Telemetry/Upload/etc.

### 6. Migration of ~200 existing call sites

`cli.py`, `onboarding.py`, `recommend_ui.py` currently hardcode literal
color names in rich markup (`[red]`, `[yellow]`, `[green]`, `[blue]`,
one `[cyan]`) and in `style="..."` kwargs (`"blue"`, `"white"`,
`"cyan"`). These already carry a consistent meaning throughout the
codebase, so the migration is a scripted rename, not a semantic
redesign:

- `[red]` / `[bold red]` → `[error]`
- `[yellow]` → `[warning]`
- `[green]` / `[bold green]` → `[success]`
- `[blue]` / `[bold blue]` / `[cyan]` → `[accent]`
- `style="white"` → `style="value"`
- `[dim]` / `style="dim"` → `[muted]` / `style="muted"`

`bold` prefixes are dropped from the markup during the rename — the
target role's `Theme` entry already encodes bold/no-bold, so keeping a
literal `bold` in the markup would double up (rich allows it, but it's
dead weight and drifts from the role definition over time).

After the scripted rename, grep for any remaining literal color tokens
in `style=` kwargs or `[...]` markup in the three files as a completion
check, and hand-fix anything the script missed or mis-mapped (e.g. a
site using `blue` for something that isn't actually "accent").

Console construction (`cli.py:310-311`) picks up the saved theme at
startup:

```python
_theme_name = load_config().get("theme", "dark")
console = Console(
    theme=theme.build_rich_theme(_theme_name),
    no_color=(_theme_name == "no-color"),
    safe_box=platform.system() == "Windows",
    highlight=False,
)
```

(mirrored for `err_console`). The existing `--no-color` flag still
works unchanged — it forces `no_color=True` regardless of the saved
theme.

### 7. Tests

- `theme.py`: `COLORFGBG` parsing (light index, dark index, unset →
  `dark`), all four presets build without error and define all six
  roles.
- Wizard theme step: mock `questionary.select` returning each of the
  four choices, assert `config["theme"]` is saved accordingly; assert
  the step is skipped (guess kept, no prompt) when stdin isn't a TTY.
- `omm setting theme --set <name>` and bare-invocation table output.
- Regression guard: a test that greps `cli.py` and `onboarding.py` for
  literal color style tokens (`red`, `green`, `yellow`, `blue`, `cyan`,
  `white`, `dim` — outside of the six role names and outside
  `theme.py` itself) and fails if any turn up, so a future PR can't
  silently reintroduce a hardcoded color.

## Open questions

None outstanding — all resolved during brainstorming with the user.
