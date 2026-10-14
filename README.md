# Skill links

Keep agent skills in one place and symlink them into selected projects or your
user skills directory. Collections have default destinations; individual skills
can override them or be assigned nowhere.

## Try the example

Requires Python 3.10+ on Linux or macOS.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
mkdir -p /tmp/skill-links-demo-a /tmp/skill-links-demo-b
python3 manage_skills.py sync
```

`sync` checks the configuration, asks whether to show the plan, then asks whether
to apply it. Enter or `no` cancels. No links or state change before approval.

The included `dummy_skills` collection demonstrates three assignments:

- `dummy-hello` inherits the collection destination: `/tmp/skill-links-demo-a/.agents`.
- `dummy-unused` is explicitly assigned nowhere.
- `dummy-goodbye` overrides the default: `/tmp/skill-links-demo-b/.claude`.

These are demonstration directories, so trying the example does not install
anything into an actual agent's discovery directory. After applying, run `sync`
again to see that no further changes are needed.

## Customize

Edit `skill-assignments.yaml`. Target paths name the directories where links
will be created, such as `~/my-project/.agents/skills` or `~/.agents/skills`.
Their parent workspace must already exist. Relative paths use this repository
as their base.

Put your collections under `skills_source/` and list each collection in the
assignment file. On every run, the script detects child directories containing
`SKILL.md` under `<collection>/.agents/skills/`, or directly under the collection
if that layout is absent. Symlinked packages work too; deeper nesting is not
scanned. Unlisted skills inherit the collection targets; `[]` means nowhere.

You can copy skills, link them from repositories, or run `npx skills add` inside
a collection with `--agent codex` and without `--global`. Optional source
checkouts go in `external_repositories/`, which is ignored by Git by default.
`AGENTS.md` rules are not installed by this script.

## Check, plan, and cleanup

You can also run the steps separately. `apply` does not ask for confirmation:

```bash
python3 manage_skills.py check
python3 manage_skills.py plan
python3 manage_skills.py apply
```

`check` and `plan` are read-only. Existing names are errors unless they are
unchanged links already managed by this script. `apply` validates first and
records completed changes in `skill-links-state.json`.

The state file remembers source and destination paths so removed, renamed, or
reassigned skills can be cleaned up later. Only recorded links that still
match are changed; manually modified links are reported and left untouched.
Keep the state file, and commit it when saving your own setup. The starter
ships with an empty state.

To remove the demo links, change the collection's `targets` to `[]` and remove
the `dummy-goodbye` override (or set it to `[]`), then run `sync` again.

Run the tests with:

```bash
python3 -m unittest -v test_manage_skills.py
```
