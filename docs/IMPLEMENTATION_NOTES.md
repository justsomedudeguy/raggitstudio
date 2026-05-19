# CustomChat Implementation Notes

This workspace is local-only and designed for a local or API OAI compatible endpoint.

Vision support is intentionally gated by a runtime probe. Configure the vision
model projection files at your endpoint separately, then restart this app; the
UI will enable image and screenshot submission once the probe succeeds.
