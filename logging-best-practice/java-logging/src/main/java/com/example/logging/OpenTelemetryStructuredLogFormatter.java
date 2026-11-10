package com.example.logging;

import ch.qos.logback.classic.pattern.ThrowableProxyConverter;
import ch.qos.logback.classic.spi.ILoggingEvent;
import java.util.Map;
import java.util.Set;
import java.util.function.Consumer;
import org.springframework.boot.json.JsonWriter;
import org.springframework.boot.logging.StackTracePrinter;
import org.springframework.boot.logging.structured.ContextPairs;
import org.springframework.boot.logging.structured.StructuredLoggingJsonMembersCustomizer;
import org.springframework.core.env.Environment;

/**
 * Vendor-neutral single-line JSON formatter for Kubernetes clusters outside GKE (AKS, EKS, Loki,
 * OpenTelemetry Collector).
 *
 * <p>Spring Boot's built-in {@code logstash} and {@code ecs} formats already write single-line JSON
 * with MDC flattening and bounded stack traces. Use this class when you also want the stack trace
 * appended to {@code message} (as the GKE formatter does), {@code stream} and {@code _}-prefix
 * filtering, and a line that survives a key used twice: the built-in formats lose the whole line
 * when a key appears in both the MDC and {@code addKeyValue}.
 *
 * <p>The OpenTelemetry Java agent's {@code logback-mdc} instrumentation writes {@code trace_id},
 * {@code span_id} and {@code trace_flags} into the event's MDC map. They are written as members of
 * their own, so {@code logging.structured.json.context.prefix} does not rename them, {@code
 * context.include=false} does not remove them, and an {@code addKeyValue} with one of those names
 * cannot replace them.
 */
public final class OpenTelemetryStructuredLogFormatter extends AbstractJsonLineFormatter {

    static final String TRACE_ID = "trace_id";
    static final String SPAN_ID = "span_id";
    static final String TRACE_FLAGS = "trace_flags";

    private static final Set<String> RESERVED =
            Set.of(
                    "timestamp",
                    "level",
                    "message",
                    "logger",
                    "thread",
                    "stream",
                    TAGS,
                    FORMAT_ERROR,
                    TRACE_ID,
                    SPAN_ID,
                    TRACE_FLAGS);

    /**
     * Called by Spring Boot's {@code StructuredLogFormatterFactory}; every argument is injected.
     *
     * @param environment the Spring environment, used for {@code app.logging.message-max-length}
     * @param stackTracePrinter the printer built from {@code logging.structured.json.stacktrace.*},
     *     or {@code null} when none of those properties is set (Logback's converter is used then)
     * @param contextPairs MDC and key-value pair handling built from {@code
     *     logging.structured.json.context.*}
     * @param throwableProxyConverter Logback's default stack trace renderer
     * @param customizer the {@code include/exclude/rename/add} customizer, or {@code null}
     */
    public OpenTelemetryStructuredLogFormatter(
            Environment environment,
            StackTracePrinter stackTracePrinter,
            ContextPairs contextPairs,
            ThrowableProxyConverter throwableProxyConverter,
            StructuredLoggingJsonMembersCustomizer<?> customizer) {
        super(
                coreMembers(
                        "level",
                        event -> event.getLevel().toString(),
                        messageRenderer(environment, stackTracePrinter, throwableProxyConverter)),
                OpenTelemetryStructuredLogFormatter::traceMembers,
                openTelemetryMembers(contextPairs),
                customizer);
    }

    /**
     * {@code trace_id}, {@code span_id} and {@code trace_flags}. They are on the reduced line as
     * well, so that a line which lost its fields is still found from its request.
     */
    private static void traceMembers(JsonWriter.Members<ILoggingEvent> members) {
        members.add(TRACE_ID, event -> mdcValue(event, TRACE_ID)).whenHasLength();
        members.add(SPAN_ID, event -> mdcValue(event, SPAN_ID)).whenHasLength();
        members.add(TRACE_FLAGS, event -> mdcValue(event, TRACE_FLAGS)).whenHasLength();
    }

    private static Consumer<JsonWriter.Members<ILoggingEvent>> openTelemetryMembers(
            ContextPairs contextPairs) {
        return members -> {
            traceMembers(members);
            members.add(TAGS, AbstractJsonLineFormatter::tags).whenNotEmpty();
            members.add()
                    .usingPairs(
                            contextPairs.flat(
                                    "_",
                                    pairs ->
                                            pairs.addMapEntries(
                                                    event ->
                                                            userFields(
                                                                    event,
                                                                    key ->
                                                                            isUserKey(
                                                                                    key,
                                                                                    RESERVED)))));
        };
    }

    private static String mdcValue(ILoggingEvent event, String key) {
        Map<String, String> mdc = event.getMDCPropertyMap();
        return (mdc != null) ? mdc.get(key) : null;
    }
}
