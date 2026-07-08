# Glass-Pipe Exceptions Specification

## Purpose

Two intentional, documented Glass-Pipe exceptions (stop-sequence filter when tools present, and `translate_to_deepseek_r1`) are labeled in code. No behavior change. Anchors R2, R8, R9.

## Requirements

### REQ-1: Single value per parameter with documented fallback (R2)

The payload forwarded to the model SHALL contain, for each sampling parameter, exactly one value: the client's value when present, else the intent default. Each intent default MUST be documented in code comments as a fallback at `routes.py`.

#### Scenario-1: Each parameter has one value

- GIVEN a forwarded payload for a request
- WHEN the payload is inspected
- THEN each sampling parameter (`temperature`, `top_p`, `max_tokens`, `thinking_budget_tokens`) appears at most once with a single value

### REQ-2: Stop-sequence filter labeled as Glass-Pipe exception (R8)

The stop-sequence filter at `routes.py` that removes `Observation:` and ```` ```output ```` when tools are present MUST carry an explicit code comment labeling it a `Glass Pipe exception — intentional`. The behavior SHALL stay unchanged.

#### Scenario-1: Stop-seq filter comment present

- GIVEN the stop-sequence filter block in `routes.py`
- WHEN the source is inspected
- THEN a comment labels it an intentional Glass-Pipe exception

### REQ-3: translate_to_deepseek_r1 labeled as Glass-Pipe exception (R9)

The `translate_to_deepseek_r1` function in `llm.py` MUST carry an explicit docstring/exception label marking it a structural Glass-Pipe exception. The behavior SHALL stay unchanged.

#### Scenario-1: translate function label present

- GIVEN the `translate_to_deepseek_r1` function in `llm.py`
- WHEN its docstring is inspected
- THEN it labels the function a Glass-Pipe exception