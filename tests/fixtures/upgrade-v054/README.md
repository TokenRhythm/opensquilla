# Complete v0.5.4 upgrade baseline

`sessions.sql` is synthetic, not a user database. It contains the two original
session/transcript DDL statements and **all 41 original migrations**, executed
from source commit `3877f9668c527a2a74c9b72bab160155669b040d` (v0.5.4).
The two differently named V010 migrations are separate IDs. No ledger rows were
invented, removed, or aliased. Chat rows are added by the preservation harness.

`manifest.json` pins the source commit, source file SHA256s, resulting SQL SHA256,
and complete ledger IDs/hashes. The SQL includes generated yoyo audit metadata;
regeneration timestamps may differ. It is a constructed legacy database, not
proof of an official installer upgrade or coverage of every real user profile.

Regenerate intentionally from a checkout containing that immutable Git object:

```sh
uv run python scripts/build_v054_upgrade_fixture.py
```

The runtime release gate uses only Python's standard library to load this frozen
SQL. It must not derive the baseline from the candidate's current migrations.
Keep the original rc3-shaped preservation case for older installation coverage.
