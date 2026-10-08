"""§6 — Backend implementations (one module per single-adapter family, one
package per multi-adapter family).

Adapter modules are imported ONLY via `emuctl.backends._registry.load_adapter`
— never statically from core, never ad-hoc. This package exposes no names
itself (no PEP 562 loader); core reaches the registry through function-local
imports only.
"""
