# emuctl — early contract proposals (v1, for review)

**Status:** hub-consolidated proposal, 2026-10-07, **v1.5 — BAKED: the promoted artifact is `CONTRACT.md`** (clean normative form; this file is the draft + 65-entry arbitration ledger/record). Produced by five siloed design agents (port, failure, discovery/registry, data/config, surfaces) against `DESIGN.md` v0.2 + `research/01–09`; revised through a two-lens adversarial review (#17–37), a two-lens slop-cut round (#39–59), user rulings (#60–62), and the enforcement-tooling audit (#63–65).

Later contracts (not lockable this early, intentionally deferred): adapter plumbing internals (memuc/HD-Adb mechanics), conformance-suite harness details, docs structure.

---

## C1 — The Port (`emuctl/port.py`)

The port is **async-only**; the sync surface is E's derivation (C5), never a second implementation. Payload types (`InstanceRef`, `ConfigSpec`, `CreateSpec`) are C/D-owned; exceptions are B-owned.

```python
class CapabilityFlag(enum.Enum):
    """Additive-only; values pinned (never enum.auto()); never removed/renamed."""
    BOOT               = "boot"         # start / stop / restart
    CREATE             = "create"       # create()
    CONFIGURE          = "configure"    # configure()
    SCREENSHOT         = "screenshot"   # screenshot()
    INPUT              = "input"        # click / swipe / key / text
    APPS               = "apps"         # start_app / is_app_installed
    APK_INSTALL        = "apk_install"  # install_apk (separately gated; Google Play cannot)

ReadyCheck = Callable[["AsyncEmulatorController"], "bool | Awaitable[bool]"]

class AsyncEmulatorController(abc.ABC):
    # ── declared, I/O-free — required in every adapter class body ──
    FEATURES: frozenset[CapabilityFlag]

    def __init__(self, ref: InstanceRef, **adapter_kwargs) -> None:
        """Side-effect-light ONLY: bind the ref C's connect() resolved.
        No discovery, no search, no boot. Never raises EmulatorNotReadyError."""

    def __init_subclass__(cls, /, **kw) -> None:
        """Wiring-time gate (zero I/O): declared attrs present; declared flag ⇒
        every mechanic listed for that flag in MechanicsTable is a CONCRETE member
        somewhere in the MRO (not a `...` body)."""

    # MechanicsTable: Mapping[CapabilityFlag, tuple[str, ...]] — module-level, owned by the
    # port, pinned like the flags. Satisfaction rule = concrete-in-MRO per name.
    MECHANICS = {
        BOOT: ("_start", "_stop", "_is_running"), CREATE: ("_create",),
        CONFIGURE: ("_configure",), SCREENSHOT: ("_screenshot",),
        INPUT: ("_click", "_swipe", "_key", "_text"),
        APPS: ("_start_app", "_is_app_installed"), APK_INSTALL: ("_install_apk",),
    }
    # `_attach` is a CONCRETE no-op port hook (not a table mechanic): ADB-family
    # backends override it to run `adb connect` against their bound server; plain
    # ADB inherits the no-op. SCREENSHOT/INPUT/APPS/APK_INSTALL are satisfied
    # concretely by AdbController.

    @property
    def identity(self) -> InstanceRef: ...
    @property
    def features(self) -> frozenset[CapabilityFlag]: ...        # no I/O, no gate
    def has_feature(self, flag: CapabilityFlag) -> bool: ...
    def _require(self, flag: CapabilityFlag) -> None:            # the shared gate
        # raises FeatureNotSupportedError(feature=flag) — derived from the declared set

    # ── transport seam ──
    @abstractmethod
    async def _vendor_run(self, argv: Sequence[str], *, binary: bool = False) -> "bytes | str":
        """Ungated internal seam. Nonzero rc / unusable output → VendorCommandError.
        Raw vendor access is deliberately NOT a public surface at v0 (slop round:
        zero consumers; the internal seam serves the adapters; a public equivalent
        is re-addable the day one appears)."""

    # ── lifecycle (mechanics via _-prefixed abstracts, gated defaults above) ──
    async def create(self, spec: CreateSpec) -> InstanceRef: ...
    """Backend-scoped: ignores the bound identity; returns the new instance's ref.
    spec.name is REQUIRED at v0 (the backend-unique auto-naming scheme is deferred
    with its first consumer). The body begins with an existence pre-check through
    the discovery seam (`get_instance`), NEVER vendor greeting text: an existing
    name raises InvalidArgumentError(parameter="spec.name", constraint="already
    provisioned") (exit 2; per-class suggestion template renders 'pick another
    name, or `emuctl list` first').
    """
    async def configure(self, spec: ConfigSpec | None = None) -> None: ...
    """Gated CONFIGURE. spec=None re-applies every non-None section of the bound
    config; a populated ConfigSpec is a targeted overlay (see C4's definition)."""
    async def start(self) -> None: ...
    async def stop(self) -> None: ...       # instance-scoped kills only
    async def restart(self, *, app: str | None = None, ready: ReadyCheck | None = None,
                      timeout: float | None = DEFAULT) -> None: ...
    """THE boot primitive. One attempt; None or EmulatorNotReadyError — never False, never retries.
    timeout caps the WHOLE op (default T_LIFECYCLE = 120 s; None = explicit unbounded opt-in).
    Phases share the remaining budget: stop-if-running → configure-while-stopped →
    start → _attach (see above) → settle (first screenshot decodes non-empty) →
    [if app: launch + bounded spawn-verify] → terminal check: ready(...) if given,
    else spawn-present-if-app, else screenshot-decodes.
    Adapters MAY override restart wholesale (BlueStacks' MIM lifecycle is the
    motivating case); an override re-invokes _require(BOOT) at its head and
    inherits no default body."""

    # mechanics, guaranteed concrete-in-MRO per MechanicsTable (wiring gate above):
    #   BOOT → _start, _stop, _is_running · CREATE → _create · CONFIGURE → _configure
    #   SCREENSHOT → _screenshot · INPUT → _click/_swipe/_key/_text
    #   APPS → _start_app/_is_app_installed · APK_INSTALL → _install_apk
    #   + the _attach hook (concrete no-op on the port)

    # EVERY I/O op above additionally carries `timeout: float | None = DEFAULT`
    # (B's contract; groups: click/swipe/key/text→interaction, screenshot→snapshot,
    # is_reachable/is_app_installed→query(config: 20 s shared), configure→config,
    # install_apk/start_app→app); shown signatures omit it for brevity.
    # Predicates return negative state as data ONLY when the observation itself
    # succeeded; a failed observation raises (VendorCommandError/DeviceStateError)
    # — a broken observation never masquerades as "not running".

    # ── interaction — implemented ONCE in AdbController over _vendor_run ──
    async def screenshot(self) -> bytes: ...
    """exec-out screencap -p binary transport; PNG magic-sniff inside AdbController;
    corruption → VendorCommandError(output_reason="unusable_output")"""
    async def click(self, x: int, y: int, *, count: int = 1, interval: float = 0.0) -> None: ...
    async def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int = 300) -> None: ...
    async def key(self, code: str | int) -> None: ...    # raw keycode/keyevent symbol, documented
    async def text(self, value: str) -> None: ...        # escaping owned by the shared default

    async def install_apk(self, apk_path: str | os.PathLike[str]) -> None: ...
    async def start_app(self, package: str) -> None: ...   # launch + bounded spawn-verify (no stdout parsing)
    async def is_app_installed(self, package: str) -> bool: ...
    async def is_reachable(self) -> ProbeResult: ...        # PROBE — NamedTuple(ok, reason); never raises

    async def aclose(self) -> None: ...          # idempotent; releases transport only


class AdbController(AsyncEmulatorController, abc.ABC):   # the ONE middle layer
    """Implements once over raw ADB: _vendor_run (auto -s <resolved-transport> scoping
    for device commands only — server verbs {connect, disconnect, devices, start-server,
    kill-server, version} are a pinned exclusion list in emuctl/_serials.py, never
    sniffed from argv; argv-only, never shell=True), screenshot + sniff, click/swipe/
    key/text, install/start/is_installed.
    Transport state (`adb_path: str | None`, `adb_server_port: int | None`) is bound at
    construction from the resolved EmuctlConfig: `config.adb` is the ONLY system-server
    selector; a backend subconfig's transport knob (e.g. `bluestacks.adb_server_port`)
    is TRANSLATED at selection into these fields (the backend's table wins for its own
    backend, never a shape-match guess). Bundled-binary resolution is documented per
    backend in BackendSpec.description.
    Vendor adapters fill _start/_stop/_create/_configure/_is_running and may override
    _attach/restart (per their notes) and declare FEATURES."""
```

**Port invariants.**
1. Async-only port; sync members are exactly the zero-I/O declared attributes.
2. Single source of truth: FEATURES in the adapter class body and nowhere else (platform-gating truth lives solely in C's spec table — adapters do not declare platforms); `_require()` derives the error; handwritten support-raises in adapters are contract violations (review-enforced; the mechanical parity the wiring gate + conformance suite already catch).
3. Every I/O edge gates first: each gated default body's first call is `_require(FLAG)` per the annotations; adapter overrides re-invoke the gate at their heads (conformance-checked). Predicates return negative state as data ONLY when the observation itself succeeded; a failed observation raises — transport failures never masquerade as state answers (kills the timeout-as-not-installed disease).
4. Bounded waits everywhere per B's contract; restart's internal phases share the remaining deadline (no per-phase config surface in v0).
5. `restart()` = one attempt; it is not idempotent — the bounded re-run-then-re-discover SHAPE is consumer policy (the exact count is theirs too; the reference consumer runs a five-restart net), not library-imposed.
6. `EmulatorNotReadyError` is restart/boot-only; construction errors are selection/config errors.
7. Instance-scoped everything (kills/config/state target the bound ref; no global sweeps); sole exception: `create()` is backend-scoped.
8. No control flow from vendor stdout parsing — machine-readable verification only.
9. Screenshot: D's INV-S1..S7 (see C4) apply verbatim.
10. Additive-only evolution: flags pinned, new methods enter as gated defaults (undeclared flag ⇒ derived error, never silent no-op); abstract-set additions post-1.0 gate a major. There is deliberately NO second version knob (`PORT_VERSION` was cut in adversarial review — semver + changelog own behavior deltas; nothing consumes a port epoch today and it's re-addable when something real does).
11. Wiring-time + conformance-time enforcement of declared-vs-mechanic parity (no third enforcement site — a bespoke lint-plugin rule was cut; review hygiene covers the rest).
12. No `__del__` behavior; cleanup is explicit `aclose()`/`close()` (+ the facade's sync CM).
13. Enumeration is NOT a port method — C's module-level `BackendDiscovery` seam.
14. No coordinate scaling, ever (absolute device pixels).

---

## C2 — Failure contract (`emuctl/errors.py`, stdlib-only)

**Base rules.** One public root, payload-first constructors (kwargs-only, typed primitives `str|int|float|bool|None|tuple[str,…]`, plus declared `cause`), picklable across worker queues (`args == ()`, base `__reduce__`), generated stable `__str__` (payloads + sanitized detail). Free text is permitted ONLY in declared free-text payloads (`detail`, `not_ready`'s `reason`) and is always sanitized at render time; every other payload is typed data. **Pure tree — no stdlib MI parents** (the seven catch-compat pairs had no v0 consumer; consumers catch `EmuctlError` or the specific class; per-class catch-compat is re-addable the day one appears). `suggestion` is a per-CLASS template attribute; the CLI interpolates payload keys into it at render time — never ctor-baked. `backend` provenance assigned by the raising layer, never a ctor arg. `CancelledError` is not a taxonomy class — never caught, rewrapped, or logged-as-error.

```python
class EmuctlError(Exception):
    error_code: ClassVar[str] = "emuctl_error"
    suggestion: ClassVar[str] = ""            # per-class remediation TEMPLATE; payload keys are
    payload:    ClassVar[tuple[str, ...]] = ()  # interpolated at render time by the CLI

# ―― caller misuse (exit 2) ――
class InvalidArgumentError(EmuctlError):     # invalid_argument
    payload = ("parameter", "constraint")
class InvalidSerialError(InvalidArgumentError):          # invalid_serial   [C raises]
    payload = ("serial",)

# ―― environment / selection (exit 3) ――
class ExtrasMissingError(EmuctlError):                   # extras_missing   [C raises]
    payload = ("extra",)                       # suggestion template: "pip install 'emuctl[{extra}]'"
class ExecutableNotFoundError(EmuctlError):              # executable_not_found
    payload = ("tool", "searched")
class ConfigError(EmuctlError):                          # config           [D raises]
    payload = ("layer", "path", "reason", "value")
    # reason ∈ {"unreadable", "bad_version"} — unreadable file / unknown schema
    # major (value carries the found version, message names the installed one).
    # ConfigValidationError below is the THIRD reason and stays its own class
    # (fix-the-value recovery is distinct — the caller-refusal surface of C4).
class ConfigValidationError(ConfigError):                # config_validation
    payload = ("layer", "path", "value", "expected")

# ―― deadline (exit 7) ――
class OperationTimeoutError(EmuctlError):                # timeout
    payload = ("operation", "deadline", "elapsed")

# ―― vendor / world (exit 8) ――
class VendorCommandError(EmuctlError):                   # vendor_command_failed
    payload = ("command", "returncode", "output_reason", "stderr_excerpt")
    # output_reason ∈ {"exit" | "unusable_output" | "stderr"}; screenshot corruption
    # raises this with output_reason="unusable_output" (NO dedicated class)
class DeviceStateError(EmuctlError):                     # device_state   (mid-op degradation only)
    payload = ("serial", "state")              # state ∈ offline | unauthorized | disconnected

# ―― identity (exit 4) ――
class InstanceNotFoundError(EmuctlError):                # instance_not_found  [C/A raise]
    payload = ("instance_ref",)                # "the addressed thing is gone"; the payload
                                               # carries the CANONICAL STRING str(ref), never
                                               # the InstanceRef object (typed-primitive rule)

# ―― capability / backend selection (exit 5) ――
class FeatureNotSupportedError(EmuctlError):             # feature_not_supported  [A's _require]
    payload = ("feature",); detail extends D's gate ("config requests [display]…")
class BackendNotAvailableError(EmuctlError):             # backend_not_available  [C raises]
    payload = ("reason",)            # "platform-unsupported" | "not-implemented-this-version"
class BackendNotFoundError(EmuctlError):                 # backend_not_found      [C raises]
    payload = ("backend_id",)

# ―― state (exit 6) ――
class EmulatorNotReadyError(EmuctlError):                # not_ready  [A raises: restart() ONLY]
    payload = ("reason", "cause")              # reason = short snake_case PHASE/VENDOR TOKEN
                                               # (e.g. "app_not_installed", "menu_not_reached",
                                               # "timeout", "start_failed"); consumer prose → detail
class ControllerClosedError(EmuctlError):                # controller_closed  [E raises]
class AmbiguousInstanceError(EmuctlError):               # ambiguous_target   [C raises]
    payload = ("selector", "matched")

# ―― bridge (exit 2) ――
class SyncCallInsideEventLoopError(EmuctlError):         # sync_call_inside_loop  [E raises]
    payload = ()                               # suggestion template names the ".aio / amain" hatch

def resolve_error_code(exc: BaseException) -> str:    # MRO walk; "emuctl_error" fallback
    # The CI drift test walks the class tree and asserts each error_code has a
    # `emuctl/cli/_exitmap.py` entry — no reverse-map artifact to maintain.
```

**Exits (B owns code strings; E owns numbers — one site `emuctl/cli/_exitmap.py` + CI drift test over the class tree).**
0 ok · 1 general (base root / unknown code) · 2 usage (`invalid_argument`, `invalid_serial`, `backend_not_found`, `sync_call_inside_loop`) · 3 environment (`extras_missing`, `executable_not_found`, `config`, `config_validation`) · 4 not_found (`instance_not_found`) · 5 unsupported (`feature_not_supported`, `backend_not_available`) · 6 state (`not_ready`, `controller_closed`, `ambiguous_target`) · 7 timeout (`timeout`) · 8 transport (`vendor_command_failed`, `device_state`) · 9 internal (unmapped default) · 130 SIGINT (documented beside the table, no code string) · 141 SIGPIPE on raw-bytes stdout (documented beside 130, no code string).

**Timeouts (`emuctl/timing.py`).** Every blocking-capable op takes `timeout: float | None = DEFAULT` (sentinel → group constant): interaction 10 · snapshot 15 · query 20 · config 20 · app 60 · lifecycle 120 · provision 180. `timeout<=0` → `InvalidArgumentError`; `None` = explicit unbounded (sanctioned, discouraged, DEBUG-logged). One deadline per call, distributed internally as the op sees fit. Expiry always raises `OperationTimeoutError`; never degrades to a value (I4: timeout ≠ not-installed, unusable output ≠ not-supported).

**Retries.** Zero exception-driven retries inside the library — no decorators, no adapter internals, no bridge (pymemuc's `retryable` is the named anti-pattern). The ONLY sanctioned internal loop is `emuctl/waits.py`'s

```python
async def wait_until(predicate, *, operation: str, timeout: float) -> None
# deadline-check → evaluate → sleep(poll internally); timeout REQUIRED — always bounded
```

(predicates' exceptions propagate; only uncertainty polls). Idempotency classification ships as consumer guidance (interaction/idempotent-ish; `create` NOT idempotent — persist and reconcile; `restart` re-run at most once then re-discover). `restart()` carries reason+cause; `is_reachable` is the only data-without-raise surface besides declared attrs (probes exemption I11).

**Cancellation.** Subprocess discipline for all of the below is implemented once in `emuctl/_proc.py` (C6): Cancel = abandon, with bounded child cleanup: every emuctl-spawned child gets `terminate()` → 2 s bounded reap → `kill()` under a shielded deadline-bounded cleanup. Kill scope = **the bound instance's discriminator**: our-spawned PIDs, OR for vendor-owned processes the bound key as discriminator (window-title-equal bound key, internal instance name, per-VM vendor stop addressed by identity) — sweeps *without* a bound-instance discriminator (MEmu's `taskkill /IM MEmuHeadless.exe`-style, py-clash-bot's `_close_everything_memu`) remain banned. `CancelledError` propagates unmodified. `close()` idempotent + sweeps tracked stragglers; GC backstop warns, never kills.

**Finalizers.** `__del__` performs no cleanup — at most one DEBUG "implicit GC close" line; real shutdown is `aclose()`/`close()` + context managers (kills the py-clash-bot `__del__`-override disease).

**Logging (`emuctl/log.py`).** `logging.getLogger(__name__)` everywhere; one `NullHandler` on `"emuctl"`; never `basicConfig`, never handlers, never `print`/stderr, no custom Handlers; all dynamic strings pass `emuctl.log.sanitize()` (strips CR/LF/control chars — closes the log-injection angle); counts + ≤200-char excerpts at DEBUG, never bodies/binary; log XOR raise (never both); narration only at DEBUG, one line per decision.

---

## C3 — Identity, discovery, registry (`emuctl/_platforms.py`, `_serials.py`, `_identification.py`, `_discovery.py`, `backends/_registry.py`)

```python
class Platform(StrEnum): WINDOWS/MACOS/LINUX; current_platform() → Platform   # single mapping point

class BackendId(StrEnum):
    ADB = "adb"; MEMU = "memu"; BLUESTACKS = "bluestacks"
    GOOGLE_PLAY = "google-play"; AVD = "avd"; BLUESTACKS_AIR = "bluestacks-air"
    # from_string(): PEP 685-style normalization (case-fold, _/.→-), else BackendNotFoundError

@dataclass(frozen=True, slots=True)
class InstanceRef:
    """Unambiguous address. Identity = (backend, key) ONLY — transport serial/port,
    display labels, run state are observations on InstanceRecord, never identity."""
    backend: BackendId
    key: str                                  # validated at construction via spec.validate_key
    # parse(): CANONICAL 'backend:key' ONLY at v0 (max-simplicity ruling #60). A bare
    # serial/host:port/bare name is an InvalidArgumentError telling the caller to
    # prefix ('adb:127.0.0.1:5555') — no shape inference, no server ambiguity,
    # no match-named fan-out (deferred, see deferred list). I/O-free.

@dataclass(frozen=True, slots=True)
class InstanceRecord:                          # point-in-time observation; not identity
    ref: InstanceRef; running: bool; serial: str | None; details: Mapping[str, str]

@dataclass(frozen=True, slots=True)
class BackendSpec:
    backend: BackendId; extra: str | None      # None for adb (core)
    module: str; adapter_attr: str             # import target + port-ABC subclass attr
    supported_platforms: frozenset[Platform]   # THE sole platform-gating truth (adapters
                                               # do not declare platforms; consumed pre-import)
    dep_modules: tuple[str, ...]               # find_spec'd, never imported-side-effect
    description: str; key_semantics: str; validate_key: Callable[[str], None]

_SPEC_TABLE: {ADB: full spec (extra=None, ALL platforms), MEMU: pymemuc dep, WINDOWS,
              BLUESTACKS: BS5, WINDOWS (HD-Player/HD-Adb.exe, MIM), 
              BLUESTACKS_AIR: macOS Apple-Silicon Air, v0 — same [bluestacks] extra,
              module emuctl.backends.bluestacks_air; /Applications/BlueStacks.app + bundled
              hd-adb (py-clash-bot precedent); key from bluestacks.conf (single instance;
              MimMeta synthesized when cold); renderer AUTO → VULKAN,
              GOOGLE_PLAY: None, AVD: None}

def get_registry() -> Mapping[BackendId, BackendSpec | None]: ...   # static; import-free
def available_backends() -> list[BackendSpec]: ...                  # platform ⊇ host AND deps resolve

def resolve_backend(backend: BackendId) -> BackendSpec:
    """THE single selection gate, fixed order:
    unknown id → BackendNotFoundError · spec None → BackendNotAvailableError
    (“not-implemented-this-version”) · platform → BackendNotAvailableError
    (“platform-unsupported”, names available alternatives) · deps → ExtrasMissingError.
    Raises BEFORE any import; imports nothing."""

def load_adapter(spec: BackendSpec) -> type[AsyncEmulatorController]:
    # the ONLY adapter importer (importlib.import_module + getattr); port-ABC
    # subclass verified; ExtrasMissingError, never raw ModuleNotFoundError

async def connect(target: InstanceRef | str, *, config: EmuctlConfig | None = None
                  ) -> AsyncEmulatorController: ...
async def connect_sync(...) -> "EmulatorController": ...   # identical signature; C5's facade
# target = a canonical ref ONLY ('backend:key' string or InstanceRef) — no bare shapes,
# no backend=None auto-resolution at v0 (max-simplicity ruling #60); programmatic
# selection = InstanceRef(BackendId.X, key). Selection refusals raise HERE, at
# construction. match-named resolution (resolve_target) is DEFERRED — see deferred list.
# (no `logger=` parameter — logging is stdlib-name-based per C2; consumers attach
#  handlers/filters to the "emuctl" logger if they want per-session sinks)
```

**Discovery.**

```python
# BackendDiscovery — a module-level CONVENTION, not a type artifact: each backend
# module implements these async functions (satisfaction verified by the registry
# conformance test; a Protocol class adds nothing the test doesn't already check):
#   list_instances(config) -> tuple[InstanceRecord, ...]   read-only, bounded
#   get_instance(config, ref) -> InstanceRecord | None     None = absent (stopped VMs return records)
# A backend's unavailability/reason arrives as a BackendStatus row DERIVED from the
# observed failure itself — no dedicated vendor_status hook per module.


async def discover(backends=None, *, config=None, offline: bool = False) -> DiscoveryReport: ...


# read-only, bounded, per-backend isolated (a dead vendor = BackendStatus row, not failure);
# raises only for unknown ids. Deterministic ordering; no caching; offline skips vendor
# process probes (config-store enumeration still runs). Cold `adb devices` may start the
# local ADB server (documented exception).
```

**Boundary sentence:** *discovery answers "what plausibly exists and is it up"; it guarantees nothing about driveability — readiness begins at construction and the boot primitive, verified never assumed.*

**Serial validation (public security control).** `emuctl.is_valid_serial(s) -> bool` / `emuctl.validate_serial(s) -> None` (raises `InvalidSerialError`), allowlist reject-by-default, three enforcement layers: identity construction + `connect()` + **per-transport-call re-check** (pure, zero I/O). `transport_id:N` allowed (documented); `""`/whitespace = error (never laundered into "auto" per py-clash-bot precedent); no case folding; new shapes require security review + pinned tests. Core transport builds argv only, never `shell=True`.

**Identity lifetime (locked).** Survives stop/start; persists across reboots (vendor stores are the authority); delete/disconnect ⇒ ref goes **stale**, ops raise `InstanceNotFoundError`; `(bluestacks, X)` never equals `(adb, X)` (private-server fact); `emulator-NNNN` serials are positional, vendor keys referential (documented per backend in `key_semantics`); no case folding; no normalization beyond strip.

**Multi-instance policy (M-A).** N independent controllers per process supported; no global registry/locks/kills. Sync facade: independent facades from **different threads** OK; **one facade shared across threads unsupported** — `close()` is the ONLY facade member sanctioned from any thread (a foreign thread's other facade op is a documented misuse, not a race emuctl adjudicates). Same-ref parallel ops, cross-process coordination, shared-vendor-tooling contention (BlueStacks MIM), two-backend-dedup: explicitly not promised (documented, not solved). Agents key RETRY policy on the JSON error `code`, never on the exit number — exit numbers are shell control flow only (`not_ready` transient, `controller_closed` deterministic, share exit 6 by design).

---

## C4 — Data + config

**Screenshot (normative wording).** `screenshot() -> bytes` returns a complete, well-formed PNG of the current framebuffer, as exactly `bytes`. Pure bytes (INV-S1); PNG signature + IHDR provable by the core helper (INV-S2, enforced adapter-side; corruption → `VendorCommandError(output_reason="unusable_output")`); no vendor noise, no base64, no regex repair ever crosses the adapter (INV-S3); no transcoding, never re-encoded (INV-S4); one attempt per call, no internal retry loop — pacing is the consumer's (INV-S5, kills the `while True` disease); byte-equality is not identifiable — compare pixels or dims (INV-S6); rotation not owned (INV-S7).

```python
def png_dimensions(data: bytes) -> tuple[int, int]:     # emuctl/png.py — core, stdlib-only
    # IHDR read; raises ValueError if absent. Replaces consumers' cv2-decode-for-shape.
```

**Coordinates (normative).** INV-C1: `click/swipe` coordinates are the screenshot PNG's top-left-origin pixel space (ADB display pixel space). Coordinate frame stable within a session; the library never re-scales/re-bases. Geometry is *data* (config applied at configure-time); *actual* geometry is observable (`png_dimensions(screenshot())`); target/actual mismatch is the consumer's detection — no auto-fix, no scaling shim (silent-fallback lesson applies).

**Config models (`emuctl/config/model.py` — frozen dataclasses, `slots=True`, validating `__post_init__`; an invalid instance cannot exist; failures → `ConfigValidationError(path, value, layer, expected)`):**

```python
class RendererMode(enum.Enum): AUTO/OPENGL/DIRECTX/VULKAN      # canonical per semantic, per backend

DisplayConfig(width, height, dpi, fps, renderer)               # all None-defaults = leave as-is
ResourceConfig(cpus=None, memory_mb=None, cpu_cap_pct=None)    # None = vendor's current value
AdbConfig(path=None, host="127.0.0.1", port=5037, serial=None) # None path → C's discovery
MemuConfig(memuc_path=None, vm_name=None, settings=MemuSettings(hide_toolbar, turbo,
           audio, window_mode, scale_percent))
BlueStacksConfig(adb_server_port=5041, install_dir=None, conf_path=None,
                 instance_name=None, settings=BlueStacksSettings(vsync, high_fps))
LogConfig(level="warning", file=None)          # consumed ONLY by the CLI surface; the
                                               # library NEVER configures handlers (C2)

EmuctlConfig(adb=…, display=…, resources=…, log=…, memu=None, bluestacks=None)
                                   # ROOT contract object; C.connect/E.connect_sync take it.
                                   # (No `backend` field — the target ref names the backend
                                   #  at v0; a hint field re-enters with resolve_target.)
CreateSpec(name: str, android_version: str | None = None)
                                   # A's create() payload; name REQUIRED at v0 (the
                                   # backend-unique auto-naming scheme is deferred with
                                   # its first consumer);
                                   # android_version=None ⇒ default image (MEmu "136"/Android 13)
# Range checks: ports 1..65535; set geometry >0; cpus≥1; memory_mb>0; cpu_cap_pct,
# scale_percent 1..100; level ∈ stdlib names.
```

Backend subpackages own **translation tables** (canonical → vendor keys/value encodings, imported only with the adapter): MEmu `graphics_render_mode` 0/1-ints, `is_customed_resolution=1` implied; BlueStacks `"gl"/"dx"/"vlcn"`, custom-res token `"419 x 633"` absorbed. Unknown vendor encoding → `ConfigValidationError` (never silent fallback — both py-clash-bot silent coercions killed). `AUTO` resolves: MEmu→DIRECTX; BlueStacks→DIRECTX(Win)/VULKAN(mac); ADB→no renderer semantics. **AUTO is emuctl's vendor default, not the reference consumer's** — a migrating pipeline validated on a specific renderer passes `renderer=` explicitly.

```python
ConfigSpec = Mapping[str, str | int | float | bool | None]
# Normative type ALIAS for configure()'s overlay (a dataclass wrapper was cut in the
# slop round — the semantics carry the weight): keys are CANONICAL config leaves
# ("display.width", "display.renderer", "resources.cpus", "memu.settings.hide_toolbar",
# … — same vocabulary as the model sections); values are strict typed primitives.
# Adapters translate entries via their translation table; an unknown canonical key
# raises InvalidArgumentError (caller misuse — not a tolerance case like file keys);
# an entry the backend can't translate deploys the C4 gate instead.
```

**Precedence (normative chain).**

```
1 flags (structured; E's CLI binds layer 1 only) → 2 EMUCTL_* env (flat leaf-only grammar:
  EMUCTL_DISPLAY_WIDTH, EMUCTL_MEMU_VM_NAME…; strict typed parse; unknown
  names → ConfigWarning) → 3 project layer (normative walk): cwd→parent dirs; PER DIR,
  emuctl.toml wins over [tool.emuctl] in that dir; the FIRST dir containing either is
  authoritative and outer dirs are ignored (monorepo-survivable); within the chosen
  file, leaf-first-wins → 4 user XDG (~/.config or %APPDATA%\emuctl)
→ 5 dataclass defaults → 6 vendor-binary discovery ONLY for still-None paths (C, at selection)
```

```python
def resolve_config(flags=None, env=os.environ, cwd=Path.cwd()) -> ResolvedConfig: ...


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    config: EmuctlConfig
    warnings: tuple[ConfigWarning, ...]


# (provenance tracking CUT — zero v0 consumers; "which layer set this leaf" is a
#  DEBUG-log question, not a data model; re-enters with a `config` verb if asked)
```

Escape hatches: `--config-file` (replaces 3+4), `--no-config` (skips both) — FLAGS ONLY; env twins were cut in the slop round (flags cover every named consumer; re-addable on demand). Leaf-first-wins merge (`dataclasses.replace` fold); pure function of (flags, env, cwd); programmatic consumers may skip files entirely (`EmuctlConfig(...)` direct) — the typed object is the contract, files are sugar. stdlib-only (`tomllib`).

**Resources rule (normative).** Any shipped file is addressed exclusively through `importlib.resources` (`emuctl.resources`; real-path needs via `as_file`) — never relative paths, `os.getcwd()`, or `__file__`-arithmetic (kills the `memu_config.json` CWD bug permanently; contract test enforces the singularity). User-override order per resource: explicit path → project profiles dir → user profiles dir → bundled (immutable, always present).

**Config↔capability gate (construction time).** `ensure_config_supported(cfg, features)` against A's declared set — `[display]`/`[resources]`/`[memu.settings]`/`[bluestacks.settings]` each require `configure`; refusal = `FeatureNotSupportedError(feature, detail)` — typed, never silent-ignore, never hand-written. Plain-ADB + `[display]` → typed refusal (adb never declares CONFIGURE). `VULKAN` on MEmu → same gate via missing table entry. Load-time validates structure only (types/ranges/versions) — capability truth binds at selection (the target ref names the backend), so the gate runs at construction against the selected adapter's declared set.

**Evolution.** `schema_version` optional, absent ⇒ 1; unknown major → `ConfigError(reason="bad_version")` carrying the found version in `value` and naming the installed one in the message. Unknown **keys** → warn+ignore with nearest-key hint (never error, never silent); unknown **values** → always strict error. New fields/members in any minor; renames get a one-minor warning alias then major-drop; removals major-only with replacement named. Emission additive-only per minor; reading tolerant; round-trip lossy-on-unknown, loudly.

---

## C5 — Surfaces (sync facade + CLI)

**Construction (the one public surface):**

```python
au  = emuctl.connect(target, *, config=None) -> AsyncEmulatorController
emu = emuctl.connect_sync(target, *, config=None) -> EmulatorController
```

`target` required and CANONICAL (`'backend:key'` string or `InstanceRef` — no bare shapes at v0; programmatic selection is `InstanceRef(BackendId.X, key)`); selection refusals raise at `connect*()` time, never first-call. The `backend=` parameter was CUT (max-simplicity ruling #60) — the ref carries the backend; the spec table carries the platform gate. One construction path, one error surface; both views share one controller state.

**Sync facade (`emuctl/_sync.py` — anyio confined to this module; core below it is pure asyncio):**

```python
class EmulatorController:                       # built only by connect_sync(); not user-constructed
    @property
    def aio(self) -> AsyncEmulatorController: ...   # native view; NEVER starts the portal

    # every op: one-line delegation through _call(); kwargs forwarded verbatim —
    restart(*, app=None, ready=None, timeout=DEFAULT) / click / swipe / key / text /
    screenshot(timeout=…) -> bytes / is_reachable() -> ProbeResult / install_apk /
    start_app / is_app_installed / create(spec: CreateSpec) -> InstanceRef (same shape
    as A's) / start / stop / configure(spec: ConfigSpec | None) / aclose / close /
    __enter__/__exit__ (start nothing; boot stays explicit)
    @property features · has_feature()           # portal-free (declared, static)

class ProbeResult(NamedTuple):                   # zero runtime cost; better typing than tuple
    ok: bool; reason: str                        # reason = pinned token ("ok" | "no-adb" |
    """Never truthy-check; check .ok. (False, "") is truthy as a 2-tuple."""     # …")
```

**ReadyCheck normalization (locked; kills the "standing open item"):** every op's `timeout` kwarg is forwarded verbatim. Sync predicate → executed in a portal **worker thread** (never the portal loop thread) against the facade object itself, so its internal `emulator.screenshot()`-style calls run the normal funnel and cannot re-enter the loop; async predicate (returns awaitable) → awaited on the portal loop with the async controller. Every evaluation runs under restart's remaining budget; the predicate MAY interact (launch-and-verify patterns like the clan-voyage popup dismiss are sanctioned); a predicate that raises propagates as restart's failure (`EmulatorNotReadyError(cause=…)`). The bridge test suite pins the deadlock scenario.

Bridge mechanics (locked):

- **One lazily-started portal per facade** (`start_blocking_portal(backend="asyncio")` — pinned asyncio; thread named `emuctl-portal`); `.aio`-only usage creates zero threads; per-call portals rejected (fork cost per click) as do per-session and process-global shared portals (dual cleanup stories / cross-instance coupling).
- `_call` is the only funnel; running-loop detection fires before portal work → `SyncCallInsideEventLoopError` with the `await emulator.aio.<op>()` escape hatch in the message; no trio-sniffing at v0.
- `close()`: idempotent, **cancel-first** (anyio CM-exit cancels pending tasks; an abandoned in-flight caller re-raises `ControllerClosedError`; documented honesty: *an abandoned op may have executed anyway*). Facade reusable after a cancelled call; permanently closed after `close()`. `close()` is the only member sanctioned from any thread (see C3's M-A wording); every other member called from a foreign thread is a documented misuse.
- Cancellation = abandon: interrupt during a sync call leaves the portal task running to its own bounded timeout; facade not poisoned. Interrupted `restart()` may leave ambiguous vendor state — caller re-verifies/re-discovers (`aclose` does not auto-stop the emulator).
- `__del__` = no-op; no atexit; **facades unpicklable — construct inside worker processes** (document-only, no defensive machinery).
- Parity: every public `.aio` op has a sync twin with identical kwargs; exceptions cross the portal as the SAME class/payloads/error_code (no bridge rewrapping).

**py-clash-bot migration shape (verified against worker.py):**

```python
emu = emuctl.connect_sync(jobs["instance_name"], backend="memu",
                          config=EmuctlConfig(display=DisplayConfig(renderer=…)))
emu.restart(app=CLASH_ROYALE_PACKAGE, ready=check_if_on_clash_main_menu)
# (their session file-logging attaches a handler to the "emuctl" logger — no logger=)
```

The four-arm construction ladder + `verify_memu_installation()` + registry import map onto `connect_sync` (typed refusals at construction); `screenshot()` → one-line `cv2.imdecode` shim in their adapter until `emuctl[image]`; **click fidelity, stated honestly:** `click(x, y)`-shaped call sites port directly, but sites calling `click(x_coord=…, y_coord=…)` by keyword or passing `clicks/interval` positionally get a one-time sed in their shim, and the click `interval` default is `0.0` vs their MEmu adapter's `0.1` — an explicit behavioral decision to document, not a silent one (`EmulatorNotReadyError`'s name survives; `install_apk` becomes introspectable (`FeatureNotSupportedError` + probe rows); `stop_worker_process()`'s hard-kill shutdown model is untouched by design (portal thread dies with the process; cancel=abandon is the documented semantics). **Renderer note:** `AUTO` is emuctl's vendor default, NOT py-clash-bot's job-validated default — the migrated worker passes the configured renderer explicitly (as the snippet does).

**CLI (`emuctl[cli]`; console script in the core distribution — pip#9726 — lazy-click stub exits 3):**

```
emuctl [--config PATH | --no-config] [--json]
       # TARGET arity = canonical 'backend:key' ref (e.g. adb:127.0.0.1:5555,
       # memu:pyclashbot-136, bluestacks-air:conf); arities pinned PER VERB:
       # lifecycle/interaction = exactly 1; probe = 0..1; list = 0..1 (subcommand)

  list                                  # one bare JSON array / human table; rows carry
                                        # backend/serial/running/platform fields to filter on
  probe TARGET                          # self-probe (no target) is v0.1 — not locked
  screenshot TARGET (-o FILE | --stdout) # EXACTLY ONE of -o/--stdout, else usage exit 2;
                                         # no default path; --json+--stdout ⇒ usage
  click TARGET X Y [--count N] [--interval F]
  swipe TARGET X1 Y1 X2 Y2 [--duration-ms N]
  key TARGET KEYCODE                     # ^KEYCODE_[A-Z_]+$ | bare digits ⇒ keyevent;
                                         # anything else ⇒ usage exit 2 (no silent text)
  text TARGET VALUE                      # input text — digits/PINs expressible here
  app start|installed TARGET PACKAGE
  start | stop | restart TARGET [--app PKG] [--timeout S]
  configure TARGET set KEY VALUE         # builds a one-key ConfigSpec overlay
  version [--json]
```

- Generic verbs only, `TARGET` positional = canonical `backend:key` ref (no bare serials/inference; malformed shape ⇒ exit 2 telling the caller to prefix; well-formed but absent ⇒ exit 4). `create`, clone/copy/export/import are deliberately excluded from v0 verbs (v0.1 club — with C's declarative descriptor).
- CLI `restart` uses backend-default readiness (a ready-check is not CLI-expressible); per-op `--timeout` where a verb offers it; no global deadline knob (v0 defaults are the agent-friendly ones).
- **Output contract:** stdout = results only — one JSON doc with `"schema_version": 1`; `list` = bare array; raw PNG bytes for `screenshot --stdout` (the ONLY raw-bytes path); stdout on error is empty. stderr = human text, logs, error objects (JSON even without `--json`): `{"error": {"code", "exit", "message", "suggestion"}}`. One mapping site (`emuctl/cli/_exitmap.py`); unmapped ⇒ 9; CI drift test walks B's exception tree. **Pipe discipline:** `screenshot --stdout` closes cleanly on `BrokenPipeError` (restore SIGPIPE default, exit 141, no traceback, no stderr noise — documented beside 130); reading raw bytes from a slow sink is never misreported as a timeout.
- `-o FILE` overwrites an existing file — documented, not gated (v0 has no destructive-enough verb to earn a `--yes` gate; the no-prompts promise makes gating moot).
- **Agent determinism:** no prompts, ever; stdin never read; non-TTY ⇒ no color, no pagination; sorted, locale-invariant JSON; no timestamps in v0; additive-only output evolution (`"schema_version"` anchors it); argv/output shapes discoverable via `--help` (a hand-maintained schema subcommand was cut — one less spec to keep in lockstep); agents key retry policy on `error.code`, never on the exit number.
- Entry: `main()` refuses to run inside an event loop (exit 2, suggests `await emuctl.cli.amain(argv)`); `asyncio.run(amain())` one loop per invocation; KeyboardInterrupt ⇒ 130 always, no traceback; SIGTERM untrapped (OS-kill semantics preserved). `version --json` = `{"schema_version":1, "emuctl_version":"x.y.z"}`.

**Probe output (contractive shape):** schema_version 1; backend; target (C's identity fields); platform (os/supported); state (adb_state/reachable); features = A's coarse CapabilityFlag vocabulary, sorted by flag name — uniform rows `{"flag": "apps", "present": true}` / `{"flag": "apk_install", "present": false}` (maturity rendering deferred with `EXPERIMENTAL`; honest per-vendor gap tables survive — Nitric precedent). TARGET required at v0 (self-probe v0.1). Error suggestions name pasteable commands.

---

## C6 — Codebase organization (locked)

The sharing taxonomy, module layout, and their enforcement — locked because adapter maintainability was an explicit goal, and left-as-convention is how the py-clash-bot emulators layer grew `is_macos()` branches and 1,480-line adapters.

**Sharing tiers — exactly four, with one promotion path:**

| Tier | Content | Lives in |
|---|---|---|
| Universal | port ABC, MechanicsTable, errors, timing/waits, process executor, serials, identity, discovery plumbing, config, platforms, png | core (`emuctl/`, never `emuctl.backends.*`) |
| ADB-family | `AdbController` — the interaction primitives `_vendor_run` scoping, server-verb exclusion list | core (port-adjacent; all ADB-speaking adapters inherit; no second middle layer for MEmu's memuc-transport) |
| Vendor-family | helpers wanted by 2+ adapters OF THE SAME vendor family (BlueStacks conf parsing, MimMetaData synth, per-platform path discovery, renderer normalization, port-derived serials) | the backend's package, `_`-private (`emuctl.backends.bluestacks._conf` …) |
| Coincidental | a helper 2+ UNRELATED families both want | **promoted to core** — never a lateral import |

**Subprocess discipline lives in one place:** `emuctl/_proc.py`'s internal async executor — bounded `timeout` → `OperationTimeoutError`, cancel-scoped PID kills with bounded reap, argv-only (never `shell=True`), DEBUG argv logging, sanitized strings, Windows console suppression (`CREATE_NO_WINDOW`; the pymemuc lesson). Every adapter `_vendor_run` implementor and every vendor-process probe calls exactly this. It is **internal** — not exported as a consumer utility (parked in the deferred list; a consumer-facing re-export re-enters when a caller names it). It carries one private injection seam (transport-constructor-level, not a plugin registry) where the recorded-fixture test tier (research/07 layer 3) replays real subprocess transcripts.

**Module layout (sketch):**

```
emuctl/
  port.py  errors.py  timing.py  waits.py  png.py  log.py
  _proc.py  _serials.py  _identification.py  _discovery.py  _platforms.py
  config/model.py
  backends/_registry.py        # spec table + lazy loader (core-owned)
  backends/adb.py              # plain-Adb adapter (core install; extra=None)
  backends/memu.py             # FLAT while it has one concrete adapter
  backends/bluestacks/         # PACKAGE — second family adapter triggered it
    __init__.py  bs5.py  air.py
    _conf.py  _meta.py  _paths.py
```

**Layout rules:**
1. **Lazy packages:** a backend becomes a package only when its family gains a second concrete adapter (BlueStacks triggers this day one via Air; MEmu stays a module; `_`-modules are family-private).
2. **Import direction is a DAG:** `core ← any backend`; never sideways between backend families; never a family reaching into another family's `_`-private modules.
3. **Only the spec table names import-facing paths:** everything else under `emuctl.backends.*` is implementation; dynamic adapter imports happen ONLY via `load_adapter` (spec-driven), never ad-hoc.
4. **Extra-boundary promotion:** sharing that would cross an extras boundary (`[memu]` code wanted by `[bluestacks]`) is, by definition, the promotion signal — it goes to core so `[extra]` installs stay minimal.

**Enforcement (in CI + `make lint`, not vibes):**
- **import-linter** (dev dependency-group; wired into pre-commit + CI): a `layers` contract asserting `emuctl.backends → emuctl` only (core never imports backends statically — the lazy loader's dynamic import is deliberately invisible to it), and an `independence` contract over the backend sibling packages (`memu`, `bluestacks`, `google_play`, `avd` — no sideways imports, which also enforces rule 1's package-encapsulation and rule 4's promotion discipline, since a would-be sideways import must instead be promoted and pass the layers contract).
- **ruff TID251 `banned-api`** (ban `subprocess` project-wide, `_proc.py` exempt via `per-file-ignores`) + the `flake8-bandit` S-rules (S602/S603/S605/S607) for shell=True family — caveat recorded in research/09: the exemption disables all of TID251 in `_proc.py`, so banned-api stays single-entry; the conformance AST-walk below remains the backstop.
- **Release-time API discipline:** griffe `check` against the last release tag in the release workflow (additive-only evolution promises); commitizen `cz check` for conventional-commit gates alongside python-semantic-release.
- **No editor/agent hooks** — hooks are fail-open and per-tool (research/09 #10); pre-commit + CI are the enforcement of record.
- **Conformance suite assertions:** `subprocess` is imported only inside `emuctl/_proc.py` (+ stdlib test fakes); dynamic imports occur only through `load_adapter`; each spec row's module exists and exports its `adapter_attr` class + the BackendDiscovery functions.

---

## Locked cross-contract resolutions (the hub arbitration record)

| # | Collision | Ruling |
|---|---|---|
| 1 | A wanted raise-style `is_reachable()`; E/py-clash-bot/B said probes return data | **probes return data** (I11) — refined in adversarial review (#36) to a `ProbeResult(ok, reason)` NamedTuple with pinned tokens |
| 2 | `MissingDependencyError` (B) vs `ExtrasMissingError` (C) | **`ExtrasMissingError`** |
| 3 | `SerialValidationError` (C) vs `InvalidSerialError` (B) | **`InvalidSerialError`** (parent InvalidArgumentError) |
| 4 | `DeviceNotFoundError` + `InstanceNotFoundError` overlap | **one class: `InstanceNotFoundError`**; mid-op ADB loss → `DeviceStateError` |
| 5 | Screenshot-invalid dedicated class (D) | **no** — `VendorCommandError(output_reason="unusable_output")` |
| 6 | Sync facade owns the name `EmulatorController` (E) vs async ABC (A) | **async ABC = `AsyncEmulatorController`; facade = `EmulatorController`** |
| 7 | Construction entry shapes (E ctor vs C connect) | **`connect`/`connect_sync` with identical signatures; target required; no searching** |
| 8 | `create()` return (A: None vs C: ref) | **returns `InstanceRef`**; backend-scoped |
| 9 | Per-phase deadline settings (A/D) | **cut** — one op-level `timeout` distributes internally (T_LIFECYCLE default) |
| 10 | `VendorResult` payload (A) | **cut** — raw `bytes \| str`; failures = VendorCommandError |
| 11 | `Key` enum (A/D) | **cut** at v0 — `key(code: str \| int)` documented |
| 12 | `device_info()` (A) | **cut** at v0 (additive slot later) |
| 13 | `EmulatorSpec`/`ConfigEntry`/`ConfigSpec` mix | **`CreateSpec` + `ConfigSpec`** |
| 14 | Exit assignments (B vs E vs C drift) | **E's amended table frozen** (backend_not_found→2; sync_call_inside_loop→2; stale/missing→4; feature+backend availability→5; not_ready/controller_closed/ambiguous→6) |
| 15 | Where instance enumeration lives | **module-level `BackendDiscovery` seam — never a port method** |
| 16 | Docs-schema machinery (E) | **trim at v0** — `emuctl schema` + `"schema_version"` header only; no regen hooks |

**Adversarial-review rulings (2026-10-07, second session — applied inline above):**

| # | Finding | Ruling |
|---|---|---|
| 17 | Sync `ReadyCheck` cannot run on the portal loop thread (deadlock / `SyncCallInsideEventLoopError`) | **RESOLVED (was the standing open item):** sync predicates run in a portal *worker thread* against the reentrant facade proxy; async predicates await on the loop with the async controller; interacting predicates sanctioned; budget + raise-propagation pinned (C5) |
| 18 | `ConfigSpec` load-bearing in 3 contracts, undefined (both reviewers) | **defined normatively in C4** (canonical-leaf overlay; `configure(None)` re-applies bound config; unknown canonical key = `InvalidArgumentError`) |
| 19 | Per-op `timeout` missing from C1/C5 signatures vs C2's universal rule | **every I/O op carries `timeout=DEFAULT`**; explicit groups incl. `is_reachable`/discovery → query |
| 20 | "byte-identical click migration" claim false (kwargs names, positional args, interval 0.1→0.0) | **honest rewording** + interval delta documented as an explicit decision |
| 21 | `vendor`/`screenshot` bytes+`--json` holes; `--binary` position unparsable; SIGPIPE discipline missing | **exactly-one-of for screenshot; vendor output contract (raw/wrapped); flag-before-`--`; exit 141 pipe discipline; `-o` overwrite needs `--yes`** |
| 22 | Layer-3 (project config) merge ambiguous across dirs | **first-dir-containing-either is authoritative; per-dir emuctl.toml preference; leaf-merge within the chosen file** |
| 23 | Kill rule ("children we spawned") made BlueStacks' only workable stop illegal | **kill scope = bound-instance discriminator** (own PID / window-title-eq-key / internal name); undiscriminated sweeps stay banned (C2) |
| 24 | USB-alnum shape inference absorbs BlueStacks internal names (`Tiramisu64` → wrong backend) | **inference narrowed to `emulator-N` + host:port only**; serial-match in `resolve_target` before shape inference; USB shape never inferred |
| 25 | `is_running()`-style predicates violate invariant 3 on observation-transport failure | **predicates return bool only for OBSERVED negatives; failed observation raises** (C1) |
| 26 | Generic bodies never shown gating; `_MECHANIC_FOR_FLAG` normative-but-undefined | **MechanicsTable** (port-owned, concrete-in-MRO satisfaction rule); gate-first wording made explicit (C1) |
| 27 | `create()` collision undetectable without vendor-greeting parsing; `CreateSpec(name=None)` fixed-name = collision factory | **pre-check via discovery seam** (`get_instance`), collision → `InvalidArgumentError` exit 2; name=None ⇒ backend-unique name; "136" is the default IMAGE |
| 28 | BlueStacks restart needs `_attach` + mid-op key→serial re-resolution; "optional override" undersells it | **`_attach` seam added; restart override REQUIRED-in-MechanicsTable terms for MIM-style backends; override re-gates** (C1) |
| 29 | `PORT_VERSION` + reserved `_DEPENDS_ON` = mechanisms without requirements | **DELETED** (pushback against the epoch proposal — nothing consumes a port epoch; semver/changelog own deltas; re-addable when real) |
| 30 | `AUTO` renderer silently changes the reference consumer's validated default | **AUTO ≠ consumer's default — explicit pass documented in migration note** |
| 31 | "re-run at most once" contradicts the reference consumer's 5-restart net | **guidance gives the shape, not the count** |
| 32 | `LogConfig.file` vs library-never-configures-handlers | **LogConfig consumed only by the CLI surface** |
| 33 | "thread-safe close()" contradicts "one facade across threads unsupported" | **`close()` is the only foreign-thread-sanctioned member**; agents retry on `error.code` not exit numbers |
| 34 | `key` smart-parse fails both directions; digits inexpressible; no `text` verb | **split verbs: `key` = KEYCODE/digits-or-usage-2; `text TARGET VALUE` added** |
| 35 | `probe` self-probe shape undescribed; set-literal render typo; `version --json` ambiguous | **self-probe v0.1; feature rows are objects; version json fields pinned** |
| 36 | `is_reachable() -> tuple` vs NamedTuple | **NamedTuple `ProbeResult(ok, reason)` adopted** — zero runtime cost, named access, and `reason` becomes a pinned token set (never renders empty) |
| 37 | `InstanceRef.parse` omitting which ADB server | **kept, bound honestly: bare host:port = configured system server only; private-server backends need canonical refs; serial-match is resolve_target's path** |
| 38 | Result-return pattern instead of exceptions? | **exceptions stay the single failure convention** — Result thinking already lives in the taxonomy (typed payloads, `error_code`, `suggestion`) and the probes/data-return exemption; revisit only if batch operations appear, and then as a specific aggregated API, not a convention change |

**Slop-cut round rulings (2026-10-07, two cutting lenses, both concurred — cuts applied inline):**

| # | Cut | Ruling |
|---|---|---|
| 39 | `run_vendor_cmd` public surface + `VENDOR_PASSTHROUGH` flag + CLI `vendor` verb | **CUT** (zero consumers; `_vendor_run` internal seam survives; re-addable day-one-consumer) |
| 40 | `clone()` + CLONE flag + `_clone` | **DEFERRED to v0.1** entirely (declared-but-unconsumed slot) |
| 41 | `configure_get` + CLI `configure get` | **CUT** (the reference consumer's only config read is their own dead code) |
| 42 | `screen_size()` + STATE flag | **CUT** (`png_dimensions(screenshot())` is the normative geometry view; settle phase reworded) |
| 43 | public `is_running()` | **CUT** (`_is_running` stays a BOOT mechanic; probe/`InstanceRecord`/`is_reachable` are the observation surfaces) |
| 44 | `stop_app` + `app stop` | **CUT** (re-addable trivially) |
| 45 | port `__aenter__/__aexit__` + wrap-CM bridge machinery | **CUT** (no async CMs exist to cross; `aclose`/`close` + facade sync CM stay) |
| 46 | `CreateSpec(name=None)` auto-naming + CLI `create` verb | **CUT / v0.1** (name required; port method stays) |
| 47 | ConfigFileError + ConfigVersionError | **MERGED into ConfigError(reason)**; ConfigValidation stays distinct |
| 48 | all seven stdlib MI parents | **CUT** — pure `EmuctlError` tree (catch-compat had no consumer) |
| 49 | `ERROR_CODES` reverse map + public `payload_fields()` | **CUT** (drift test walks the class tree; `payload` ClassVar + generated `__str__` are the renderer contract) |
| 50 | per-instance `suggestion` baking | **CUT** — per-class templates interpolated at render site (two mechanically-derived classes keep template params) |
| 51 | exit 10 (cancelled) + `needs_confirmation` code | **CUT** (9 unmapped covers; nothing prompts). --yes/--deadline/--verbose flags CUT with it (`-o` overwrite documented) |
| 52 | `schema <verb>` (incl. `schema config`) | **CUT** (`--help` + docs; `schema_version` header KEPT as the additive anchor; disagreement resolved toward the peer) |
| 53 | `--deadline`/`--verbose`/env twins (`EMUCTL_CONFIG_FILE`, `EMUCTL_NO_CONFIG`) | **CUT** (flags only) |
| 54 | `list devices|instances|backends` | → **one `list`** (rows carry the discriminating fields) |
| 55 | `ConfigProvenance` | **CUT** (zero consumers; re-enters with a `config` verb if asked) |
| 56 | `AdbConfig.serial` | **CUT** (fourth selection channel nobody pinned) |
| 57 | `ConfigSpec` dataclass | → **type alias** (disagreement resolved toward the consumer lens: vocabulary kept, class ceremony gone) |
| 58 | "ReentrantSyncProxy" named mechanism | **CUT** — predicates receive the facade itself (scenario/budget/propagation/pinned test unchanged) |
| 59 | `backend_version` + `EXPERIMENTAL`/`maturity` + `SUPPORTED_PLATFORMS` on adapters + `logger=` params + Protocol artifact + `vendor_status()` hook + custom lint-rule mandate + `poll_interval` + `config_schema_version` field + query/config split | **CUT** (spec table is the sole platform truth; three-parity enforcement sites → two: wiring gate + conformance) |
| 60 | Bare-target resolution (match-named discovery, serial-match, backend=None auto) — user ruling "max simplicity" | **CUT for v0** — targets are canonical `backend:key` refs only (programmatic: `InstanceRef(backend, key)`); `resolve_target` DEFERRED; CLI `--backend` flag CUT; serial/shape inference GONE (parse = `InvalidArgumentError` for unprefixed shapes); `EmuctlConfig.backend` + `EMUCTL_BACKEND` CUT (no consumer under canonical refs) |
| 61 | BlueStacks universal adapter vs per-platform split (BS5 vs BlueStacks Air) | **SPLIT, Air ships v0** (user): `bluestacks` = Windows BS5 (HD-Player/MIM); `bluestacks-air` = macOS (v0 spec from py-clash-bot precedent: /Applications/BlueStacks.app, bundled hd-adb, bluestacks.conf key + MimMeta synth, AUTO→VULKAN); same `[bluestacks]` extra; both thin over the shared ADB base; standing research item: Air's identity/provider details at implementation time |
| 62 | `restart()` exposure (user) | **KEPT** — it is the named question: bounded boot-to-usable composition (stop → configure-while-stopped → attach → start → settle → app → ready) with one deadline and one honest `not_ready`; start/stop-only would push that policy into every consumer (the disease the port kills) |
| 63 | Shared shell callback: needed? public? | **YES internal, NOT public:** one internal async process executor (`emuctl/_proc.py`) carries the C2 discipline as code; `_vendor_run` implementors + discovery probes call it; private injection seam = the recorded-fixture test tier's plug point; consumer-facing re-export PARKED (deferred list) until a caller names it |
| 64 | Vendor-family shared code (modular between 2+, not all) — lock the organization? | **YES, locked as C6:** four sharing tiers, lazy package rule (package only when a family gains its 2nd adapter), DAG import direction, spec-table-owned import paths, extra-boundary promotion signal; enforcement = import-linter (layers + independence contracts) in dev-group/pre-commit/CI + conformance assertions (subprocess-only-in-_proc, load_adapter-only dynamic imports, spec rows export adapter class + discovery fns) |
| 65 | Enforcement tooling audit (research/09): hooks for agent feedback? | **NO hooks** (user): hooks are fail-open and per-tool — pre-commit + CI are the enforcement of record. Adopted into C6: ruff TID251 banned-api + S-rules (subprocess/shell discipline), commitizen `cz check` (conventional commits), griffe `check` at release (additive-only API). Confirmed bespoke: dynamic-import/spec-row/taxonomy/parity conformance stays pytest+AST — no 2026 tool exists; ty has no plugins by design, so the lint-plugin cut stands |

**Deliberately deferred (not lockable now, or cut-and-parked):** probed feature sets + `EXPERIMENTAL`/maturity graduation (v0.1), clone family (flag/method/mechanic — v0.1), CLI `create` + backend-unique auto-naming (v0.1), probe self-probe, match-named bare-name target resolution (`resolve_target`, bare-shape inference — returns with a bare-name caller; until then: canonical refs only), `run_vendor_cmd` public equivalent + `stop_app`/`screen_size`/`is_running` public surfaces (re-add on first named consumer), async-CM pair + `wrap_async_context_manager` machinery (v0.1, with stream CMs), `emuctl[image]` shape (BGR helper documents cv2 channel order), entry-point plugin backends, `uninstall`/push/GPS slots, config schema-v2 + `config_schema_version` field + provenance-tracked `config` verb, custom lint plugin, exit token `cancelled`, port-version epochs, `poll_interval` knob, env twins (`EMUCTL_CONFIG_FILE`/`EMUCTL_NO_CONFIG`), per-class catch-compat MI parents (re-add per consumer).

---

## What conformance must assert (offline; ties to research/07 + DESIGN §8.6)

1. Root-walk: every fake-adapter raise is a taxonomy class, correct code/payloads/stable str/picklable.
2. Timeout shape: op-level deadline honored under injected slowness; `OperationTimeoutError` payloads; no cancellation conflation.
3. Cancel shape: `CancelledError` unmodified, children killed+reaped by PID, no stragglers.
4. Logger silence: import + fake run adds nothing to root logger; no stdout.
5. Bridge identity: portal re-raises same class/payloads; parity (sync twin exists per op); running-loop detection; close() cancel-first; **ReadyCheck deadlock scenario** (sync predicate calling `emulator.screenshot()` from a worker-thread proxy runs to completion without `SyncCallInsideEventLoopError` or re-entrant portal deadlock).
6. Registry conformance: spec table is the SOLE platform truth (no adapter-declared platforms to compare); `[all]`/extras == concrete specs; BackendDiscovery convention satisfied per module; find_spec honesty.
7. Config: precedence fold purity; unknown-key warnings; gate refusals; resource-access singularity.
8. Screenshot PNG sniff against recorded byte fixtures (transport tier-3 when live).
9. Suggestion rendering: per-class templates interpolate payload keys at the CLI render site; no ctor-baked suggestions.
10. Organization (C6): import-linter layers + independence contracts pass; `subprocess` imported only in `emuctl/_proc.py`; dynamic imports occur only through `load_adapter`; every spec row's module exists and exports its adapter class + BackendDiscovery functions.
