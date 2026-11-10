# java-logging: Cloud Logging JSON for Spring Boot on GKE

A Spring Boot service whose logs suit Cloud Logging on GKE. Every line the service writes to stdout is one JSON document that GKE's logging agent turns into a `LogEntry` with `severity`, `timestamp`, `sourceLocation`, `labels`, and the `trace` / `spanId` / `traceSampled` fields that join it to the Cloud Trace waterfall. Stack traces travel inside the line, never as separate lines.

Two lines from `kubectl logs` on a GKE cluster, under the Java agent (project id replaced; the stack trace in the second line is cut here for display):

```json
{"timestamp":"2026-11-10T06:12:20.630193537Z","severity":"INFO","message":"placed order","logger":"com.example.logging.OrderController","thread":"http-nio-8080-exec-9","timestampSeconds":1794291140,"timestampNanos":630193537,"logging.googleapis.com/sourceLocation":{"file":"OrderController.java","line":56,"function":"com.example.logging.OrderController.placeOrder"},"logging.googleapis.com/trace":"projects/PROJECT_ID/traces/7d1f0e5a9c3b4a6e8f2d1c0b9a8e7f81","logging.googleapis.com/spanId":"16d9c441202f954b","logging.googleapis.com/trace_sampled":true,"order_id":"ord-0e5223c7","cart_id":"cart-1001","total":67.97}
{"timestamp":"2026-11-10T06:12:21.429422902Z","severity":"ERROR","message":"request failed\njava.lang.IllegalStateException: payment gateway timed out\n\tat com.example.logging.OrderController.boom(OrderController.java:63)\n\tat java.base/jdk.internal.reflect.DirectMethodHandleAccessor.invoke(Unknown Source)\n ... \nCaused by: java.net.SocketTimeoutException: read timed out\n\t... 45 more","logger":"com.example.logging.ApiExceptionHandler","thread":"http-nio-8080-exec-10","timestampSeconds":1794291141,"timestampNanos":429422902,"logging.googleapis.com/sourceLocation":{"file":"ApiExceptionHandler.java","line":37,"function":"com.example.logging.ApiExceptionHandler.handleUnexpected"},"logging.googleapis.com/trace":"projects/PROJECT_ID/traces/7d1f0e5a9c3b4a6e8f2d1c0b9a8e7f82","logging.googleapis.com/spanId":"5d634941f19bf58a","logging.googleapis.com/trace_sampled":true}
```

Run with `java -jar` on a laptop, without the agent, the lines lack `logging.googleapis.com/trace`, `/spanId` and `/trace_sampled`.

## What is in here

| File | Role |
| :--- | :--- |
| [`GoogleCloudStructuredLogFormatter.java`](src/main/java/com/example/logging/GoogleCloudStructuredLogFormatter.java) | The Cloud Logging schema: severity names, the `logging.googleapis.com/*` keys, the project-prefixed trace. A Spring Boot `StructuredLogFormatter` (3.4+), no Logback XML. |
| [`AbstractJsonLineFormatter.java`](src/main/java/com/example/logging/AbstractJsonLineFormatter.java) | What both formatters share: message plus stack trace, MDC and key-value pairs merged into one map, the message cap, `NaN` as a string, the fallback line. Copy it along with either formatter. |
| [`OpenTelemetryStructuredLogFormatter.java`](src/main/java/com/example/logging/OpenTelemetryStructuredLogFormatter.java) | Vendor-neutral schema for AKS, EKS, Loki, or Collector pipelines (see [`PORTABILITY.md`](PORTABILITY.md)). |
| [`application.yaml`](src/main/resources/application.yaml) | Activates the formatter and bounds stack traces and messages. |
| [`GoogleCloudStructuredLogFormatterTest.java`](src/test/java/com/example/logging/GoogleCloudStructuredLogFormatterTest.java), [`OpenTelemetryStructuredLogFormatterTest.java`](src/test/java/com/example/logging/OpenTelemetryStructuredLogFormatterTest.java) | Both formatters through Spring's `StructuredLogEncoder`. [`LogCapture.java`](src/test/java/com/example/logging/LogCapture.java) parses every line with strict duplicate detection. |
| [`ApiExceptionHandlerTest.java`](src/test/java/com/example/logging/ApiExceptionHandlerTest.java), [`JavaLoggingApplicationTest.java`](src/test/java/com/example/logging/JavaLoggingApplicationTest.java) | Status codes and log levels of the handler; the whole application with its real `application.yaml`, asserting on stdout. |
| [`OrderController.java`](src/main/java/com/example/logging/OrderController.java), [`ApiExceptionHandler.java`](src/main/java/com/example/logging/ApiExceptionHandler.java) | Three endpoints to see a key-value line, a 404 and a 500 with a `Caused by:` chain. |
| [`Dockerfile`](Dockerfile) | Runs under the OpenTelemetry Java agent with nothing but JSON on stdout and stderr. |
| [`k8s/java-logging.yaml`](k8s/java-logging.yaml) | `Instrumentation` CR with `logger_provider: null` plus Deployment (pinning `LOGGING_STRUCTURED_FORMAT_CONSOLE`) and Service for the `glb-trace-adk` cluster. |

## Run it

Spring Boot 4.1, Java 21, Maven. Note for Boot 4: `spring-boot-starter-webmvc` no longer brings JSON; `spring-boot-starter-jackson` is declared, otherwise every response returns 406.

```bash
mvn spotless:apply verify                                   # format, compile, 53 tests
GOOGLE_CLOUD_PROJECT=my-project java -jar target/java-logging-0.1.0.jar
curl -X POST 'localhost:8080/orders?cartId=cart-1001'       # INFO line with order_id, cart_id, total
curl -X POST 'localhost:8080/orders?cartId=cart-9'          # 404, WARNING line with cart_id
curl localhost:8080/boom                                    # 500, ERROR line with the stack trace in message
curl localhost:8080/nope                                    # 404, nothing logged
```

Without `GOOGLE_CLOUD_PROJECT` `GoogleCloudStructuredLogFormatter` refuses to start: Cloud Logging links a log entry to its trace when the trace field carries the full resource name `projects/<id>/traces/<trace_id>`, so a missing project would break correlation without warning. Local runs set any value, or override the format with `LOGGING_STRUCTURED_FORMAT_CONSOLE=logstash` (or `com.example.logging.OpenTelemetryStructuredLogFormatter`). [`k8s/java-logging.yaml`](k8s/java-logging.yaml) pins `LOGGING_STRUCTURED_FORMAT_CONSOLE` in the GKE Deployment so you can change or remove the property in `application.yaml` for local dev or other clouds without GKE falling back to plain text.

On the cluster, `scripts/02-build-deploy-gke.sh` in `glb-trace-adk` is the model: build the image with Cloud Build, `kubectl apply -f k8s/java-logging.yaml`, then `gcloud logging read 'trace="projects/PROJECT_ID/traces/<id>"'` shows the Java entries next to the load balancer entries.

Two properties belong to this sample's formatters, not to Spring Boot:

| Property | Default | Effect |
| :--- | :--- | :--- |
| `app.logging.message-max-length` | `65536` | Characters of the log message kept; the rest becomes `... [N more characters]`. The stack trace keeps its own limits. `0` disables the cap. |
| `app.logging.source-location` | `true` | `false` drops `logging.googleapis.com/sourceLocation` and the stack walk per event behind it. GKE formatter only. |

## What goes into each line

| Field | Source |
| :--- | :--- |
| `timestamp` | `ILoggingEvent.getInstant()`, nanosecond ISO-8601 `Z`. For people reading `kubectl logs`; GKE leaves it in `jsonPayload.timestamp` |
| `timestampSeconds`, `timestampNanos` | the same instant as two numbers. GKE's agent makes them the entry's timestamp and removes them from `jsonPayload`, so the entry carries the time of the event |
| `severity` | explicit map: `TRACE`,`DEBUG` to `DEBUG`, `WARN` to `WARNING` (Logback's names are not in Cloud Logging's enum) |
| `message` | `getFormattedMessage()`, then `\n` and the stack trace when the event carries an exception. The message part is capped, and a null message is `""` |
| `logger`, `thread` | `getLoggerName()`, `getThreadName()` |
| `logging.googleapis.com/sourceLocation` | `getCallerData()[0]`; costs a stack walk per event, like `%file:%line`; `app.logging.source-location=false` turns it off |
| `logging.googleapis.com/trace`, `/spanId`, `/trace_sampled` | MDC keys `trace_id`/`span_id`/`trace_flags` written by the Java agent first (captured on the logging thread, so correct under async appenders), `Span.current()` as the fallback. An MDC `trace_id` that is not a W3C trace id (an application's request id) is ignored |
| `logging.googleapis.com/labels` | `addKeyValue("logging.googleapis.com/labels", Map.of(...))`; becomes `LogEntry.labels` |
| `tags` | SLF4J marker names |
| every other top-level key | MDC entries and SLF4J `addKeyValue(...)` pairs, merged into one map: a key-value pair replaces an MDC entry of the same name |
| names user data cannot use | the fields above, keys starting with `_`, `stream` (GKE removes it), `time`, `log_format_error`, and any other `logging.googleapis.com/*` key |
| values | numbers, booleans, strings, maps and lists as JSON; anything else through `toString()`; `NaN` and `Infinity` as strings, because bare they are not JSON |

## The stack-trace caveat, and why the JSON looks like this

A Java exception printed as text is 30 to 80 lines. The container runtime records one line at a time, so without structured logging each `\tat` frame becomes its own `LogEntry`, at stdout's default severity and without the trace ID. The open-source answer for Kubernetes: never emit multi-line text; write one JSON object per event with the trace as an escaped string. Fluent Bit's built-in `java` multiline parser exists for the case where you own the agent configuration, which you do not on GKE.

Two Java-specific consequences, both handled in `application.yaml`:

- **A JSON line above 256 KiB does not arrive as a structured entry.** The documentation says Cloud Logging drops it. On the test cluster a 300 KB line arrived as two plain-text entries at `INFO`, 62 KB short, and a 200 KB line arrived intact. Java traces through Tomcat, Spring MVC and a client library can get there, and so can a logged request body. `logging.structured.json.stacktrace.max-throwable-depth: 40` with `include-common-frames: false` bounds every `Caused by:` block; `max-length: 131072` is the last-resort cap on the trace, kept high because it chops the tail of the text, and with Java's default root-last order the tail is the root cause. `app.logging.message-max-length: 65536` bounds the message in front of it. Key-value pairs are not bounded; keep payloads out of them.
- **Error Reporting parses the trace out of `message`** when it is in plain JVM format. Keep `root: last` and `include-hashes: false` (the defaults); `root: first` rewrites the captions to `Wrapped by:`. Putting the trace in `message` rather than a separate `stack_trace` field also keeps the log message as the error's title; with a `stack_trace` field Error Reporting takes the title from the exception header instead.

## Checked on a live cluster

GKE `v1.37.1-gke.3020000` with Managed OpenTelemetry, 10 November 2026. Hand-made lines written to stdout by a throwaway pod:

| Line on stdout | What arrived in Cloud Logging |
| :--- | :--- |
| JSON with `severity`, `message`, extra keys | `jsonPayload`, entry severity set |
| `"ratio":NaN` (bare) | one `textPayload` entry at `INFO`; severity and fields lost |
| `"ratio":"NaN"` | `jsonPayload` |
| one key twice | `jsonPayload`, last value kept (the documentation calls it unsupported) |
| 300 KB JSON line | two `textPayload` entries at `INFO`, 200,000 and 37,921 characters |
| 200 KB JSON line | `jsonPayload`, intact |
| `timestamp` as RFC 3339 string | stays in `jsonPayload.timestamp`; the entry is stamped with the time the runtime wrote the line |
| `time` as RFC 3339 string | stays in `jsonPayload.time` |
| `timestampSeconds` and `timestampNanos` | become the entry's timestamp and leave `jsonPayload` |
| `logging.googleapis.com/labels` object | `LogEntry.labels` |
| `stream` | removed from `jsonPayload` |

The sample itself, called with a `traceparent` header:

| Request | Status | Entries under `trace="projects/.../traces/<id>"` |
| :--- | :--- | :--- |
| `POST /orders?cartId=cart-1001`, sampled | 200 | one `INFO` with `spanId`, `traceSampled: true`, `sourceLocation`, `order_id`, `cart_id`, `total` |
| same, unsampled | 200 | one `INFO`, trace-linked, `traceSampled` false |
| `GET /boom` | 500 | one `ERROR`, 3,902-character `message` holding the whole stack trace |
| `POST /orders?cartId=cart-9` | 404 | one `WARNING` with `cart_id` |
| `GET /nope` | 404 | none |
| `POST /orders` without `cartId` | 400 | none |
| `GET /orders` | 405 | one `WARNING` from Spring (`Request method 'GET' is not supported`), no stack trace |

Those requests ran against an image that wrote only the `timestamp` string, and each entry was stamped 2 to 13 ms after its event: the moment the container runtime received the line. With `timestampSeconds` and `timestampNanos` on the line, a second run gave entries whose timestamp equals `jsonPayload.timestamp` to the nanosecond, with the numeric pair gone from `jsonPayload`.

## Trace correlation and the traps around it

The `Instrumentation` CR injects `OTEL_*` environment variables, nothing else. The Java agent in the Dockerfile reads them, and its `logback-mdc` instrumentation is what fills the MDC keys the formatter reads.

| Trap | Effect | Where it is handled |
| :--- | :--- | :--- |
| Agent exports logs over OTLP by default | every line ingested twice (stdout JSON and OTLP) | `logger_provider: null` in the CR, `OTEL_LOGS_EXPORTER=none` in the image (for Azure's `applicationinsights-agent.jar`, set `APPLICATIONINSIGHTS_INSTRUMENTATION_LOGGING_LEVEL=OFF` instead) |
| Upstream OpenTelemetry Operator `inject-java: "true"` with this image | mounts and attaches a second `-javaagent` next to the one in the `ENTRYPOINT` | pass plain `OTEL_*` env vars or use `instrumentation.opentelemetry.io/inject-sdk: "true"` |
| Agent jar fetched with `ADD <url>` | file mode 0600 owned by root; the non-root user cannot read it and the JVM exits at startup | `chmod 0644` in the build stage |
| Agent prints its banner to stderr | plain-text `ERROR` entries at startup | `OTEL_JAVAAGENT_LOGGING=none` |
| JVM flags in `JAVA_TOOL_OPTIONS` | the JVM prints `Picked up JAVA_TOOL_OPTIONS: ...` on stderr at every start: an `ERROR` entry | flags on the `ENTRYPOINT` command line |
| Class data sharing with an agent on the bootstrap class path | `warning: Sharing is only supported for boot loader classes` on stderr: an `ERROR` entry | `-Xshare:off` |
| GC logs and other JVM logging | plain-text entries | `-Xlog:disable`, `-XX:+ExitOnOutOfMemoryError` |
| Spring Boot banner | multi-line plain text | `spring.main.banner-mode=off` |
| `System.out.println`, `printStackTrace()` | bypass Logback | code review or a Checkstyle `RegexpSingleline` rule |
| `@ExceptionHandler(Exception.class)` on its own | every client mistake (missing parameter, unknown path, wrong method) becomes a 500 and an `ERROR` entry with a stack trace | `ApiExceptionHandler extends ResponseEntityExceptionHandler` |
| A name written twice, or a value whose `toString()` throws | Spring Boot's `JsonWriter` throws inside the appender and the line never reaches stdout. `logging.structured.json.add` or `rename` producing a name that an MDC key also uses is one way in | the formatter writes timestamp, severity, message, logger, thread and `log_format_error` instead; alert on `jsonPayload.log_format_error:*` |
| Logback `AsyncAppender` | `sourceLocation` is empty and the `Span.current()` fallback sees the worker thread | `includeCallerData=true` on the appender; the agent's MDC keys carry the trace |
| Health probes become root traces | kubelet and load balancer probes traced at the CR's ratio 1.0 | not handled here; the Java agent needs a sampler extension (`RuleBasedRoutingSampler`) or you accept the noise |
| Formatter built for every Spring context | `@SpringBootTest` fails without a project id | `JavaLoggingApplicationTest` sets `google.cloud.project` as a test property; the formatter tests use a `MockEnvironment` |

## Using it outside GKE

Spring names its built-in structured formats after the schema they emit (`ecs`, `gelf`, `logstash`), and `GoogleCloudStructuredLogFormatter` follows that convention for Cloud Logging. On AKS, EKS, Loki, or Elastic, `logging.structured.format.console: logstash` (or `ecs`) needs no custom code, with one condition: on Spring Boot 4.1.1 those formats lose the whole line when a key appears in both the MDC and `addKeyValue`. [`OpenTelemetryStructuredLogFormatter.java`](src/main/java/com/example/logging/OpenTelemetryStructuredLogFormatter.java) keeps the line, puts the stack trace inside `message`, and filters `stream` and `_`-prefixed keys. [`PORTABILITY.md`](PORTABILITY.md) covers the choice, per-cloud size limits, operator annotations, and trace correlation queries.

## References

- Spring Boot reference, [structured logging](https://docs.spring.io/spring-boot/reference/features/logging.html#features.logging.structured) (formats, `StructuredLogFormatter`, stack trace properties)
- Cloud Logging, [structured logging special fields](https://cloud.google.com/logging/docs/structured-logging) and [quotas](https://cloud.google.com/logging/quotas)
- GKE, [about GKE logs](https://cloud.google.com/kubernetes-engine/docs/concepts/about-logs) (single-line JSON, duplicate keys, `stream`, oversized entries)
- Error Reporting, [formatting error messages](https://cloud.google.com/error-reporting/docs/formatting-error-messages)
- OpenTelemetry Java agent, [configuration](https://opentelemetry.io/docs/zero-code/java/agent/configuration/) and the [`logback-mdc`](https://github.com/open-telemetry/opentelemetry-java-instrumentation/tree/main/instrumentation/logback/logback-mdc-1.0) instrumentation
- Fluent Bit, [multiline parsing](https://docs.fluentbit.io/manual/data-pipeline/filters/multiline-stacktrace)
