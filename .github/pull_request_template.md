## What this changes and why

## How you checked it

Operating system the suite ran on (Linux, Windows or both), and the BACnet client you tried it with if it touches the wire:

## Checklist

- [ ] One change. For anything larger I opened an issue first
- [ ] Tests added or changed, and the full suite passes on my operating system
- [ ] `ruff check` and `basedpyright` are clean
- [ ] `PICS.md` regenerated (`scripts/make_pics.py`) if I changed a service, object type, property or write rule
- [ ] If I changed `frontend/src`, the rebuilt `src/bacpack/web/spa/` is committed
- [ ] A one-line changelog entry, in words, in this description (the maintainer adds it to the release's entry)
- [ ] I can explain the change, including any part an AI assistant wrote

By opening this pull request you agree that your change is released under the project's MIT license. The maintainer applies accepted changes in the private development repository and closes the pull request with the release that ships it. See [CONTRIBUTING.md](https://github.com/dmpatters/BACpack/blob/main/CONTRIBUTING.md).
