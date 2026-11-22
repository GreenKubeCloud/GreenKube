# greenkube-source-example

A minimal third-party recommendation source for GreenKube, built on the Source
SDK (`greenkube.signals`). It reads container request proposals from a JSON
file and emits them as signals. Use it as a template; the full guide is
[docs/sources.md](../../docs/sources.md).

## Install

Install it into the environment GreenKube runs in, for example in a derived
image:

```dockerfile
FROM greenkube/greenkube:<version>
COPY greenkube-source-example /tmp/greenkube-source-example
RUN pip install --no-cache-dir /tmp/greenkube-source-example
```

The package declares an entry point in the `greenkube.sources` group
(`pyproject.toml`). GreenKube loads it at the next start; it appears in
`GET /api/v1/sources` with `origin: plugin`.

## Configure

```yaml
# Helm values
sources:
  settings:
    example.file:
      enabled: auto            # run when the file exists
      path: /etc/greenkube/example-recommendations.json
```

## Test

From the GreenKube repository:

```bash
uv run --with ./examples/greenkube-source-example pytest examples/greenkube-source-example/tests
```

The tests call `greenkube.signals.testing.check_source_contract`, the same
contract the built-in sources pass.
