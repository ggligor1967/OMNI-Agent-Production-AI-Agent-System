# crypto_utils.py Quality Ratchet

## Starting Coverage

`29.18%`

## Target

`>= 50%`

## Final Coverage

`99.57%`

## Tests Added

- `tests/test_crypto_utils_quality_ratchet.py`
- existing guard retained: `tests/test_md5_sweep.py`

## Security Behaviors Covered

- safe hash helpers and digest output paths
- unsupported MD5 remains rejected
- HMAC signing and verification for valid and invalid keys
- PBKDF2 and HKDF deterministic derivation with explicit salt/info
- password hash verification for valid, invalid, and malformed stored payloads
- AES-GCM round-trip encryption and authentication-tag mismatch rejection
- key wrap/unwrap round-trip and wrong-key rejection
- JWT encode/decode/verify for valid, malformed, invalid-signature, and expired tokens
- random token encoding variants and constant-time comparison
- `CUStore` audit/key-registry persistence behavior
- `/crypto/*` route registration, allowed hash algorithms, unsupported-algorithm rejection, decrypt error path, JWT endpoint, and stats endpoint
- exception and API error paths do not echo secret material

## Notes

- real defects fixed: none
