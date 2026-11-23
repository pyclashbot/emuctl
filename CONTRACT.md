# emuctl — contract (baked)

Version: 1.0 · Status: PROMOTED 2026-10-07 — the operating contract for emuctl §1+ development. Baked from the draft draft + arbitration ledger (now `docs/archive/CONTRACTS.md` v1.5).

**Provenance.** This is the collapsed normative form of five siloed contract drafts (port, failure, discovery/registry, data/config, surfaces), consolidated against `docs/archive/DESIGN.md` v0.2 + `docs/research 01–09` and arbitrated through a 65-entry ledger (cross-contract collisions, adversarial review, slop cuts, user rulings); every ruling is applied inline. The alternatives considered and per-ruling rationale remain in `docs/archive/CONTRACTS.md` — that file is the record, this file is the rules. Adapter plumbing internals (memuc/HD-Adb mechanics), conformance-harness details, and docs structure are later contracts, intentionally not locked here.

## Identity

**What it is.** A higher-level, vendor-agnostic Android emulator controller library for Python: interaction primitives plus engine-specific lifecycle operations (`create`, `start`, `stop`, `configure`) where the engine provides them, with deliberate, discoverable handling of the gaps between engines. The emulator is a black box driven over ADB and vendor CLIs. It is not a screen-scraping/botting framework, a game API, or a drop-in shim for pymemuc; game logic, image recognition, and state machines stay in consumers; no coordinate scaling; no provisioning-as-config. First consumer: `py-clash-bot` — integration may edit py-clash-bot; drop-in compatibility is not required.

**Release scope (v0.1).** v0.1 ships the **ADB backend only** (headless Android over
ADB). MEmu, BlueStacks 5, BlueStacks Air, AVD and Google Play are preserved on
their stack branches and are **unverified** for v0.1. Their `BackendId` values stay
pinned for identity/wire compatibility, but they have no registry row: selection
fails cleanly with `BackendNotAvailableError("not-implemented-this-version")`.

**Shape.** Async core (pure asyncio) + sync bridge (anyio portals, confined to one module). v0.1 backends: ADB devices (core) only. Distribution: `emuctl` core + PEP 685 extras (`[all]`, `[cli]`) — only what is wanted, where it is wanted. Python 3.12+, stdlib-only core, `py.typed` from day one, conventional-commit auto-release (python-semantic-release, PyPI Trusted Publishing). Standing principles: fail-fast typed errors with bounded internal waits — never unbounded loops, never prompts; `restart()` is the explicit boot primitive and construction is cheap discovery; never parse vendor stdout for control flow; kills scoped to instances; bundled resources resolved through the package; serial validation as a security control; binary screenshot transport, no base64 round-trips.

## §1 — The port (`emuctl/port.py`)

The port is **async-only**; the sync surface is §5's derivation, never a second implementation. Payload types (`InstanceRef`, `ConfigSpec`, `CreateSpec`) are §3/§4-owned; exceptions are §2-owned.

```python
class CapabilityFlag(enum.Enum):
    """Additive-only; values pinned (never enum.auto()); never removed/renamed."""

    BOOT = "boot"  # start / stop / restart
    CREATE = "create"  # create()
    CONFIGURE = "configure"  # configure()
    SCREENSHOT = "screenshot"  # screenshot()
    INPUT = "input"  # click / swipe / key / text
    APPS = "apps"  # start_app / is_app_installed
    APK_INSTALL = "apk_install"  # install_apk (separately gated; Google Play cannot)


ReadyCheck = Callable[["AsyncEmulatorController"], "bool | Awaitable[bool]"]


class ProbeResult(NamedTuple):
    """The probes exemption: the one data-without-raise surface. Never truthy-check; check `.ok` —
    `(False, "")` is truthy as a 2-tuple."""  # reason = pinned token ("ok" | "no-adb" | …); never renders empty

    ok: bool
    reason: str


class DisplayState(NamedTuple):
    """Display geometry observed immediately BEFORE configure() applies its overlay.
    Fields are None when the backend has no display-readback seam; ADB always fills
    them. Restore is explicit: configure(state.to_spec()) — never automatic."""

    width: int | None
    height: int | None
    dpi: int | None

    def to_spec(self) -> "ConfigSpec": ...  # canonical display.width/height/dpi overlay


class AsyncEmulatorController(abc.ABC):
    FEATURES: frozenset[CapabilityFlag]  # declared I/O-free, class body only

    def __init__(self, ref: InstanceRef, **adapter_kwargs) -> None:
        """Binds the ref `connect()` resolved. No discovery, no search, no boot; never raises EmulatorNotReadyError."""

    def __init_subclass__(cls, /, **kw) -> None:
        """Wiring-time gate (zero I/O): every mechanic named for each declared flag in MechanicsTable is a concrete member somewhere in the MRO (not a `...` body)."""

    @property
    def identity(self) -> InstanceRef: ...
    @property
    def features(self) -> frozenset[CapabilityFlag]: ...  # no I/O, no gate
    def has_feature(self, flag: CapabilityFlag) -> bool: ...
    def _require(
        self, flag: CapabilityFlag
    ) -> None: ...  # shared gate → FeatureNotSupportedError(feature=flag), derived from FEATURES
    # ── transport seam ──
    @abstractmethod
    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> "bytes | str":
        """Ungated internal seam. Nonzero rc / unusable output → VendorCommandError."""

    def _attach(self) -> None:
        """Concrete no-op hook (not a MechanicsTable mechanic). ADB-family backends override it to run `adb connect` against their bound server; plain ADB inherits the no-op."""

    # ── lifecycle ──
    async def create(self, spec: CreateSpec) -> InstanceRef: ...
    async def configure(
        self, spec: ConfigSpec | None = None
    ) -> DisplayState: ...  # gated CONFIGURE; returns the PRE-apply display observation
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def restart(
        self,
        *,
        app: str | None = None,
        ready: ReadyCheck | None = None,
        timeout: float | None = DEFAULT,
    ) -> None: ...
    # ── interaction / apps (implemented once in AdbController) ──
    async def screenshot(self) -> bytes: ...
    async def click(self, x: int, y: int, *, count: int = 1, interval: float = 0.0) -> None: ...
    async def swipe(
        self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int = 300
    ) -> None: ...
    async def key(self, code: str | int) -> None: ...  # raw keycode/keyevent symbol, documented
    async def text(self, value: str) -> None: ...  # escaping owned by the shared default
    async def install_apk(self, apk_path: str | os.PathLike[str]) -> None: ...
    async def start_app(
        self, package: str
    ) -> None: ...  # launch + bounded spawn-verify (no stdout parsing)
    async def is_app_installed(self, package: str) -> bool: ...
    async def is_reachable(self) -> ProbeResult: ...
    async def aclose(self) -> None: ...  # idempotent; releases transport only


# MechanicsTable — port-owned, module-level, pinned like the flags. Satisfaction rule: every
# mechanic named for a declared flag is a concrete member somewhere in the MRO.
MECHANICS = {
    BOOT: ("_start", "_stop", "_is_running"),
    CREATE: ("_create",),
    CONFIGURE: ("_configure",),
    SCREENSHOT: ("_screenshot",),
    INPUT: ("_click", "_swipe", "_key", "_text"),
    APPS: ("_start_app", "_is_app_installed"),
    APK_INSTALL: ("_install_apk",),
}


class AdbController(AsyncEmulatorController, abc.ABC):
    """The ONE middle layer; SCREENSHOT/INPUT/APPS/APK_INSTALL are satisfied concretely here.
    Implemented once over raw ADB: _vendor_run (auto `-s <resolved-transport>` scoping for device
    commands only — server verbs {connect, disconnect, devices, start-server, kill-server, version}
    are a pinned exclusion list in emuctl/_serials.py, never sniffed from argv; argv-only, never
    shell=True); screenshot (exec-out screencap -p binary transport + PNG magic-sniff);
    click/swipe/key/text (text escaping owned here); install_apk/start_app/is_app_installed.
    Transport state (`adb_path: str | None`, `adb_server_port: int | None`) is bound at construction
    from the resolved EmuctlConfig: `config.adb` is the ONLY system-server selector; a backend
    subconfig's transport knob (e.g. `bluestacks.adb_server_port`) is translated at selection into
    these fields (the backend's table wins for its own backend, never a shape-match guess).
    Bundled-binary resolution is documented per backend in BackendSpec.description. Vendor adapters
    fill _start/_stop/_create/_configure/_is_running and may override _attach/restart; they declare
    FEATURES."""
```

**Invariants.**

1. Async-only port; sync members are exactly the zero-I/O declared attributes.
2. Single source of truth: `FEATURES` is declared in the adapter class body and nowhere else; platform truth is declared only in §3's spec table (adapters never declare platforms); `_require()` derives the error — handwritten support-raises in adapters are contract violations.
3. Gate first: each gated default body's first call is `_require(FLAG)`; adapter overrides re-invoke the gate at their heads (conformance-checked). An undeclared flag ⇒ derived `FeatureNotSupportedError`, never a silent no-op.
4. Predicates return negative state as data only when the observation itself succeeded; a failed observation raises (`VendorCommandError`/`DeviceStateError`) — a transport failure never masquerades as "not running" (timeout ≠ not-installed, unusable output ≠ not-supported).
5. Bounded waits everywhere: every I/O op carries `timeout: float | None = DEFAULT` (§2's timing table); `restart()`'s internal phases share the one remaining deadline — no per-phase config surface at v0.
6. `restart()` is one attempt: `None` or `EmulatorNotReadyError(reason, cause)` — never `False`, never internal retries, not idempotent. Phases: stop-if-running → configure-while-stopped → start → `_attach` → settle (first screenshot decodes non-empty) → [app: launch + bounded spawn-verify] → terminal check: `ready(...)` if given, else spawn-present-if-app, else screenshot-decodes. Adapters MAY override it wholesale (BlueStacks' MIM lifecycle is the motivating case); an override re-invokes `_require(BOOT)` at its head and inherits no default body. The bounded re-run-then-re-discover shape is consumer policy — the count is the consumer's too.
7. `create()` is backend-scoped (ignores the bound identity; returns the new instance's ref). `spec.name` is required at v0; the body begins with an existence pre-check through the discovery seam (`get_instance`), never vendor greeting text — an existing name raises `InvalidArgumentError(parameter="spec.name", constraint="already provisioned")` (exit 2).
8. `configure()` is gated CONFIGURE: `spec=None` re-applies every non-None section of the bound config; a populated `ConfigSpec` is a targeted overlay (§4). It returns a `DisplayState` observed immediately before the apply (all fields `None` when the backend cannot observe display); restore is explicit via `configure(state.to_spec())`, never automatic. Instance-scoped everything — kills, config, and state target the bound ref; the sole exception is `create()`. No global sweeps.
9. No control flow from vendor stdout parsing — machine-readable verification only. One sanctioned data readback exists: the ADB `configure()` display observation (`wm size` / `wm density`), whose exact `Physical|Override size|density` line grammar is parsed for a `DisplayState` only; it never selects a command path beyond skipping a write whose value already matches, fails closed on malformed output, and shares the op deadline. Screenshot: §4's INV-S1..S7 apply verbatim.
10. Additive-only evolution: flags pinned; new methods enter as gated defaults; abstract-set additions post-1.0 gate a major; no second version knob — semver + the changelog own behavior deltas.
11. Enforcement has exactly two sites: wiring-time (`__init_subclass__`) and conformance-time (§7); no bespoke lint rule. No `__del__` behavior — cleanup is explicit `aclose()`/`close()` (+ §5's facade CM), with at most one DEBUG "implicit GC close" backstop line. Enumeration is not a port method — §3's module-level `BackendDiscovery` seam. No coordinate scaling, ever (absolute device pixels; §4's INV-C1).

## §2 — Failure contract (`emuctl/errors.py`, stdlib-only)

- Exceptions are the single failure convention — no result-return pattern; the data-without-raise exemption is §1's `ProbeResult` plus declared attributes. One public root `EmuctlError`; **pure tree, no stdlib MI parents** — consumers catch `EmuctlError` or the specific class.
- Payload-first constructors: kwargs-only, typed primitives only (`str|int|float|bool|None|tuple[str,…]`, plus declared `cause`); instances are picklable across worker queues (`args == ()`, base `__reduce__`); `__str__` is generated and stable from payloads + sanitized detail.
- Free text is permitted only in `detail` and is sanitized at render time; every other payload is typed data; `EmulatorNotReadyError.reason` is a pinned snake_case token, never prose. `suggestion` is a per-CLASS template attribute; the CLI interpolates payload keys into it at render time — never ctor-baked. `backend` provenance is assigned by the raising layer, never a ctor arg. `CancelledError` is not a taxonomy class — never caught, rewrapped, or logged as an error.

```python
class EmuctlError(Exception):
    error_code: ClassVar[str] = "emuctl_error"
    suggestion: ClassVar[str] = ""              # per-class remediation TEMPLATE; payload keys are
    payload:    ClassVar[tuple[str, ...]] = ()  # interpolated at render time by the CLI
class InvalidArgumentError(EmuctlError):         payload = ("parameter", "constraint")  # invalid_argument → 2
class InvalidSerialError(InvalidArgumentError):  payload = ("serial",)                   # invalid_serial → 2
class ExtrasMissingError(EmuctlError):           payload = ("extra",)                    # extras_missing → 3
class ExecutableNotFoundError(EmuctlError):      payload = ("tool", "searched")          # executable_not_found → 3
class ConfigError(EmuctlError):                  payload = ("layer", "path", "reason", "value")  # config → 3
    # reason ∈ {"unreadable", "bad_version"}; bad_version carries the found version in `value` and
    # names the installed one in the message. ConfigValidationError stays its own class (distinct fix-the-value caller surface).
class ConfigValidationError(ConfigError):        payload = ("layer", "path", "value", "expected")  # config_validation → 3
class OperationTimeoutError(EmuctlError):        payload = ("operation", "deadline", "elapsed")  # timeout → 7
class VendorCommandError(EmuctlError):           payload = ("command", "returncode", "output_reason", "stderr_excerpt")  # vendor_command_failed → 8
    # output_reason ∈ {"exit" | "unusable_output" | "stderr"}; screenshot corruption ⇒ unusable_output (no dedicated class)
class DeviceStateError(EmuctlError):             payload = ("serial", "state")  # device_state → 8; state ∈ offline | unauthorized | disconnected (mid-op degradation only)
class InstanceNotFoundError(EmuctlError):        payload = ("instance_ref",)  # instance_not_found → 4; payload carries canonical str(ref), never the object
class FeatureNotSupportedError(EmuctlError):     payload = ("feature",)  # feature_not_supported → 5; detail extends the config gate (e.g. "config requests [display]…")
class BackendNotAvailableError(EmuctlError):     payload = ("reason",)  # backend_not_available → 5; reason ∈ "platform-unsupported" | "not-implemented-this-version"
class BackendNotFoundError(EmuctlError):         payload = ("backend_id",)  # backend_not_found → 2
class EmulatorNotReadyError(EmuctlError):        payload = ("reason", "cause")  # not_ready → 6; restart() ONLY
    # reason = short snake_case phase/vendor token ("app_not_installed", "menu_not_reached", "timeout", "start_failed")
class ControllerClosedError(EmuctlError):        payload = ()  # controller_closed → 6
class AmbiguousInstanceError(EmuctlError):       payload = ("selector", "matched")  # ambiguous_target → 6
class SyncCallInsideEventLoopError(EmuctlError): payload = ()  # sync_call_inside_loop → 2
    # suggestion template names the ".aio / amain" hatch

def resolve_error_code(exc: BaseException) -> str:  # MRO walk; "emuctl_error" fallback
```

**Exits.** Exit numbers live in one site, `emuctl/cli/_exitmap.py`; the comments above pin the mapping. Unmapped codes exit 9. Exit 130 = SIGINT; exit 141 = SIGPIPE on raw-bytes stdout — both documented beside the table, no code strings. A CI drift test walks the class tree and asserts every `error_code` has a table entry — no reverse-map artifact to maintain.

**Timeouts (`emuctl/timing.py`).** Every blocking-capable op takes `timeout: float | None = DEFAULT`; `DEFAULT` is the module sentinel resolving to the op's group constant: interaction (click/swipe/key/text) 10 · snapshot 15 · query (is_reachable, is_app_installed, discovery) 20 · config 20 · app (install_apk, start_app) 60 · lifecycle (start/stop/restart) 120 · provision (create) 180. `timeout <= 0` → `InvalidArgumentError`; `None` = explicit unbounded (sanctioned, discouraged, DEBUG-logged). One deadline per call, distributed internally as the op sees fit. Expiry always raises `OperationTimeoutError` — never degrades to a value.

**Retries.** Zero exception-driven retries inside the library — no decorators, no adapter internals, no bridge. The only sanctioned internal loop is `emuctl/waits.py`:

```python
async def wait_until(predicate, *, operation: str, timeout: float) -> None
# deadline-check → evaluate → sleep(poll); timeout REQUIRED; predicates' exceptions propagate
```

Idempotency classification ships as consumer guidance: interaction ops are idempotent-ish; `create` is not idempotent (persist and reconcile); `restart`'s re-run shape is consumer policy (§1.6). `is_reachable` is the only data-without-raise surface besides declared attrs.

**Cancellation.** Subprocess discipline is implemented once in `emuctl/_proc.py` (§6). Cancel = abandon, with bounded child cleanup: `terminate()` → 2 s bounded reap → `kill()` under a shielded, deadline-bounded cleanup. Kill scope = **the bound instance's discriminator**: our-spawned PIDs, or for vendor-owned processes the bound key as discriminator (window-title-equal bound key, internal instance name, per-VM vendor stop addressed by identity). Sweeps without a bound-instance discriminator (`taskkill /IM MEmuHeadless.exe`-style) are banned. `CancelledError` propagates unmodified. `close()` is idempotent and sweeps tracked stragglers; the GC backstop warns, never kills.

**Logging (`emuctl/log.py`).** `logging.getLogger(__name__)` everywhere; one `NullHandler` on `"emuctl"`; never `basicConfig`, never handlers, never `print`/stderr, no custom Handlers; all dynamic strings pass `emuctl.log.sanitize()` (strips CR/LF/control chars); counts + ≤200-char excerpts at DEBUG, never bodies/binary; log XOR raise (never both); narration only at DEBUG, one line per decision.

## §3 — Identity, discovery, selection (`_platforms.py`, `_serials.py`, `_identification.py`, `_discovery.py`, `backends/_registry.py`)

```python
class Platform(StrEnum): WINDOWS / MACOS / LINUX
def current_platform() -> Platform   # the single mapping point
class BackendId(StrEnum):
    ADB = "adb"; MEMU = "memu"; BLUESTACKS = "bluestacks"
    GOOGLE_PLAY = "google-play"; AVD = "avd"; BLUESTACKS_AIR = "bluestacks-air"
    # from_string(): PEP 685-style normalization (case-fold, _/. → -), else BackendNotFoundError
@dataclass(frozen=True, slots=True)
class InstanceRef:
    """Unambiguous address. Identity = (backend, key) ONLY — transport serial/port, display labels,
    run state are observations on InstanceRecord, never identity."""
    backend: BackendId
    key: str  # validated at construction via spec.validate_key
    # parse(): canonical 'backend:key' ONLY. A bare serial / host:port / bare name is an InvalidArgumentError
    # telling the caller to prefix (e.g. 'adb:127.0.0.1:5555') — no shape inference, no server
    # ambiguity, no match-named fan-out; I/O-free.
@dataclass(frozen=True, slots=True)
class InstanceRecord:  # point-in-time observation; not identity
    ref: InstanceRef; running: bool; serial: str | None; details: Mapping[str, str]
@dataclass(frozen=True, slots=True)
class BackendSpec:
    backend: BackendId; extra: str | None      # None for adb (core)
    module: str; adapter_attr: str             # import target + port-ABC subclass attribute
    supported_platforms: frozenset[Platform]   # THE sole platform-gating truth (consumed pre-import)
    dep_modules: tuple[str, ...]               # find_spec'd, never imported for side effects
    description: str; key_semantics: str; validate_key: Callable[[str], None]
# _SPEC_TABLE (module-level, pinned for v0.1):
#   ADB full spec — extra=None, all platforms. The sole row.
#   MEMU / BLUESTACKS / BLUESTACKS_AIR / GOOGLE_PLAY / AVD keep pinned BackendId
#   values but have NO row in v0.1: resolve_backend refuses cleanly, no import.
#   Those adapters live on their stack branches and are unverified this release.
def get_registry() -> Mapping[BackendId, BackendSpec | None]: ...  # static; import-free
def available_backends() -> list[BackendSpec]: ...                 # platform ⊇ host AND deps resolve
def resolve_backend(backend: BackendId) -> BackendSpec:
    """THE single selection gate, fixed order:
    unknown id → BackendNotFoundError · spec None → BackendNotAvailableError("not-implemented-this-version")
    · platform → BackendNotAvailableError("platform-unsupported", names available alternatives)
    · deps → ExtrasMissingError. Raises BEFORE any import; imports nothing."""  # fixed order = the listed order
def load_adapter(spec: BackendSpec) -> type[AsyncEmulatorController]:
    """The ONLY adapter importer (importlib.import_module + getattr); port-ABC subclass verified;
    raises ExtrasMissingError, never raw ModuleNotFoundError."""
async def connect(target: InstanceRef | str, *, config: EmuctlConfig | None = None
                  ) -> AsyncEmulatorController: ...
async def connect_sync(...) -> "EmulatorController": ...  # identical signature (§5)
# target = a canonical ref ONLY ('backend:key' string or InstanceRef); programmatic selection is
# InstanceRef(BackendId.X, key). Selection refusals raise HERE, at construction. No `logger=`
# parameter — logging is stdlib-name-based (§2); consumers attach handlers/filters to "emuctl".
# BackendDiscovery — a module-level CONVENTION, not a type artifact: each backend module implements
# these async functions (satisfaction verified by the registry conformance test):
#   list_instances(config) -> tuple[InstanceRecord, ...]   read-only, bounded
#   get_instance(config, ref) -> InstanceRecord | None     None = absent (stopped VMs return records)
# A backend's unavailability/reason arrives as a BackendStatus row derived from the observed failure
# itself — no dedicated vendor_status hook per module.
async def discover(backends=None, *, config=None, offline: bool = False) -> DiscoveryReport: ...
# read-only, bounded, per-backend isolated (a dead vendor = BackendStatus row, not failure); raises
# only for unknown ids; deterministic ordering; no caching. offline skips vendor process probes
# (config-store enumeration still runs). Cold `adb devices` may start the local ADB server
# (documented exception).
```

**Boundary.** Discovery answers "what plausibly exists and is it up"; it guarantees nothing about driveability — readiness begins at construction and the boot primitive, verified never assumed.

**Serial validation (public security control).** `emuctl.is_valid_serial(s) -> bool` / `emuctl.validate_serial(s) -> None` (raises `InvalidSerialError`): allowlist, reject-by-default, three enforcement layers — identity construction, `connect()`, and a per-transport-call re-check (pure, zero I/O). `transport_id:N` is allowed (documented); empty/whitespace is an error, never laundered into "auto"; no case folding; new shapes require security review + pinned tests. Core transport builds argv only, never `shell=True`.

**Identity lifetime.** Survives stop/start; persists across reboots (vendor stores are the authority); delete/disconnect ⇒ the ref goes **stale**, ops raise `InstanceNotFoundError`. `(bluestacks, X)` never equals `(adb, X)`. `emulator-NNNN` serials are positional, vendor keys referential (documented per backend in `key_semantics`); no case folding; no normalization beyond strip.

**Multi-instance policy.** N independent controllers per process are supported; no global registry, locks, or kills. Sync facade: independent facades from different threads are OK; one facade shared across threads is unsupported — `close()` is the ONLY facade member sanctioned from any thread; any other member called from a foreign thread is documented misuse. Same-ref parallel ops, cross-process coordination, shared-vendor-tooling contention (BlueStacks MIM), and two-backend dedup are explicitly not promised (documented, not solved). Exit numbers are shell control flow only — retry policy keys on the JSON error `code` (§5).

## §4 — Data + config

**Screenshot (INV-S1..S7).** `screenshot() -> bytes` returns a complete, well-formed PNG of the current framebuffer, as exactly `bytes` (S1 pure bytes). PNG signature + IHDR provable by the core helper, enforced adapter-side; corruption → `VendorCommandError(output_reason="unusable_output")` (S2). No vendor noise, no base64, no regex repair ever crosses the adapter (S3). No transcoding, never re-encoded (S4). One attempt per call, no internal retry loop — pacing is the consumer's (S5). Byte-equality is not identifiable — compare pixels or dims (S6). Rotation is not owned (S7). Core helper: `def png_dimensions(data: bytes) -> tuple[int, int]` (emuctl/png.py, stdlib-only; IHDR read, raises `ValueError` if absent; replaces consumers' cv2-decode-for-shape).

**Coordinates (INV-C1).** `click`/`swipe` coordinates are the screenshot PNG's top-left-origin pixel space (ADB display pixel space); the frame is stable within a session; the library never re-scales or re-bases. Geometry is *data* (applied at configure-time; the ADB backend applies size/density via `wm size WxH` / `wm density D` and returns the pre-apply `DisplayState`); *actual* geometry is observable (`png_dimensions(screenshot())`); target/actual mismatch is the consumer's detection — no auto-fix, no scaling shim.

**Config models (`emuctl/config/model.py`) — frozen dataclasses, `slots=True`, validating `__post_init__`; an invalid instance cannot exist; failures → `ConfigValidationError(path, value, layer, expected)`:**

```python
class RendererMode(enum.Enum): AUTO / OPENGL / DIRECTX / VULKAN   # canonical per semantic, per backend
DisplayConfig(width, height, dpi, fps, renderer)                 # all None-defaults = leave as-is
ResourceConfig(cpus=None, memory_mb=None, cpu_cap_pct=None)      # None = vendor's current value
AdbConfig(path=None, host="127.0.0.1", port=5037)                # None path → §3's discovery
LogConfig(level="warning", file=None)   # consumed ONLY by the CLI surface; the library never configures handlers
EmuctlConfig(adb=…, display=…, resources=…, log=…)  # ROOT contract object (v0.1: ADB only)
CreateSpec(name: str, android_version: str | None = None)  # name REQUIRED at v0; None version ⇒
                                                           # default image (Android 13)
# Range checks: ports 1..65535; set geometry >0; cpus ≥1; memory_mb >0; cpu_cap_pct, scale_percent
# 1..100; level ∈ stdlib names.
ConfigSpec = Mapping[str, str | int | float | bool | None]  # normative type ALIAS for the overlay
# Keys are CANONICAL config leaves ("display.width", "display.renderer", "resources.cpus",
# "resources.cpus", … — same vocabulary as the model sections); values are strict typed
# primitives. Adapters translate entries via their translation table; an unknown canonical key →
# InvalidArgumentError (caller misuse); an entry the backend can't translate deploys the gate.
```

**Translation tables.** v0.1 has no vendor translation table: the ADB backend applies only `display.width` / `display.height` / `display.dpi` through `wm`, and `display.renderer` has no ADB semantics. Translation tables for MEmu/BlueStacks remain on their stack branches (unverified). **`AUTO` is a vendor default, not the reference consumer's** — a future vendor pipeline passes `renderer=` explicitly.

**Precedence (normative chain).**

1. **Flags** (structured; the CLI binds layer 1 only) →
2. **`EMUCTL_*` env** — flat leaf-only grammar (`EMUCTL_DISPLAY_WIDTH`, `EMUCTL_RESOURCES_CPUS`, …); strict typed parse; unknown names → `ConfigWarning` →
3. **Project layer** — walk cwd → parent dirs; per dir, `emuctl.toml` wins over `[tool.emuctl]` in that dir; the FIRST dir containing either is authoritative and outer dirs are ignored; within the chosen file, leaf-first-wins →
4. **User XDG** (`~/.config` or `%APPDATA%\emuctl`) →
5. **Dataclass defaults** →
6. **Vendor-binary discovery** ONLY for still-None paths (§3, at selection).

```python
def resolve_config(flags=None, env=os.environ, cwd=Path.cwd()) -> ResolvedConfig: ...


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    config: EmuctlConfig
    warnings: tuple[ConfigWarning, ...]
```

Escape hatches: `--config-file` (replaces layers 3+4) and `--no-config` (skips both) — **flags only**. The merge is leaf-first-wins (`dataclasses.replace` fold); `resolve_config` is a pure function of (flags, env, cwd). Programmatic consumers may skip files entirely (`EmuctlConfig(...)` direct) — the typed object is the contract, files are sugar. Config parsing is stdlib-only (`tomllib`).

**Resources rule.** Any shipped file is addressed exclusively through `importlib.resources` (`emuctl.resources`; real-path needs via `as_file`) — never relative paths, `os.getcwd()`, or `__file__`-arithmetic; a contract test enforces the singularity. User-override order per resource: explicit path → project profiles dir → user profiles dir → bundled (immutable, always present).

**Config ↔ capability gate (construction time).** `ensure_config_supported(cfg, features)` runs against the selected adapter's declared set: `[display]` / `[resources]` each require `configure`; refusal = `FeatureNotSupportedError(feature, detail)` — typed, never silent-ignore, never hand-written. Plain-ADB declares CONFIGURE for guest display geometry (`configure()` → `DisplayState`), so `[display]` is accepted there. v0.1 has no vendor config sections. Load-time validates structure only (types/ranges/versions) — capability truth binds at selection (the target ref names the backend), so the gate runs at construction.

**Evolution.** `schema_version` optional, absent ⇒ 1; unknown major → `ConfigError(reason="bad_version")` carrying the found version in `value` and naming the installed one in the message. Unknown **keys** → warn+ignore with nearest-key hint (never error, never silent); unknown **values** → always strict error. New fields/members in any minor; renames get a one-minor warning alias then major-drop; removals are major-only with the replacement named. Emission additive-only per minor; reading tolerant; round-trip lossy-on-unknown, loudly.

## §5 — Surfaces (sync facade + CLI)

**Construction (the one public surface):** `au = emuctl.connect(target, *, config=None) -> AsyncEmulatorController` · `emu = emuctl.connect_sync(target, *, config=None) -> EmulatorController`. `target` is required and CANONICAL (`'backend:key'` string or `InstanceRef`); selection refusals raise at `connect*()` time, never first-call (§3). The ref carries the backend; the spec table carries the platform gate. One construction path, one error surface; both views share one controller state.

**Sync facade (`emuctl/_sync.py` — anyio confined to this module; everything below it is pure asyncio):**

```python
class EmulatorController:  # built only by connect_sync(); not user-constructed
    @property
    def aio(self) -> AsyncEmulatorController: ...  # native view; never starts the portal
    # every op: one-line delegation through _call(); kwargs forwarded verbatim —
    restart(*, app=None, ready=None, timeout=DEFAULT) / click / swipe / key / text /
    screenshot(timeout=…) -> bytes / is_reachable() -> ProbeResult / install_apk /
    start_app / is_app_installed / create(spec: CreateSpec) -> InstanceRef (same shape as §1's) /
    start / stop / configure(spec: ConfigSpec | None) -> DisplayState / aclose / close /
    __enter__ / __exit__ (start nothing; boot stays explicit)
    @property features · has_feature()             # portal-free (declared, static)
```

Bridge mechanics (locked):

- **One lazily-started portal per facade** (`start_blocking_portal(backend="asyncio")` — pinned asyncio; thread named `emuctl-portal`). `.aio`-only usage creates zero threads. Per-call portals are rejected (fork cost per click); per-session and process-global shared portals are rejected (dual cleanup stories / cross-instance coupling).
- `_call` is the only funnel; running-loop detection fires before portal work → `SyncCallInsideEventLoopError` with the `await emulator.aio.<op>()` escape hatch in the message. No trio-sniffing at v0.
- `close()` is idempotent and **cancel-first** (anyio CM-exit cancels pending tasks; an abandoned in-flight caller re-raises `ControllerClosedError`; documented honesty: *an abandoned op may have executed anyway*). The facade is reusable after a cancelled call; permanently closed after `close()`. `close()` is the only member sanctioned from any thread (§3).
- Cancellation = abandon: an interrupt during a sync call leaves the portal task running to its own bounded timeout; the facade is not poisoned. Interrupted `restart()` may leave ambiguous vendor state — the caller re-verifies/re-discovers (`aclose` does not auto-stop the emulator).
- `__del__` is a no-op; no atexit; **facades are unpicklable — construct inside worker processes** (document-only, no defensive machinery).
- Parity: every public `.aio` op has a sync twin with identical kwargs; exceptions cross the portal as the SAME class/payloads/`error_code` — no bridge rewrapping.

**ReadyCheck normalization (locked).** Every op's `timeout` kwarg is forwarded verbatim. A sync predicate is executed in a portal **worker thread** (never the portal loop thread) against the facade object itself, so its internal `emulator.screenshot()`-style calls run the normal funnel and cannot re-enter the loop; an async predicate (returns an awaitable) is awaited on the portal loop with the async controller. Every evaluation runs under `restart()`'s remaining budget; the predicate MAY interact (launch-and-verify patterns are sanctioned); a predicate that raises propagates as `restart()`'s failure (`EmulatorNotReadyError(cause=…)`). The bridge test suite pins the deadlock scenario (§7.5).

**CLI (`emuctl[cli]`).** Console script in the core distribution (pip ignores extras on entry points — pip#9726): a lazy-click stub exits 3 with "install `emuctl[cli]`"; CLI code is in-tree behind the extra; framework is click.

```
emuctl [--config PATH | --no-config] [--json]
       # TARGET = canonical 'backend:key' ref (adb:127.0.0.1:5555, memu:pyclashbot-136,
       #   bluestacks-air:conf); arity PER VERB — lifecycle/interaction exactly 1; probe 0..1; list 0..1
  list                                   # one bare JSON array / human table; rows carry backend/serial/running/platform fields to filter on
  probe TARGET                           # self-probe (no target) is v0.1 — §8
  screenshot TARGET (-o FILE | --stdout) # EXACTLY ONE of -o/--stdout, else usage exit 2; no default path; --json + --stdout ⇒ usage
  click TARGET X Y [--count N] [--interval F]
  swipe TARGET X1 Y1 X2 Y2 [--duration-ms N]
  key TARGET KEYCODE                     # ^KEYCODE_[A-Z_]+$ | bare digits ⇒ keyevent; anything else ⇒ usage exit 2 (no silent text)
  text TARGET VALUE                      # digits/PINs expressible here
  app start|installed TARGET PACKAGE
  start | stop | restart TARGET [--app PKG] [--timeout S]
  configure TARGET set KEY VALUE         # builds a one-key ConfigSpec overlay
  version [--json]
```

- Generic verbs only; `TARGET` is positional = canonical `backend:key` ref. No bare serials, no inference, no `--backend` flag: a malformed shape ⇒ exit 2 telling the caller to prefix; well-formed but absent ⇒ exit 4. `create`, clone/copy/export/import are excluded from v0 verbs (§8).
- CLI `restart` uses backend-default readiness (a ready-check is not CLI-expressible); per-op `--timeout` where a verb offers it; no global deadline knob.
- **Output contract:** stdout = results only — one JSON doc with `"schema_version": 1`; `list` is a bare array; raw PNG bytes for `screenshot --stdout` (the ONLY raw-bytes path); stdout on error is empty. stderr = human text, logs, error objects (JSON even without `--json`): `{"error": {"code", "exit", "message", "suggestion"}}`. One mapping site (`emuctl/cli/_exitmap.py`); unmapped ⇒ 9. **Pipe discipline:** `screenshot --stdout` closes cleanly on `BrokenPipeError` (restore SIGPIPE default, exit 141, no traceback, no stderr noise); reading raw bytes from a slow sink is never misreported as a timeout.
- `-o FILE` overwrites an existing file — documented, not gated (v0 has no destructive-enough verb to earn a `--yes` gate; the no-prompts promise makes gating moot).
- **Agent determinism:** no prompts, ever; stdin is never read; non-TTY ⇒ no color, no pagination; sorted, locale-invariant JSON; no timestamps in v0; additive-only output evolution (`"schema_version"` anchors it); argv/output shapes are discoverable via `--help`. Agents key retry policy on `error.code`, never on the exit number (`not_ready` is transient, `controller_closed` deterministic — they share exit 6 by design).
- **Entry:** `main()` refuses to run inside an event loop (exit 2, suggests `await emuctl.cli.amain(argv)`); `asyncio.run(amain())` — one loop per invocation; KeyboardInterrupt ⇒ 130 always, no traceback; SIGTERM untrapped (OS-kill semantics preserved). `version --json` = `{"schema_version": 1, "emuctl_version": "x.y.z"}`.
- **Probe output:** schema_version 1; backend; target (§3 identity fields); platform (os/supported); state (adb_state/reachable); features = the `CapabilityFlag` vocabulary sorted by flag name, uniform rows `{"flag": "apps", "present": true}` / `{"flag": "apk_install", "present": false}`. TARGET required at v0 (self-probe v0.1). Error suggestions name pasteable commands.

**Reference-consumer migration (py-clash-bot).** `connect_sync(ref, config=…)` + `emu.restart(app=PACKAGE, ready=check_if_on_clash_main_menu)` is the mapped shape; their session file-logging attaches a handler to the `"emuctl"` logger (no `logger=` param); the four-arm construction ladder, `verify_memu_installation()`, and the registry import map onto `connect_sync` with typed refusals at construction; `screenshot()` gets a one-line `cv2.imdecode` shim until `emuctl[image]`. Click fidelity, stated honestly: `click(x, y)`-shaped call sites port directly; sites calling `click` by keyword (`x_coord=`/`y_coord=`) or passing `clicks`/`interval` positionally need a one-time sed in their shim; the click `interval` default is `0.0` (vs their MEmu adapter's `0.1`) — an explicit, documented behavioral decision. `EmulatorNotReadyError`'s name survives; `install_apk` becomes introspectable (`FeatureNotSupportedError` + probe rows); `stop_worker_process()`'s hard-kill shutdown model is untouched by design (the portal thread dies with the process; cancel = abandon is the documented semantics).

## §6 — Codebase organization + enforcement

Organization is locked because adapter maintainability is an explicit goal: left as convention, the py-clash-bot emulators layer grew `is_macos()` branches and 1,480-line adapters.

**Sharing tiers — exactly four, one promotion path:**

| Tier | Content | Lives in |
|---|---|---|
| Universal | port ABC, MechanicsTable, errors, timing/waits, process executor, serials, identity, discovery plumbing, config, platforms, png | core (`emuctl/`, never `emuctl.backends.*`) |
| ADB-family | `AdbController` — the interaction primitives, `_vendor_run` scoping, server-verb exclusion list | core, port-adjacent (all ADB-speaking adapters inherit; no second middle layer for MEmu's memuc transport) |
| Vendor-family | helpers wanted by 2+ adapters of the same vendor family (BlueStacks conf parsing, MimMeta synth, per-platform path discovery, renderer normalization, port-derived serials) | the backend's package, `_`-private (`emuctl.backends.bluestacks._conf` …) |
| Coincidental | a helper 2+ unrelated families both want | **promoted to core** — never a lateral import |

**Subprocess discipline lives in one place:** `emuctl/_proc.py`'s internal async executor — bounded `timeout` → `OperationTimeoutError`; cancel-scoped PID kills with bounded reap; argv-only, never `shell=True`; DEBUG argv logging; sanitized strings; Windows console suppression (`CREATE_NO_WINDOW`). Every `_vendor_run` implementor and every vendor-process probe calls exactly this. It is internal — not exported as a consumer utility (§8). It carries one private injection seam (transport-constructor-level, not a plugin registry) where the recorded-fixture test tier replays real subprocess transcripts.

**Module layout (sketch):**

```
emuctl/
  port.py errors.py timing.py waits.py png.py log.py _proc.py
  _serials.py _identification.py _discovery.py _platforms.py
  config/model.py
  backends/_registry.py backends/adb.py
```

**v0.1 scope.** Only the plain ADB backend ships. MEmu, BlueStacks, AVD, and the Google
Play stub are preserved on their stack branches for later, unverified work; the release
has no adapter modules, extras, or registry rows for them. Their `BackendId` values stay
in the identity vocabulary, so selection refuses cleanly with
`BackendNotAvailableError` instead of pretending they do not exist.

**Layout rules.**

1. Lazy packages: a backend becomes a package only when its family gains a second concrete adapter; `_`-modules are family-private. v0.1's sole adapter is a module (`backends/adb.py`).
2. Import direction is a DAG: core ← any backend; never sideways between backend families; never a family reaching into another family's `_`-private modules.
3. Only the spec table names import-facing paths; everything else under `emuctl.backends.*` is implementation; dynamic adapter imports happen ONLY via `load_adapter`, never ad-hoc.
4. Extra-boundary promotion: sharing that would cross an extras boundary is, by definition, the promotion signal — it goes to core so extras stay minimal.

**Enforcement (CI + `make lint`, not vibes).**

- **import-linter** (dev dependency group; pre-commit + CI): a `layers` contract asserting `emuctl.backends → emuctl` only (core never imports backends statically — the lazy loader's dynamic import is deliberately invisible to it), and an `independence` contract over the backend sibling packages (v0.1: `adb` only) — no sideways imports; this also enforces rules 1 and 4, since a would-be sideways import must instead be promoted and pass the layers contract.
- **ruff TID251 banned-api:** `subprocess` banned project-wide, `_proc.py` exempt via `per-file-ignores` (the exemption disables all of TID251 there, so banned-api stays single-entry; §7's AST-walk is the backstop), plus the flake8-bandit S-rules (S602/S603/S605/S607) for the `shell=True` family.
- **Release-time API discipline:** griffe `check` against the last release tag in the release workflow (additive-only evolution promises); commitizen `cz check` for conventional-commit gates alongside python-semantic-release.
- **No editor/agent hooks.** Hooks are fail-open and per-tool; pre-commit + CI are the enforcement of record. The conformance suite (§7) carries what linters cannot.

## §7 — Conformance (what the offline suite asserts; fake-backed, live tier opt-in)

Offline against the behavioral fake of the port, backed by recorded real-hardware byte fixtures; the live tier is opt-in/marked and never required for CI.

1. **Taxonomy:** every fake-adapter raise is a taxonomy class with the correct code, payloads, and stable `__str__`; instances are picklable.
2. **Timeout shape:** the op-level deadline is honored under injected slowness; `OperationTimeoutError` payloads; no cancellation conflation.
3. **Cancel shape:** `CancelledError` propagates unmodified; children are killed + reaped by PID; no stragglers.
4. **Logger silence:** import + fake run adds nothing to the root logger; nothing on stdout.
5. **Bridge identity:** the portal re-raises the same class/payloads; every public `.aio` op has a sync twin; running-loop detection fires; `close()` cancels first; the ReadyCheck deadlock scenario — a sync predicate calling `emulator.screenshot()` from the worker-thread proxy runs to completion without `SyncCallInsideEventLoopError` or re-entrant portal deadlock.
6. **Registry + wiring:** the spec table is the sole platform truth; `[all]`/extras resolve to concrete specs; BackendDiscovery is satisfied per backend module; `find_spec` honesty; declared-flag/mechanic parity (concrete-in-MRO) and gate-first order hold for every adapter.
7. **Config:** precedence fold purity; unknown-key warnings; capability-gate refusals; resource-access singularity.
8. **Screenshot:** PNG sniff against recorded byte fixtures (transport tier-3 when live).
9. **Suggestion rendering:** per-class templates interpolate payload keys at the CLI render site; no ctor-baked suggestions.
10. **Organization:** import-linter layers + independence contracts pass; `subprocess` is imported only inside `emuctl/_proc.py` (+ stdlib test fakes); dynamic imports occur only through `load_adapter`; every spec row's module exists and exports its `adapter_attr` class + the BackendDiscovery functions.

## §8 — Deferred and parked (with re-add triggers)

Nothing below is promised at v0; each item names the trigger that brings it back.

| Item | Re-add trigger |
|---|---|
| Probed feature sets + `EXPERIMENTAL`/maturity graduation | a consumer needs maturity rendering (probe rows stay uniform `{"flag", "present"}` objects) |
| `clone` family (flag, method, `_clone` mechanic) | v0.1 — first consumer duplicating instances |
| v0.1 club: CLI `create` verb + backend-unique auto-naming · `probe` self-probe (no TARGET) | first named consumer for each (port `create()` ships with `name` required) |
| `resolve_target`: match-named bare-name resolution + bare-shape inference | a bare-name caller appears; until then canonical `backend:key` refs only |
| Public `run_vendor_cmd` equivalent (+ CLI `vendor` verb); public `stop_app` / `screen_size` / `is_running` | first named consumer (the internal `_vendor_run` seam survives; observation surfaces today: probe, `InstanceRecord`, `is_reachable`) |
| Async-CM pair + `wrap_async_context_manager` machinery | v0.1, together with stream CMs |
| Slots: `emuctl[image]` decode helpers · `uninstall`/push/GPS · `poll_interval` knob · env twins (`EMUCTL_CONFIG_FILE`, `EMUCTL_NO_CONFIG`) · per-phase deadline settings · public `emuctl/_proc.py` re-export · per-class catch-compat MI parents · config schema-v2 + `config_schema_version` field + provenance-tracked `config` verb | a named consumer asks for the specific item (each re-adds on demand) |
| Without v0 consumers: custom lint plugin · exit token `cancelled` + `--yes`/confirmation gating · port-version epochs (`PORT_VERSION`) | the first real trigger appears — a prompt/confirm flow, the §7 AST-walk proving insufficient (no 2026 tool exists; ty has no plugins by design), or something consuming a port epoch (semver + the changelog own deltas today) |
