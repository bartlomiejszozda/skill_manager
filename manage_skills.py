#!/usr/bin/env python3
"""Assign skill collections to directories and maintain only our own symlinks."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required. Install it with: python3 -m pip install -r requirements.txt")


class ConfigurationError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently losing assignments."""


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ConfigurationError(f"Invalid or duplicate configuration key: {key!r}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def absolute(path, root):
    if not isinstance(path, str) or not path:
        raise ConfigurationError("Paths must be nonempty strings")
    expanded = Path(path).expanduser()
    return Path(os.path.abspath(expanded if expanded.is_absolute() else root / expanded))


def portable(path, root):
    """Keep recorded paths relative to this project or to the user's home."""
    if path.is_relative_to(root):
        return path.relative_to(root).as_posix()
    home = Path.home()
    if path.is_relative_to(home):
        return "~/" + path.relative_to(home).as_posix()
    return str(path)


def mapping(value, label, allowed=None):
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be a mapping")
    if allowed is not None and set(value) - allowed:
        raise ConfigurationError(f"Unknown keys in {label}: {sorted(set(value) - allowed)}")
    return value


def target_names(value, targets, label):
    if not isinstance(value, list) or any(not isinstance(name, str) for name in value):
        raise ConfigurationError(f"{label} must be a list of target names; use [] for nowhere")
    if len(value) != len(set(value)):
        raise ConfigurationError(f"Duplicate targets in {label}")
    unknown = set(value) - targets.keys()
    if unknown:
        raise ConfigurationError(f"Unknown targets in {label}: {sorted(unknown)}")
    return value


def desired_links(config_path):
    root = config_path.parent
    config = mapping(yaml.load(config_path.read_text(), Loader=UniqueLoader), "configuration", {"targets", "collections"})
    targets = {
        name: absolute(path, root)
        for name, path in mapping(config.get("targets"), "targets").items()
    }
    # Aliases for the same physical directory would hide duplicate assignments.
    resolved = [path.resolve() for path in targets.values()]
    if len(set(resolved)) != len(resolved):
        raise ConfigurationError("Two targets refer to the same directory")
    for name, path in targets.items():
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ConfigurationError(f"Target {name} must be a directory, not a symlink or file: {path}")
        # Creating discovery directories is fine; creating an entire missing
        # repository because of a typo is not.
        parent = path.parent.parent if path.parent.name == ".agents" else path.parent
        if not parent.is_dir():
            raise ConfigurationError(f"Target {name} has no existing parent workspace: {parent}")
        for ancestor in path.parents:
            if os.path.lexists(ancestor) and not ancestor.is_dir():
                raise ConfigurationError(f"Target {name} has an invalid parent directory: {ancestor}")

    skills_root = root / "skills_source"
    if not skills_root.is_dir():
        raise ConfigurationError(f"Skills directory is missing: {skills_root}")
    found = {path.name: path for path in skills_root.iterdir() if path.is_dir() and not path.name.startswith(".")}
    collections = mapping(config.get("collections"), "collections")
    if set(found) != set(collections):
        missing = sorted(set(found) - set(collections))
        removed = sorted(set(collections) - set(found))
        raise ConfigurationError(f"Collection mismatch: not configured={missing}, not found={removed}")

    desired = {}
    count = 0
    for collection, directory in sorted(found.items()):
        options = mapping(collections[collection], collection, {"targets", "skills"})
        defaults = target_names(options.get("targets", []), targets, collection)
        overrides = mapping(options.get("skills", {}), f"{collection}.skills")
        discovery = directory / ".agents/skills"
        if not discovery.is_dir():
            discovery = directory
        skills = {path.name: path for path in discovery.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()}
        count += len(skills)
        stale = set(overrides) - set(skills)
        if stale:
            raise ConfigurationError(f"Configured skills not found in {collection}: {sorted(stale)}")
        for name, source in sorted(skills.items()):
            destinations = target_names(overrides.get(name, defaults), targets, f"{collection}/{name}")
            for target in destinations:
                destination = targets[target] / name
                if destination in desired:
                    raise ConfigurationError(f"Skill name collision at {destination}: {desired[destination]} and {source}")
                if destination == source or destination.parent.resolve().is_relative_to(source.resolve()):
                    raise ConfigurationError(f"A skill cannot be installed inside itself: {destination}")
                desired[destination] = source
    return desired, count


def read_state(path, root):
    if not path.exists():
        return {}
    state = mapping(json.loads(path.read_text()), "state", {"version", "links"})
    if state.get("version") != 1 or not isinstance(state.get("links"), list):
        raise ConfigurationError("State must have version 1 and a links list")
    links = {}
    for row in state["links"]:
        row = mapping(row, "state link", {"destination", "source"})
        destination = absolute(row.get("destination"), root)
        source = absolute(row.get("source"), root)
        if destination in links:
            raise ConfigurationError(f"Duplicate state destination: {destination}")
        if not source.is_relative_to(root / "skills_source") or destination.name != source.name:
            raise ConfigurationError(f"Invalid state link: {destination} -> {source}")
        links[destination] = source
    return links


def link_matches(destination, source):
    # Compare the immediate target, not its final resolved directory: a source
    # skill may itself be a symlink, or may already have been deleted.
    return destination.is_symlink() and absolute(os.readlink(destination), destination.parent.resolve()) == source


@dataclass(frozen=True)
class Action:
    kind: str
    destination: Path
    source: Path | None


def make_plan(desired, recorded):
    actions = []
    errors = []
    for destination, source in sorted(recorded.items()):
        exists = os.path.lexists(destination)
        if exists and not link_matches(destination, source):
            errors.append(f"Managed link was changed; leaving it untouched: {destination}")
        elif destination not in desired:
            actions.append(Action("remove" if exists else "forget", destination, None))
    for destination, source in sorted(desired.items()):
        if not os.path.lexists(destination):
            actions.append(Action("create", destination, source))
        elif destination not in recorded:
            errors.append(f"Name already exists (not managed by this script): {destination}")
        elif link_matches(destination, recorded[destination]) and source != recorded[destination]:
            actions.append(Action("replace", destination, source))
    if errors:
        raise ConfigurationError("\n".join(errors))
    return actions


def write_state(path, links, root):
    data = {"version": 1, "links": [
        {"destination": portable(destination, root), "source": portable(source, root)}
        for destination, source in sorted(links.items())
    ]}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply_plan(actions, recorded, state_path, root):
    for action in actions:
        destination = action.destination
        exists = os.path.lexists(destination)
        if action.kind in {"remove", "replace"}:
            if not link_matches(destination, recorded[destination]):
                raise ConfigurationError(f"Link changed since planning: {destination}")
            destination.unlink()
            del recorded[destination]
            write_state(state_path, recorded, root)
        elif action.kind == "forget":
            if exists:
                raise ConfigurationError(f"Path appeared since planning: {destination}")
            del recorded[destination]
        elif exists:
            raise ConfigurationError(f"Path appeared since planning: {destination}")
        if action.kind in {"create", "replace"}:
            if not (action.source / "SKILL.md").is_file():
                raise ConfigurationError(f"Source disappeared since planning: {action.source}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(os.path.relpath(action.source, destination.parent.resolve()))
            recorded[destination] = action.source
        write_state(state_path, recorded, root)
    write_state(state_path, recorded, root)


@contextmanager
def apply_lock(root):
    with (root / ".skill-links.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def confirm(question):
    while True:
        try:
            answer = input(f"{question} [y/N] ").strip().lower()
        except EOFError:
            print()
            return False
        if answer in {"y", "yes"}:
            return True
        if answer in {"", "n", "no"}:
            return False
        print("Please enter yes or no. Press Enter to cancel.")


def run(args):
    config_path = args.config.expanduser().resolve()
    root = config_path.parent
    state_path = root / "skill-links-state.json"

    def inspect():
        desired, count = desired_links(config_path)
        recorded = read_state(state_path, root)
        return desired, count, recorded, make_plan(desired, recorded)

    def report(count, desired, actions):
        print(f"{count} skills detected; {len(desired)} assigned links; {len(actions)} changes.")
        for action in actions:
            source = f" -> {portable(action.source, root)}" if action.source else ""
            print(f"{action.kind.upper():7} {portable(action.destination, root)}{source}")

    def resolved_locations(desired, recorded):
        paths = set(desired.values()) | set(recorded.values())
        paths.update(destination.parent for destination in desired.keys() | recorded.keys())
        return {path: path.resolve() for path in paths}

    if args.command == "sync":
        desired, count, recorded, actions = inspect()
        locations = resolved_locations(desired, recorded)
        print(f"Checks passed: {count} skills detected, {len(desired)} assigned links.")
        if not actions:
            print("Everything is up to date. No changes are needed.")
            return
        print(f"There are {len(actions)} proposed changes. No links or state have been changed.")
        if not confirm("Would you like to see the planned changes?"):
            print("Cancelled. No links or state were changed.")
            return
        print()
        report(count, desired, actions)
        print()
        if not confirm("Apply this plan now?"):
            print("Cancelled. No links or state were changed.")
            return
        with apply_lock(root):
            current_desired, _, current_recorded, current_actions = inspect()
            if ((current_desired, current_recorded, current_actions) != (desired, recorded, actions)
                    or resolved_locations(current_desired, current_recorded) != locations):
                raise ConfigurationError("The plan changed while you were reviewing it. Run sync again to review the updated plan; nothing was applied.")
            apply_plan(actions, recorded, state_path, root)
        print("Done. Changes applied and skill-links-state.json updated.")
    elif args.command == "apply":
        with apply_lock(root):
            desired, count, recorded, actions = inspect()
            report(count, desired, actions)
            apply_plan(actions, recorded, state_path, root)
        print("Applied. Managed links recorded in skill-links-state.json.")
    else:
        desired, count, _, actions = inspect()
        if args.command == "plan":
            report(count, desired, actions)
        else:
            print(f"OK: {count} skills detected; {len(desired)} assigned links; {len(actions)} changes needed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "plan", "apply", "sync"), help="sync guides you through checking, reviewing, and approving changes")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("skill-assignments.yaml"), help="Assignment file; relative paths and state are based on its directory")
    args = parser.parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (ConfigurationError, OSError, ValueError, yaml.YAMLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
