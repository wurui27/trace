# Device Agent native packages

The three native packages contain the same frozen `perfpilot-agent` core, a fixed
Android `platform-tools` payload, one deployment configuration, and one public
deployment CA certificate. They never contain a registration code, Agent identity,
private key, or runtime token.

Build each binary on its target operating system. The build scripts require absolute
paths to `config.json`, `perfpilot-ca.crt`, and the matching `platform-tools`
directory. They reject a configuration whose native install paths do not match the
target package.

## First internal release warning

These internal `.pkg`, `.msi`, and `.deb` files are unsigned. macOS may show an
unidentified-developer warning because there is no Apple Developer ID signature or
notarization. Windows may show a SmartScreen warning because Windows code signing is
not configured. Verify the published `SHA256SUMS` over a trusted channel before any
manual approval. Public distribution must add Apple notarization and Windows code
signing first.

WiX 6 is used only by the Windows build. Review the current WiX open-source
maintenance terms before commercial distribution.

## Lifecycle

- macOS: install with `sudo installer -pkg ... -target /`; remove with
  `/Library/PerfPilot Agent/uninstall.sh --keep-data` or `--remove-data`.
- Windows: install or remove with `msiexec`; run `uninstall.ps1 -RemoveData` when the
  local DPAPI state and workspace must also be erased.
- Linux: install with `sudo dpkg -i ...`; `dpkg -r` preserves runtime state and
  `dpkg -P` purges it.

An upgrade never deletes registration state. Removing state is an explicit operator
choice.
