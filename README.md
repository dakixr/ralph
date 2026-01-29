# Ralph

A CLI harness for running LLM agents on PRD-driven tasks.

## Installation

```bash
pip install -e .
```

## Usage

```bash
# Initialize in a git repository
ralph init --name "my-project" --language "python"

# Edit .ralph/prd.json to add work items

# Run the harness
ralph run

# Check status
ralph status
```
