# edwarp

`edwarp` is the non-CLI reusable core extracted from `edwh-bundler-plugin`.

## What it contains

- JS/CSS content extraction and bundling helpers
- TypeScript transpilation and dependency inlining
- SCSS/SASS conversion and variable injection
- Config loading from `bundle.yaml`, `bundle.toml`, or `pyproject.toml` (`tool.edwarp.bundle`)

## Basic usage

```python
from edwarp import bundle_js, bundle_css

js = bundle_js(["https://unpkg.com/htmx.org@1.9.2/dist/htmx.min.js"], minify=True)
css = bundle_css(["./styles.scss"], minify=True, scss_variables={"primary": "#000"})
```

```python
from edwarp import build

# builds using bundle.yaml/bundle.toml/pyproject config in cwd
build(config="bundle.yaml", verbose=True)
```
