# DeepSeek model selection

The public delegation API supports exactly two stable model profiles:

- `flash`
- `pro`

All four delegation entry points default to `model="flash"` when the argument is omitted. Use `model="pro"` only when the host decides the task needs the stronger model, for example complex debugging, architecture work, or a retry after Flash is insufficient.

The actual provider model IDs are user-configurable in `~/.deepseek-mcp/config.json`:

```json
{
  "flash": "deepseek-v4-flash",
  "pro": "deepseek-v4-pro"
}
```

The strings behind `flash` and `pro` are intentionally not hard-coded in the routing layer. Users can update them when DeepSeek releases new model revisions, or when a compatible API endpoint exposes different model names, without changing the MCP tool API.

A background job keeps the profile and resolved provider model selected when it starts. Steering messages do not change the model of an already-running job.

For upgrade compatibility, the legacy single `model` config field is still accepted when `flash` and `pro` are absent. In that case its value is used for both slots. Do not combine legacy `model` with the new `flash` / `pro` fields.
