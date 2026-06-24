# Project TODO

## Completed Today

- [x] Plugin configuration Web UI
  - Add plugin-owned config schemas.
  - Render plugin config forms in the local admin panel.
  - Persist config overrides to SQLite and apply them at runtime.

- [x] Plugin event bus
  - Add a generic plugin event object and `on_event` hook.
  - Emit core lifecycle events from message, reply, memory, plugin, and admin flows.
  - Keep existing direct hooks compatible.
  - Discover external plugins through Python entry points.
  - Isolate plugin Hook exceptions and timeouts from the core polling loop.

## Next

- [ ] Knowledge base and file memory
  - Upload local files, chunk content, index it, and retrieve relevant snippets for replies.

- [ ] Tool calling system
  - Add a safe tool registry with permissions, parameter validation, and audit logs.

- [ ] Plugin catalog
  - Show plugin metadata, version, config schema, health, errors, and runtime status.

- [ ] Runtime health dashboard
  - Track WeChat connection, polling time, LLM latency, plugin task status, error counts, and recent exceptions.

- [ ] Deployment support
  - Add Docker or Windows service startup scripts and restart guidance.

- [ ] Plugin safety isolation
  - Separate permissions for sending messages, reading memory, writing memory, changing config, and network access.

## Done

- [x] WeChat QR login and session restore.
- [x] Multi-model API management.
- [x] Three-layer memory.
- [x] Proactive response plugin.
- [x] Weather monitor plugin.
- [x] Task, reminder, and schedule plugin.
- [x] Flow-state multi-part message plugin.
- [x] Segmented replies with interruption protection.
