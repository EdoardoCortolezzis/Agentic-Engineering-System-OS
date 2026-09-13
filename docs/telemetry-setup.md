# Optional telemetry setup

Telemetry is disabled by default. An operator who has reviewed data flow and
retention may enable metadata-only OpenTelemetry export before starting the
agent runtime:

```sh
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.example.invalid/v1/otel
export OTEL_EXPORTER_OTLP_HEADERS='Authorization=Basic <operator-configured-value>'
```

Keep `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`,
`OTEL_LOG_TOOL_DETAILS`, `OTEL_LOG_TOOL_CONTENT`, and
`OTEL_LOG_RAW_API_BODIES` unset unless a separate privacy decision explicitly
authorizes content export. AES does not validate a collector's policy or
credentials; test the endpoint with non-sensitive data and inspect the result.
