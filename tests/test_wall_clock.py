"""No business logic reads the wall clock (ADR 0005): every time-based behaviour runs on the
simulation clock. This scans `src/txrisk` for anything that reads, or waits on, real time, and
fails on any use that is not in `OPERATIONAL`.

`OPERATIONAL` is the documentation: each entry is a use that is about running the system, not about
what the data means, with the reason. Adding to it is a decision a reviewer should see. The audit log's timestamps (#15) will belong here too.
"""

import ast
import re
from collections import Counter
from pathlib import Path

import txrisk

SOURCE = Path(txrisk.__file__).parent

# What reads the wall clock or waits on it, as fully qualified names (`np.datetime64('now')` is
# checked separately).
FORBIDDEN_CALLS = {
    "datetime.datetime.now",
    "datetime.datetime.utcnow",
    "datetime.datetime.today",
    "datetime.datetime.fromtimestamp",
    "datetime.datetime.utcfromtimestamp",
    "datetime.date.today",
    "datetime.date.fromtimestamp",
    "time.time",
    "time.time_ns",
    "time.monotonic",
    "time.monotonic_ns",
    "time.perf_counter",
    "time.perf_counter_ns",
    "time.process_time",
    "time.localtime",
    "time.gmtime",
    "time.ctime",
    "time.asctime",
    "time.sleep",
    "pandas.Timestamp.now",
    "pandas.Timestamp.today",
    "pandas.Timestamp.utcnow",
    "sqlalchemy.func.now",
    "sqlalchemy.func.current_timestamp",
    "sqlalchemy.func.current_date",
    "sqlalchemy.func.clock_timestamp",
}
# What the database reads as "now", in SQL text (migrations and SQL in strings).
SQL_NOW = re.compile(
    r"\b(now\(\)|current_timestamp|current_date|clock_timestamp\(\)|localtimestamp"
    r"|transaction_timestamp\(\)|statement_timestamp\(\))",
    re.IGNORECASE,
)

# (file under src/txrisk, what it reads) -> why that is operational, not business logic.
OPERATIONAL: dict[tuple[str, str], str] = {
    ("store/backup.py", "datetime.datetime.now"): (
        "names a backup file by when the dump ran; no business decision reads it"
    ),
    ("events/worker.py", "time.monotonic"): (
        "a deadline on catching up with the SIM swap topic: a timeout on the broker, not a time "
        "in the data"
    ),
    ("events/replay.py", "time.monotonic"): (
        "paces a replay against real time: speed only controls how fast events are published, "
        "never their event times"
    ),
    ("events/replay.py", "time.sleep"): "waits between published events to keep the replay's pace",
    ("api/app.py", "datetime.datetime.now"): (
        "the wall clock handed to API key expiry and per-key rate limits: keys expire and "
        "requests are limited in real time, while scoring runs on the Transaction's event time"
    ),
    ("api/auth.py", "time.monotonic"): (
        "when the Keycloak signing keys were last fetched, to decide when to refresh them"
    ),
    ("cli.py", "datetime.datetime.now"): (
        "stamps a newly issued API key's creation and expiry in real time"
    ),
    ("store/database.py", "now()"): (
        "schema_migrations.applied_at records when a migration ran; not read by any logic"
    ),
}


def _qualified_names(tree: ast.Module) -> dict[str, str]:
    """What each imported name stands for, e.g. `from datetime import datetime as dt` gives
    {"dt": "datetime.datetime"} and `import pandas as pd` gives {"pd": "pandas"}."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _dotted(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id, *reversed(parts)]
    return None


def _qualified(node: ast.expr, names: dict[str, str]) -> str | None:
    parts = _dotted(node)
    if parts is None or parts[0] not in names:
        return None
    return ".".join([names[parts[0]], *parts[1:]])


def _docstrings(tree: ast.Module) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def clock_reads(source: str) -> list[str]:
    """What the module reads from real time: forbidden calls, as qualified names, and SQL "now"
    in its string constants (docstrings aside)."""
    tree = ast.parse(source)
    names = _qualified_names(tree)
    docstrings = _docstrings(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _qualified(node.func, names) == "numpy.datetime64":
            args = [a.value for a in node.args if isinstance(a, ast.Constant)]
            if "now" in args or "today" in args:
                found.append("numpy.datetime64('now')")
        elif isinstance(node, ast.Attribute | ast.Name):
            # A reference, not only a call: `Pacer(monotonic=time.monotonic)` reads the clock too.
            qualified = _qualified(node, names)
            if qualified in FORBIDDEN_CALLS:
                found.append(qualified)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and (sql := SQL_NOW.search(node.value)):
                found.append(sql.group(1).lower())
    return found


def scan() -> Counter[tuple[str, str]]:
    found: Counter[tuple[str, str]] = Counter()
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        for read in clock_reads(path.read_text(encoding="utf-8")):
            found[(relative, read)] += 1
    for path in sorted(SOURCE.rglob("*.sql")):
        relative = path.relative_to(SOURCE).as_posix()
        for match in SQL_NOW.finditer(_without_comments(path.read_text(encoding="utf-8"))):
            found[(relative, match.group(1).lower())] += 1
    return found


def _without_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def test_no_business_logic_reads_the_wall_clock():
    unexplained = {use: n for use, n in scan().items() if use not in OPERATIONAL}

    assert not unexplained, (
        "these read the wall clock; business logic must use the simulation clock (ADR 0005). "
        f"If a use is operational, add it to OPERATIONAL with the reason: {unexplained}"
    )


def test_every_operational_use_is_still_there():
    """A stale allow-list would excuse a use that is gone, and a later one in its place."""
    found = scan()

    stale = [use for use in OPERATIONAL if use not in found]

    assert not stale, f"no longer used, remove from OPERATIONAL: {stale}"


def test_the_scan_catches_every_way_of_reading_the_clock():
    reads = {
        "from datetime import datetime\ndatetime.now()": "datetime.datetime.now",
        "import datetime\ndatetime.datetime.utcnow()": "datetime.datetime.utcnow",
        "import datetime as dt\ndt.date.today()": "datetime.date.today",
        "from datetime import date as d\nd.today()": "datetime.date.today",
        "import time\ntime.time()": "time.time",
        "import time\nclock = time.monotonic": "time.monotonic",
        "from time import monotonic\nmonotonic()": "time.monotonic",
        "from time import sleep as nap\nnap(1)": "time.sleep",
        "import pandas as pd\npd.Timestamp.now()": "pandas.Timestamp.now",
        "import numpy as np\nnp.datetime64('now')": "numpy.datetime64('now')",
        "import sqlalchemy as sa\nsa.func.now()": "sqlalchemy.func.now",
        "q = 'SELECT now()'": "now()",
        "q = 'DEFAULT CURRENT_TIMESTAMP'": "current_timestamp",
    }
    for source, expected in reads.items():
        assert clock_reads(source) == [expected], source


def test_the_scan_leaves_the_simulation_clock_and_prose_alone():
    quiet = [
        "clock.now()",
        "self._clock.now()",
        "from datetime import datetime\ndatetime(2026, 1, 1)",
        "from datetime import datetime\ndatetime.fromisoformat('2026-01-01')",
        "import numpy as np\nnp.datetime64('2026-01-01')",
        '"""Never call now() for business logic."""\n',
        "def f():\n    '''reads now() from the clock'''\n",
    ]
    for source in quiet:
        assert clock_reads(source) == [], source
