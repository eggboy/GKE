package com.example.logging;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.pattern.ThrowableProxyConverter;
import ch.qos.logback.classic.spi.ILoggingEvent;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.SpanId;
import io.opentelemetry.api.trace.TraceId;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Consumer;
import org.slf4j.event.KeyValuePair;
import org.springframework.boot.json.JsonWriter;
import org.springframework.boot.logging.StackTracePrinter;
import org.springframework.boot.logging.structured.ContextPairs;
import org.springframework.boot.logging.structured.StructuredLoggingJsonMembersCustomizer;
import org.springframework.core.env.Environment;
import org.springframework.util.StringUtils;

/**
 * Cloud Logging structured JSON on stdout, correlated with the active OpenTelemetry span.
 *
 * <p>GKE's logging agent parses single-line JSON written to stdout and moves the {@code
 * logging.googleapis.com/*} keys onto the LogEntry ({@code trace}, {@code spanId}, {@code
 * traceSampled}, {@code sourceLocation}, {@code labels}). Every line written while a request span
 * is active therefore shows up next to the load balancer and application spans in Trace Explorer
 * and under {@code trace="projects/<id>/traces/<trace_id>"} in Logs Explorer. Application logs are
 * not exported through the OTLP collector; this stdout path is the only one.
 *
 * <p>The agent takes the entry's timestamp from {@code timestampSeconds} and {@code timestampNanos}
 * and removes both from the payload. It does not read the RFC 3339 {@code timestamp} string;
 * without the numeric pair an entry is stamped with the moment the container runtime received the
 * line. Both forms are written: the pair for the agent, the string for whoever reads {@code kubectl
 * logs}.
 *
 * <p>Spring Boot instantiates this class from {@code logging.structured.format.console} and injects
 * every constructor argument: the stack trace limits come from {@code
 * logging.structured.json.stacktrace.*}, the MDC handling from {@code
 * logging.structured.json.context.*}. Exceptions are appended to {@code message} after a newline,
 * which is the layout Error Reporting parses.
 *
 * <p>Behind Logback's {@code AsyncAppender}, set {@code includeCallerData=true} on that appender or
 * {@code sourceLocation} is empty, and rely on the Java agent's MDC keys for the trace: {@code
 * Span.current()} on the appender's worker thread is not the request's span.
 */
public final class GoogleCloudStructuredLogFormatter extends AbstractJsonLineFormatter {

    /** Relaxed-bound from the {@code GOOGLE_CLOUD_PROJECT} environment variable. */
    static final String PROJECT_PROPERTY = "google.cloud.project";

    /** {@code false} skips {@code sourceLocation} and the stack walk per event it costs. */
    static final String SOURCE_LOCATION_PROPERTY = "app.logging.source-location";

    static final String TRACE = "logging.googleapis.com/trace";
    static final String SPAN_ID = "logging.googleapis.com/spanId";
    static final String TRACE_SAMPLED = "logging.googleapis.com/trace_sampled";
    static final String SOURCE_LOCATION = "logging.googleapis.com/sourceLocation";
    static final String LABELS = "logging.googleapis.com/labels";

    /** The event's instant as numbers; GKE's agent turns the pair into the entry's timestamp. */
    static final String TIMESTAMP_SECONDS = "timestampSeconds";

    static final String TIMESTAMP_NANOS = "timestampNanos";

    /** MDC keys written by the OpenTelemetry Java agent's logback-mdc instrumentation. */
    static final String MDC_TRACE_ID = "trace_id";

    static final String MDC_SPAN_ID = "span_id";
    static final String MDC_TRACE_FLAGS = "trace_flags";

    private static final String GOOGLE_PREFIX = "logging.googleapis.com/";

    /*
     * Names that MDC entries and key-value pairs may not use. Most are written by this class.
     * GKE's agent removes "stream" from the payload, and "time" is the documented string spelling
     * of the entry timestamp. The agent's MDC keys are consumed by the trace members.
     */
    private static final Set<String> RESERVED =
            Set.of(
                    "timestamp",
                    "severity",
                    "message",
                    "logger",
                    "thread",
                    "stream",
                    "time",
                    TIMESTAMP_SECONDS,
                    TIMESTAMP_NANOS,
                    TAGS,
                    FORMAT_ERROR,
                    MDC_TRACE_ID,
                    MDC_SPAN_ID,
                    MDC_TRACE_FLAGS);

    /**
     * Called by Spring Boot's {@code StructuredLogFormatterFactory}; every argument is injected.
     *
     * @param environment the Spring environment, used for the project id and the two {@code
     *     app.logging.*} properties
     * @param stackTracePrinter the printer built from {@code logging.structured.json.stacktrace.*},
     *     or {@code null} when none of those properties is set (Logback's converter is used then)
     * @param contextPairs MDC and key-value pair handling built from {@code
     *     logging.structured.json.context.*}
     * @param throwableProxyConverter Logback's default stack trace renderer
     * @param customizer the {@code include/exclude/rename/add} customizer, or {@code null}
     * @throws IllegalStateException if {@code GOOGLE_CLOUD_PROJECT} is unset. Cloud Logging only
     *     links a log entry to its trace when the trace field is the full resource name {@code
     *     projects/<id>/traces/<trace_id>}, so a missing project id would silently break
     *     correlation instead of failing here.
     */
    public GoogleCloudStructuredLogFormatter(
            Environment environment,
            StackTracePrinter stackTracePrinter,
            ContextPairs contextPairs,
            ThrowableProxyConverter throwableProxyConverter,
            StructuredLoggingJsonMembersCustomizer<?> customizer) {
        super(
                cloudLoggingCoreMembers(
                        messageRenderer(environment, stackTracePrinter, throwableProxyConverter)),
                cloudLoggingMembers(
                        requiredProjectId(environment),
                        environment.getProperty(SOURCE_LOCATION_PROPERTY, Boolean.class, true),
                        contextPairs),
                customizer);
    }

    /**
     * The shared core plus the numeric timestamp pair. Being core, the pair is on the fallback line
     * too, so that entry also carries the time of its event.
     */
    private static Consumer<JsonWriter.Members<ILoggingEvent>> cloudLoggingCoreMembers(
            MessageRenderer message) {
        Consumer<JsonWriter.Members<ILoggingEvent>> shared =
                coreMembers("severity", event -> severity(event.getLevel()), message);
        return members -> {
            shared.accept(members);
            members.add(TIMESTAMP_SECONDS, event -> event.getInstant().getEpochSecond());
            members.add(TIMESTAMP_NANOS, event -> event.getInstant().getNano());
        };
    }

    private static Consumer<JsonWriter.Members<ILoggingEvent>> cloudLoggingMembers(
            String projectId, boolean sourceLocation, ContextPairs contextPairs) {
        String tracePrefix = "projects/" + projectId + "/traces/";
        return members -> {
            if (sourceLocation) {
                members.add(SOURCE_LOCATION, GoogleCloudStructuredLogFormatter::callerFrame)
                        .whenNotNull()
                        .usingMembers(
                                location -> {
                                    location.add("file", StackTraceElement::getFileName)
                                            .whenNotNull();
                                    location.add("line", StackTraceElement::getLineNumber);
                                    location.add(
                                            "function",
                                            frame ->
                                                    frame.getClassName()
                                                            + "."
                                                            + frame.getMethodName());
                                });
            }
            members.from(GoogleCloudStructuredLogFormatter::traceContext)
                    .whenNotNull()
                    .usingMembers(
                            trace -> {
                                trace.add(TRACE, context -> tracePrefix + context.traceId());
                                trace.add(SPAN_ID, TraceContext::spanId).whenNotNull();
                                trace.add(TRACE_SAMPLED, TraceContext::sampled);
                            });
            members.add(LABELS, GoogleCloudStructuredLogFormatter::labels).whenNotEmpty();
            members.add(TAGS, AbstractJsonLineFormatter::tags).whenNotEmpty();
            // MDC entries and SLF4J key-value pairs become top-level fields.
            members.add()
                    .usingPairs(
                            contextPairs.flat(
                                    "_",
                                    pairs ->
                                            pairs.addMapEntries(
                                                    event ->
                                                            userFields(
                                                                    event,
                                                                    GoogleCloudStructuredLogFormatter
                                                                            ::isUserKey))));
        };
    }

    private static String requiredProjectId(Environment environment) {
        String projectId = environment.getProperty(PROJECT_PROPERTY);
        if (!StringUtils.hasText(projectId)) {
            throw new IllegalStateException(
                    "GOOGLE_CLOUD_PROJECT (property "
                            + PROJECT_PROPERTY
                            + ") must be set: Cloud Logging only links a log entry to its trace when"
                            + " the trace field is the full resource name"
                            + " projects/<id>/traces/<trace_id>");
        }
        return projectId;
    }

    /** Cloud Logging severity names; Logback's WARN and TRACE are not in the LogSeverity enum. */
    private static String severity(Level level) {
        return switch (level.toInt()) {
            case Level.ERROR_INT -> "ERROR";
            case Level.WARN_INT -> "WARNING";
            case Level.INFO_INT -> "INFO";
            case Level.DEBUG_INT, Level.TRACE_INT -> "DEBUG";
            default -> "DEFAULT";
        };
    }

    private static StackTraceElement callerFrame(ILoggingEvent event) {
        StackTraceElement[] callerData = event.getCallerData();
        return (callerData != null && callerData.length > 0) ? callerData[0] : null;
    }

    /**
     * The agent's MDC keys first: they are captured on the logging thread, so they stay correct
     * under an async appender. {@code Span.current()} covers processes without the agent, and an
     * application that keeps something else under {@code trace_id} (a request id, say): only a
     * well-formed W3C trace id is taken from the MDC.
     */
    private static TraceContext traceContext(ILoggingEvent event) {
        Map<String, String> mdc = event.getMDCPropertyMap();
        String traceId = (mdc != null) ? mdc.get(MDC_TRACE_ID) : null;
        if (TraceId.isValid(traceId)) {
            String spanId = mdc.get(MDC_SPAN_ID);
            return new TraceContext(
                    traceId,
                    SpanId.isValid(spanId) ? spanId : null,
                    sampled(mdc.get(MDC_TRACE_FLAGS)));
        }
        SpanContext spanContext = Span.current().getSpanContext();
        if (!spanContext.isValid()) {
            return null;
        }
        return new TraceContext(
                spanContext.getTraceId(), spanContext.getSpanId(), spanContext.isSampled());
    }

    /** W3C trace flags are one hex byte; bit 0 is the sampled flag. */
    private static boolean sampled(String traceFlags) {
        if (!StringUtils.hasText(traceFlags)) {
            return false;
        }
        try {
            return (Integer.parseInt(traceFlags, 16) & 1) == 1;
        } catch (NumberFormatException ex) {
            return false;
        }
    }

    /**
     * {@code addKeyValue("logging.googleapis.com/labels", Map.of(...))} becomes {@code
     * LogEntry.labels}. Label values are strings in Cloud Logging, so they are converted here; any
     * value that is not a map is ignored.
     */
    private static Map<String, String> labels(ILoggingEvent event) {
        List<KeyValuePair> keyValues = event.getKeyValuePairs();
        if (keyValues == null || keyValues.isEmpty()) {
            return Map.of();
        }
        Map<String, String> labels = new LinkedHashMap<>();
        for (KeyValuePair pair : keyValues) {
            if (pair != null && LABELS.equals(pair.key) && pair.value instanceof Map<?, ?> map) {
                map.forEach(
                        (name, value) -> labels.put(String.valueOf(name), String.valueOf(value)));
            }
        }
        return labels;
    }

    private static boolean isUserKey(String key) {
        return isUserKey(key, RESERVED) && !key.startsWith(GOOGLE_PREFIX);
    }

    private record TraceContext(String traceId, String spanId, boolean sampled) {}
}
