# Lint Rules

`blerk lint` checks indexed code for size, complexity, and coupling problems.
It reads from the blerk database, so the daemon must index the target directory before you run lint.

## Running lint

```
blerk lint [--dir PATH] [--exclude PATTERN] [flags...]
```

`--dir` sets the root directory to check. It defaults to the current directory.
`--exclude` removes paths that match a glob pattern. You can repeat the flag.

Rules with a default threshold run by default. Opt-in rules do nothing unless you pass their flag with a threshold value.

## Suppressing rules per directory

Place a `.blerk` file in any directory to suppress rules under that subtree.

```toml
suppress = ["dip_hint", "wide_module"]
exclude = ["*_generated.cs"]
```

`suppress` lists rule names to silence. Use `["*"]` to silence all rules under that directory.
`exclude` removes file patterns from the lint scope for that directory.

---

## Rules

### `long_function`

**Default threshold:** 40 lines
**Flag:** `--max-lines N`

Flags any function or method longer than N lines.
Long functions are hard to read and test in isolation.

### `god_file`

**Default threshold:** 20 symbols
**Flag:** `--max-symbols N`

Flags any file with more than N symbols.
A file with a high symbol count likely handles more than one concern.

### `high_fan_out`

**Default threshold:** 8 callees
**Flag:** `--max-callees N`

Flags any function or method that calls more than N distinct targets.
High fan-out makes a function hard to reason about and fragile to change.

### `too_many_params`

**Default threshold:** 4 parameters
**Flag:** `--max-params N`

Flags any function or method with more than N parameters.
A long parameter list may indicate the function does too much, or that a data object is absent.

### `deep_nesting`

**Default threshold:** 3 levels
**Flag:** `--max-nesting N`

Flags any function or method whose nesting depth exceeds N.
Deep nesting makes control flow hard to follow. Early returns or extracted helpers fix it.

### `fat_class`

**Default threshold:** 10 methods
**Flag:** `--max-methods N`

Flags any class, struct, or interface with more than N methods.
A type with a high method count likely violates the Interface Segregation Principle and should split into smaller interfaces.

### `wide_module`

**Default threshold:** 10 file dependencies
**Flag:** `--max-deps N`

Flags any file whose symbols call into more than N other distinct files.
A file with a high number of file-level dependencies handles multiple separate concerns.

### `wide_package`

**Default threshold:** 5 packages
**Flag:** `--max-pkg-deps N`

Flags any file whose symbols call into more than N distinct parent directories (packages).
Where `wide_module` counts individual files, `wide_package` counts directories.
A file that reaches across a high number of packages mixes concerns at a coarser level.

### `duplicate_symbol`

**Default threshold:** 3 (SimHash Hamming distance)
**Flag:** `--max-clone-distance N`

Flags exact and near-duplicate functions across files.
Exact duplicates share a normalized hash. Near-duplicates have a SimHash bit distance at or below N.
Set `--max-clone-distance -1` to disable near-clone detection and report exact clones only.

### `dip_hint`

**Default threshold:** 3 dependents
**Flag:** `--dip-threshold N`

Flags modules that depend on lower-level modules.
A module is "lower-level" if N or more other modules depend on it. A caller with fewer dependents than its callee may have the dependency inverted.
For C# this uses the namespace as the module boundary. For Go it uses the package directory. All other languages use the file path.

---

## Opt-in rules

These rules are off by default. Pass the flag with a threshold to turn them on.

### `unused_symbol`

**Flag:** `--unused`

Flags any function or method with no recorded callers in the index.
It finds dead code well, but produces false positives for public API entry points, event handlers, and exported symbols that external code calls.

### `static_symbol`

**Flag:** `--statics`

Flags all static functions, methods, and fields.
Use it as a project-specific convention check, not a universal rule.

### `dep_spread`

**Flag:** `--max-dep-spread N`

Flags any file where the number of distinct dependency files exceeds N percent of the file total symbol count.
The ratio is an integer percentage: 6 dependency files and 2 symbols gives a spread of 300%.
A high spread means most symbols reach into a unique external file, which marks the file as a thin routing layer.
The right threshold depends on project style. Try 100 to 200 as a starting point.

### `split_class`

**Flag:** `--max-cohesion N`

Flags any class or struct whose methods form N or more disconnected groups.
The rule connects two methods when one calls the other. Groups that share no calls have nothing in common and likely belong in separate types.
LCOM stands for Lack of Cohesion of Methods. Set `--max-cohesion 2` to flag any class with at least two disconnected method groups.

### `mixed_abstraction`

**Flag:** `--abstraction-threshold N`

Flags any function or method that calls into both high-inbound modules and low-inbound modules.
A module is "high-inbound" if five or more other files call into it (a shared utility or framework layer).
A module is "low-inbound" if one or fewer files call into it (a leaf implementation).
A file that mixes calls to both levels is likely orchestrating at two different abstraction levels at once.
Set `--abstraction-threshold 2` to flag functions that reach into at least two high-inbound and two low-inbound modules.
