"""§6 — BlueStacks family-private: bluestacks.conf parsing + renderer
normalization + the §4 canonical→vendor translation.

Vendor evidence (py-clash-bot, ``pyclashbot/emulators/bluestacks.py``):

- The conf grammar is quoted key/value lines — ``key="value"`` — read with a
  per-key regex ``^key="([^"]*)"$`` and written by regex-substitution or
  append (:179-192). Keys are namespaced: global leaves (``bst.custom_
  resolutions``, ``bst.enable_adb_access``) and instance-scoped leaves
  (``bst.instance.<internal>.display_name``, ``.graphics_renderer``,
  ``.fb_width`` / ``.fb_height``, ``.max_fps``, ``.cpus``, ``.ram``,
  ``.dpi``, ``.enable_vsync`` / ``.enable_high_fps``,
  ``.custom_resolution_selected``; the ADB port rides
  ``bst.instance.<i>.status.adb_port`` — or a plain ``.adb_port`` twin,
  :220-239). Internal instance names are ``Pie64``/``Tiramisu64``/``…
  _N`` spellings (:194-203).
- Renderer vendor tokens are exactly ``gl`` / ``dx`` / ``vlcn`` (:241-253);
  emuctl's AUTO resolves per platform (§4: DIRECTX on Windows / VULKAN on
  macOS) through the core ``resolve_renderer`` pin, never here.
- The custom-resolution token is a ``"W x H"`` string absorbed into the
  semicolon-separated ``bst.custom_resolutions`` list (membership matched
  after ``\\u00d7→x`` + space-stripping normalization, :255-268) alongside
  ``bst.instance.<i>.custom_resolution_selected="1"`` (:441).
- MimMetaData.json shape: ``{"Organization": [{"ID": n, "Name":
  <display name>, "IsFolder": false, "ParentFolder": -1, "IsOpen": false,
  "IsVisible": true, "InstanceName": <internal>}, …]}`` (:288-307 — the
  synthesized shape IS the vendor shape).

Interpretation decisions (CONTRACT wins; these fill its silences):

- Parsing is STRICT: every non-blank line must be ``key="value"``; anything
  else is a typed ``ConfigValidationError`` (a malformed conf can never be
  silently laundered into rows). Round-trip is preserved by keeping every
  entry verbatim in file order; writing replaces the pinned key's value
  in place (or appends — the vendor's own "bad solution but it works",
  bluestacks.py:191) so unknown keys survive untouched.
- §4 translation: canonical leaves map instance-scoped; ``AUTO`` never
  reaches this module (adapters resolve it through the core pin); unknown
  spellings → ``ConfigValidationError``; ports/ranges re-checked at
  translate time exactly like the model does.
- The custom-resolution absorb fires only when BOTH ``display.width`` and
  ``display.height`` are set (a ``"W x H"`` token needs both halves; never
  invent the missing one). The ``"419 x 633"`` spelling in the contract is
  the token grammar's example — the token is built from the requested
  width/height, not pinned to that literal.
- ``bst.enable_adb_access`` / ``bst.mem_opt_mode`` are NOT translation
  targets: they are the consumer's install posture (py-clash-bot forces
  them in every boot, bluestacks.py:419-425), not canonical config leaves
  in §4's vocabulary; emuctl writes only what the overlay/section asks
  for. Documented here because forcing them silently would be
  provisioning-as-config (§Identity ban).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from emuctl.config.model import BlueStacksConfig, EmuctlConfig, RendererMode
from emuctl.errors import (
    ConfigValidationError,
    FeatureNotSupportedError,
    InvalidArgumentError,
)

__all__ = [
    "ConfDocument",
    "bound_entries",
    "instance_adb_port",
    "instance_internals",
    "internal_by_display",
    "overlay_entries",
    "parse",
    "renderer_mode",
    "renderer_token",
    "serialize",
    "translate",
]

#: Conf grammar: quoted key/value lines ONLY (see module docstring).
_LINE_RE: Final[re.Pattern[str]] = re.compile(r'^(?P<key>[A-Za-z0-9_.]+)="(?P<value>.*)"$')

_INSTANCE_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^bst\.instance\.([^.]+)\.")

#: Vendor renderer tokens (§4: exact — "gl"/"dx"/"vlcn"), token → canonical.
RENDERER_TOKENS: Final[Mapping[str, RendererMode]] = {
    "gl": RendererMode.OPENGL,
    "dx": RendererMode.DIRECTX,
    "vlcn": RendererMode.VULKAN,
}

_TOKEN_BY_MODE: Final[Mapping[RendererMode, str]] = {
    RendererMode.OPENGL: "gl",
    RendererMode.DIRECTX: "dx",
    RendererMode.VULKAN: "vlcn",
}

#: Canonical leaf vocabulary the BlueStacks family can translate (§4 extent).
_BLUESTACKS_LEAVES: Final[frozenset[str]] = frozenset(
    {
        "display.renderer",
        "display.width",
        "display.height",
        "display.dpi",
        "display.fps",
        "resources.cpus",
        "resources.memory_mb",
        "bluestacks.settings.vsync",
        "bluestacks.settings.high_fps",
    }
)

#: Known-canonical heads whose leaves this table partially covers — a leaf
#: under them that is NOT in the table is the typed gate refusal, as opposed
#: to an unknown key (caller misuse).
_PARTIAL_HEADS: Final[frozenset[str]] = frozenset({"display", "resources", "bluestacks"})


def _fail(path: str, value: object, expected: str) -> ConfigValidationError:
    """Validation error pinned to the BlueStacks conf layer."""
    return ConfigValidationError(
        layer="bluestacks",
        path=path,
        value=value if value is None or isinstance(value, (str, int, float, bool)) else str(value),
        expected=expected,
    )


@dataclass(frozen=True, slots=True)
class ConfDocument:
    """An immutable ordered (key, value) conf document (round-trip exact).

    ``parse`` yields one; every mutation returns a NEW document, so a
    partially-translated write never half-mutates a shared state.
    """

    entries: tuple[tuple[str, str], ...] = field(default=())

    def get(self, key: str) -> str | None:
        """Last matching value (later lines override; None when absent)."""
        found: str | None = None
        for name, value in self.entries:
            if name == key:
                found = value
        return found

    def with_entry(self, key: str, value: str) -> ConfDocument:
        """Replace ``key``'s LAST line in place (round-trip preserving), or
        append when absent (the vendor's own append shape, bluestacks.py:191).
        """
        pairs = list(self.entries)
        for index in range(len(pairs) - 1, -1, -1):
            if pairs[index][0] == key:
                pairs[index] = (key, value)
                break
        else:
            pairs.append((key, value))
        return ConfDocument(entries=tuple(pairs))


def parse(text: str, *, source: str = "bluestacks.conf") -> ConfDocument:
    """Parse conf text — STRICTLY ``key="value"`` lines (see module docstring).

    Blank lines are skipped; anything else that does not match the grammar
    (including ``#``-comments, which the vendor format does not sanction)
    is a typed malformed-line error carrying the offending line.
    """
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r").strip()
        if not line:
            continue
        match = _LINE_RE.match(line)
        if match is None:
            raise ConfigValidationError(
                layer="bluestacks",
                path=source,
                value=raw,
                expected='a bluestacks.conf line of the form key="value"',
            )
        pairs.append((match.group("key"), match.group("value")))
    return ConfDocument(entries=tuple(pairs))


def serialize(doc: ConfDocument) -> str:
    """Round-trip serialization (verbatim order, one ``key="value"`` per line)."""
    if not doc.entries:
        return ""
    return "".join(f'{key}="{value}"\n' for key, value in doc.entries)


# ── instance-scoped key helpers (pure) ──


def instance_internals(doc: ConfDocument) -> tuple[str, ...]:
    """Internal instance names in first-appearance order (deterministic; the
    vendor enumerates the same ``bst.instance.<name>.`` namespace,
    bluestacks.py:194-203). Internal names never contain a dot."""
    seen: list[str] = []
    for key, _value in doc.entries:
        match = _INSTANCE_PREFIX_RE.match(key)
        if match is None:
            continue
        internal = match.group(1)
        if internal and internal not in seen:
            seen.append(internal)
    return tuple(seen)


def _display_key(internal: str) -> str:
    return f"bst.instance.{internal}.display_name"


def display_name(doc: ConfDocument, internal: str) -> str | None:
    """The instance's MIM display name (``bst.instance.<i>.display_name``),
    falling back to the internal name itself (the vendor's synth rule,
    bluestacks.py:293)."""
    return doc.get(_display_key(internal))


def internal_by_display(doc: ConfDocument, display: str) -> str | None:
    """First internal instance whose display_name is EXACTLY ``display``."""
    for internal in instance_internals(doc):
        if doc.get(_display_key(internal)) == display:
            return internal
    return None


def instance_adb_port(doc: ConfDocument, internal: str) -> int:
    """The instance's ADB device port (``status.adb_port`` then the plain
    ``.adb_port`` twin — vendor evidence bluestacks.py:220-239). Absent or
    non-numeric is a typed conf error: the transport can never be derived
    from a guess."""
    port: str | None = None
    path: str | None = None
    candidate = f"bst.instance.{internal}.status.adb_port"
    value = doc.get(candidate)
    if value is not None:
        port, path = value, candidate
    else:
        candidate = f"bst.instance.{internal}.adb_port"
        value = doc.get(candidate)
        if value is not None:
            port, path = value, candidate
    if port is None or path is None:
        raise _fail(
            f"bst.instance.{internal}.adb_port",
            None,
            "the instance ADB port key in bluestacks.conf",
        )
    if not port.isdigit() or not 3 <= len(port) <= 5:
        raise _fail(path, port, "a 3..5 digit decimal port")
    return int(port)


# ── renderer normalization (§4) ──


def renderer_mode(key: str, value: object) -> RendererMode:
    """Resolve a renderer entry STRICTLY (see module docstring).

    Accepts ``RendererMode`` members, the canonical spellings (auto/opengl/
    directx/vulkan) and the exact vendor tokens (gl/dx/vlcn); anything else
    is a typed ``ConfigValidationError``. AUTO is NEVER resolved here: the
    per-platform AUTO pin (§4) is the adapter's job — handlers hand this
    function a concrete mode.
    """
    if isinstance(value, RendererMode):
        return value
    if isinstance(value, str):
        spelling = value.strip().lower()
        if spelling in RENDERER_TOKENS:
            return RENDERER_TOKENS[spelling]
        try:
            return RendererMode(spelling)
        except ValueError:
            pass
    raise _fail(key, value, "one of auto, opengl, directx, vulkan (or gl, dx, vlcn)")


def renderer_token(mode: RendererMode) -> str:
    """The §4 vendor token for a CONCRETE mode; AUTO has none by definition."""
    try:
        return _TOKEN_BY_MODE[mode]
    except KeyError:
        raise ConfigValidationError(
            layer="bluestacks",
            path="display.renderer",
            value=mode.value,
            expected="a concrete renderer (AUTO is resolved by the caller's platform pin)",
        ) from None


# ── custom-resolution absorb (§4) ──


def _normalized_token(token: str) -> str:
    """The vendor's list-normalization shape: ×→x, spaces stripped,
    lowercase (py-clash-bot:258-259)."""
    return token.lower().replace("×", "x").replace(" ", "")


def absorb_custom_resolution(
    doc: ConfDocument, internal: str, width: int, height: int
) -> ConfDocument:
    """Absorb the requested W×H into ``bst.custom_resolutions`` (order-preserving,
    deduplicating; the separator is ``"; "`` like the vendor's write) and pin
    ``custom_resolution_selected="1"`` on the instance. Absorption, not a
    separate config — the token list is the vendor's resolution registry.
    """
    token = f"{width} x {height}"
    doc = doc.with_entry(f"bst.instance.{internal}.custom_resolution_selected", "1")
    existing = doc.get("bst.custom_resolutions")
    want = _normalized_token(token)
    tokens: list[str] = []
    if existing:
        tokens = [item.strip() for item in existing.split(";") if item.strip()]
    if not any(_normalized_token(item) == want for item in tokens):
        tokens.append(token)
    return doc.with_entry("bst.custom_resolutions", "; ".join(tokens))


# ── translation (§4) ──


def _int(key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(key, value, "an int")
    return value


def _str_pair(key: str, value: str, internal: str) -> tuple[str, str]:
    return (f"bst.instance.{internal}.{key}", value)


def translate(
    doc: ConfDocument, internal: str, entries: Sequence[tuple[str, object]]
) -> ConfDocument:
    """Translate canon entries into a new conf document (pure; strict).

    The overlay/bound source is already classified (see
    :func:`overlay_entries`); translation validates VALUES strictly —
    unknown renderer spellings and type/range breaks are typed refusals.
    The custom-resolution absorb fires once, after width/height are known
    to both exist. A None entry targets nothing (leave as-is), per §4's
    overlay semantics.
    """
    width: int | None = None
    height: int | None = None
    for key, value in entries:
        if value is None:
            continue
        if key == "display.width":
            width = _int(key, value)
        elif key == "display.height":
            height = _int(key, value)

    new_doc = doc
    for key, value in entries:
        if value is None:
            continue
        if key == "display.renderer":
            new_doc = new_doc.with_entry(
                f"bst.instance.{internal}.graphics_renderer",
                renderer_token(renderer_mode(key, value)),
            )
        elif key == "display.width":
            new_doc = new_doc.with_entry(*_str_pair("fb_width", str(width), internal))
        elif key == "display.height":
            new_doc = new_doc.with_entry(*_str_pair("fb_height", str(height), internal))
        elif key == "display.dpi":
            new_doc = new_doc.with_entry(*_str_pair("dpi", str(_int(key, value)), internal))
        elif key == "display.fps":
            new_doc = new_doc.with_entry(*_str_pair("max_fps", str(_int(key, value)), internal))
        elif key == "resources.cpus":
            new_doc = new_doc.with_entry(*_str_pair("cpus", str(_int(key, value)), internal))
        elif key == "resources.memory_mb":
            ram = _int(key, value)
            if ram <= 0:
                raise _fail(key, ram, "an int > 0")
            new_doc = new_doc.with_entry(*_str_pair("ram", str(ram), internal))
        elif key == "bluestacks.settings.vsync":
            if not isinstance(value, bool):
                raise _fail(key, value, "a bool")
            new_doc = new_doc.with_entry(
                *_str_pair("enable_vsync", "1" if value else "0", internal)
            )
        elif key == "bluestacks.settings.high_fps":
            if not isinstance(value, bool):
                raise _fail(key, value, "a bool")
            new_doc = new_doc.with_entry(
                *_str_pair("enable_high_fps", "1" if value else "0", internal)
            )
        else:  # unreachable: entries come from the classified helpers
            raise InvalidArgumentError(  # pragma: no cover
                parameter=key,
                constraint="unknown canonical config leaf (ConfigSpec vocabulary)",
            )

    if width is not None and height is not None:
        new_doc = absorb_custom_resolution(new_doc, internal, width, height)
    return new_doc


def _untranslatable(key: str) -> FeatureNotSupportedError:
    """Known-canonical leaf with no BlueStacks table entry → the typed gate."""
    return FeatureNotSupportedError(
        feature="configure",
        detail=f"canonical leaf '{key}' has no entry in the BlueStacks vendor translation table",
    )


def overlay_entries(
    spec: Mapping[str, str | int | float | bool | None],
) -> list[tuple[str, object]]:
    """A populated ConfigSpec overlay → ordered (leaf, value) entries.

    Refusals fire BEFORE any I/O (memu.py funnel parity): unknown keys →
    ``InvalidArgumentError`` (caller misuse); leaves under a known partial
    head that this table cannot translate → ``FeatureNotSupportedError``.
    """
    entries: list[tuple[str, object]] = []
    for key, value in spec.items():
        if not isinstance(key, str):
            raise InvalidArgumentError(
                parameter="spec",
                constraint="ConfigSpec keys are canonical config leaf names",
            )
        if key not in _BLUESTACKS_LEAVES:
            head = key.partition(".")[0]
            if head in _PARTIAL_HEADS | {"memu", "adb", "log", "google-play", "avd"}:
                raise _untranslatable(key)
            raise InvalidArgumentError(
                parameter=key,
                constraint="unknown canonical config leaf (ConfigSpec vocabulary)",
            )
        entries.append((key, value))
    return entries


def bound_entries(cfg: EmuctlConfig) -> list[tuple[str, object]]:
    """``spec=None`` source: every non-None leaf of the bound config (§1.8),
    ordered bluestacks.settings → resources → display for determinism. The
    BlueStacks SELECTTION knobs (adb_server_port/install_dir/conf_path/
    instance_name) are paths/names, not translatable leaves — never here."""
    entries: list[tuple[str, object]] = []
    bs = cfg.bluestacks if cfg.bluestacks is not None else BlueStacksConfig()
    for name in ("vsync", "high_fps"):
        value = getattr(bs.settings, name)
        if value is not None:
            entries.append((f"bluestacks.settings.{name}", value))
    for name in ("cpus", "memory_mb", "cpu_cap_pct"):
        value = getattr(cfg.resources, name)
        if value is not None:
            entries.append((f"resources.{name}", value))
    for name in ("width", "height", "dpi", "fps", "renderer"):
        value = getattr(cfg.display, name)
        if value is not None:
            entries.append((f"display.{name}", value))
    return entries
