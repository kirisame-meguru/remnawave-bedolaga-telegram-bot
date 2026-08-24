# Fork-local alembic migrations

This fork (`feat/xbedolaga`) carries migrations upstream does not have. Upstream
numbers its migrations sequentially — `0099`, `0100`, `0101`, … — and will keep
consuming that range forever. Any fork migration that parks itself inside that
range is a landmine on the next upstream sync.

## What went wrong on 2026-07-31

A fork migration was authored as `0099_add_wl_traffic_to_subscriptions.py` with
`revision = '0099'`. Upstream then shipped `0099_add_platega_subscriptions.py`,
also `revision = '0099'`.

Git merged it cleanly — different filenames, no textual conflict. Alembic did
not:

```
alembic.script.revision.MultipleHeads: Multiple heads are present for given argument 'head'; 0099, 0103
```

The bot crash-looped on every boot. The second-order problem was worse: prod's
`alembic_version` read `0099` from the *fork's* migration, so simply deleting
the duplicate would have made alembic treat upstream's `0099` as already
applied and silently skip `create_table('platega_subscriptions')` — schema drift
with no error at all.

## The rule

**Fork migrations use the `xbNNNN` revision id space and live on their own
alembic branch.**

```python
revision: str = 'xb0001'
down_revision: Union[str, None] = '0098'          # the fork point, not upstream's tip
branch_labels: Union[str, Sequence[str], None] = ('xbedolaga',)
```

Name the file to match: `xb0001_add_wl_traffic_to_subscriptions.py`.

Two properties follow:

1. Upstream's sequential `NNNN` scheme can never mint an `xb`-prefixed id, so a
   revision-id collision is structurally impossible.
2. The fork branch hangs off the fork point, not off upstream's moving tip, so
   upstream's chain can advance arbitrarily far without the fork branch needing
   to be touched.

### Do not re-chain onto upstream's tip

The tempting alternative — keep one linear chain and re-point the fork
migration's `down_revision` at upstream's newest revision after each sync — is
the trap that caused the outage. Once the fork migration is applied and stamped,
re-parenting it makes it the single head again, and alembic concludes everything
below it is applied. The newly merged upstream migrations are then skipped
without a word.

## Consequence: `heads`, not `head`

With a fork branch present the graph legitimately has more than one head, so
migrations must be applied with the plural form:

```
alembic upgrade heads
```

`app/database/migrations.py` and the `make migrate` target already do this.
`alembic upgrade head` (singular) raises `MultipleHeads` as soon as upstream
adds a migration alongside the fork branch.

## After every upstream sync

`tests/test_alembic_revision_graph.py` enforces all of the above and runs in CI
via `.github/workflows/alembic-guard.yml`. It needs no database and no
dependencies beyond pytest:

```
pytest tests/test_alembic_revision_graph.py --noconftest
```

It fails on duplicate revision ids, on a `down_revision` pointing at nothing, on
a fork migration using a bare numeric id, on the fork branch losing its head
status, and on the runner reverting to singular `head`.

Also worth a look before deploying:

```
alembic heads    # expect upstream's tip AND the newest xbNNNN
```

### Upstream's `tests/database/test_migration_chain.py` is intentionally absent

Upstream added that file in 4.x; it asserts a **single** alembic head and walks
the graph from `get_current_head()` (singular). Both assumptions are the exact
opposite of the rule above, so the file is deleted on every sync rather than
kept red. Everything in it that still applies to a branched history —
duplicate revision ids, `down_revision` pointing at nothing — is already
covered branch-aware by `tests/test_alembic_revision_graph.py`. If a future
sync re-adds it, delete it again; do not "fix" it by re-chaining the fork
branch onto upstream's tip (see above for why that caused the outage).

## Deploying `xb0003` (or anything that re-keys on `remnawave_id`)

`xb0003` re-keys `traffic_dimension_samples` from the panel uuid to the numeric
panel id, reading the mapping out of `subscriptions.remnawave_id` /
`users.remnawave_id`. Upstream's `0104` only *adds* those columns — filling them
needs the panel, so it happens in `scripts/backfill_remnawave_ids.py`, which runs
**after** migrations. A plain `upgrade heads` on an install that predates 3.0.0
therefore runs `xb0003` while every id is still NULL, and the journal is dropped
instead of re-keyed (a coverage gap, i.e. quota that cannot be enforced until the
window refills).

So on an existing install, deploy in this order — the bot stopped throughout, so
no row is created with a NULL identity in between:

```
docker compose stop bot
docker compose run --rm --no-deps bot alembic upgrade 0106            # upstream branch only
docker compose run --rm --no-deps bot python -m scripts.backfill_remnawave_ids
docker compose run --rm --no-deps bot python -m scripts.backfill_remnawave_ids --apply
docker compose run --rm --no-deps bot alembic upgrade heads            # now xb0003 re-keys
docker compose up -d bot
```

Before the `--apply` step, read the dry-run report: `conflicts` must be 0 and
`ИТОГ` must say `полный`. After `upgrade heads`, the row count of
`traffic_dimension_samples` must be unchanged and `remnawave_id` fully populated —
that is the proof the journal survived rather than being silently discarded.

A fresh install needs none of this: there is no journal to carry.

## Adding a new fork migration

Take the next free `xbNNNN`, chain it onto the previous fork migration (not onto
anything upstream owns), and keep it idempotent where practical — deployments
that already ran an earlier variant of it should roll forward without manual
surgery.
