# Test Infrastructure Specification

## Purpose

Bootstrap a pytest + pytest-asyncio harness with a lifespan-free app fixture and mocked dependencies, providing one test class per Glass-Pipe requirement so R1–R9 are verifiable and would fail red on regression. Anchors R10.

## Requirements

### REQ-1: glass_pipe_test harness bootstrapped (R10)

The repository SHALL add `pyproject.toml` with pytest + pytest-asyncio config (`asyncio_mode = "auto"`, `testpaths = ["tests"]`, `pythonpath = ["."]`). The repository SHALL add `tests/conftest.py` exposing a `test_app` fixture that builds a lifespan-free FastAPI `TestClient` with `Database`, `SystemdController`, `CoolingStateMachine`, and `ShadowAuditor` mocked, and `FlashRank` init guarded/mocked to prevent model download on import. The repository SHALL add `tests/glass_pipe_test.py` with at least one test class per R1–R9 requirement. The test command MUST be runnable as `.venv/bin/python -m pytest tests/ -v`.

#### Scenario-1: Harness runs green on passing proxy

- GIVEN the test harness is bootstrapped and the proxy implements R1–R9
- WHEN `.venv/bin/python -m pytest tests/ -v` runs
- THEN all tests pass

#### Scenario-2: Harness fails red on regression

- GIVEN the proxy regresses on R1 (overwrites client temperature)
- WHEN the R1 test class runs
- THEN at least one test fails

#### Scenario-3: Import safety under collection

- GIVEN `tests/conftest.py` is loaded
- WHEN `pytest` collects tests
- THEN no module-level side effect (FlashRank download, lifespan systemd/database init) crashes collection

## Notes

- Open question resolved: `strict_tdd` stays `false` until R10 lands; re-evaluate `strict_tdd` as a follow-up trigger after the harness exists.
- `pytest-asyncio` MUST be installed in `.venv` as part of R10 (not present today).