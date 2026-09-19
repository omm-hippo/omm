# omm evaluate Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four confirmed problems in `omm evaluate`'s Python coding evaluation (all-or-nothing task scoring with a fictional `test_count`, a single generation failure aborting the whole run, a 4-task built-in pack, and a dead `raw_responses_stored` field) without changing the command's public shape (`omm evaluate <model> [--pack] [--output]`).

**Architecture:** `coding_eval.py` gains a small fixed JSON-reporting test-runner harness that is written into the sandbox workspace alongside the model's solution, replacing the current all-or-nothing "container exit code" grading with real per-`unittest`-method pass counts. The pack schema is bumped to `schema_version: 3` / `evaluator: "python-unittest-v2"`: `test_count` is no longer author-declared, it is derived from the `tests` source itself, and pack test sources stop calling `unittest.main()` (the new harness drives execution instead). `evaluate_pack` isolates each task's model-generation call so one bad generation can't destroy the whole report. The built-in pack grows from 4 to 8 tasks using the existing generation/repair convention.

**Tech Stack:** Python 3.10+, stdlib `unittest`/`json`/`re`, existing Docker/Podman sandbox in `coding_eval.py`, `pytest` + `monkeypatch` for tests (no real Docker needed — existing tests already fake `subprocess.Popen`).

**Spec:** No separate spec doc — the four problems were confirmed by the user during a live `omm evaluate` audit in this conversation (2026-09-19); this plan doc is the spec of record. See project memory `project_omm_compare_deprecated_evaluate_revision_2026-09-19.md` for the decision history.

## Global Constraints

- Do not touch `omm compare` / `compare.py` — that command is confirmed for removal in a separate, not-yet-started effort. Do not delete it here either.
- No hidden back-compat: old pack files (`schema_version: 2`, `evaluator: "python-unittest-v1"`, manual `test_count`) must be rejected outright, not silently accepted. This is a local-file, not a network-download format, so a breaking bump is safe (per repo's "fully delete the old, no aliases" convention).
- Branch off `beta` (per repo CLAUDE.md — most feature work targets `beta`). Do not push without asking first.
- Every commit's own tip must build cleanly against `python -m pytest -q`; run pre-commit's version-bump hook as normal (no `--no-verify`).

---

## Task 1: Isolate per-task generation failures in `evaluate_pack`

**Files:**
- Modify: `src/omm/coding_eval.py:448-497` (`evaluate_pack`)
- Test: `tests/test_coding_eval.py`

**Interfaces:**
- Consumes: existing `CodingTaskResult(id, kind, outcome, tests_passed, tests_total, duration_seconds)`, existing `run_in_sandbox`, existing `CodingEvaluationError`/`SandboxUnavailable`.
- Produces: `evaluate_pack` now never lets an exception from the caller-supplied `generate` callable escape — it records that task's outcome as the string `"generation_failed"` and continues to the next task. This is a new outcome string other code (report renderers) may see alongside `"completed"`, `"failed_tests"`, `"sandbox_unavailable"`, `"invalid_response"`, `"timeout"`, `"output_limit"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_coding_eval.py`:

```python
def test_evaluate_pack_isolates_generation_failures(monkeypatch):
    pack = coding_eval.load_pack()
    monkeypatch.setattr(
        coding_eval,
        "run_in_sandbox",
        lambda response, task, pack, runtime=None: coding_eval.SandboxResult(
            "completed", task.test_count, task.test_count, 0.1, "ok"
        ),
    )
    calls = {"n": 0}

    def generate(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ollama unreachable")
        return "```python\ndef placeholder(): return 1\n```"

    report = coding_eval.evaluate_pack("model:latest", pack, generate, runtime="docker")
    outcomes = [task.outcome for task in report.tasks]
    assert outcomes[0] == "generation_failed"
    assert outcomes[1:] == ["completed"] * (len(pack.tasks) - 1)
    assert calls["n"] == len(pack.tasks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_coding_eval.py::test_evaluate_pack_isolates_generation_failures -v`
Expected: FAIL — `RuntimeError: ollama unreachable` propagates out of `evaluate_pack` uncaught (current code only catches `coding_eval.CodingEvaluationError` around both `generate()` and `run_in_sandbox()` together).

- [ ] **Step 3: Write minimal implementation**

In `src/omm/coding_eval.py`, replace the loop body of `evaluate_pack` (currently lines ~464-481):

```python
    for task in pack.tasks:
        started = time.monotonic()
        try:
            response = generate(task_prompt(task))
        except Exception:
            results.append(
                CodingTaskResult(task.id, task.kind, "generation_failed", 0, task.test_count, time.monotonic() - started)
            )
            continue
        try:
            sandbox = run_in_sandbox(response, task, pack, runtime=runtime)
            results.append(
                CodingTaskResult(
                    task.id,
                    task.kind,
                    sandbox.outcome,
                    sandbox.tests_passed,
                    sandbox.tests_total,
                    sandbox.duration_seconds,
                )
            )
        except CodingEvaluationError as error:
            outcome = "sandbox_unavailable" if isinstance(error, SandboxUnavailable) else "invalid_response"
            results.append(CodingTaskResult(task.id, task.kind, outcome, 0, task.test_count, time.monotonic() - started))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_coding_eval.py::test_evaluate_pack_isolates_generation_failures -v`
Expected: PASS. Also run `python -m pytest tests/test_coding_eval.py -v` to confirm no existing test regressed.

- [ ] **Step 5: Commit**

```bash
git add src/omm/coding_eval.py tests/test_coding_eval.py
git commit -m "fix(evaluate): isolate per-task generation failures instead of aborting the run"
```

---

## Task 2: Remove the dead `raw_responses_stored` field

**Files:**
- Modify: `src/omm/coding_eval.py:90-154` (`CodingEvaluationReport` dataclass + `as_dict`)
- Test: `tests/test_coding_eval.py`, `tests/test_cli_evaluate.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `CodingEvaluationReport.as_dict()` output no longer has a `raw_responses_stored` key. (Note: `quality.py:1017`'s own unrelated `"raw_responses_stored": False` literal — a different report structure for `omm quality`/catalog builds, not `CodingEvaluationReport` — is out of scope and must not be touched.)

- [ ] **Step 1: Write the failing test**

In `tests/test_coding_eval.py`, change line 185 from:
```python
    assert payload["raw_responses_stored"] is False
```
to:
```python
    assert "raw_responses_stored" not in payload
```

In `tests/test_cli_evaluate.py`, change both occurrences (lines 62 and 75) from:
```python
    assert payload["raw_responses_stored"] is False
```
to:
```python
    assert "raw_responses_stored" not in payload
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_coding_eval.py tests/test_cli_evaluate.py -v -k raw_responses or evaluate`
Expected: the three touched tests FAIL (field is currently present and `False`, so `not in payload` is currently false).

- [ ] **Step 3: Write minimal implementation**

In `src/omm/coding_eval.py`:
- Remove the field `raw_responses_stored: bool = False` from the `CodingEvaluationReport` dataclass (currently line 106).
- Remove the line `"raw_responses_stored": self.raw_responses_stored,` from `as_dict()` (currently line 153).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_coding_eval.py tests/test_cli_evaluate.py -v`
Expected: PASS, full file for both.

- [ ] **Step 5: Commit**

```bash
git add src/omm/coding_eval.py tests/test_coding_eval.py tests/test_cli_evaluate.py
git commit -m "refactor(evaluate): drop always-false raw_responses_stored field"
```

---

## Task 3: Real per-assert scoring + self-derived test counts + bigger pack (schema v3)

This is one atomic task: the JSON-reporting harness, the schema/evaluator bump, and the rewritten default pack file are mutually load-bearing — none of the three works correctly shipped alone (a v3 loader with the old pack file fails to load; the harness only works with the trailing `unittest.main()` call removed from the pack's `tests` source, since importing a test module that unconditionally calls `unittest.main()` raises `SystemExit` during `import`, before the harness's own result-reporting code runs).

**Files:**
- Modify: `src/omm/coding_eval.py` (module constants near line 31; `load_pack` lines 167-251; `sandbox_argv` lines 308-350; `run_in_sandbox` lines 370-434)
- Modify: `src/omm/data/coding-pack-v1.json` (full rewrite)
- Test: `tests/test_coding_eval.py`

**Interfaces:**
- Consumes: `CodingTask`, `CodingPack`, `SandboxResult` (unchanged shapes).
- Produces:
  - `coding_eval._TEST_METHOD_RE` — module-level compiled regex, `re.Pattern[str]`.
  - `coding_eval._RUNNER_SOURCE` — module-level `str`, the fixed harness script.
  - `coding_eval._parse_result_line(text: str) -> tuple[int, int] | None` — new helper, `(tests_run, passed)` or `None`.
  - `CodingTask.test_count` is now derived at load time from the task's own `tests` source (counts `def test_<name>` methods), not read from the pack JSON.
  - `SandboxResult.tests_passed` can now be any value `0 <= tests_passed <= tests_total`, not just `0` or `tests_total`.
  - `load_pack` now requires `schema_version == 3` and `evaluator == "python-unittest-v2"`; pack items no longer need (or accept) a `test_count` key.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_coding_eval.py`:

```python
def test_load_pack_derives_test_count_from_test_methods():
    pack = coding_eval.load_pack()
    for task in pack.tasks:
        assert task.test_count == len(coding_eval._TEST_METHOD_RE.findall(task.tests))
        assert task.test_count >= 1


def test_load_pack_rejects_old_schema_version(tmp_path):
    source = json.loads(coding_eval.default_pack_path().read_text(encoding="utf-8"))
    source["schema_version"] = 2
    path = tmp_path / "old.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(coding_eval.CodingEvaluationError, match="schema"):
        coding_eval.load_pack(path)


def test_run_in_sandbox_reports_partial_credit_from_json(monkeypatch):
    pack = coding_eval.load_pack()
    task = pack.tasks[0]
    payload = json.dumps({"tests_run": task.test_count, "passed": task.test_count - 1}).encode() + b"\n"

    class FakeStdout:
        chunks = iter([payload, b""])
        def read(self, _size):
            return next(self.chunks)

    class FakeProcess:
        stdout = FakeStdout()
        returncode = 1
        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def terminate(self): pass
        def kill(self): pass

    monkeypatch.setattr(coding_eval.subprocess, "Popen", lambda argv, **kwargs: FakeProcess())
    result = coding_eval.run_in_sandbox("irrelevant source", task, pack, runtime="docker")
    assert result.outcome == "failed_tests"
    assert result.tests_passed == task.test_count - 1
    assert result.tests_total == task.test_count


def test_run_in_sandbox_reports_full_credit_from_json(monkeypatch):
    pack = coding_eval.load_pack()
    task = pack.tasks[0]
    payload = json.dumps({"tests_run": task.test_count, "passed": task.test_count}).encode() + b"\n"

    class FakeStdout:
        chunks = iter([payload, b""])
        def read(self, _size):
            return next(self.chunks)

    class FakeProcess:
        stdout = FakeStdout()
        returncode = 0
        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def terminate(self): pass
        def kill(self): pass

    monkeypatch.setattr(coding_eval.subprocess, "Popen", lambda argv, **kwargs: FakeProcess())
    result = coding_eval.run_in_sandbox("irrelevant source", task, pack, runtime="docker")
    assert result.outcome == "completed"
    assert result.tests_passed == task.test_count
    assert result.tests_total == task.test_count
```

Also update the two now-stale assertions in the same file:
- `test_default_pack_is_bounded_versioned_and_digest_pinned` (line 14): change `assert pack.version == "1.0.0"` to `assert pack.version == "2.0.0"`.
- `test_sandbox_argv_has_security_boundaries` (line 54): change
  `assert argv[-4:] == ["python", "-I", "-B", "/workspace/test_solution.py"]`
  to
  `assert argv[-4:] == ["python", "-I", "-B", "/workspace/run_tests.py"]`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_coding_eval.py -v`
Expected: the four new tests FAIL, and the two updated assertions FAIL against current code (current pack has `version: "1.0.0"`, current `sandbox_argv` targets `test_solution.py`, `test_count` isn't derived, schema check accepts version 2).

- [ ] **Step 3: Write minimal implementation**

3a. In `src/omm/coding_eval.py`, add next to the existing `_IMAGE_RE`/`_FENCE_RE` constants (currently lines 31-32):

```python
_TEST_METHOD_RE = re.compile(r"(?m)^\s*def test_\w+")
_RUNNER_SOURCE = (
    "import json, sys, unittest\n"
    "sys.path.insert(0, \"/workspace\")\n"
    "try:\n"
    "    import test_solution\n"
    "    suite = unittest.defaultTestLoader.loadTestsFromModule(test_solution)\n"
    "    result = unittest.TestResult()\n"
    "    suite.run(result)\n"
    "    total = result.testsRun\n"
    "    passed = total - len(result.failures) - len(result.errors)\n"
    "except Exception:\n"
    "    total, passed = 0, 0\n"
    "print(json.dumps({\"tests_run\": total, \"passed\": passed}))\n"
    "sys.exit(0 if total and passed == total else 1)\n"
)


def _parse_result_line(text: str) -> tuple[int, int] | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        data = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    tests_run, passed = data.get("tests_run"), data.get("passed")
    if (
        isinstance(tests_run, int) and not isinstance(tests_run, bool)
        and isinstance(passed, int) and not isinstance(passed, bool)
        and tests_run >= 0 and 0 <= passed <= tests_run
    ):
        return tests_run, passed
    return None
```

This has been validated by hand outside the sandbox against four cases (fully correct solution, partially correct solution, a solution with a syntax error, and a solution that prints noise to stdout during the test run) — in every case the harness emits exactly one trailing JSON line with accurate counts.

3b. In `load_pack`, change the schema check (currently line 179):
```python
    if not isinstance(payload, dict) or payload.get("schema_version") != 3:
        raise CodingEvaluationError("unsupported coding pack schema")
```

Change the evaluator check (currently line 186):
```python
    if payload["language"] != "python" or payload["evaluator"] != "python-unittest-v2":
        raise CodingEvaluationError("unsupported coding pack language or evaluator")
```

Replace the task-building block (currently lines 229-246):
```python
        values = {}
        for key in ("prompt", "starter", "tests"):
            value = item.get(key)
            if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS:
                raise CodingEvaluationError(f"coding task {task_id} has invalid {key}")
            values[key] = value
        if not values["prompt"].strip() or not values["tests"].strip():
            raise CodingEvaluationError(f"coding task {task_id} is incomplete")
        derived_test_count = len(_TEST_METHOD_RE.findall(values["tests"]))
        if not 1 <= derived_test_count <= 100:
            raise CodingEvaluationError(f"coding task {task_id} must declare 1 to 100 test_ methods")
        tasks.append(
            CodingTask(
                task_id,
                kind,
                values["prompt"],
                values["starter"],
                values["tests"],
                derived_test_count,
            )
        )
```

3c. In `sandbox_argv`, change the final four argv entries (currently line 349) from
`"/workspace/test_solution.py"` to `"/workspace/run_tests.py"`.

3d. In `run_in_sandbox`, write the runner file alongside the existing solution/tests files. Add after the existing `tests_path.chmod(0o444)` (currently line 382):
```python
        runner_path = workspace / "run_tests.py"
        runner_path.write_text(_RUNNER_SOURCE, encoding="utf-8")
        runner_path.chmod(0o444)
```

Then replace the tail of `run_in_sandbox` (currently lines 429-434):
```python
        duration = time.monotonic() - started
        text = bytes(output).decode("utf-8", errors="replace")
        parsed = _parse_result_line(text) if outcome == "completed" else None
        if parsed is not None:
            tests_run, tests_passed = parsed
            total = tests_run or task.test_count
            passed = min(tests_passed, total)
            outcome = "completed" if tests_run and tests_passed == tests_run and process.returncode == 0 else "failed_tests"
        else:
            if outcome == "completed" and process.returncode != 0:
                outcome = "failed_tests"
            total = task.test_count
            passed = task.test_count if outcome == "completed" and process.returncode == 0 else 0
        return SandboxResult(outcome, passed, total, duration, text)
```

3e. Rewrite `src/omm/data/coding-pack-v1.json` in full with this content (8 tasks: 4 generation, 4 repair; verified locally with plain `unittest` — the 4 new tasks' reference implementations pass all their own tests, and the 2 new repair tasks' buggy starters fail exactly the assertions they're meant to test):

```json
{
  "schema_version": 3,
  "pack_id": "omm-python-coding-smoke",
  "version": "2.0.0",
  "language": "python",
  "evaluator": "python-unittest-v2",
  "license": "MIT",
  "source": "omm built-in pack",
  "generation": {
    "temperature": 0,
    "seed": 0,
    "num_ctx": 4096,
    "num_predict": 1024,
    "think": false
  },
  "sandbox": {
    "image": "docker.io/library/python:3.12.10-alpine3.21@sha256:9c51ecce261773a684c8345b2d4673700055c513b4d54bc0719337d3e4ee552e",
    "timeout_seconds": 30,
    "memory_mb": 256,
    "cpus": 1,
    "pids": 32
  },
  "items": [
    {
      "id": "generation_chunks",
      "kind": "generation",
      "prompt": "Return only the complete contents of solution.py. Implement chunks(values, size), returning consecutive lists of at most size items. Raise ValueError when size is not a positive integer. Do not mutate values.",
      "starter": "def chunks(values, size):\n    pass\n",
      "tests": "import sys, unittest\nsys.path.insert(0, '/workspace')\nfrom solution import chunks\nclass Tests(unittest.TestCase):\n    def test_even(self): self.assertEqual(chunks([1,2,3,4], 2), [[1,2],[3,4]])\n    def test_tail(self): self.assertEqual(chunks([1,2,3,4,5], 2), [[1,2],[3,4],[5]])\n    def test_empty(self): self.assertEqual(chunks([], 3), [])\n    def test_invalid(self):\n        for value in (0, -1, 1.5, True):\n            with self.assertRaises(ValueError): chunks([1], value)\n    def test_no_mutation(self):\n        values=[1,2,3]; chunks(values,2); self.assertEqual(values,[1,2,3])\n"
    },
    {
      "id": "generation_unique",
      "kind": "generation",
      "prompt": "Return only the complete contents of solution.py. Implement unique_preserving_order(values). Return the first occurrence of each hashable value in input order without mutating the input.",
      "starter": "def unique_preserving_order(values):\n    pass\n",
      "tests": "import sys, unittest\nsys.path.insert(0, '/workspace')\nfrom solution import unique_preserving_order\nclass Tests(unittest.TestCase):\n    def test_numbers(self): self.assertEqual(unique_preserving_order([3,1,3,2,1]), [3,1,2])\n    def test_strings(self): self.assertEqual(unique_preserving_order(['a','b','a']), ['a','b'])\n    def test_empty(self): self.assertEqual(unique_preserving_order([]), [])\n    def test_no_mutation(self):\n        values=[1,1,2]; unique_preserving_order(values); self.assertEqual(values,[1,1,2])\n"
    },
    {
      "id": "generation_flatten",
      "kind": "generation",
      "prompt": "Return only the complete contents of solution.py. Implement flatten(nested), flattening one level of nesting from a list of lists into a single list, preserving order, without mutating the input.",
      "starter": "def flatten(nested):\n    pass\n",
      "tests": "import sys, unittest\nsys.path.insert(0, '/workspace')\nfrom solution import flatten\nclass Tests(unittest.TestCase):\n    def test_basic(self): self.assertEqual(flatten([[1,2],[3],[]]), [1,2,3])\n    def test_empty(self): self.assertEqual(flatten([]), [])\n    def test_singletons(self): self.assertEqual(flatten([[1],[2],[3]]), [1,2,3])\n    def test_no_mutation(self):\n        nested=[[1,2],[3]]; flatten(nested); self.assertEqual(nested,[[1,2],[3]])\n"
    },
    {
      "id": "generation_word_count",
      "kind": "generation",
      "prompt": "Return only the complete contents of solution.py. Implement word_count(text), splitting text on whitespace and returning a dict mapping each token to the number of times it appears, case-sensitive, in first-appearance order.",
      "starter": "def word_count(text):\n    pass\n",
      "tests": "import sys, unittest\nsys.path.insert(0, '/workspace')\nfrom solution import word_count\nclass Tests(unittest.TestCase):\n    def test_basic(self): self.assertEqual(word_count(\"a b a c b a\"), {\"a\":3,\"b\":2,\"c\":1})\n    def test_empty(self): self.assertEqual(word_count(\"\"), {})\n    def test_single(self): self.assertEqual(word_count(\"x\"), {\"x\":1})\n    def test_order(self): self.assertEqual(list(word_count(\"b a b\").keys()), [\"b\",\"a\"])\n"
    },
    {
      "id": "repair_average",
      "kind": "repair",
      "prompt": "Return only the corrected complete contents of solution.py. average(values) must return 0 for an empty iterable and the arithmetic mean otherwise. It must accept generators. Preserve the public function name.",
      "starter": "def average(values):\n    return sum(values) / len(values)\n",
      "tests": "import sys, unittest\nsys.path.insert(0, '/workspace')\nfrom solution import average\nclass Tests(unittest.TestCase):\n    def test_values(self): self.assertEqual(average([2,4,6]), 4)\n    def test_empty(self): self.assertEqual(average([]), 0)\n    def test_generator(self): self.assertEqual(average(x for x in [1,2,3]), 2)\n    def test_float(self): self.assertAlmostEqual(average([0.5,1.5]), 1.0)\n"
    },
    {
      "id": "repair_clamp",
      "kind": "repair",
      "prompt": "Return only the corrected complete contents of solution.py. clamp(value, low, high) must constrain value to the inclusive range. Raise ValueError if low is greater than high.",
      "starter": "def clamp(value, low, high):\n    return min(low, max(high, value))\n",
      "tests": "import sys, unittest\nsys.path.insert(0, '/workspace')\nfrom solution import clamp\nclass Tests(unittest.TestCase):\n    def test_middle(self): self.assertEqual(clamp(5,0,10),5)\n    def test_low(self): self.assertEqual(clamp(-1,0,10),0)\n    def test_high(self): self.assertEqual(clamp(11,0,10),10)\n    def test_edges(self): self.assertEqual((clamp(0,0,10),clamp(10,0,10)),(0,10))\n    def test_invalid(self):\n        with self.assertRaises(ValueError): clamp(1,2,1)\n"
    },
    {
      "id": "repair_is_palindrome",
      "kind": "repair",
      "prompt": "Return only the corrected complete contents of solution.py. is_palindrome(text) must return True if text reads the same forwards and backwards, ignoring case and spaces.",
      "starter": "def is_palindrome(text):\n    return text == text[::-1]\n",
      "tests": "import sys, unittest\nsys.path.insert(0, '/workspace')\nfrom solution import is_palindrome\nclass Tests(unittest.TestCase):\n    def test_simple(self): self.assertTrue(is_palindrome(\"level\"))\n    def test_case_space(self): self.assertTrue(is_palindrome(\"A man a plan a canal Panama\"))\n    def test_false(self): self.assertFalse(is_palindrome(\"hello\"))\n    def test_empty(self): self.assertTrue(is_palindrome(\"\"))\n"
    },
    {
      "id": "repair_second_largest",
      "kind": "repair",
      "prompt": "Return only the corrected complete contents of solution.py. second_largest(values) must return the second-largest distinct value in a list of numbers. Raise ValueError if fewer than two distinct values exist.",
      "starter": "def second_largest(values):\n    s = sorted(values)\n    return s[-2]\n",
      "tests": "import sys, unittest\nsys.path.insert(0, '/workspace')\nfrom solution import second_largest\nclass Tests(unittest.TestCase):\n    def test_basic(self): self.assertEqual(second_largest([1,3,2]), 2)\n    def test_duplicates(self): self.assertEqual(second_largest([5,5,3,5]), 3)\n    def test_raises(self):\n        with self.assertRaises(ValueError): second_largest([1,1,1])\n    def test_negatives(self): self.assertEqual(second_largest([-1,-5,-2]), -2)\n"
    }
  ]
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_coding_eval.py -v`
Expected: PASS, full file — including the pre-existing `test_run_in_sandbox_reports_pass_without_storing_source` (its `FakeStdout` output has no JSON line, so it now exercises the fallback branch of `run_in_sandbox` and still asserts `tests_passed == task.test_count`).

Also sanity-check the new pack loads and derives 8 tasks with the right per-kind split:
```bash
python -c "
from omm import coding_eval
pack = coding_eval.load_pack()
print(len(pack.tasks), [(t.id, t.kind, t.test_count) for t in pack.tasks])
"
```
Expected: `8 [('generation_chunks', 'generation', 5), ('generation_unique', 'generation', 4), ('generation_flatten', 'generation', 4), ('generation_word_count', 'generation', 4), ('repair_average', 'repair', 4), ('repair_clamp', 'repair', 5), ('repair_is_palindrome', 'repair', 4), ('repair_second_largest', 'repair', 4)]`

- [ ] **Step 5: Commit**

```bash
git add src/omm/coding_eval.py src/omm/data/coding-pack-v1.json tests/test_coding_eval.py
git commit -m "feat(evaluate): score real per-test results instead of all-or-nothing, expand built-in pack to 8 tasks"
```

---

## Task 4: Drop now-unreachable exception handling in `evaluate_cmd`

Depends on Task 1: once `evaluate_pack` never raises `quality_mod.QualityEvaluationError` (or anything else) from a task's generation call, the `except quality_mod.QualityEvaluationError` wrapped around the `evaluate_pack(...)` call in the CLI is dead code — remove it rather than leave it as defensive handling for a case that can no longer happen.

**Files:**
- Modify: `src/omm/cli.py:10467-10486` (`evaluate_cmd`)

**Interfaces:**
- Consumes: `coding_eval.evaluate_pack` (Task 1's contract — never raises from a per-task generation failure).
- Produces: no change to `evaluate_cmd`'s observable behavior; this is dead-code removal.

- [ ] **Step 1: Confirm the code path is actually dead**

Run: `grep -n "QualityEvaluationError" src/omm/coding_eval.py`
Expected: no matches — `coding_eval.py` never imports or raises `quality.QualityEvaluationError`, and after Task 1, nothing else `evaluate_pack` calls can let a `generate()`-side exception escape either. There is no test to write for a removal of unreachable dead code; verification here is the full suite staying green in Step 3.

- [ ] **Step 2: Remove the dead except clause**

In `src/omm/cli.py`, change (currently lines 10467-10486):
```python
    try:
        report = coding_eval.evaluate_pack(
            model,
            coding_pack,
            generate,
            runtime=runtime,
            model_provider=(registry_entry or {}).get("provider"),
            model_repo_id=(registry_entry or {}).get("repo_id"),
            model_filename=(registry_entry or {}).get("filename"),
            model_digest=quality_mod._normalized_digest(metadata.get("digest")),
            quantization=metadata.get("quantization_level"),
            engine="ollama",
            engine_version=quality_mod.ollama_version(),
        )
    except quality_mod.QualityEvaluationError as error:
        err_console.print(f"[error]{escape(str(error))}[/error]")
        raise typer.Exit(1) from error
    finally:
        if was_loaded is False:
            quality_mod.ensure_model_unloaded(model)
```
to:
```python
    try:
        report = coding_eval.evaluate_pack(
            model,
            coding_pack,
            generate,
            runtime=runtime,
            model_provider=(registry_entry or {}).get("provider"),
            model_repo_id=(registry_entry or {}).get("repo_id"),
            model_filename=(registry_entry or {}).get("filename"),
            model_digest=quality_mod._normalized_digest(metadata.get("digest")),
            quantization=metadata.get("quantization_level"),
            engine="ollama",
            engine_version=quality_mod.ollama_version(),
        )
    finally:
        if was_loaded is False:
            quality_mod.ensure_model_unloaded(model)
```

- [ ] **Step 3: Run the full relevant test files to verify nothing regressed**

Run: `python -m pytest tests/test_cli_evaluate.py tests/test_coding_eval.py -v`
Expected: PASS, both files in full.

- [ ] **Step 4: Commit**

```bash
git add src/omm/cli.py
git commit -m "refactor(evaluate): drop unreachable exception handling around evaluate_pack"
```

---

## Final check

- [ ] Run the full suite: `python -m pytest -q`
- [ ] Run `omm evaluate --help` to confirm the command's flags/help text are unchanged (this plan changes internals and the pack, not the CLI surface, so `docs/commands.json`/`README.md` do NOT need regenerating — confirm with `python scripts/check_docs_sync.py`).
