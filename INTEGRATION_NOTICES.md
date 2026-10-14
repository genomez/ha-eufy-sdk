# Integration license and modifications

The integration is distributed under the MIT License in `LICENSE` and `custom_components/eufy_sdk/LICENSE`. Original copyright and license text are preserved without replacement. Derived work credits remain in README.md.

Candidate `0.3.1.dev1` is maintained by `genomez`, independently of upstream. Changes from the public S1 source preview are fork/install metadata, installation/compatibility/rollback documentation, reproducible packaging, and a narrow explicit endpoint reconfigure flow with translated UI and synthetic native-framework tests. The reconfigure flow preserves the existing entry and registry identities, validates connection/authentication before endpoint changes, rejects other-entry endpoint collisions and closes removed/failed validation connections.

No SDK/bridge runtime, bundled third-party Python library, native binary, add-on data or Core overlay is added by this integration package. Optional frontend assets are outside the integration artifact. Its prepared paired container has separate notices/provenance requirements.
