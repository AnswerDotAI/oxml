# oxml

Office Open XML client.

## Development

```bash
maturin develop && pytest -q
```

In the shared `aai-ws` workspace, run `ws-add oxml` after the first GitHub push to register the project.

## Build

```bash
ship-rs-build
```

## Release

```bash
ship-release
```

`ship-release` tags the Cargo version, leaves wheel publication to GitHub Actions, then bumps the project.
