# DeepSeek model selection

The public delegation API supports exactly two model profiles:

- `flash` -> `deepseek-v4-flash`
- `pro` -> `deepseek-v4-pro`

All four delegation entry points default to `model="flash"` when the argument is omitted. Use `model="pro"` only when the host decides the task needs the stronger model, for example complex debugging, architecture work, or a retry after Flash is insufficient.

A background job keeps the model chosen when it starts. Steering messages do not change the model of an already-running job.

The `model` field in the legacy config remains accepted for compatibility, but public delegation calls select Flash by default and override it with the per-call `flash` / `pro` choice.
