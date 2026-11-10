package com.example.logging;

import static com.example.logging.AbstractJsonLineFormatter.COMMONS_LOGGING_MARKER;
import static com.example.logging.AbstractJsonLineFormatter.FORMAT_ERROR;
import static com.example.logging.AbstractJsonLineFormatter.MESSAGE_MAX_LENGTH_PROPERTY;
import static com.example.logging.AbstractJsonLineFormatter.TAGS;
import static com.example.logging.GoogleCloudStructuredLogFormatter.LABELS;
import static com.example.logging.GoogleCloudStructuredLogFormatter.MDC_SPAN_ID;
import static com.example.logging.GoogleCloudStructuredLogFormatter.MDC_TRACE_FLAGS;
import static com.example.logging.GoogleCloudStructuredLogFormatter.MDC_TRACE_ID;
import static com.example.logging.GoogleCloudStructuredLogFormatter.PROJECT_PROPERTY;
import static com.example.logging.GoogleCloudStructuredLogFormatter.SOURCE_LOCATION;
import static com.example.logging.GoogleCloudStructuredLogFormatter.SOURCE_LOCATION_PROPERTY;
import static com.example.logging.GoogleCloudStructuredLogFormatter.SPAN_ID;
import static com.example.logging.GoogleCloudStructuredLogFormatter.TIMESTAMP_NANOS;
import static com.example.logging.GoogleCloudStructuredLogFormatter.TIMESTAMP_SECONDS;
import static com.example.logging.GoogleCloudStructuredLogFormatter.TRACE;
import static com.example.logging.GoogleCloudStructuredLogFormatter.TRACE_SAMPLED;
import static com.example.logging.LogCapture.nested;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import ch.qos.logback.classic.Logger;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.samplers.Sampler;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.slf4j.MDC;
import org.slf4j.Marker;
import org.slf4j.MarkerFactory;
import org.springframework.core.env.Environment;
import org.springframework.core.env.SystemEnvironmentPropertySource;
import org.springframework.mock.env.MockEnvironment;

/**
 * The Cloud Logging formatter through Spring's own encoder: the schema, trace correlation, stack
 * traces and user fields first, then Spring Boot's customizer, the reduced line, Logback's async
 * appender, non-finite numbers and the size of a Cloud Logging entry.
 */
class GoogleCloudStructuredLogFormatterTest {

    private static final String PROJECT_ID = "unit-test-project";
    private static final String AGENT_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736";
    private static final String AGENT_SPAN_ID = "00f067aa0ba902b7";
    private static final int CLOUD_LOGGING_ENTRY_LIMIT_BYTES = 256 * 1024;

    private LogCapture capture;

    @AfterEach
    void tearDown() {
        if (capture != null) {
            capture.close();
        }
    }

    private static MockEnvironment environment() {
        return new MockEnvironment().withProperty(PROJECT_PROPERTY, PROJECT_ID);
    }

    private static Object valueWhoseToStringThrows() {
        return new Object() {
            @Override
            public String toString() {
                throw new IllegalStateException("toString failed");
            }
        };
    }

    private Logger start(Environment environment) {
        capture = LogCapture.start(GoogleCloudStructuredLogFormatter.class, environment);
        return capture.logger();
    }

    private static void putAgentMdcKeys(String traceFlags) {
        MDC.put(MDC_TRACE_ID, AGENT_TRACE_ID);
        MDC.put(MDC_SPAN_ID, AGENT_SPAN_ID);
        MDC.put(MDC_TRACE_FLAGS, traceFlags);
    }

    private static SpanContext inSpan(Sampler sampler, Runnable action) {
        try (SdkTracerProvider provider = SdkTracerProvider.builder().setSampler(sampler).build()) {
            Span span = provider.get("tests").spanBuilder("checkout").startSpan();
            try (Scope ignored = span.makeCurrent()) {
                action.run();
            } finally {
                span.end();
            }
            return span.getSpanContext();
        }
    }

    @Test
    void recordOutsideASpanHasReservedFieldsAndNoTraceKeys() {
        Logger logger = start(environment());

        logger.warn("cart {} missing", "cart-9");
        Map<String, Object> payload = capture.json();

        assertThat(payload)
                .containsEntry("severity", "WARNING")
                .containsEntry("message", "cart cart-9 missing")
                .containsEntry("logger", logger.getName());
        assertThat((String) payload.get("timestamp")).endsWith("Z");
        assertThat(nested(payload, SOURCE_LOCATION))
                .containsEntry(
                        "function",
                        getClass().getName() + ".recordOutsideASpanHasReservedFieldsAndNoTraceKeys")
                .containsKeys("file", "line");
        assertThat(payload.keySet())
                .noneMatch(key -> key.startsWith("logging.googleapis.com/trace"))
                .doesNotContain(SPAN_ID);
    }

    @ParameterizedTest
    @ValueSource(booleans = {true, false})
    void recordInsideASpanCarriesFullTraceResourceName(boolean sampled) {
        Logger logger = start(environment());

        SpanContext spanContext =
                inSpan(
                        sampled ? Sampler.alwaysOn() : Sampler.alwaysOff(),
                        () -> logger.info("placed order"));
        Map<String, Object> payload = capture.json();

        assertThat(payload)
                .containsEntry(
                        TRACE, "projects/" + PROJECT_ID + "/traces/" + spanContext.getTraceId())
                .containsEntry(SPAN_ID, spanContext.getSpanId())
                .containsEntry(TRACE_SAMPLED, sampled);
        assertThat(Span.current().getSpanContext().isValid()).isFalse();
    }

    @Test
    void agentMdcKeysWinOverTheCurrentSpan() {
        Logger logger = start(environment());
        putAgentMdcKeys("01");

        logger.info("from the agent");
        Map<String, Object> payload = capture.json();

        assertThat(payload)
                .containsEntry(TRACE, "projects/" + PROJECT_ID + "/traces/" + AGENT_TRACE_ID)
                .containsEntry(SPAN_ID, AGENT_SPAN_ID)
                .containsEntry(TRACE_SAMPLED, true)
                .doesNotContainKeys(MDC_TRACE_ID, MDC_SPAN_ID, MDC_TRACE_FLAGS);
    }

    @Test
    void unsampledAgentTraceFlagsGiveTraceSampledFalse() {
        Logger logger = start(environment());
        putAgentMdcKeys("00");

        logger.info("from the agent");

        assertThat(capture.json())
                .containsEntry(TRACE, "projects/" + PROJECT_ID + "/traces/" + AGENT_TRACE_ID)
                .containsEntry(TRACE_SAMPLED, false);
    }

    @Test
    void applicationOwnedMdcTraceIdDoesNotReplaceTheCurrentSpan() {
        Logger logger = start(environment());

        SpanContext spanContext =
                inSpan(
                        Sampler.alwaysOn(),
                        () -> {
                            MDC.put(MDC_TRACE_ID, "req-12345");
                            MDC.put(MDC_SPAN_ID, "not-a-span-id");
                            logger.info("placed order");
                        });
        Map<String, Object> payload = capture.json();

        assertThat(payload)
                .containsEntry(
                        TRACE, "projects/" + PROJECT_ID + "/traces/" + spanContext.getTraceId())
                .containsEntry(SPAN_ID, spanContext.getSpanId())
                .doesNotContainKeys(MDC_TRACE_ID, MDC_SPAN_ID);
    }

    @Test
    void exceptionStackTraceIsAppendedToMessageOnOneLine() {
        Logger logger = start(environment());

        logger.error(
                "order failed",
                new IllegalStateException("boom", new IllegalArgumentException("root")));
        Map<String, Object> payload = capture.json();

        assertThat(payload).containsEntry("severity", "ERROR");
        assertThat((String) payload.get("message"))
                .startsWith("order failed\njava.lang.IllegalStateException: boom\n\tat ")
                .contains("\nCaused by: java.lang.IllegalArgumentException: root");
    }

    @Test
    void stackTraceDepthIsBoundedByProperties() {
        Logger logger =
                start(
                        environment()
                                .withProperty(
                                        "logging.structured.json.stacktrace.max-throwable-depth",
                                        "1"));

        logger.error("order failed", new IllegalStateException("boom"));

        assertThat((String) capture.json().get("message"))
                .startsWith("order failed\njava.lang.IllegalStateException: boom\n\tat ")
                .containsOnlyOnce("\tat ");
    }

    @Test
    void stackTraceLengthIsBoundedByProperties() {
        int maxLength = 200;
        Logger logger =
                start(
                        environment()
                                .withProperty(
                                        "logging.structured.json.stacktrace.max-length",
                                        String.valueOf(maxLength)));

        logger.error("order failed", new IllegalStateException("boom"));
        String message = (String) capture.json().get("message");

        assertThat(message).startsWith("order failed\njava.lang.IllegalStateException: boom\n");
        assertThat(message.length()).isLessThanOrEqualTo("order failed\n".length() + maxLength);
    }

    @Test
    void nullMessageWithAThrowableIsTheStackTraceAlone() {
        Logger logger = start(environment());

        logger.error(null, new IllegalStateException("boom"));

        assertThat((String) capture.json().get("message"))
                .startsWith("java.lang.IllegalStateException: boom\n\tat ");
    }

    @Test
    void nullMessageIsWrittenAsAnEmptyString() {
        Logger logger = start(environment());

        logger.info(null);

        assertThat(capture.json()).containsEntry("message", "");
    }

    @Test
    void longMessageIsCutAtTheConfiguredLength() {
        Logger logger = start(environment().withProperty(MESSAGE_MAX_LENGTH_PROPERTY, "10"));

        logger.error("0123456789abcdef", new IllegalStateException("boom"));

        assertThat((String) capture.json().get("message"))
                .startsWith(
                        "0123456789... [6 more characters]\njava.lang.IllegalStateException: boom");
    }

    @Test
    void defaultMessageCapKeepsAHugeMessageUnderTheCloudLoggingEntryLimit() {
        Logger logger = start(environment());

        logger.info("payload={}", "x".repeat(300_000));
        String line = capture.line();

        assertThat(line.getBytes(StandardCharsets.UTF_8).length)
                .isLessThan(CLOUD_LOGGING_ENTRY_LIMIT_BYTES);
        assertThat((String) LogCapture.parse(line).get("message")).endsWith(" more characters]");
    }

    @Test
    void extraFieldsBecomeTopLevelKeysButCannotShadowReservedOnes() {
        Logger logger = start(environment());
        MDC.put("order_id", "ord-1");
        MDC.put("severity", "DEBUG");
        MDC.put("_private", "1");
        MDC.put("stream", "stderr");

        logger.atInfo().addKeyValue("total", 67.97).addKeyValue("message", "shadow").log("placed");
        Map<String, Object> payload = capture.json();

        assertThat(payload)
                .containsEntry("order_id", "ord-1")
                .containsEntry("total", 67.97)
                .containsEntry("severity", "INFO")
                .containsEntry("message", "placed")
                .doesNotContainKeys("_private", "stream");
    }

    @Test
    void timestampPairAndTimestampStringAreTheInstantOfTheEvent() {
        Logger logger = start(environment());

        logger.info("placed");
        Map<String, Object> payload = capture.json();
        Instant eventInstant = capture.events().getFirst().getInstant();

        assertThat(instantFromTimestampPair(payload)).isEqualTo(eventInstant);
        assertThat(Instant.parse((String) payload.get("timestamp"))).isEqualTo(eventInstant);
    }

    @Test
    void userDataCannotMoveTheEntryTimestamp() {
        Logger logger = start(environment());
        MDC.put("time", "1999-01-01T00:00:00Z");

        logger.atInfo()
                .addKeyValue(TIMESTAMP_SECONDS, 1)
                .addKeyValue(TIMESTAMP_NANOS, 2)
                .addKeyValue("elapsed_ms", 12)
                .log("placed");
        Map<String, Object> payload = capture.json();

        assertThat(instantFromTimestampPair(payload))
                .isEqualTo(capture.events().getFirst().getInstant());
        assertThat(payload)
                .containsEntry("elapsed_ms", 12)
                .doesNotContainKeys("time", FORMAT_ERROR);
    }

    private static Instant instantFromTimestampPair(Map<String, Object> payload) {
        return Instant.ofEpochSecond(
                ((Number) payload.get(TIMESTAMP_SECONDS)).longValue(),
                ((Number) payload.get(TIMESTAMP_NANOS)).longValue());
    }

    @Test
    void duplicateKeysAcrossMdcAndKeyValuePairsEmitSingleKey() {
        Logger logger = start(environment());
        MDC.put("order_id", "ord-from-mdc");
        MDC.put("region", "asia-east1");

        logger.atInfo()
                .addKeyValue("order_id", "ord-from-kv")
                .addKeyValue("attempt", 1)
                .addKeyValue("attempt", 2)
                .log("placed");
        String line = capture.line();

        assertThat(line).containsOnlyOnce("\"order_id\":").containsOnlyOnce("\"attempt\":");
        assertThat(LogCapture.parse(line))
                .containsEntry("order_id", "ord-from-kv")
                .containsEntry("attempt", 2)
                .containsEntry("region", "asia-east1")
                .doesNotContainKey(FORMAT_ERROR);
    }

    @Test
    void nonJsonValuesFallBackToToString() {
        Logger logger = start(environment());

        logger.atInfo().addKeyValue("when", new Object()).log("x");

        assertThat((String) capture.json().get("when")).startsWith("java.lang.Object@");
    }

    @Test
    void nonFiniteNumbersAreWrittenAsStrings() {
        Logger logger = start(environment());

        logger.atInfo()
                .addKeyValue("ratio", Double.NaN)
                .addKeyValue("limit", Double.POSITIVE_INFINITY)
                .addKeyValue("score", Float.NaN)
                .addKeyValue("stats", Map.of("p99", Double.NEGATIVE_INFINITY))
                .addKeyValue("samples", List.of(1.5, Double.NaN))
                .log("x");
        Map<String, Object> payload = capture.json();

        assertThat(payload)
                .containsEntry("ratio", "NaN")
                .containsEntry("limit", "Infinity")
                .containsEntry("score", "NaN")
                .containsEntry("stats", Map.of("p99", "-Infinity"))
                .containsEntry("samples", List.of(1.5, "NaN"))
                .doesNotContainKey(FORMAT_ERROR);
    }

    @Test
    void customizerNameCollisionFallsBackToTheCoreLine() {
        Logger logger =
                start(environment().withProperty("logging.structured.json.add.order_id", "fixed"));
        MDC.put("order_id", "ord-1");

        logger.info("placed");
        Map<String, Object> payload = capture.json();

        assertThat(payload)
                .containsEntry("severity", "INFO")
                .containsEntry("message", "placed")
                .containsEntry("logger", logger.getName())
                .containsKeys("timestamp", "thread", TIMESTAMP_SECONDS, TIMESTAMP_NANOS)
                .doesNotContainKeys("order_id", SOURCE_LOCATION);
        assertThat((String) payload.get(FORMAT_ERROR)).contains("order_id");
    }

    @Test
    void valueWhoseToStringThrowsFallsBackToTheCoreLine() {
        Logger logger = start(environment());

        logger.atWarn().addKeyValue("bad", valueWhoseToStringThrows()).log("still logged");
        Map<String, Object> payload = capture.json();

        assertThat(payload)
                .containsEntry("severity", "WARNING")
                .containsEntry("message", "still logged")
                .doesNotContainKey("bad");
        assertThat((String) payload.get(FORMAT_ERROR)).contains("toString failed");
    }

    @Test
    void reducedLineKeepsTheTraceOfTheAgentMdcKeys() {
        Logger logger = start(environment());
        putAgentMdcKeys("01");

        logger.atWarn().addKeyValue("bad", valueWhoseToStringThrows()).log("still logged");

        assertThat(capture.json())
                .containsKey(FORMAT_ERROR)
                .containsEntry(TRACE, "projects/" + PROJECT_ID + "/traces/" + AGENT_TRACE_ID)
                .containsEntry(SPAN_ID, AGENT_SPAN_ID)
                .containsEntry(TRACE_SAMPLED, true)
                .doesNotContainKeys(MDC_TRACE_ID, MDC_SPAN_ID, MDC_TRACE_FLAGS);
    }

    @Test
    void reducedLineKeepsTheTraceOfTheCurrentSpan() {
        Logger logger = start(environment());

        SpanContext spanContext =
                inSpan(
                        Sampler.alwaysOn(),
                        () ->
                                logger.atWarn()
                                        .addKeyValue("bad", valueWhoseToStringThrows())
                                        .log("still logged"));

        assertThat(capture.json())
                .containsKey(FORMAT_ERROR)
                .containsEntry(
                        TRACE, "projects/" + PROJECT_ID + "/traces/" + spanContext.getTraceId())
                .containsEntry(SPAN_ID, spanContext.getSpanId())
                .containsEntry(TRACE_SAMPLED, true);
    }

    @Test
    void sourceLocationCanBeSwitchedOffTogetherWithItsStackWalk() {
        Logger logger = start(environment().withProperty(SOURCE_LOCATION_PROPERTY, "false"));

        logger.info("x");

        assertThat(capture.json()).doesNotContainKey(SOURCE_LOCATION);
        assertThat(capture.events())
                .singleElement()
                .satisfies(event -> assertThat(event.hasCallerData()).isFalse());
    }

    @Test
    void asyncAppenderKeepsTraceAndSourceLocationWhenCallerDataIsIncluded() {
        capture =
                LogCapture.start(GoogleCloudStructuredLogFormatter.class, environment())
                        .behindAsyncAppender(true);
        putAgentMdcKeys("01");

        capture.logger().info("formatted on the worker thread");
        Map<String, Object> payload = capture.json();

        assertThat(payload)
                .containsEntry(TRACE, "projects/" + PROJECT_ID + "/traces/" + AGENT_TRACE_ID)
                .containsEntry(SPAN_ID, AGENT_SPAN_ID);
        assertThat(nested(payload, SOURCE_LOCATION))
                .containsEntry(
                        "function",
                        getClass().getName()
                                + ".asyncAppenderKeepsTraceAndSourceLocationWhenCallerDataIsIncluded");
    }

    @Test
    void asyncAppenderWithoutCallerDataHasNoSourceLocation() {
        capture =
                LogCapture.start(GoogleCloudStructuredLogFormatter.class, environment())
                        .behindAsyncAppender(false);

        capture.logger().info("formatted on the worker thread");

        assertThat(capture.json())
                .containsEntry("message", "formatted on the worker thread")
                .doesNotContainKey(SOURCE_LOCATION);
    }

    @Test
    void labelsMapBecomesTheLabelsMemberWithStringValues() {
        Logger logger = start(environment());

        logger.atInfo()
                .addKeyValue(LABELS, Map.of("team", "checkout"))
                .addKeyValue(LABELS, Map.of("attempt", 2))
                .log("placed");

        assertThat(nested(capture.json(), LABELS))
                .containsOnly(Map.entry("team", "checkout"), Map.entry("attempt", "2"));
    }

    @Test
    void otherGoogleKeysAndLabelsThatAreNotAMapAreDropped() {
        Logger logger = start(environment());
        MDC.put(LABELS, "from-mdc");

        logger.atInfo()
                .addKeyValue(LABELS, "not-a-map")
                .addKeyValue(TRACE, "projects/forged/traces/1")
                .addKeyValue("logging.googleapis.com/insertId", "forged")
                .log("placed");

        assertThat(capture.json().keySet())
                .noneMatch(
                        key ->
                                key.startsWith("logging.googleapis.com/")
                                        && !key.equals(SOURCE_LOCATION));
    }

    @Test
    void markersBecomeTags() {
        Logger logger = start(environment());
        Marker audit = MarkerFactory.getDetachedMarker("AUDIT");
        audit.add(MarkerFactory.getDetachedMarker("PII"));
        MDC.put(TAGS, "from-mdc");

        logger.atInfo().addMarker(audit).log("exported");

        assertThat(capture.json()).containsEntry(TAGS, List.of("AUDIT", "PII"));
    }

    @Test
    void commonsLoggingBridgeMarkerIsNotATag() {
        Logger logger = start(environment());

        logger.atInfo()
                .addMarker(MarkerFactory.getDetachedMarker(COMMONS_LOGGING_MARKER))
                .log("logged by Spring Framework");

        assertThat(capture.json()).doesNotContainKey(TAGS);
    }

    @Test
    void failsFastWithoutProject() {
        // Spring's Instantiator wraps whatever the constructor throws; the root cause is ours.
        assertThatThrownBy(() -> start(new MockEnvironment()))
                .rootCause()
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("GOOGLE_CLOUD_PROJECT");
    }

    @Test
    void projectIdBindsFromTheGoogleCloudProjectEnvironmentVariable() {
        MockEnvironment fromEnvironmentVariable = new MockEnvironment();
        fromEnvironmentVariable
                .getPropertySources()
                .addFirst(
                        new SystemEnvironmentPropertySource(
                                "systemEnvironment",
                                Map.<String, Object>of("GOOGLE_CLOUD_PROJECT", "from-env")));
        Logger logger = start(fromEnvironmentVariable);
        putAgentMdcKeys("01");

        logger.info("x");

        assertThat(capture.json())
                .containsEntry(TRACE, "projects/from-env/traces/" + AGENT_TRACE_ID);
    }
}
