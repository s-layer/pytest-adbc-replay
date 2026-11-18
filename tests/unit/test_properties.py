"""
Property tests for the cassette key and the cassette files.

A cassette is replayed by matching each ``execute()`` against the recorded key: the
normalised SQL plus the serialised parameters. Two different statements that share a key
replay each other's results (a shipped bug: Snowflake SHOW and DESCRIBE kinds once collapsed
onto one key, fixed in #42), and a key that changes with harmless edits makes replay miss.
The files themselves must give back exactly what was recorded.
"""

from __future__ import annotations

import datetime as dt
import uuid
import warnings
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from pytest_adbc_replay._cassette_io import (
    read_arrow_table,
    read_params_json,
    write_arrow_table,
    write_params_json,
)
from pytest_adbc_replay._normaliser import NormalisationWarning, normalise_sql
from pytest_adbc_replay._params import (
    build_registry,
    deserialise_params,
    params_to_cache_key,
    serialise_params,
)

REGISTRY = build_registry(None)
DIALECTS = [None, "snowflake", "duckdb", "databricks"]

# --------------------------------------------------------------------------------------
# Generated SQL
# --------------------------------------------------------------------------------------

identifiers = st.from_regex(r"[a-z][a-z0-9_]{0,7}", fullmatch=True).map(lambda name: f"c_{name}")
"""Unquoted names, prefixed so that none is a keyword in any dialect."""

int_literals = st.integers(-(10**12), 10**12).map(str)
string_literals = st.text(
    st.characters(codec="utf-8", exclude_categories=["Cs", "Cc"], exclude_characters="\\"),
    max_size=12,
).map(lambda text: "'" + text.replace("'", "''") + "'")
literals = int_literals | string_literals


@st.composite
def select_statements(draw: st.DrawFn) -> dict[str, Any]:
    """The parts of a ``SELECT ... FROM ... WHERE col = literal`` statement."""
    return {
        "columns": draw(st.lists(identifiers, min_size=1, max_size=3, unique=True)),
        "table": draw(identifiers),
        "where": draw(identifiers),
        "literal": draw(literals),
    }


def _render(parts: dict[str, Any], spaces: list[str] | None = None, upper: bool = True) -> str:
    """Write the statement with the given whitespace runs and keyword case."""
    keywords = ["SELECT", "FROM", "WHERE"]
    if not upper:
        keywords = [word.lower() for word in keywords]
    tokens = [
        keywords[0],
        ", ".join(parts["columns"]),
        keywords[1],
        parts["table"],
        keywords[2],
        parts["where"],
        "=",
        parts["literal"],
    ]
    gaps = spaces or [" "] * (len(tokens) - 1)
    out = tokens[0]
    for gap, token in zip(gaps, tokens[1:], strict=True):
        out += gap + token
    return out


whitespace_runs = st.lists(
    st.text(st.sampled_from([" ", "\t", "\n", "\r"]), min_size=1, max_size=3),
    min_size=7,
    max_size=7,
)


def _key(sql: str, dialect: str | None) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", NormalisationWarning)
        return normalise_sql(sql, dialect)


@settings(max_examples=300)
@given(select_statements(), st.sampled_from(DIALECTS))
def test_normalise_sql_is_idempotent(parts: dict[str, Any], dialect: str | None) -> None:
    once = _key(_render(parts), dialect)
    assert _key(once, dialect) == once


@settings(max_examples=300)
@given(select_statements(), whitespace_runs, st.booleans(), st.sampled_from(DIALECTS))
def test_whitespace_and_keyword_case_never_change_the_key(
    parts: dict[str, Any], spaces: list[str], upper: bool, dialect: str | None
) -> None:
    assert _key(_render(parts, spaces, upper), dialect) == _key(_render(parts), dialect)


@settings(max_examples=300)
@given(select_statements(), literals, st.sampled_from(DIALECTS))
def test_different_literals_never_share_a_key(
    parts: dict[str, Any], other: str, dialect: str | None
) -> None:
    assume(other != parts["literal"])
    changed = {**parts, "literal": other}
    assert _key(_render(parts), dialect) != _key(_render(changed), dialect)


@settings(max_examples=300)
@given(
    select_statements(), identifiers, st.sampled_from(["table", "where"]), st.sampled_from(DIALECTS)
)
def test_different_identifiers_never_share_a_key(
    parts: dict[str, Any], other: str, slot: str, dialect: str | None
) -> None:
    assume(other != parts[slot])
    changed = {**parts, slot: other}
    assert _key(_render(parts), dialect) != _key(_render(changed), dialect)


SNOWFLAKE_SHOW = [
    "SHOW TABLES",
    "SHOW VIEWS",
    "SHOW SCHEMAS",
    "SHOW SEMANTIC VIEWS",
    "SHOW COLUMNS IN TABLE {name}",
    "SHOW COLUMNS IN VIEW {name}",
    "SHOW SEMANTIC DIMENSIONS IN {name}",
    "SHOW SEMANTIC METRICS IN {name}",
    "SHOW SEMANTIC FACTS IN {name}",
    "SHOW TERSE TABLES",
    "SHOW TABLES IN SCHEMA {name}",
    "SHOW VIEWS IN SCHEMA {name}",
]
DESCRIBE_KINDS = ["TABLE", "VIEW", "SEMANTIC VIEW"]


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.sampled_from([*SNOWFLAKE_SHOW, *(f"DESCRIBE {kind} {{name}}" for kind in DESCRIBE_KINDS)]),
    st.sampled_from([*SNOWFLAKE_SHOW, *(f"DESCRIBE {kind} {{name}}" for kind in DESCRIBE_KINDS)]),
    identifiers,
    identifiers,
)
def test_different_snowflake_metadata_statements_never_share_a_key(
    first: str, second: str, name: str, other_name: str
) -> None:
    """Every pair of SHOW / DESCRIBE statements of a different kind or object keeps its own key."""
    a, b = first.format(name=name), second.format(name=other_name)
    assume(a != b)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NormalisationWarning)
        assert normalise_sql(a, "snowflake") != normalise_sql(b, "snowflake")


# --------------------------------------------------------------------------------------
# Generated parameters
# --------------------------------------------------------------------------------------

timezones = st.none() | st.timezones(no_cache=True) | st.just(dt.UTC)
param_values = (
    st.none()
    | st.booleans()
    | st.integers(-(2**63), 2**63 - 1)
    | st.floats(allow_nan=False)
    | st.text(max_size=10)
    | st.binary(max_size=10)
    | st.decimals(allow_nan=False, allow_infinity=False, places=None)
    | st.datetimes(timezones=timezones)
    | st.dates()
    | st.times()
    | st.uuids()
)
params = (
    st.none()
    | st.lists(param_values, max_size=5)
    | st.tuples(param_values, param_values).map(list)
    | st.dictionaries(st.text(min_size=1, max_size=5), param_values, max_size=4)
)


def _identity(value: Any) -> Any:
    """A comparison key that tells apart values Python calls equal (1, 1.0, True, Decimal(1))."""
    if isinstance(value, list):
        return ("list", tuple(_identity(item) for item in value))
    if isinstance(value, dict):
        return ("dict", tuple(sorted((k, _identity(v)) for k, v in value.items())))
    if isinstance(value, (dt.datetime, dt.time)):
        # isoformat() carries the offset, so it tells two instants apart; fold alone does not
        # change a naive value.
        return (type(value).__name__, value.isoformat())
    if isinstance(value, float):
        return ("float", value.hex())
    return (type(value).__name__, repr(value))


@settings(max_examples=300)
@given(params)
def test_params_round_trip_through_the_cassette_file(tmp_path_factory: Any, value: Any) -> None:
    path: Path = tmp_path_factory.mktemp("params") / "000_params.json"
    write_params_json(serialise_params(value, REGISTRY), path)
    restored = deserialise_params(read_params_json(path))
    assert _identity(restored) == _identity(value)


@settings(max_examples=300)
@given(params, params)
def test_different_params_never_share_a_key(first: Any, second: Any) -> None:
    assume(_identity(first) != _identity(second))
    assert params_to_cache_key(first, REGISTRY) != params_to_cache_key(second, REGISTRY)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ([1], [1.0]),
        ([1], [True]),
        ([0.0], [-0.0]),
        ([Decimal("1.0")], [Decimal("1.00")]),
        ([dt.datetime(2026, 1, 1, tzinfo=dt.UTC)], [dt.datetime(2026, 1, 1)]),  # noqa: DTZ001
        (["1"], [1]),
        ([b"a"], ["a"]),
        ([uuid.UUID(int=1)], [str(uuid.UUID(int=1))]),
    ],
)
def test_values_python_calls_equal_keep_their_own_key(first: Any, second: Any) -> None:
    """The key property must tell these apart, so the identity it compares by does too."""
    assert _identity(first) != _identity(second)
    assert params_to_cache_key(first, REGISTRY) != params_to_cache_key(second, REGISTRY)


# --------------------------------------------------------------------------------------
# Generated Arrow tables
# --------------------------------------------------------------------------------------

ARROW_TYPES: list[tuple[pa.DataType, st.SearchStrategy[Any]]] = [
    (pa.bool_(), st.booleans()),
    (pa.int8(), st.integers(-(2**7), 2**7 - 1)),
    (pa.int64(), st.integers(-(2**63), 2**63 - 1)),
    (pa.uint32(), st.integers(0, 2**32 - 1)),
    (pa.float64(), st.floats(allow_nan=True)),
    (pa.string(), st.text(max_size=8)),
    (pa.large_string(), st.text(max_size=8)),
    (pa.binary(), st.binary(max_size=8)),
    (pa.decimal128(18, 4), st.decimals(min_value=-(10**13), max_value=10**13, places=4)),
    (pa.date32(), st.dates()),
    (pa.timestamp("us"), st.datetimes()),
    (pa.timestamp("us", tz="UTC"), st.datetimes(timezones=st.just(dt.UTC))),
    (
        pa.timestamp("ns", tz="Europe/London"),
        st.datetimes(
            min_value=dt.datetime(1678, 1, 1),  # noqa: DTZ001
            max_value=dt.datetime(2262, 1, 1),  # noqa: DTZ001
            timezones=st.just(dt.UTC),
        ),
    ),
    (pa.time64("us"), st.times()),
    (pa.list_(pa.int32()), st.lists(st.integers(-(2**31), 2**31 - 1), max_size=3)),
    (
        pa.struct([("a", pa.int16()), ("b", pa.string())]),
        st.fixed_dictionaries({"a": st.integers(-(2**15), 2**15 - 1), "b": st.text(max_size=4)}),
    ),
]


@st.composite
def arrow_tables(draw: st.DrawFn) -> pa.Table:
    width = draw(st.integers(1, 4))
    rows = draw(st.integers(0, 6))
    fields: list[pa.Field] = []
    columns: list[pa.Array] = []
    for index in range(width):
        arrow_type, values = draw(st.sampled_from(ARROW_TYPES))
        column = draw(st.lists(st.none() | values, min_size=rows, max_size=rows))
        metadata = draw(
            st.none() | st.dictionaries(st.text(min_size=1, max_size=4), st.text(max_size=6))
        )
        fields.append(
            pa.field(f"col_{index}", arrow_type, nullable=True, metadata=metadata or None)
        )
        columns.append(pa.array(column, type=arrow_type))
    schema = pa.schema(fields, metadata=draw(st.none() | st.just({"source": "test"})))
    return pa.Table.from_arrays(columns, schema=schema)


@settings(max_examples=200)
@given(arrow_tables())
def test_arrow_tables_round_trip_through_the_cassette_file(
    tmp_path_factory: Any, table: pa.Table
) -> None:
    path: Path = tmp_path_factory.mktemp("arrow") / "000_result.arrow"
    write_arrow_table(table, path)
    restored = read_arrow_table(path)
    assert restored.schema.equals(table.schema, check_metadata=True)
    # equals() calls NaN unequal to itself, so compare the bytes Arrow would send instead.
    assert _ipc_bytes(restored) == _ipc_bytes(table)


def _ipc_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table.combine_chunks())
    return sink.getvalue().to_pybytes()
