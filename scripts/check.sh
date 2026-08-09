#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "[check] missing .venv; create it and install requirements.txt + requirements-dev.txt"
  exit 1
fi

echo "[check] py_compile"
"$PY" <<'PY'
import py_compile
from pathlib import Path

for root in ("agent", "arag", "common", "skillcenter", "a2a_service", "eval", "tests"):
    for path in sorted(Path(root).rglob("*.py")):
        if "reports" in path.parts:
            continue
        py_compile.compile(path, doraise=True)
PY

echo "[check] reliability tests"
"$PY" -m pytest -q tests/reliability

CHECK_TMP="$(mktemp -d)"
trap 'rm -rf "$CHECK_TMP"' EXIT

echo "[check] SQLite checksums, Draft 2020-12 schemas and REL/FI traceability"
"$PY" - "$CHECK_TMP" <<'PY'
import asyncio
import ast
import json
import re
import sqlite3
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from agent.runtime.adapters.sqlite import RuntimeDatabase
from arag.persistence.migrations import LATEST_SCHEMA_VERSION
from arag.persistence.repository import RagRepository


async def main(root: Path) -> None:
    runtime = RuntimeDatabase(root / "runtime" / "runtime.db")
    await runtime.migrate()
    await runtime.verify()
    # A second pass exercises recorded migration checksums rather than only creation.
    await runtime.verify()

    rag = RagRepository(root / "arag" / "rag.db", root / "arag")
    await rag.initialize()
    await rag.initialize()
    if await rag.schema_version() != LATEST_SCHEMA_VERSION:
        raise RuntimeError("rag.db did not reach the latest schema version")
    with sqlite3.connect(root / "arag" / "rag.db") as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("rag.db integrity_check failed")

    schema_paths = sorted(Path("docs/reliability/schemas").glob("*.json"))
    expected_schema_names = {
        "runtime-envelope-v1.schema.json",
        "canonical-event-v1.schema.json",
        "working-state-v1.schema.json",
        "tool-result-envelope-v1.schema.json",
        "artifact-v1.schema.json",
        "evidence-v1.schema.json",
    }
    if {path.name for path in schema_paths} != expected_schema_names:
        raise RuntimeError("reliability schema set does not match the six frozen v1 contracts")
    schemas = []
    for path in schema_paths:
        schema = json.loads(path.read_text(encoding="utf-8"))
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise RuntimeError(f"{path} does not declare JSON Schema Draft 2020-12")
        Draft202012Validator.check_schema(schema)
        schemas.append(schema)

    schema_ids = [schema.get("$id") for schema in schemas]
    if any(not item for item in schema_ids) or len(schema_ids) != len(set(schema_ids)):
        raise RuntimeError("reliability schemas must have unique non-empty $id values")
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas
    )
    for schema in schemas:
        refs = []

        def collect_refs(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    refs.append(value["$ref"])
                for child in value.values():
                    collect_refs(child)
            elif isinstance(value, list):
                for child in value:
                    collect_refs(child)

        collect_refs(schema)
        resolver = registry.resolver(schema["$id"])
        for ref in refs:
            resolver.lookup(ref)

    coverage_path = Path("docs/reliability/implementation-coverage.md")
    coverage = coverage_path.read_text(encoding="utf-8")
    rel_rows = re.findall(
        r"^\| `REL-(\d{3})` \| (DIRECT|PARTIAL|GAP) \|", coverage, re.MULTILINE
    )
    fi_rows = re.findall(
        r"^\| `FI-(\d{2})` \| (DIRECT|PARTIAL|GAP) \|", coverage, re.MULTILINE
    )
    if [item[0] for item in rel_rows] != [f"{number:03d}" for number in range(1, 31)]:
        raise RuntimeError("implementation coverage must contain ordered REL-001..REL-030 once")
    if [item[0] for item in fi_rows] != [f"{number:02d}" for number in range(1, 13)]:
        raise RuntimeError("implementation coverage must contain ordered FI-01..FI-12 once")

    node_pattern = re.compile(
        r"`(tests/reliability/[A-Za-z0-9_./-]+\.py::test_[A-Za-z0-9_]+)`"
    )
    mapping_rows = [
        line for line in coverage.splitlines()
        if re.match(r"^\| `(?:REL-\d{3}|FI-\d{2})` \|", line)
    ]
    if any(not node_pattern.search(line) for line in mapping_rows):
        raise RuntimeError("every REL/FI coverage row must reference at least one pytest node")
    parsed_functions = {}
    for node in sorted(set(node_pattern.findall(coverage))):
        filename, function_name = node.split("::", 1)
        path = Path(filename)
        if not path.is_file():
            raise RuntimeError(f"coverage references missing test file: {filename}")
        functions = parsed_functions.get(path)
        if functions is None:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            functions = {
                item.name for item in ast.walk(tree)
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            parsed_functions[path] = functions
        if function_name not in functions:
            raise RuntimeError(f"coverage references missing pytest function: {node}")


asyncio.run(main(Path(sys.argv[1])))
PY

echo "[check] obsolete protocol/authority scan"
"$PY" <<'PY'
import re
from pathlib import Path

roots = [
    Path("agent"), Path("arag"), Path("web"), Path("eval/harness"),
    Path("README.md"), Path("RUNBOOK.md"), Path("AGENTS.md"),
    Path("CLAUDE.md"), Path("eval/README.md"), Path(".env.example"),
]
patterns = {
    "removed chat endpoint": re.compile(r"/api/v1/chat/"),
    "startup engine selection": re.compile(r"(?<![A-Z_])ENGINE\s*="),
    "legacy embedding authority": re.compile(r"local_storage/embedding|EMBEDDING_STORAGE_DIR"),
    "legacy done event": re.compile(r"StreamEvent\(\s*['\"]done['\"]|event:\s*done"),
    "legacy source ownership": re.compile(r"api/chat\.py|HistoryStore|CitationInjector|SessionManager"),
}
failures = []
for root in roots:
    files = [root] if root.is_file() else root.rglob("*")
    for path in files:
        if not path.is_file() or path.suffix not in {".py", ".js", ".html", ".md", ".example"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in patterns.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                failures.append(f"{path}:{line}: {label}: {match.group(0)}")
if failures:
    raise SystemExit("\n".join(failures))
PY

echo "[check] git diff whitespace"
git diff --check
echo "[check] PASS"
