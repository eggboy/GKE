package com.example.logging;

import static com.example.logging.AbstractJsonLineFormatter.FORMAT_ERROR;
import static com.example.logging.AbstractJsonLineFormatter.MESSAGE_MAX_LENGTH_PROPERTY;
import static com.example.logging.AbstractJsonLineFormatter.TAGS;
import static com.example.logging.OpenTelemetryStructuredLogFormatter.SPAN_ID;
import static com.example.logging.OpenTelemetryStructuredLogFormatter.TRACE_FLAGS;
import static com.example.logging.OpenTelemetryStructuredLogFormatter.TRACE_ID;
import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Logger;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.slf4j.MDC;
import org.slf4j.MarkerFactory;
import org.springframework.mock.env.MockEnvironment;

/**
 * The vendor-neutral formatter (Option B in PORTABILITY.md) through Spring's own encoder. The
 * mechanics it shares with the GKE formatter are tested in depth there; this class covers the
 * schema and one case of each shared mechanic.
 */
class OpenTelemetryStructuredLogFormatterTest {

    private static final String AGENT_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736";
    private static final String AGENT_SPAN_ID = "00f067aa0ba902b7";

    private LogCapture capture;

    @AfterEach
    void tearDown() {
        if (capture != null) {
            capture.close();
        }
    }

    private Logger start(MockEnvironment environment) {
        capture = LogCapture.start(OpenTelemetryStructuredLogFormatter.class, environment);
        return capture.logger();
    }

    private static void putAgentMdcKeys() {
        MDC.put(TRACE_ID, AGENT_TRACE_ID);
        MDC.put(SPAN_ID, AGENT_SPAN_ID);
        MDC.put(TRACE_FLAGS, "01");
    }

    @Test
    void recordOutsideASpanHasReservedFieldsAndNoTraceKeys() {
        Logger logger = start(new MockEnvironment());

        logger.warn("cart {} missing", "cart-9");
        Map<String, Object> payload = capture.json();

        assertThat(payload)
                .containsEntry("level", "WARN")
                .containsEntry("message", "cart cart-9 missing")
                .containsEntry("logger", logger.getName())
                .doesNotContainKeys(TRACE_ID, SPAN_ID, TRACE_FLAGS, "severity", "timestampSeconds");
        assertThat((String) payload.get("timestamp")).endsWith("Z");
    }

    @Test
    void agentMdcTraceKeysPassThroughWhileKeyValuesCannotOverrideOrDuplicateThem() {
        Logger logger = start(new MockEnvironment());
        putAgentMdcKeys();

        logger.atInfo()
                .addKeyValue(TRACE_ID, "forged-trace")
                .addKeyValue(SPAN_ID, "forged-span")
                .addKeyValue(TRACE_FLAGS, "00")
                .log("from the agent");
        String line = capture.line();

        assertThat(line)
                .containsOnlyOnce("\"trace_id\":")
                .containsOnlyOnce("\"span_id\":")
                .containsOnlyOnce("\"trace_flags\":");
        assertThat(LogCapture.parse(line))
                .containsEntry(TRACE_ID, AGENT_TRACE_ID)
                .containsEntry(SPAN_ID, AGENT_SPAN_ID)
                .containsEntry(TRACE_FLAGS, "01")
                .doesNotContainKey(FORMAT_ERROR);
    }

    @Test
    void contextPrefixRenamesUserFieldsButNotTheTraceKeys() {
        Logger logger =
                start(
                        new MockEnvironment()
                                .withProperty("logging.structured.json.context.prefix", "ctx"));
        putAgentMdcKeys();
        MDC.put("order_id", "ord-1");

        logger.info("placed");

        assertThat(capture.json())
                .containsEntry(TRACE_ID, AGENT_TRACE_ID)
                .containsEntry(SPAN_ID, AGENT_SPAN_ID)
                .containsEntry("ctx_order_id", "ord-1")
                .doesNotContainKeys("ctx_trace_id", "ctx_span_id", "order_id");
    }

    @Test
    void contextIncludeFalseDropsUserFieldsButKeepsTheTraceKeys() {
        Logger logger =
                start(
                        new MockEnvironment()
                                .withProperty("logging.structured.json.context.include", "false"));
        putAgentMdcKeys();
        MDC.put("order_id", "ord-1");

        logger.info("placed");

        assertThat(capture.json())
                .containsEntry(TRACE_ID, AGENT_TRACE_ID)
                .containsEntry(TRACE_FLAGS, "01")
                .doesNotContainKey("order_id");
    }

    @Test
    void exceptionStackTraceIsAppendedToMessageOnOneLine() {
        Logger logger = start(new MockEnvironment());

        logger.error(
                "order failed",
                new IllegalStateException("boom", new IllegalArgumentException("root")));
        Map<String, Object> payload = capture.json();

        assertThat(payload).containsEntry("level", "ERROR");
        assertThat((String) payload.get("message"))
                .startsWith("order failed\njava.lang.IllegalStateException: boom\n\tat ")
                .contains("\nCaused by: java.lang.IllegalArgumentException: root");
    }

    @Test
    void stackTraceDepthIsBoundedByProperties() {
        Logger logger =
                start(
                        new MockEnvironment()
                                .withProperty(
                                        "logging.structured.json.stacktrace.max-throwable-depth",
                                        "1"));

        logger.error("order failed", new IllegalStateException("boom"));

        assertThat((String) capture.json().get("message"))
                .startsWith("order failed\njava.lang.IllegalStateException: boom\n\tat ")
                .containsOnlyOnce("\tat ");
    }

    @Test
    void longMessageIsCutAtTheConfiguredLength() {
        Logger logger =
                start(new MockEnvironment().withProperty(MESSAGE_MAX_LENGTH_PROPERTY, "10"));

        logger.info("0123456789abcdef");

        assertThat(capture.json()).containsEntry("message", "0123456789... [6 more characters]");
    }

    @Test
    void extraFieldsBecomeTopLevelKeysButCannotShadowReservedOnes() {
        Logger logger = start(new MockEnvironment());
        MDC.put("order_id", "ord-1");
        MDC.put("level", "DEBUG");
        MDC.put("_private", "1");
        MDC.put("stream", "stderr");

        logger.atInfo()
                .addKeyValue("total", 67.97)
                .addKeyValue("message", "shadow")
                .addKeyValue(TRACE_ID, "untrusted")
                .log("placed");
        Map<String, Object> payload = capture.json();

        assertThat(payload)
                .containsEntry("order_id", "ord-1")
                .containsEntry("total", 67.97)
                .containsEntry("level", "INFO")
                .containsEntry("message", "placed")
                .doesNotContainKeys("_private", "stream", TRACE_ID);
    }

    @Test
    void duplicateKeysAcrossMdcAndKeyValuePairsEmitSingleKey() {
        Logger logger = start(new MockEnvironment());
        MDC.put("order_id", "ord-from-mdc");

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
                .doesNotContainKey(FORMAT_ERROR);
    }

    @Test
    void nonJsonValuesFallBackToToString() {
        Logger logger = start(new MockEnvironment());

        logger.atInfo().addKeyValue("when", new Object()).log("x");

        assertThat((String) capture.json().get("when")).startsWith("java.lang.Object@");
    }

    @Test
    void nonFiniteNumbersAreWrittenAsStrings() {
        Logger logger = start(new MockEnvironment());

        logger.atInfo().addKeyValue("ratio", Double.NaN).log("x");

        assertThat(capture.json()).containsEntry("ratio", "NaN");
    }

    @Test
    void customizerNameCollisionFallsBackToTheCoreLine() {
        Logger logger =
                start(
                        new MockEnvironment()
                                .withProperty("logging.structured.json.add.order_id", "fixed"));
        putAgentMdcKeys();
        MDC.put("order_id", "ord-1");

        logger.info("placed");
        Map<String, Object> payload = capture.json();

        assertThat(payload)
                .containsEntry("level", "INFO")
                .containsEntry("message", "placed")
                .doesNotContainKey("order_id");
        assertThat((String) payload.get(FORMAT_ERROR)).contains("order_id");
    }

    @Test
    void markersBecomeTags() {
        Logger logger = start(new MockEnvironment());
        MDC.put(TAGS, "from-mdc");

        logger.atInfo().addMarker(MarkerFactory.getDetachedMarker("AUDIT")).log("exported");

        assertThat(capture.json()).containsEntry(TAGS, List.of("AUDIT"));
    }
}
