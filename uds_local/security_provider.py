"""Provider seam for UDS security-access and immobilizer key-derivation.

This published tree ships **no** seed/key algorithms, immobilizer response
derivation, or decryption keys. Every computation is delegated to a provider you
supply for hardware you are lawfully entitled to service. When no provider is
configured the seam is *fail-closed*: the call raises :class:`NotImplementedError`
pointing at ``docs/SECURITY_PROVIDER.md``.

Two provider surfaces are defined here:

* **security-access** — ``compute_key(algorithm, seed, kw) -> bytes`` for the UDS
  SecurityAccess seed→key exchange. Consumed by :mod:`uds_local.client`.
* **key-derivation** — an optional, interface-only hook for immobilizer response
  derivation and keystore access. Consumed by the DI bench tooling. The framework
  ships only the interface; there is no bundled implementation.

Resolution order for each surface:

1. If ``TM3_SECURITY_PROVIDER`` is set, import that module. A misconfigured value
   (unimportable module) raises loudly — it is not silently ignored.
2. Otherwise, try the conventional local drop-in module, if present:
   ``uds_local.security_impl`` (security-access) / ``uds_local.immobilizer``
   (key-derivation). Both are gitignored; absence is silent.
3. Otherwise, fail closed.

A provider module may expose either module-level callables matching the names
below, or a ``get_security_access_provider()`` / ``get_key_derivation_provider()``
factory returning an object with the same attributes. See
``docs/SECURITY_PROVIDER.md`` for the exact signatures (signatures only — the doc
describes no algorithm).
"""

from __future__ import annotations

import importlib
import os
from typing import Protocol, runtime_checkable

__all__ = [
    "SecurityAccessProvider",
    "KeyDerivationProvider",
    "compute_key",
    "get_security_access_provider",
    "get_key_derivation_provider",
    "ProviderUnavailable",
]

# The immobilizer key-derivation facade names (see ``_KD_FACADE`` below) are also
# importable from this module, e.g. ``from uds_local.security_provider import
# Keystore``. They are resolved lazily from the configured provider via
# ``__getattr__`` (PEP 562), so they are intentionally omitted from ``__all__``.

_ENV = "TM3_SECURITY_PROVIDER"
_DOC = "docs/SECURITY_PROVIDER.md"
_LOCAL_SECURITY_ACCESS = "uds_local.security_impl"
_LOCAL_KEY_DERIVATION = "uds_local.immobilizer"


class ProviderUnavailable(NotImplementedError):
    """Raised when a security/key-derivation computation is attempted but no
    provider is configured. This build ships no such algorithms by design."""


def _unavailable(what: str) -> ProviderUnavailable:
    return ProviderUnavailable(
        f"No {what} provider is configured. This build ships no seed/key or "
        f"immobilizer algorithms. Set the {_ENV} environment variable to an "
        f"importable module that implements the provider interface, or drop in a "
        f"local implementation module. See {_DOC}."
    )


# ---------------------------------------------------------------------------
# Interfaces (signatures only — no algorithm is described or implemented here)
# ---------------------------------------------------------------------------


@runtime_checkable
class SecurityAccessProvider(Protocol):
    """Computes a UDS SecurityAccess key from a seed for a named algorithm."""

    def compute_key(self, algorithm: str, seed: bytes, kw: dict | None = None) -> bytes:
        ...


@runtime_checkable
class KeyDerivationProvider(Protocol):
    """Optional immobilizer key-derivation hook. Interface only; no bundled impl.

    A provider module may expose any subset of these callables (see
    ``docs/SECURITY_PROVIDER.md``). Names not provided remain fail-closed.
    """

    def challenge_response(self, key: bytes, challenge: bytes) -> bytes:
        ...


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _import_provider_module(local_name: str):
    """Return the configured provider module, or None if the local drop-in is
    simply absent. A set-but-unimportable ``TM3_SECURITY_PROVIDER`` raises."""
    env_name = os.environ.get(_ENV)
    if env_name:
        try:
            return importlib.import_module(env_name)
        except ImportError as exc:  # misconfiguration — be loud
            raise ImportError(
                f"{_ENV}={env_name!r} could not be imported ({exc}). Point it at an "
                f"importable provider module or unset it. See {_DOC}."
            ) from exc
    try:
        return importlib.import_module(local_name)
    except ModuleNotFoundError as exc:
        if exc.name == local_name:  # the drop-in itself is absent — fail closed
            return None
        raise  # a real import error inside the drop-in must surface


_sa_cache: object | None = None
_kd_cache: object | None = None
_SENTINEL = object()


def get_security_access_provider() -> SecurityAccessProvider:
    """Return the configured security-access provider, or a fail-closed stub."""
    global _sa_cache
    if _sa_cache is _SENTINEL:
        return _FailClosedSecurityAccess()
    if _sa_cache is None:
        mod = _import_provider_module(_LOCAL_SECURITY_ACCESS)
        if mod is None:
            _sa_cache = _SENTINEL
            return _FailClosedSecurityAccess()
        if hasattr(mod, "get_security_access_provider"):
            _sa_cache = mod.get_security_access_provider()
        else:
            _sa_cache = mod  # duck-typed: exposes compute_key
    return _sa_cache  # type: ignore[return-value]


def get_key_derivation_provider() -> object:
    """Return the configured key-derivation provider module/object, or None."""
    global _kd_cache
    if _kd_cache is _SENTINEL:
        return None
    if _kd_cache is None:
        mod = _import_provider_module(_LOCAL_KEY_DERIVATION)
        if mod is None:
            _kd_cache = _SENTINEL
            return None
        if hasattr(mod, "get_key_derivation_provider"):
            _kd_cache = mod.get_key_derivation_provider()
        else:
            _kd_cache = mod
    return _kd_cache


class _FailClosedSecurityAccess:
    def compute_key(self, algorithm: str, seed: bytes, kw: dict | None = None) -> bytes:
        raise _unavailable("security-access key computation")


def compute_key(algorithm: str, seed: bytes, kw: dict | None = None) -> bytes:
    """Compute a UDS SecurityAccess key via the configured provider (fail-closed)."""
    provider = get_security_access_provider()
    fn = getattr(provider, "compute_key", None)
    if fn is None:
        raise _unavailable("security-access key computation")
    return fn(algorithm, seed, kw)


# ---------------------------------------------------------------------------
# Immobilizer key-derivation facade
#
# These names are resolved lazily from the configured key-derivation provider so
# that importing this module — and therefore the whole framework — succeeds with
# no provider present. Attribute access raises only when a name is actually used
# without a provider (callables/classes fail closed on call/instantiation).
# ---------------------------------------------------------------------------

_KD_FACADE = {
    "Keystore",
    "ImmoKey",
    "challenge_response",
    "challenge_response_l04",
    "challenge_counter",
    "imm_setkey_response",
    "immo_response",
    "resolve_di_key",
    "read_board_sn",
}


def _fail_closed(name: str):
    def _raise(*_args, **_kwargs):
        raise _unavailable(f"key-derivation ({name})")

    return _raise


def __getattr__(name: str):  # PEP 562 — lazy resolution of the facade names
    if name in _KD_FACADE:
        provider = get_key_derivation_provider()
        if provider is not None and hasattr(provider, name):
            return getattr(provider, name)
        return _fail_closed(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
