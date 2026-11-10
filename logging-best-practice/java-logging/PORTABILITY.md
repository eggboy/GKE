# Portability of `GoogleCloudStructuredLogFormatter`

Why the class carries a Google name, which parts of it are Google-specific, and what changes when the same service runs on AKS, EKS, or any other Kubernetes cluster. An independent critic reviewed the first version. A later code review with probes against Spring Boot 4.1.1 corrected the part about repeated keys, and a run on a GKE cluster corrected the parts about `timestamp` and the Dockerfile. The statements about AKS, EKS, Loki and Elastic come from their documentation and were not run.

## Why the name

Spring Boot names its built-in structured formats after the schema they emit: `ecs`, `gelf`, `logstash`. There is no built-in for Google Cloud Logging, which is the only reason this class exists. It emits that `LogEntry` schema (the `logging.googleapis.com/*` keys, the `projects/<id>/traces/<id>` trace name, Cloud Logging severity names), so it is named after it. Everything that is not schema lives in [`AbstractJsonLineFormatter.java`](src/main/java/com/example/logging/AbstractJsonLineFormatter.java) and serves the portable class as well.

## What is Google-specific

| Element in the class | Why it is there | On AKS, EKS, or elsewhere |
| :--- | :--- | :--- |
| Keys `logging.googleapis.com/trace`, `/spanId`, `/trace_sampled` | GKE's built-in agent moves these onto `LogEntry.trace`, `spanId`, `traceSampled`; Logs Explorer and Trace Explorer join on them | Opaque strings nobody reads. Use `trace_id`, `span_id`, `trace_flags` (the OpenTelemetry convention, and the names the Java agent already writes into the MDC) |
| Trace value `projects/<id>/traces/<32 hex>` | Cloud Trace resource name; Cloud Logging only links log and trace when the prefix is present | Bare W3C trace id |
| `GOOGLE_CLOUD_PROJECT` fail-fast (`PROJECT_PROPERTY`, `requiredProjectId`) | Only exists to build that prefix | Gone |
| `logging.googleapis.com/sourceLocation` and `app.logging.source-location` | Moved onto `LogEntry.sourceLocation` | Dropped (it costs a stack walk per event) |
| `logging.googleapis.com/labels` from `addKeyValue` | Moved onto `LogEntry.labels` | Dropped; put labels in ordinary fields |
| `severity` with `WARNING` and `DEBUG` | Cloud Logging's `LogSeverity` enum; GKE's agent sets the entry severity from it | `level` with Logback's names (`WARN`, `TRACE`), which dashboards and parsers expect |
| Members `timestampSeconds` and `timestampNanos`; reserved name `time` | GKE's agent takes the entry's timestamp from the numeric pair and removes it from the payload (seen on a live cluster); `time` is the documented string spelling | Not written; ordinary field names |
| `root: last`, `include-hashes: false` in `application.yaml` | Error Reporting's stack-trace parser needs plain JVM format | Irrelevant; the defaults are fine anyway |
| Caps `max-throwable-depth: 40`, `max-length: 131072`, `app.logging.message-max-length: 65536` | Sized to Cloud Logging's 256 KiB limit, above which a JSON line no longer arrives as a structured entry | Mechanism stays, numbers change (table below) |

## What is portable, unchanged

- **One JSON document per line, stack trace inside the line as `\n`-escaped text.** containerd and CRI-O write one record per line on every distribution, and every agent (Fluent Bit, Fluentd, Vector, the OpenTelemetry Collector `filelog` receiver, Azure Monitor agent, CloudWatch agent) reads them that way. This is the whole reason multi-line Java traces need handling in the application.
- **Caps on the trace and on the message.** `logging.structured.json.stacktrace.*` applies to the built-in formats and to custom ones alike. `app.logging.message-max-length` belongs to this sample's two classes. Only the values are cloud-specific.
- **MDC entries and SLF4J key-value pairs as top-level fields, merged into one map, with reserved names protected.** `stream` stays reserved everywhere: Fluent Bit's CRI parser emits a `stream` field (`stdout`/`stderr`), and a pipeline that merges application JSON into the record without a key prefix collides on it.
- **A line for every event.** `NaN` and `Infinity` are written as strings, because a bare `NaN` is not JSON. If writing the full document throws, the formatter writes timestamp, level, message, logger and thread plus `log_format_error`.
- **The `timestamp` key.** No agent reads it unprompted. GKE `v1.37.1` kept the RFC 3339 string in `jsonPayload.timestamp`; the GKE class therefore also writes `timestampSeconds` and `timestampNanos`, which that agent does read. Elsewhere, tell the agent the key (Fluent Bit `Time_Key`, Collector `timestamp` parser) to get the event time instead of the ingestion time. Keep the field; it costs nothing and records when the event happened.
- **The Dockerfile.** Agent jar readable by the non-root user, agent banner off (`OTEL_JAVAAGENT_LOGGING=none`), no OTLP log export (`OTEL_LOGS_EXPORTER=none`), JVM flags on the command line instead of `JAVA_TOOL_OPTIONS`, JVM stderr quiet (`-Xshare:off`, `-Xlog:disable`), Spring banner off. None of it mentions Google.

## Changes for AKS or EKS

### Option A: no custom code

```yaml
logging:
  structured:
    format:
      console: logstash          # or ecs for Elastic / OpenSearch
    json:
      stacktrace:
        max-throwable-depth: 40
        include-common-frames: false
        max-length: 131072       # resize per backend, table below
```

Spring's built-in `logstash` format writes one JSON line per event, flattens MDC keys (so the agent's `trace_id`, `span_id`, `trace_flags` appear at the top level) and SLF4J key-value pairs, and bounds the stack trace with the same properties. The trace goes into a separate `stack_trace` field instead of `message`, and the level is `level` with Logback names.

> [!WARNING]
> On Spring Boot 4.1.1 the built-in formats lose the whole line when a name repeats. `JsonWriter` throws, Logback catches the exception inside the appender, and nothing reaches stdout.

| Built-in format | Same key in MDC and `addKeyValue` | `addKeyValue` twice with one key | MDC key named `message` |
| :--- | :--- | :--- | :--- |
| `logstash` | line lost | line lost | line lost |
| `ecs` | line lost | line lost | line lost |

Choose Option A when your code never puts one key in both places and never uses a name the format writes itself (`message` was the one tested; `level` and the other built-in members go through the same writer) as an MDC or key-value key. A request filter that fills the MDC plus a call site that repeats one of those keys with `addKeyValue` is enough to trip it.

### Option B: the portable class

Set `logging.structured.format.console: com.example.logging.OpenTelemetryStructuredLogFormatter`. [`OpenTelemetryStructuredLogFormatter.java`](src/main/java/com/example/logging/OpenTelemetryStructuredLogFormatter.java) keeps the line in all three cases of the table above: a key-value pair replaces an MDC entry of the same name, the last of two pairs wins, and a reserved name is dropped. It also appends the stack trace to `message` (one field to grep) and filters `stream` and `_`-prefixed keys. Copy it together with `AbstractJsonLineFormatter.java`; it needs neither `opentelemetry-api` nor an environment variable.

Differences from the GKE class:

1. **Trace keys.** `trace_id`, `span_id` and `trace_flags` are read from the event's MDC map, where the Java agent puts them, and written as members of their own. `logging.structured.json.context.prefix` therefore does not rename them, `context.include=false` does not remove them, and an `addKeyValue("trace_id", ...)` from application code cannot replace them. There is no `Span.current()` fallback, so without the agent the three keys are absent.
2. **Names.** `level` with Logback's names instead of `severity`; no `sourceLocation`, no `labels`.
3. **Caps sized to the backend.** Verify the current limits before pinning; these are the documented figures at the time of writing.

   | Backend | Limit per line or event | Suggested stack trace `max-length` / `max-throwable-depth` |
   | :--- | :--- | :--- |
   | Cloud Logging (GKE) | 256 KiB per `LogEntry`, JSON entries above it are dropped | 131072 / 40 (as shipped) |
   | Azure Monitor Container Insights (AKS) | 16 KB per log line; up to 64 KB with the multiline feature enabled. Application Insights caps a trace message at 32,768 characters | 12288 / 15 to 20 |
   | CloudWatch Logs (EKS) | 256 KB per event for years, raised to 1 MB in 2025 | 131072 / 40 |
   | Grafana Loki | `max_line_size` default 256 KB | 131072 / 40 |

   Size `app.logging.message-max-length` with the same budget in mind; on AKS without the multiline feature, 2048 keeps message plus trace under 16 KB.

4. **Swap the telemetry wiring, not the image.** The image already carries `-javaagent` on its `ENTRYPOINT` command line. On EKS or AKS, give the Deployment plain `OTEL_*` variables pointing at the ADOT Collector or your Collector, or use the OpenTelemetry Operator's `Instrumentation` CR with the SDK-only annotation `instrumentation.opentelemetry.io/inject-sdk: "true"`. Do not use `inject-java: "true"` with this image: the Operator mounts its own agent and adds a second `-javaagent` through `JAVA_TOOL_OPTIONS`, and two agents in one JVM mean duplicate telemetry or classloader failures. `OTEL_LOGS_EXPORTER=none` in the image keeps the upstream agent from exporting logs over OTLP. If you choose the Azure Monitor OpenTelemetry distro for Java (`applicationinsights-agent.jar`) instead, it ignores `OTEL_LOGS_EXPORTER` and ships logs to Application Insights by itself unless you set `APPLICATIONINSIGHTS_INSTRUMENTATION_LOGGING_LEVEL=OFF` (or `instrumentation.logging.level: OFF` in `applicationinsights.json`). Run one log path.
5. **One image, one env var.** [`k8s/java-logging.yaml`](k8s/java-logging.yaml) pins `LOGGING_STRUCTURED_FORMAT_CONSOLE=com.example.logging.GoogleCloudStructuredLogFormatter` in the GKE Deployment, and environment variables override `application.yaml`. When a single image serves multiple clouds, set `logstash` or `com.example.logging.OpenTelemetryStructuredLogFormatter` as the default in [`application.yaml`](src/main/resources/application.yaml) (or omit `console` to keep plain-text logs on laptops) while the GKE Deployment override keeps Cloud Logging JSON active in the cluster.

## Who reads `trace_id` on each platform

| Platform | How the bare `trace_id` gets used |
| :--- | :--- |
| OpenTelemetry Collector (`filelog` receiver) | The operator's `trace` parser config maps `trace_id`, `span_id`, `trace_flags` onto the log record, so any backend the Collector exports to sees them as real trace fields |
| EKS with CloudWatch | Logs Insights: `filter trace_id = "4bf9..."`. The ADOT build of the Java agent also injects `AWS-XRAY-TRACE-ID` into the MDC in X-Ray format; the MDC passthrough forwards it unchanged, which is what the CloudWatch log-to-trace links key on |
| AKS with Container Insights | `ContainerLogV2 \| extend trace_id = tostring(parse_json(LogMessage).trace_id)`; join to Application Insights `requests` and `dependencies` on `operation_Id`, which is the same W3C trace id |
| Grafana Loki and Tempo | A derived field on `trace_id` in the Loki data source turns it into a Tempo link |
| Elastic or OpenSearch | `logging.structured.format.console: ecs` plus the agent settings `otel.instrumentation.common.logging.trace-id-key=trace.id` and `span-id-key=span.id` give ECS-correct `trace.id` and `span.id`, because the ECS formatter nests dotted MDC keys. The stack trace lands in `error.stack_trace`, which Kibana renders natively. The repeated-key warning under Option A applies |

## Shape of the portable class

The schema is all that is left in the class; [`OpenTelemetryStructuredLogFormatterTest.java`](src/test/java/com/example/logging/OpenTelemetryStructuredLogFormatterTest.java) covers it with 13 tests.

```java
public final class OpenTelemetryStructuredLogFormatter extends AbstractJsonLineFormatter {

    private static final Set<String> RESERVED =
            Set.of("timestamp", "level", "message", "logger", "thread", "stream",
                    TAGS, FORMAT_ERROR, TRACE_ID, SPAN_ID, TRACE_FLAGS);

    public OpenTelemetryStructuredLogFormatter(
            Environment environment,
            StackTracePrinter stackTracePrinter,
            ContextPairs contextPairs,
            ThrowableProxyConverter throwableProxyConverter,
            StructuredLoggingJsonMembersCustomizer<?> customizer) {
        super(
                coreMembers(                                  // timestamp, level, message, logger, thread
                        "level",
                        event -> event.getLevel().toString(),
                        messageRenderer(environment, stackTracePrinter, throwableProxyConverter)),
                openTelemetryMembers(contextPairs),           // below
                customizer);
    }

    private static Consumer<JsonWriter.Members<ILoggingEvent>> openTelemetryMembers(
            ContextPairs contextPairs) {
        return members -> {
            members.add(TRACE_ID, event -> mdcValue(event, TRACE_ID)).whenHasLength();
            members.add(SPAN_ID, event -> mdcValue(event, SPAN_ID)).whenHasLength();
            members.add(TRACE_FLAGS, event -> mdcValue(event, TRACE_FLAGS)).whenHasLength();
            members.add(TAGS, AbstractJsonLineFormatter::tags).whenNotEmpty();
            members.add().usingPairs(contextPairs.flat("_", pairs ->
                    pairs.addMapEntries(event -> userFields(event, key -> isUserKey(key, RESERVED)))));
        };
    }
}
```

`AbstractJsonLineFormatter` supplies `coreMembers`, the message with its stack trace and cap, `userFields` (MDC first, then key-value pairs, one map), `tags`, the string form of non-finite numbers, and the fallback line.
