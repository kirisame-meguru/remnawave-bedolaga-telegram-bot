"""Guard rails for the alembic revision graph across upstream syncs.

This fork carries its own migrations on top of upstream's. Upstream numbers its
migrations sequentially as ``NNNN``; if a fork migration parks itself inside
that sequence it will eventually claim an id upstream also claims. Git merges
such a collision cleanly (the filenames differ) and the bot then crash-loops at
startup on ``MultipleHeads`` — or, worse, silently skips an upstream migration
whose id the DB is already stamped with.

These tests are cheap, need no database, and fail loudly right after an upstream
merge instead of in production.
"""

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent
VERSIONS_DIR = PROJECT_ROOT / 'migrations' / 'alembic' / 'versions'

# Fork-local migrations live in this id space. Upstream's sequential NNNN
# scheme can never generate it, so the two can never collide.
FORK_PREFIX = 'xb'

_REVISION_RE = re.compile(r"^revision(?:\s*:\s*str)?\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_DOWN_RE = re.compile(r"^down_revision(?:\s*:[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)


def _parse_revisions() -> dict[str, tuple[str | None, str]]:
    """Map revision id -> (down_revision, filename), parsed textually.

    Deliberately does not go through alembic: alembic silently tolerates some
    duplicate-id shapes, and we want to see the raw declarations.
    """
    revisions: dict[str, tuple[str | None, str]] = {}
    duplicates: list[str] = []
    for path in sorted(VERSIONS_DIR.glob('*.py')):
        source = path.read_text(encoding='utf-8')
        match = _REVISION_RE.search(source)
        if not match:
            continue
        revision = match.group(1)
        down_match = _DOWN_RE.search(source)
        down = down_match.group(1) if down_match else None
        if revision in revisions:
            duplicates.append(f'{revision}: {revisions[revision][1]} vs {path.name}')
        revisions[revision] = (down, path.name)
    assert not duplicates, 'duplicate alembic revision ids:\n  ' + '\n  '.join(duplicates)
    return revisions


def test_no_duplicate_revision_ids():
    """Two files claiming the same revision id is the collision that broke prod."""
    _parse_revisions()


def test_every_down_revision_resolves():
    revisions = _parse_revisions()
    dangling = [
        f'{rev} ({filename}) -> {down}'
        for rev, (down, filename) in revisions.items()
        if down is not None and down not in revisions
    ]
    assert not dangling, 'down_revision points at a missing revision:\n  ' + '\n  '.join(dangling)


def test_fork_migrations_stay_out_of_upstream_id_space():
    """Fork migrations must not use a bare numeric id upstream could also mint."""
    offenders = [
        f'{rev} ({filename})'
        for rev, (_, filename) in _parse_revisions().items()
        if filename.startswith(FORK_PREFIX) and not rev.startswith(FORK_PREFIX)
    ]
    assert not offenders, f'fork migrations must use {FORK_PREFIX!r}-prefixed revision ids:\n  ' + '\n  '.join(
        offenders
    )


def test_fork_branch_is_a_separate_head():
    """The fork branch must stay a head of its own.

    If a fork migration ever stops being a head it has been absorbed into
    upstream's chain, which is exactly the interleaving we are avoiding. Computed
    textually rather than via alembic: loading the graph through alembic execs
    every version module, and some upstream migrations import application code.
    """
    revisions = _parse_revisions()
    parents = {down for down, _ in revisions.values() if down is not None}
    heads = {rev for rev in revisions if rev not in parents}

    fork_heads = {h for h in heads if h.startswith(FORK_PREFIX)}
    assert fork_heads, f'no {FORK_PREFIX!r} revision among heads {sorted(heads)}'


def test_runner_uses_plural_heads():
    """With a fork branch present, ``upgrade head`` (singular) raises MultipleHeads."""
    source = (PROJECT_ROOT / 'app' / 'database' / 'migrations.py').read_text(encoding='utf-8')
    assert "command.upgrade, cfg, 'heads'" in source, (
        'app/database/migrations.py must run `alembic upgrade heads` (plural); '
        "'head' raises MultipleHeads once upstream adds migrations alongside the "
        'fork branch.'
    )
