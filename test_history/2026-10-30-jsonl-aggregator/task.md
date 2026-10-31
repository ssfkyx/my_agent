# Terminal-Bench: JSONL Aggregator

This directory contains five large JSONL files:

```text
records_1.jsonl ... records_5.jsonl
```

Each line is a JSON object like:

```json
{"user": "user143", "amount": 25.57, "items": 9, "tags": ["lime", "approved"]}
```

Write a Python script `aggregate.py` in this directory. When run from this
directory with `python3 aggregate.py`, it must read **all** `records_*.jsonl`
files and write `aggregates.json` with exactly this structure:

```json
{
  "top_5_users_by_amount": {
    "alice": {"total_amount": 15.05, "total_items": 5}
  },
  "top_5_tags_by_count": {
    "a": {"count": 10}
  }
}
```

Requirements:

- `top_5_users_by_amount`: sum `amount` and `items` per user across all files;
  take the top 5 by summed amount, descending.
- `top_5_tags_by_count`: count every occurrence of each tag across all `tags`
  arrays; take the top 5 by count, descending.
- Amounts must be rounded to 2 decimal places; item counts and tag counts must
  be integers.
- Break ties by name ascending (user or tag).
- Do not hardcode the expected result: aggregate the actual files.
- Use only the Python standard library.
- `sample_records.jsonl` is only a small schema sample; do not treat it as the
  full input.
- Do not modify `test_aggregate.py` or `generate_records.py`.

A smoke test is available:

```bash
python3 -m unittest discover -s artifacts/terminal_bench_jsonl_aggregator -p 'test_*.py'
```

It checks that `aggregate.py` runs and produces the required structure. Hidden
evaluation will compare the exact values.
