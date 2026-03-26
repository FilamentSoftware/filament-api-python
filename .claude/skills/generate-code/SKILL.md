# Generate Code

Run the Filament backend code generator for Python types.

## Arguments

- `<worktree-branch>` (optional) - Backend worktree branch name. If omitted, uses the main Filament repo.

## Instructions

Run the code generator on the host:

- **No argument**: `hostcmd 'scripts\generate_code.cmd' "D:\dev\FilamentSoftware\MVP\Filament"`
- **With argument**: `hostcmd 'scripts\generate_code.cmd' "D:\dev\FilamentSoftware\MVP\Filament.worktrees\<argument>"`

After generation completes, run `git status` to show what changed.
