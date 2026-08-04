# Security / key-derivation provider

This project ships **no** seed/key algorithms, immobilizer response derivation, or
decryption keys. Any such computation is delegated to a **provider** that you
supply, for hardware you are lawfully entitled to service.

This document describes the provider **interface only** — module names, method
names, and argument/return types. It does not describe, specify, or hint at any
algorithm. What a provider computes, and how, is entirely up to you.

If no provider is configured, the seam is **fail-closed**: the call raises
`NotImplementedError` (a `uds_local.security_provider.ProviderUnavailable`) with a
message pointing back here. The rest of the framework — CAN decode, UDS
session/DID/routine plumbing, firmware parsing, flashing that consumes a
user-supplied image and key — works without any provider.

## Configuring a provider

Resolution order, per surface:

1. **Environment variable.** Set `TM3_SECURITY_PROVIDER` to the import path of a
   module you provide, e.g.:

   ```bash
   export TM3_SECURITY_PROVIDER=my_company.tesla_security
   ```

   A value that cannot be imported raises loudly (it is a misconfiguration, not a
   silent fall-through).

2. **Local drop-in module** (used only when the environment variable is unset;
   both are gitignored so they never enter the tree):
   - security-access: `uds_local/security_impl.py`
   - key-derivation: `uds_local/immobilizer.py`

3. **Fail-closed** if neither is present.

A provider module may expose the callables directly at module level, or a factory
(`get_security_access_provider()` / `get_key_derivation_provider()`) that returns
an object exposing the same attributes.

## Surface 1 — security-access (UDS SecurityAccess)

Consumed by `uds_local.client` during the SecurityAccess seed→key exchange.

```python
def compute_key(algorithm: str, seed: bytes, kw: dict | None = None) -> bytes:
    """Return the key bytes for the given algorithm identifier and seed.

    `algorithm` is the identifier taken verbatim from the target node's security
    config (`NodeConfig.security_algorithm`). `seed` is the seed bytes returned by
    the ECU's SecurityAccess requestSeed. `kw` carries any per-node parameters
    from the node config (`NodeConfig.security_kw`). Return the key bytes to send
    in the SecurityAccess sendKey sub-function.

    Raise `ValueError` for an algorithm identifier you do not implement.
    """
```

That single callable is the entire required surface. The framework calls it as
`uds_local.security_provider.compute_key(algorithm, seed, kw)`.

## Surface 2 — key-derivation (immobilizer), optional

Used only by the DI bench tooling when answering an immobilizer challenge. This
surface is optional: if you never run that tooling you do not need to implement
it. A provider may expose any subset of the following names; any name not
provided remains fail-closed.

```python
def challenge_response(key: bytes, challenge: bytes) -> bytes:
    """Return the response bytes for a challenge, given a key. Bytes in, bytes out."""

def challenge_response_l04(key: bytes, challenge: bytes) -> bytes:
    """As above, for the runtime variant used by the DI bench. Bytes in, bytes out."""

def imm_setkey_response(key: bytes, salt: bytes) -> bytes:
    """Return the response bytes for a set-key/pairing exchange. Bytes in, bytes out."""

def immo_response(key: bytes, message: bytes = b"") -> bytes:
    """Return the response bytes for an arbitrary message. Bytes in, bytes out."""

def challenge_counter(challenge: bytes) -> int:
    """Return the integer counter carried in a challenge frame."""


class ImmoKey:
    """A stored key record. `.key_bytes -> bytes` is the only attribute the
    framework reads; define the rest however you like."""


class Keystore:
    """A lookup of board serial -> ImmoKey.

    def __init__(self, path: str | os.PathLike | None = None) -> None: ...
    def get(self, board_sn: str) -> ImmoKey | None: ...
    """


def resolve_di_key(store, channel, interface="socketcan",
                   node=None, explicit_key_hex=None) -> tuple[bytes | None, str | None, str]:
    """Resolve (key_bytes | None, board_sn | None, note) for the attached unit."""


def read_board_sn(channel, interface="socketcan", *args, **kwargs) -> str | None:
    """Read the unit's board serial over UDS, or None on failure."""
```

The framework imports these lazily from
`uds_local.security_provider` (e.g. `from uds_local.security_provider import
Keystore, challenge_response`); the seam resolves them from your provider at call
time and fails closed otherwise.

## Minimal skeleton

A provider that implements nothing is valid — every call simply fails closed:

```python
# my_company/tesla_security.py

def compute_key(algorithm: str, seed: bytes, kw: dict | None = None) -> bytes:
    raise NotImplementedError("supply your own security-access implementation")
```

Fill in the bodies with an implementation you are lawfully entitled to use.
