# DeepSeek model selection

The public delegation API supports exactly two stable model profiles:

- `flash`
- `pro`

All four delegation entry points default to `model="flash"` when the argument is omitted. Use `model="pro"` only when the host decides the task needs the stronger model, for example complex debugging, architecture work, or a retry after Flash is insufficient.

The actual provider model IDs and reasoning effort for each slot are user-configurable in `~/.deepseek-mcp/config.json`:

```json
{
  "flash": "deepseek-v4-flash",
  "flash_reasoning_effort": "high",
  "pro": "deepseek-v4-pro",
  "pro_reasoning_effort": "high",
  "_reasoning_effort_options": ["none", "low", "high", "max"]
}
```

`_reasoning_effort_options` is a documentation hint only and is ignored at runtime. The effective fields are `flash_reasoning_effort` and `pro_reasoning_effort`. Supported values are `none`, `low`, `high`, and `max`; `none` disables thinking, while the other values enable thinking at the selected effort.

The strings behind `flash` and `pro` are intentionally not hard-coded in the routing layer. Users can update them when DeepSeek releases new model revisions, or when a compatible API endpoint exposes different model names, without changing the MCP tool API. The host still passes only `model="flash"` or `model="pro"`; it never sends provider model IDs or reasoning effort values directly.

A background job keeps the profile, resolved provider model, and configured reasoning effort selected when it starts. Steering messages do not change them for an already-running job.

For upgrade compatibility, the legacy single `model` config field is still accepted when `flash` and `pro` are absent. In that case its value is used for both slots. Do not combine legacy `model` with the new `flash` / `pro` fields.
