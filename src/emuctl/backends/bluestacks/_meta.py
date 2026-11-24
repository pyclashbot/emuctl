"""§6 — BlueStacks family-private: MimMeta synthesis (MIM metadata).

Vendor evidence (py-clash-bot, ``pyclashbot/emulators/bluestacks.py``):
the Multi-Instance Manager keeps its instance registry in
``MimMetaData.json`` — ``{"Organization": [{"ID", "Name" (the MIM display
name), "IsFolder", "ParentFolder" (-1 for instances), "IsOpen", "IsVisible",
"InstanceName" (the internal ``bst.instance.<name>.`` key)}, …]}`` — read
for the display→internal mapping (:210-218) and SYNTHESIZED cold from
``bluestacks.conf`` when the file does not exist (:288-307; cold installs
and BlueStacks Air have never run the MIM, :82-88).

Interpretation decisions (CONTRACT wins; these fill its silences):

- Synthesis is PURE and deterministic (first-appearance order; ``Name``
  falls back to the internal name exactly like the vendor's synth) and —
  unlike the consumer's implementation — never writes the vendor's
  metadata file. emuctl treats metadata as read-only vendor truth: writing
  into ``…/UserData`` with an e-mail-then-pray replacement file risks
  corrupting MIM state the vendor owns, and discovery/configure never
  needs it (the conf file is the machine-readable register our seams use;
  MimMeta only supplies the display→internal map). Cold metadata is
  therefore synthesized IN MEMORY per read — synthesized honestly, never
  invented: no hardware facts, ports, or state are fabricated.
- A PRESENT metadata file whose JSON does not parse is a typed
  ``ConfigValidationError`` — vendor-state corruption surfaces loudly,
  it never silently masquerades as "no metadata" (§1.4's failed-observation
  rule applied to file observations).
- ``IsOpen`` is intentionally NOT the run-state truth (it is MIM UI state;
  the vendor synth pins it ``False`` and never consults it): run-state
  observation stays in the adapters' process scopes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from emuctl.backends.bluestacks._conf import ConfDocument, display_name, instance_internals
from emuctl.errors import ConfigValidationError

__all__ = ["MimEntry", "MimMeta", "internal_for", "parse", "synthesize"]


@dataclass(frozen=True, slots=True)
class MimEntry:
    """One MIM instance registry row (displayName ↔ internal key map)."""

    name: str  # the MIM display Name
    internal: str  # the ``bst.instance.<internal>.`` key stem
    is_open: bool  # MIM UI state — NOT emulator run state (see docstring)


@dataclass(frozen=True, slots=True)
class MimMeta:
    """The Multi-Instance Manager instance registry (parsed or synthesized)."""

    entries: tuple[MimEntry, ...]


def parse(text: str, *, source: str = "MimMetaData.json") -> MimMeta:
    """Parse MIM metadata JSON — loud on malformation, honest on shape.

    Accepts the vendor document shape (``Organization`` array of objects
    with at least ``Name`` + ``InstanceName``); rows missing either member
    are skipped, never laundered. Any JSON failure is a typed conf error.
    """
    try:
        data: Any = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ConfigValidationError(
            layer="bluestacks",
            path=source,
            value=None,
            expected="parseable Multi-Instance Manager metadata JSON",
        ) from exc
    org = data.get("Organization") if isinstance(data, dict) else None
    if not isinstance(org, list):
        raise ConfigValidationError(
            layer="bluestacks",
            path=source,
            value="no Organization list",
            expected="an Organization array",
        )
    entries: list[MimEntry] = []
    for item in org:
        if not isinstance(item, dict):
            continue
        name = item.get("Name")
        internal = item.get("InstanceName")
        if not isinstance(name, str) or not name or not isinstance(internal, str) or not internal:
            continue
        entries.append(
            MimEntry(
                name=name,
                internal=internal,
                is_open=bool(item.get("IsOpen", False)),
            )
        )
    return MimMeta(entries=tuple(entries))


def synthesize(doc: ConfDocument) -> MimMeta:
    """Cold synthesis from ``bluestacks.conf`` (deterministic; the exact
    vendor synth shape — display name falls back to the internal name, IDs
    are registry positions, every non-fact field is the vendor's constant).
    """
    entries = tuple(
        MimEntry(name=display_name(doc, internal) or internal, internal=internal, is_open=False)
        for internal in instance_internals(doc)
    )
    return MimMeta(entries=entries)


def internal_for(meta: MimMeta, display: str) -> str | None:
    """First entry whose ``Name`` is EXACTLY ``display`` (vendor lookup shape,
    py-clash-bot bluestacks.py:210-218); exact match, no folding."""
    for entry in meta.entries:
        if entry.name == display:
            return entry.internal
    return None
